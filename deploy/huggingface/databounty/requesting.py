# SPDX-License-Identifier: Apache-2.0
"""Requesting a dataset from the Space: the form's choices, a request's arguments, and how a
person's requests read back.

Plain data in, plain data out, like contribute.py, validate.py and sponsor.py. The choices come from
DataBounty's public planner catalog (the dataset types open for requests, with the languages and
difficulty levels each can honour). A request goes to DataBounty's request_dataset tool, which checks
it all again, and an admin reviews it before a pool opens. The defaults are the console planner's.
"""

from __future__ import annotations

import re
from typing import Any

# The licences DataBounty can publish a dataset under (its lib/publication/license-texts.ts). It
# refuses any other.
LICENCES = [
    ("CC-BY-4.0", "CC BY 4.0", "Anyone may use it, crediting the contributors."),
    ("CC-BY-SA-4.0", "CC BY-SA 4.0", "Credit the contributors, and share changes under the same licence."),
    ("CC0-1.0", "CC0 1.0", "Public domain: no conditions at all."),
    ("ODC-By-1.0", "ODC-By 1.0", "Open data licence, crediting the contributors."),
]
MIXES = {"mostly_beginner": "Mostly beginner", "balanced": "Balanced", "mostly_advanced": "Mostly advanced"}
DEFAULTS = {"targetItems": 500, "difficultyMix": "balanced", "auditCoveragePct": 25, "proposedLicense": "CC-BY-4.0"}
TITLE_LENGTH = (5, 120)
DESCRIPTION_LENGTH = (20, 2000)
TARGET_ITEMS = (10, 100_000)
SHORT_TEXT = 60  # language and framework
# Characters DataBounty refuses in a request's text (control and bidirectional-override characters).
UNSAFE = re.compile("[\u0000-\u001f\u007f-\u009f‎‏‪-‮⁦-⁩]")
KEY = re.compile(r"^[A-Za-z0-9_.:-]{8,200}$")

# Where a request stands, from the requester's side: (label, tone).
REQUEST_STATUS = {
    "submitted": ("Waiting for review", "info"),
    "under_review": ("In review", "review"),
    "changes_requested": ("Changes asked for", "warn"),
    "approved": ("Approved", "ok"),
    "implemented": ("Pool is open", "ok"),
    "declined": ("Declined", "bad"),
    "disputed": ("Disputed", "warn"),
}
# DataBounty freezes a request's reference samples once it's decided (services/artifacts.ts).
SAMPLES_OPEN = {"submitted", "under_review", "changes_requested"}


def mixes_for(levels: Any) -> list[str]:
    """The difficulty mixes a dataset type can honour, by DataBounty's own rule (coherenceProblems in
    services/planner.ts): mostly beginner needs a beginner level, mostly advanced an advanced or an
    expert one. A type that declares no levels takes any mix."""
    levels = {str(level) for level in levels or []}
    if not levels:
        return list(MIXES)
    allowed = {"balanced"}
    if "beginner" in levels:
        allowed.add("mostly_beginner")
    if levels & {"advanced", "expert"}:
        allowed.add("mostly_advanced")
    return [mix for mix in MIXES if mix in allowed]


def type_option(t: dict[str, Any]) -> dict[str, Any]:
    """One dataset type, as the form offers it."""
    support = t.get("languageSupport") or {}
    mode = support.get("mode") if support.get("mode") in {"fixed", "choice", "any", "none"} else "any"
    languages = [str(l.get("label") or l.get("id") or "") for l in support.get("languages") or [] if isinstance(l, dict)]
    audit = sorted({int(a) for a in t.get("auditOptions") or [] if isinstance(a, (int, float)) and 0 <= a <= 100})
    return {
        "id": str(t.get("id") or ""),
        "name": str(t.get("name") or t.get("id") or ""),
        "description": " ".join(str(t.get("description") or "").split()),
        "category": str(t.get("category") or "other").replace("_", " "),
        "languageMode": mode,
        "languages": [l for l in languages if l],
        "mixes": [{"value": m, "label": MIXES[m]} for m in mixes_for(t.get("difficultyLevels"))],
        "auditOptions": audit or [DEFAULTS["auditCoveragePct"]],
        # The item's field keys, for a sample template.
        "fields": [str(f["key"]) for f in t.get("fields") or [] if isinstance(f, dict) and f.get("key")],
    }


def catalog_view(data: dict[str, Any]) -> dict[str, Any]:
    """The request form's choices, from DataBounty's planner catalog."""
    types = [
        type_option(t)
        for t in data.get("datasetTypes") or []
        if isinstance(t, dict) and t.get("id") and (t.get("status") or "active") == "active"
    ]
    types.sort(key=lambda t: (t["category"], t["name"].lower()))
    gate = data.get("sampleGate") or {}
    wanted = gate.get("min")
    return {
        "types": types,
        "sampleMin": wanted if isinstance(wanted, int) and wanted > 0 else 3,
        "licences": [{"value": v, "label": label, "hint": hint} for v, label, hint in LICENCES],
        "defaults": DEFAULTS,
    }


def _text(body: dict[str, Any], key: str) -> str:
    return " ".join(str(body.get(key) or "").split()) if key != "description" else str(body.get(key) or "").strip()


def request_arguments(body: dict[str, Any], types: dict[str, dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    """request_dataset's arguments for this form, or what to fix first. DataBounty checks it all again."""
    t = types.get(str(body.get("datasetTypeId") or ""))
    if not t:
        return None, "Choose a dataset type."
    title, description = _text(body, "title"), _text(body, "description")
    language, framework = _text(body, "language"), _text(body, "framework")
    if len(title) < TITLE_LENGTH[0]:
        return None, f"Give the request a title of at least {TITLE_LENGTH[0]} characters."
    if len(title) > TITLE_LENGTH[1]:
        return None, f"Keep the title under {TITLE_LENGTH[1]} characters."
    if len(description) < DESCRIPTION_LENGTH[0]:
        return None, f"Describe the dataset in at least {DESCRIPTION_LENGTH[0]} characters."
    if len(description) > DESCRIPTION_LENGTH[1]:
        return None, f"Keep the description under {DESCRIPTION_LENGTH[1]} characters."
    if any(UNSAFE.search(value) for value in (title, language, framework)):
        return None, "Remove the hidden control characters from the title, language or framework."
    if len(language) > SHORT_TEXT or len(framework) > SHORT_TEXT:
        return None, f"Keep the language and framework under {SHORT_TEXT} characters."
    try:
        items = int(body.get("targetItems"))
    except (TypeError, ValueError):
        items = 0
    if not TARGET_ITEMS[0] <= items <= TARGET_ITEMS[1]:
        return None, f"Ask for between {TARGET_ITEMS[0]} and {TARGET_ITEMS[1]:,} items."
    mix = str(body.get("difficultyMix") or "")
    if mix not in {m["value"] for m in t["mixes"]}:
        return None, "Choose a difficulty this dataset type offers."
    try:
        audit = int(body.get("auditCoveragePct"))
    except (TypeError, ValueError):
        audit = -1
    if not 0 <= audit <= 100:
        return None, "Choose how many items validators check by hand."
    licence = str(body.get("proposedLicense") or "")
    if licence not in {v for v, _, _ in LICENCES}:
        return None, "Choose a licence."

    arguments: dict[str, Any] = {
        "datasetTypeId": t["id"],
        "title": title,
        "description": description,
        "targetItems": items,
        "difficultyMix": mix,
        "auditCoveragePct": audit,
        "proposedLicense": licence,
    }
    if t["languageMode"] == "fixed" and t["languages"]:
        arguments["language"] = t["languages"][0]
    elif t["languageMode"] == "choice":
        match = next((l for l in t["languages"] if l.lower() == language.lower()), None)
        if not match:
            return None, f"Choose the language: {', '.join(t['languages'])}."
        arguments["language"] = match
    elif t["languageMode"] == "any" and language:
        arguments["language"] = language
    if framework:
        arguments["framework"] = framework
    key = str(body.get("idempotencyKey") or "")
    if KEY.match(key):
        arguments["idempotencyKey"] = key  # the same form sent twice files one request
    return arguments, ""


def _count(value: Any) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def request_row(r: dict[str, Any], type_names: dict[str, str] | None = None) -> dict[str, Any]:
    """One of the person's requests, from list_my_dataset_requests."""
    status = str(r.get("status") or "")
    label, tone = REQUEST_STATUS.get(status, (status.replace("_", " ").capitalize() or "Unknown", "info"))
    gate = r.get("sampleGate") or {}
    approved, pending, rejected = _count(gate.get("approved")), _count(gate.get("pending")), _count(gate.get("rejected"))
    wanted = _count(gate.get("min")) or 3
    limit = _count(gate.get("max")) or wanted
    if gate.get("ok"):
        sample_text = "Example items approved."
    elif approved:
        sample_text = f"{approved} of {wanted} example items approved" + (f", {pending} waiting for review." if pending else ".")
    elif pending:
        sample_text = f"{pending} example item{'s' if pending != 1 else ''} waiting for review. The pool can open once one is approved."
    else:
        sample_text = f"Add up to {limit} example items. The pool can open once an admin approves one."
    type_id = str(r.get("datasetTypeId") or "")
    return {
        "id": r.get("id"),
        "title": r.get("title") or "",
        "typeId": type_id,
        "type": (type_names or {}).get(type_id) or type_id.replace("_", " "),
        "status": status,
        "statusLabel": label,
        "statusTone": tone,
        "date": str(r.get("createdAt") or "")[:10],
        "targetItems": r.get("targetItems"),
        "difficulty": MIXES.get(str(r.get("difficultyMix") or ""), ""),
        "language": r.get("language") or "",
        "licence": r.get("proposedLicense") or "",
        "adminNote": r.get("adminNote") or "",
        "samples": {"approved": approved, "pending": pending, "rejected": rejected, "wanted": wanted, "ok": bool(gate.get("ok"))},
        "sampleText": sample_text,
        # A rejected sample frees its slot, as DataBounty counts them.
        "canAddSample": status in SAMPLES_OPEN and approved + pending < limit,
        "poolId": r.get("mintedBountyId") or "",
    }
