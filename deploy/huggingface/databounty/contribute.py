# SPDX-License-Identifier: Apache-2.0
"""The Contribute and My submissions tabs: sign in to DataBounty and add items to an open pool.

Everything here acts as the person who signed in, through account.py, and only when they press
a button. The page keeps an encrypted session handle in its own storage (gr.BrowserState), so
signing in also works inside the huggingface.co embed, where the Space's cookies are withheld.
The Space's other tabs and its MCP tools stay public and read-only.

Copy rule for this file: say what to do next in a few plain words. Dataset types carry long,
precise field specs written for agents; the form shows a one-line hint per field and keeps the
full specs in one collapsed "Full requirements" section.
"""

from __future__ import annotations

import csv
import html
import io
import json
import mimetypes
import re
import tempfile
from pathlib import Path
from typing import Any

import gradio as gr

import account
import databounty as db
import items as checks

GENERATION_METHODS = [("I wrote it", "human"), ("With AI help", "ai_assisted"), ("AI generated", "ai_generated")]
MAX_BULK_ROWS = 500
CODE_ROLES = {"input_code", "solution_code", "tests", "expected_output"}
LONG_TEXT_ROLES = {"instruction", "input_context", "rationale", "list"}
TITLE_KEYS = ("title", "prompt", "name", "question", "task", "instruction")
TEMPLATE_DIR = Path(tempfile.gettempdir()) / "databounty-space-templates"

# A plain one-line hint per field role, used when a field's own help is a long spec.
ROLE_HINTS = {
    "instruction": "What should be done, in plain words.",
    "input_context": "Any context the task needs.",
    "input_code": "The code the task starts from.",
    "solution_code": "Your working solution.",
    "tests": "Tests that show the solution works.",
    "expected_output": "What a correct answer produces.",
    "rationale": "Why the answer is right.",
    "list": "One entry per line.",
    "reference": "A source or a link.",
    "enum": "Pick one.",
    "file": "Attach a file.",
}
DIFFICULTY_TIPS = {
    "beginner": "Keep it simple and self-contained.",
    "intermediate": "Make it realistic, with at least one tricky case.",
    "advanced": "Make it challenging: several constraints and real edge cases.",
}
# DataBounty field `lang` values, mapped to the editors Gradio's code box supports.
CODE_LANGUAGES = {
    "py": "python", "python": "python", "js": "javascript", "javascript": "javascript", "ts": "typescript",
    "typescript": "typescript", "sql": "sql", "sh": "shell", "bash": "shell", "shell": "shell", "json": "json",
    "yaml": "yaml", "yml": "yaml", "c": "c", "cpp": "cpp", "c++": "cpp", "html": "html", "css": "css", "r": "r",
    "dockerfile": "dockerfile", "md": "markdown", "markdown": "markdown",
}
# Submission statuses, grouped the way a contributor thinks about them: (label, dot, tone).
STATUS_GROUPS = [
    ({"draft", "submitted", "duplicate_check", "contamination_check", "running_tests", "llm_validation"}, "Checking", "🔵", "info"),
    ({"provisionally_accepted", "in_audit", "in_sponsor_review", "accepted_pending_sample", "disputed"}, "In review", "🟣", "review"),
    ({"needs_fixes", "flagged", "tests_failed"}, "Needs your fix", "🟠", "warn"),
    ({"accepted"}, "Accepted", "🟢", "ok"),
    ({"rejected"}, "Rejected", "🔴", "bad"),
]
CHECK_LABELS = {
    "dedupe": "Not a duplicate",
    "duplicate_check": "Not a duplicate",
    "contamination": "Not copied from a benchmark",
    "ai_attribution": "AI disclosure",
    "execution": "Tests in a sandbox",
    "llm": "Quality review",
    "pool_capacity": "Room in the pool",
    "human_audit": "Reviewer",
}
_SHOUTING = re.compile(r"\b[A-Z]{4,}\b")
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+!|<>])")


def _md(value: Any) -> str:
    return _MD_SPECIAL.sub(r"\\\1", str(value or ""))


def _md_keep_code(value: Any) -> str:
    """Escape Markdown except inside `code` spans, which show backslashes literally. With an
    unmatched backtick there are no spans to keep, so everything is escaped."""
    parts = str(value or "").split("`")
    if len(parts) % 2 == 0:
        return _md(value)
    return "`".join(_md(part) if index % 2 == 0 else part for index, part in enumerate(parts))


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _cell(value: Any, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _content_type(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def _title_for(payload: dict[str, Any] | None, number: int) -> str:
    """The same rule DataBounty uses when an item comes without a title."""
    payload = payload or {}
    for key in TITLE_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:120]
    for value in payload.values():
        if isinstance(value, str) and len(value.strip()) >= 3:
            return " ".join(value.split())[:120]
    return f"Item from row {number}"


def short_hint(spec: dict[str, Any]) -> str:
    """A one-line hint: the field's own help when it is short and plain, else a plain hint for its role."""
    text = " ".join(str(spec.get("help") or "").split())
    if text and len(text) <= 110 and not _SHOUTING.search(text):
        return text
    return ROLE_HINTS.get(spec.get("role") or "", "")


def code_language(spec: dict[str, Any]) -> str | None:
    """The code editor's language for a field, from its `lang`, else its label or a JSON spec."""
    lang = str(spec.get("lang") or "").strip().lower()
    if lang in CODE_LANGUAGES:
        return CODE_LANGUAGES[lang]
    label = str(spec.get("label") or "").lower()
    for word, language in (("python", "python"), ("typescript", "typescript"), ("javascript", "javascript"), ("sql", "sql"), ("json", "json"), ("bash", "shell"), ("shell", "shell")):
        if word in label:
            return language
    if str(spec.get("help") or "").lstrip().upper().startswith("JSON"):
        return "json"
    return None


def status_view(status: str | None) -> tuple[str, str, str]:
    """(label, dot, tone) for a submission status."""
    for statuses, label, dot, tone in STATUS_GROUPS:
        if status in statuses:
            return label, dot, tone
    return (str(status or "Unknown").replace("_", " ").capitalize(), "⚪", "info")


def check_state(result: dict[str, Any]) -> tuple[str, str]:
    """('pass' | 'fail' | 'wait' | 'note', reason) for one check, read the way DataBounty reads it:
    a check with no score never ran, which is never a failure, and a failing quality review is
    advisory: a reviewer still decides."""
    stage = str(result.get("stage") or "")
    detail = result.get("detailJson") if isinstance(result.get("detailJson"), dict) else {}
    reason = detail.get("reason") if isinstance(detail.get("reason"), str) else ""
    if result.get("passed"):
        if detail.get("contaminationDecision") == "review_required" or detail.get("duplicateDecision") == "review_required":
            return "note", reason or "Close to existing work: a reviewer will look"
        return "pass", ""
    if result.get("outcome") == "pool_capacity_reached" or stage == "pool_capacity":
        return "fail", reason or "The pool filled up before this item was counted."
    if result.get("score") is None:
        return "wait", reason or "Not run yet"
    if stage == "llm":
        return "note", "Below the quality bar. Advisory: a reviewer still decides."
    if stage == "ai_attribution" and detail.get("status") == "flagged":
        return "note", "AI involvement found: a reviewer will look"
    return "fail", reason or "Didn't pass"


def template_csv(contract: dict[str, Any]) -> str | None:
    """A CSV with the pool's columns, for the file upload. File fields are left out: items with
    files are added one at a time."""
    keys = [spec["key"] for spec in contract.get("fields") or [] if spec.get("role") != "file"]
    if not keys:
        return None
    TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", str(contract.get("title") or "pool").lower()).strip("-")[:48] or "pool"
    path = TEMPLATE_DIR / f"{slug}-template.csv"
    buffer = io.StringIO()
    csv.writer(buffer).writerow(keys)
    path.write_text(buffer.getvalue(), encoding="utf-8")
    return str(path)


def _notice(text: str, tone: str = "info", title: str | None = None) -> str:
    head = f"<b>{_e(title)}</b> " if title else ""
    return f'<div class="db-notice db-{tone}">{head}{text}</div>'


# --- Components --------------------------------------------------------------------------------


class _UI:
    """The components handlers update, set once by build() and build_submissions()."""


ui = _UI()


# --- Account -----------------------------------------------------------------------------------


def _pools() -> list[dict[str, Any]]:
    try:
        return db.open_pools()
    except db.UpstreamError:
        return []


def pool_card_label(pool: dict[str, Any]) -> str:
    """A pool card: a short line of level · karma · items wanted, then the title. The short line
    comes first so it never wraps into the title's style (the cards style it as ::first-line)."""
    karma = pool.get("karma_per_accepted_item")
    wanted = max(0, int(pool.get("target_items") or 0) - int(pool.get("accepted_items") or 0))
    meta = [
        str(pool.get("difficulty") or "").capitalize(),
        f"{karma} karma" if karma else "",
        f"{wanted:,} wanted" if wanted else "",
    ]
    return f"{' · '.join(part for part in meta if part)}\n{pool['title']}"


def _pool_choices(pools: list[dict[str, Any]], query: str = "") -> list[tuple[str, str]]:
    words = [w for w in (query or "").lower().split() if w]
    chosen = []
    for pool in pools:
        haystack = " ".join(str(pool.get(k) or "") for k in ("title", "dataset_type", "language", "difficulty", "category")).lower()
        if all(word in haystack for word in words):
            chosen.append((pool_card_label(pool), pool["id"]))
    return chosen


def _account_bar(me: dict[str, Any]) -> str:
    name = me.get("displayName") or "You"
    handle = me.get("handle")
    karma = me.get("karmaTotal")
    initial = (str(name).strip()[:1] or "?").upper()
    return (
        '<div class="db-account">'
        f'<div class="db-avatar" aria-hidden="true">{_e(initial)}</div>'
        f'<div class="db-who"><b>{_e(name)}</b>{f" <span>@{_e(handle)}</span>" if handle else ""}</div>'
        f'{f"<div class=db-chip>{karma:,} karma</div>" if isinstance(karma, (int, float)) else ""}'
        "</div>"
    )


SIGNED_OUT_HTML = (
    '<div class="db-hero">'
    "<h2>Contribute to open datasets</h2>"
    "<p>Pick a pool, add an item, and earn karma when reviewers accept it.</p>"
    "</div>"
)


def _view(
    *,
    session_id: str | None,
    account_html: str,
    signed_in: bool,
    setup: str | None = None,
    handle: str | None = None,
    verify: bool = False,
    finish: bool = False,
    work: bool = False,
    pools: list[dict[str, Any]] | None = None,
) -> dict[Any, Any]:
    """Every account-dependent component, for the state the account is in."""
    setup_shown = setup is not None
    return {
        ui.session: session_id,
        ui.account_html: account_html,
        ui.signin_col: gr.update(visible=not signed_in),
        ui.sign_out_btn: gr.update(visible=signed_in),
        ui.setup_col: gr.update(visible=setup_shown),
        ui.setup_html: setup or "",
        ui.setup_msg: "",
        ui.handle_in: gr.update(visible=handle is not None, value=handle or ""),
        ui.check_btn: gr.update(visible=handle is not None),
        ui.claim_btn: gr.update(visible=handle is not None or finish),
        ui.resend_btn: gr.update(visible=verify),
        ui.verified_btn: gr.update(visible=verify),
        ui.work_col: gr.update(visible=work),
        ui.pools_state: pools if pools is not None else [],
        ui.pool_picker: gr.update(choices=_pool_choices(pools or []), value=None),
        ui.picker_col: gr.update(visible=True),
        ui.pool_col: gr.update(visible=False),
        ui.contract: None,
        ui.subs_tab: gr.update(visible=signed_in),
    }


def _signed_out(note: str = "") -> dict[Any, Any]:
    html_ = SIGNED_OUT_HTML + (_notice(_e(note), "warn") if note else "")
    return _view(session_id=None, account_html=html_, signed_in=False)


def claim_view(nonce: str | None):
    """On page load: the session left by a sign-in this page started, if one has finished.
    `nonce` is the page's own sign-in secret, read from its sessionStorage (see app.py)."""
    claimed = account.claim_sign_in(nonce) if nonce else None
    return claimed if claimed else gr.skip()


def account_view(session_id: str | None):
    """The account area for this browser's stored session."""
    session = account.session_for(session_id)
    if session is None:
        return _signed_out("Your sign-in ended. Sign in again to continue." if session_id else "")
    try:
        me = account.call_tool(session, "whoami")
    except account.NotSignedIn:
        account.sign_out(session_id)
        return _signed_out("Your sign-in ended. Sign in again to continue.")
    except account.ToolError as exc:
        return _view(
            session_id=session_id,
            account_html=_notice(f"Couldn't reach DataBounty: {_e(exc)}. Try again in a moment.", "warn"),
            signed_in=True,
        )
    session.me = me
    bar = _account_bar(me)

    if "contribute" not in set(session.scope.split()):
        return _view(
            session_id=session_id,
            account_html=bar + _notice("The Space wasn't allowed to submit for you. Sign out, sign in again, and keep <b>contribute</b> ticked.", "warn"),
            signed_in=True,
        )
    if not me.get("emailVerified"):
        return _view(
            session_id=session_id,
            account_html=bar,
            signed_in=True,
            setup="<h3>Verify your email</h3><p>Open the link we sent to your inbox, then come back here.</p>",
            verify=True,
        )
    if not me.get("handle"):
        suggestion = ""
        if "account" in set(session.scope.split()):
            try:
                suggested = account.call_tool(session, "suggest_handles", {"baseName": me.get("displayName") or "", "count": 3})
                suggestion = suggested[0] if isinstance(suggested, list) and suggested else ""
            except account.ToolError:
                suggestion = ""
        return _view(
            session_id=session_id,
            account_html=bar,
            signed_in=True,
            setup="<h3>Choose a username</h3><p>Your contributions are credited to it, on your public profile.</p>",
            handle=suggestion,
        )
    if not me.get("onboarded"):
        return _view(
            session_id=session_id,
            account_html=bar,
            signed_in=True,
            setup="<h3>One last step</h3><p>Finish setting up your account to start contributing.</p>",
            finish=True,
        )
    return _view(session_id=session_id, account_html=bar, signed_in=True, work=True, pools=_pools())


def check_handle(handle: str, session_id: str | None) -> str:
    session = account.session_for(session_id)
    if session is None:
        return _notice("Sign in again to continue.", "warn")
    handle = (handle or "").strip().lower().lstrip("@")
    if not handle:
        return _notice("Type a username first.", "warn")
    try:
        result = account.call_tool(session, "get_handle_availability", {"handle": handle})
    except (account.ToolError, account.NotSignedIn) as exc:
        return _notice(_e(exc or "Sign in again to continue."), "warn")
    if result.get("available"):
        return _notice(f"<b>@{_e(handle)}</b> is available.", "ok")
    return _notice(f"<b>@{_e(handle)}</b> isn't available: {_e(result.get('reason') or 'it is taken')}.", "warn")


def finish_setup(handle: str, session_id: str | None) -> str:
    session = account.session_for(session_id)
    if session is None:
        return _notice("Sign in again to continue.", "warn")
    try:
        me = session.me or account.call_tool(session, "whoami")
        if not me.get("handle"):
            handle = (handle or "").strip().lower().lstrip("@")
            if not handle:
                return _notice("Type the username you want first.", "warn")
            result = account.call_tool(session, "get_handle_availability", {"handle": handle})
            if not result.get("available"):
                return _notice(f"<b>@{_e(handle)}</b> isn't available: {_e(result.get('reason') or 'it is taken')}.", "warn")
            account.call_tool(session, "claim_handle", {"handle": handle})
        account.call_tool(session, "complete_onboarding", {"persona": "contributor"})
    except account.NotSignedIn:
        return _notice("Sign in again to continue.", "warn")
    except account.ToolError as exc:
        return _notice(_e(exc), "warn")
    session.me = None
    return ""


def resend_verification(session_id: str | None) -> str:
    session = account.session_for(session_id)
    if session is None:
        return _notice("Sign in again to continue.", "warn")
    try:
        result = account.call_tool(session, "resend_email_verification")
    except account.NotSignedIn:
        return _notice("Sign in again to continue.", "warn")
    except account.ToolError as exc:
        return _notice(_e(exc), "warn")
    if (result or {}).get("status") == "already_verified":
        return _notice("Your email is already verified.", "ok")
    return _notice("Sent. Open the link in that email, then press <b>I've verified my email</b>.", "ok")


def sign_out(session_id: str | None):
    account.sign_out(session_id)
    return _signed_out()


# --- Choosing a pool ---------------------------------------------------------------------------


def filter_pools(query: str, pools: list[dict[str, Any]]):
    return gr.update(choices=_pool_choices(pools or [], query), value=None)


def pool_summary_html(
    *,
    title: str,
    type_name: str = "",
    difficulty: str = "",
    karma: Any = None,
    wanted: Any = None,
    language: str = "",
    field_labels: list[str] | None = None,
    note: str = "",
) -> str:
    """The pool card shown above the form, and on the Open pools tab."""
    chips = []
    if karma:
        chips.append(f'<span class="db-chip db-chip-accent">{_e(karma)} karma per accepted item</span>')
    if isinstance(wanted, int) and wanted > 0:
        chips.append(f'<span class="db-chip">{wanted:,} items wanted</span>')
    if difficulty:
        chips.append(f'<span class="db-chip">{_e(str(difficulty).capitalize())}</span>')
    if language and language.lower() not in {"none", "n/a"}:
        chips.append(f'<span class="db-chip">{_e(language)}</span>')
    parts = [f'<div class="db-pool"><div class="db-pool-type">{_e(type_name)}</div><h3>{_e(title)}</h3>']
    if chips:
        parts.append(f'<div class="db-chips">{"".join(chips)}</div>')
    if field_labels:
        parts.append(f'<div class="db-submit-what"><span>What you submit</span> {" · ".join(_e(label) for label in field_labels)}</div>')
    tip = DIFFICULTY_TIPS.get(str(difficulty or "").lower())
    if tip:
        parts.append(f'<div class="db-tip">{_e(tip)}</div>')
    if note:
        parts.append(note)
    parts.append("</div>")
    return "".join(parts)


def _requirements_md(contract: dict[str, Any]) -> str:
    """The full specs, for the collapsed section: the pool's description, then each field's help."""
    lines = []
    description = " ".join(str(contract.get("description") or "").split())
    if description:
        lines += [f"**About this pool.** {_md(description)}", ""]
    for spec in contract["fields"]:
        label = spec.get("label") or spec["key"]
        need = "required" if spec.get("required") else "optional"
        help_text = " ".join(str(spec.get("help") or "").split()) or short_hint(spec)
        if spec.get("role") == "enum" and spec.get("options"):
            help_text = f"{help_text} One of: {', '.join(str(o) for o in spec['options'])}.".strip()
        # Keep `code` spans from the spec; escape everything else.
        lines += [f"**{_md(label)}** · {need} · column `{spec['key']}`", "", _md_keep_code(help_text), ""]
    guidance = contract.get("guidance")
    if guidance:
        lines += [f"**Level.** {_md(guidance)}"]
    return "\n".join(lines)


def _example_html(contract: dict[str, Any]) -> str:
    samples = contract.get("samples") or []
    if not samples:
        return ""
    values = samples[0]
    labels = {spec["key"]: spec.get("label") or spec["key"] for spec in contract["fields"]}
    rows = []
    for key, value in values.items():
        rows.append(f'<div class="db-example-label">{_e(labels.get(key, key))}</div><pre>{_e(value)}</pre>')
    return (
        '<div class="db-example">'
        '<p class="db-muted">An approved example from this pool. Use it to see the format, then write your own: '
        "copies of examples are rejected.</p>"
        f'{"".join(rows)}</div>'
    )


def load_contract(pool_id: str | None, session_id: str | None):
    if not pool_id:
        return {ui.contract: None, ui.picker_col: gr.update(visible=True), ui.pool_col: gr.update(visible=False)}
    session = account.session_for(session_id)
    if session is None:
        return {**_signed_out("Your sign-in ended. Sign in again to continue.")}
    try:
        raw = account.call_tool(session, "get_pool_contract", {"bountyId": pool_id})
    except account.NotSignedIn:
        return {**_signed_out("Your sign-in ended. Sign in again to continue.")}
    except account.ToolError as exc:
        gr.Warning(f"Couldn't open that pool: {exc}")
        return {ui.contract: None}

    bounty = raw.get("bounty") or {}
    capacity = raw.get("capacity") or {}
    limits = raw.get("submitLimits") or {}
    dataset_type = raw.get("datasetType") or {}
    fields = [f for f in dataset_type.get("fields") or [] if isinstance(f, dict) and f.get("key")]
    samples = [
        s["fields"] for s in dataset_type.get("sampleAssets") or []
        if isinstance(s, dict) and isinstance(s.get("fields"), dict) and s["fields"]
    ]
    karma = (bounty.get("karmaPricing") or {}).get("contributorPerItem") or bounty.get("karmaPerAcceptedItem")
    remaining = capacity.get("poolRemaining")
    contract = {
        "bountyId": pool_id,
        "title": bounty.get("title") or "",
        "description": bounty.get("description") or "",
        "fields": fields,
        "samples": samples,
        "karma": karma,
        "maxItems": int(limits.get("maxItemsPerRequest") or 20),
        "open": capacity.get("recommendedPath") != "none" and bounty.get("status") == "active",
        "nextStep": capacity.get("nextStep") or "",
        "guidance": (bounty.get("difficultyRequirement") or {}).get("guidance") or "",
    }
    note = "" if contract["open"] else _notice(f"This pool isn't taking items right now. {_e(contract['nextStep'])}", "warn")
    summary = pool_summary_html(
        title=contract["title"],
        type_name=dataset_type.get("name") or "",
        difficulty=bounty.get("difficulty") or "",
        karma=karma,
        wanted=remaining if isinstance(remaining, int) else None,
        language=bounty.get("language") or "",
        field_labels=[spec.get("label") or spec["key"] for spec in fields],
        note=note,
    )
    example = _example_html(contract)
    template = template_csv(contract)
    return {
        ui.contract: contract,
        ui.summary_html: summary,
        ui.example_acc: gr.update(visible=bool(example), open=False),
        ui.example_html: example,
        ui.reqs_md: _requirements_md(contract),
        ui.template_btn: gr.update(value=template, visible=bool(template)),
        ui.mode: gr.update(value="One item"),
        ui.picker_col: gr.update(visible=False),
        ui.pool_col: gr.update(visible=True),
        ui.bulk_file: None,
        ui.bulk_summary: "",
        ui.bulk_table: gr.update(value=[], visible=False),
        ui.bulk_send: gr.update(visible=False),
        ui.bulk_out: "",
    }


def change_pool():
    return {
        ui.contract: None,
        ui.pool_picker: gr.update(value=None),
        ui.picker_col: gr.update(visible=True),
        ui.pool_col: gr.update(visible=False),
    }


def switch_mode(mode: str):
    return {ui.one_col: gr.update(visible=mode == "One item"), ui.file_col: gr.update(visible=mode != "One item")}


# --- One item --------------------------------------------------------------------------------


def _sent_html(result: dict[str, Any], contract: dict[str, Any], count: int | None = None) -> str:
    subs = result.get("submissions") or []
    duplicates = sum(1 for s in subs if s.get("status") == "rejected" and s.get("duplicateOfSubmissionId"))
    karma = contract.get("karma")
    done = f"{count} items sent" if count and count > 1 else "Submitted"
    text = "Automated checks start now, then a reviewer takes a look."
    if karma:
        text += f" You earn <b>{_e(karma)} karma</b> for each item that's accepted."
    text += " Follow progress in <b>My submissions</b>."
    if duplicates:
        text += f"<br>{duplicates} {'was' if duplicates == 1 else 'were'} already in this pool, so {'it was' if duplicates == 1 else 'they were'} not added."
    return _notice(text, "ok", title=f"{done} ✓")


def submit_one(contract: dict[str, Any] | None, title: str, method: str, values: tuple, session_id: str | None) -> str:
    session = account.session_for(session_id)
    if session is None:
        return _notice("Your sign-in ended. Sign in again to continue.", "warn")
    if not contract or not contract.get("open"):
        return _notice("This pool isn't taking items right now.", "warn")

    payload: dict[str, Any] = {}
    uploads: list[tuple[dict[str, Any], list[str]]] = []
    for spec, value in zip(contract["fields"], values):
        key, role = spec["key"], spec.get("role")
        if role == "file":
            paths = [p for p in (value if isinstance(value, list) else [value]) if p]
            if paths:
                uploads.append((spec, paths))
            continue
        if role == "list":
            value = [line.strip() for line in str(value or "").splitlines() if line.strip()]
        elif isinstance(value, str):
            value = value.strip()
        if value not in (None, "", []):
            payload[key] = value

    labels = {spec["key"]: spec.get("label") or spec["key"] for spec in contract["fields"]}
    missing = [labels[spec["key"]] for spec in contract["fields"] if spec.get("required") and spec["key"] not in payload and not any(s is spec for s, _ in uploads)]
    if missing:
        return _notice(f"Fill in: {_e(', '.join(missing))}.", "warn", title="Not sent yet.")

    # Check everything before uploading anything, so no file is sent for an item DataBounty would refuse.
    probe = dict(payload)
    for spec, paths in uploads:
        if len(paths) > (spec.get("maxCount") or 1):
            return _notice(f"{_e(labels[spec['key']])} takes at most {spec.get('maxCount') or 1} file(s).", "warn", title="Not sent yet.")
        probe[spec["key"]] = ["file"] * len(paths) if (spec.get("maxCount") or 1) > 1 else "file"
    problems = checks.item_problems(contract["fields"], probe)
    if problems:
        friendly = [re.sub(r'"([^"]+)"', lambda m: labels.get(m.group(1), m.group(1)), p) for p in problems]
        return _notice(_e("; ".join(friendly)) + ".", "warn", title="Not sent yet.")

    title = " ".join((title or "").split()) or _title_for(payload, 1)
    if len(title) < 3:
        title = f"{title} item".strip()
    try:
        for spec, paths in uploads:
            ids = [account.upload_file(session, contract["bountyId"], p, Path(p).name, _content_type(p)) for p in paths]
            payload[spec["key"]] = ids if (spec.get("maxCount") or 1) > 1 else ids[0]
        result = account.call_tool(
            session,
            "submit_pool_items",
            {"bountyId": contract["bountyId"], "items": [{"title": title[:120], "payloadJson": payload, "generationMethod": method}]},
        )
    except account.NotSignedIn:
        return _notice("Your sign-in ended. Sign in again to continue.", "warn")
    except account.ToolError as exc:
        return _notice(_e(exc), "bad", title="DataBounty didn't take it.")
    subs = result.get("submissions") or []
    if subs and all(s.get("status") == "rejected" and s.get("duplicateOfSubmissionId") for s in subs):
        return _notice("The same item is already in this pool, so it wasn't added.", "warn", title="Already there.")
    return _sent_html(result, contract)


# --- A file of items ---------------------------------------------------------------------------


def parse_upload(path: str | None, contract: dict[str, Any] | None):
    hidden = gr.update(value=[], visible=False)
    if not path:
        return [], hidden, "", gr.update(visible=False)
    if not contract:
        return [], hidden, _notice("Choose a pool first.", "warn"), gr.update(visible=False)
    try:
        rows = checks.parse_bulk(Path(path).read_bytes(), Path(path).name)
    except ValueError as exc:
        return [], hidden, _notice(_e(exc), "warn"), gr.update(visible=False)
    if len(rows) > MAX_BULK_ROWS:
        return [], hidden, _notice(f"That file has {len(rows):,} rows. Upload up to {MAX_BULK_ROWS} at a time.", "warn"), gr.update(visible=False)
    fields = contract["fields"]
    for row in rows:
        if row.payload is not None:
            row.payload = checks.coerce_for_contract(fields, row.payload)
    checks.check_rows(rows, fields)

    state = [{"number": r.number, "payload": r.payload, "ok": r.ok, "title": _title_for(r.payload, r.number)} for r in rows]
    table = [[r.ok, r.number, "✓ Ready" if r.ok else "✗ " + (r.error or "; ".join(r.problems)), _cell(s["title"], 90)] for r, s in zip(rows, state)]
    ready = sum(1 for r in rows if r.ok)
    if not rows:
        summary = _notice("That file has no rows.", "warn")
    elif ready == len(rows):
        summary = _notice(f"All {len(rows)} rows are ready. Untick any you don't want to send.", "ok")
    else:
        summary = _notice(f"{ready} of {len(rows)} rows are ready. Rows marked ✗ won't be sent; fix them in your file and upload it again.", "warn")
    return state, gr.update(value=table, visible=bool(rows)), summary, gr.update(visible=ready > 0)


def submit_rows(table: Any, state: list[dict[str, Any]], method: str, contract: dict[str, Any] | None, session_id: str | None) -> str:
    session = account.session_for(session_id)
    if session is None:
        return _notice("Your sign-in ended. Sign in again to continue.", "warn")
    if not contract or not contract.get("open"):
        return _notice("This pool isn't taking items right now.", "warn")
    values = table.values.tolist() if hasattr(table, "values") else (table or [])
    ticked = set()
    for row in values:
        if row and (row[0] is True or str(row[0]).strip().lower() == "true"):
            try:
                ticked.add(int(row[1]))
            except (TypeError, ValueError):
                continue
    rows = [row for row in state if row["number"] in ticked and row["ok"]]
    if not rows:
        return _notice("Tick at least one ready row.", "warn")

    size = max(1, contract["maxItems"])
    sent = 0
    combined: dict[str, Any] = {"submissions": []}
    problem = ""
    for start in range(0, len(rows), size):
        batch = rows[start : start + size]
        items = [{"title": row["title"], "payloadJson": row["payload"], "generationMethod": method} for row in batch]
        try:
            result = account.call_tool(session, "submit_pool_items", {"bountyId": contract["bountyId"], "items": items})
        except account.NotSignedIn:
            problem = "Your sign-in ended, so the rest weren't sent. Sign in again to continue."
            break
        except account.ToolError as exc:
            problem = f"Rows {batch[0]['number']}–{batch[-1]['number']} and any after them weren't sent: {exc}"
            break
        created = result.get("submissions") or []
        sent += len(created)
        combined["submissions"] += created
        if result.get("stoppedAtCapacity"):
            problem = "The pool filled up, so the remaining rows weren't sent."
            break
    out = _sent_html(combined, contract, count=sent) if sent else _notice("Nothing was sent.", "warn")
    return out + (_notice(_e(problem), "warn") if problem else "")


# --- My submissions ---------------------------------------------------------------------------


def load_submissions(session_id: str | None):
    session = account.session_for(session_id)
    if session is None:
        return gr.update(value=[]), [], _notice("Sign in on the Contribute tab to see your submissions.", "info"), ""
    try:
        data = account.call_tool(session, "list_my_submissions", {"limit": 50})
    except account.NotSignedIn:
        return gr.update(value=[]), [], _notice("Your sign-in ended. Sign in again on the Contribute tab.", "warn"), ""
    except account.ToolError as exc:
        return gr.update(value=[]), [], _notice(_e(exc), "warn"), ""
    rows, ids = [], []
    for item in (data or {}).get("items") or []:
        label, dot, _ = status_view(item.get("status"))
        rows.append([f"{dot} {label}", _cell(item.get("title"), 80), _cell((item.get("bounty") or {}).get("title"), 50), str(item.get("createdAt") or "")[:10]])
        ids.append(item.get("id"))
    if not rows:
        return gr.update(value=[]), [], _notice("Nothing yet. Items you submit show up here.", "info"), ""
    total = (data or {}).get("total")
    note = f"Your {len(rows)} most recent" + (f" of {total:,}" if isinstance(total, int) and total > len(rows) else "") + ". Select one for details."
    return gr.update(value=rows), ids, f'<div class="db-muted">{_e(note)}</div>', ""


def show_submission(ids: list[str], session_id: str | None, evt: gr.SelectData) -> str:
    row = evt.index[0] if isinstance(evt.index, (list, tuple)) else evt.index
    if not isinstance(row, int) or row >= len(ids or []):
        return ""
    session = account.session_for(session_id)
    if session is None:
        return _notice("Sign in again to continue.", "warn")
    try:
        sub = account.call_tool(session, "check_submission", {"submissionId": ids[row]})
    except account.NotSignedIn:
        return _notice("Sign in again to continue.", "warn")
    except account.ToolError as exc:
        return _notice(_e(exc), "warn")
    label, _, tone = status_view(sub.get("status"))
    latest: dict[str, dict[str, Any]] = {}
    for result in sub.get("validationResults") or []:  # newest first
        stage = str(result.get("stage") or "")
        if stage and stage not in latest:
            latest[stage] = result
    icons = {"pass": "✓", "fail": "✗", "wait": "…", "note": "!"}
    checks_html = []
    for stage, result in latest.items():
        state, reason = check_state(result)
        name = CHECK_LABELS.get(stage, stage.replace("_", " ").capitalize())
        checks_html.append(
            f'<li class="db-check db-check-{state}"><span class="db-check-icon">{icons[state]}</span>'
            f'<span><b>{_e(name)}</b>{f"<br><span class=db-muted>{_e(reason)}</span>" if reason else ""}</span></li>'
        )
    notes = []
    for flag in sub.get("flags") or []:
        note = flag.get("note") or flag.get("validatorNote") or flag.get("message") or ""
        notes.append(_notice(_e(note) or "A reviewer flagged this item.", "warn", title="Reviewer note."))
    fix = ""
    if sub.get("status") in {"needs_fixes", "flagged", "tests_failed"}:
        fix = (
            f'<a class="db-btn db-btn-primary" href="{_e(db.APP_URL)}/contributor/submissions/{_e(ids[row])}/" '
            'target="_blank" rel="noopener">Fix it on databounty.io</a>'
        )
    recheck = (sub.get("validation") or {}).get("recheckAfterSeconds")
    pending = (
        f'<div class="db-muted">Checks are still running. Look again in about {int(recheck)} seconds.</div>'
        if isinstance(recheck, (int, float)) and recheck > 0
        else ""
    )
    return (
        '<div class="db-detail">'
        f'<div class="db-detail-head"><h3>{_e(sub.get("title"))}</h3><span class="db-badge db-{tone}">{_e(label)}</span></div>'
        f'<div class="db-muted">{_e((sub.get("bounty") or {}).get("title"))}</div>'
        f'{f"<ul class=db-checks>{chr(10).join(checks_html)}</ul>" if checks_html else ""}'
        f'{"".join(notes)}{pending}{fix}</div>'
    )


# --- Layout ------------------------------------------------------------------------------------


def build_state() -> None:
    """The page-wide state. Call at the top of the Blocks, outside every tab: Gradio mounts only
    the open tab, and a BrowserState in an unopened tab is never read back from storage, so a
    reload on another tab would look signed out."""
    ui.session = gr.BrowserState(None, storage_key="databounty-space-session", secret=account.BROWSER_STATE_SECRET)
    ui.nonce = gr.Textbox(visible=False)


def build() -> None:
    """The Contribute tab's contents. Call inside `with gr.Tab(...)` after build_state(), then build_submissions()."""
    ui.contract = gr.State(None)
    ui.pools_state = gr.State([])

    with gr.Row(equal_height=True, elem_classes=["db-account-row"]):
        ui.account_html = gr.HTML(SIGNED_OUT_HTML)
        ui.sign_out_btn = gr.Button("Sign out", size="sm", scale=0, min_width=90, visible=False, elem_classes=["db-quiet"])
    with gr.Column() as ui.signin_col:
        ui.sign_in_btn = gr.Button("Sign in to contribute", variant="primary", elem_id="db-signin", elem_classes=["db-signin"])
        gr.HTML(
            '<div class="db-muted" id="db-signin-note">Sign in with your DataBounty account. '
            "New here? You can create one on the sign-in page.</div>",
            elem_classes=["db-tight"],
        )

    with gr.Column(visible=False, elem_classes=["db-card"]) as ui.setup_col:
        ui.setup_html = gr.HTML()
        ui.handle_in = gr.Textbox(show_label=False, placeholder="username", visible=False, max_lines=1)
        with gr.Row():
            ui.check_btn = gr.Button("Check", size="sm", visible=False, elem_classes=["db-quiet"])
            ui.claim_btn = gr.Button("Continue", variant="primary", size="sm", visible=False)
            ui.verified_btn = gr.Button("I've verified my email", variant="primary", size="sm", visible=False)
            ui.resend_btn = gr.Button("Send the email again", size="sm", visible=False, elem_classes=["db-quiet"])
        ui.setup_msg = gr.HTML(elem_classes=["db-tight"])

    with gr.Column(visible=False) as ui.work_col:
        with gr.Column() as ui.picker_col:
            with gr.Row(equal_height=True):
                gr.HTML('<h3 class="db-step">Choose a pool</h3>', scale=1)
                ui.pool_search = gr.Textbox(
                    show_label=False, placeholder="Search pools: try cache, sql or parser", max_lines=1, container=False, scale=2
                )
            ui.pool_picker = gr.Radio(
                choices=[], show_label=False, container=False, elem_id="db-pools", elem_classes=["db-pool-grid"], interactive=True
            )

        with gr.Column(visible=False) as ui.pool_col:
            ui.change_btn = gr.Button("← All pools", size="sm", elem_classes=["db-link-btn", "db-quiet"])
            ui.summary_html = gr.HTML()
            with gr.Accordion("See an example item", open=False, visible=False) as ui.example_acc:
                ui.example_html = gr.HTML()
            with gr.Accordion("Full requirements", open=False):
                ui.reqs_md = gr.Markdown()

            ui.mode = gr.Radio(["One item", "Upload a file"], value="One item", show_label=False, container=False, elem_id="db-mode")
            with gr.Column() as ui.one_col:

                @gr.render(inputs=[ui.contract])
                def item_form(current: dict[str, Any] | None):
                    if not current or not current.get("open"):
                        return
                    widgets = []
                    for spec in current["fields"]:
                        label = spec.get("label") or spec["key"]
                        if not spec.get("required"):
                            label += " (optional)"
                        hint = short_hint(spec) or None
                        role = spec.get("role")
                        if role == "enum" and spec.get("options"):
                            widgets.append(gr.Dropdown([str(o) for o in spec["options"]], label=label, info=hint))
                        elif role == "file":
                            many = (spec.get("maxCount") or 1) > 1
                            accept = [t.strip() for t in str(spec.get("accept") or "").split(",") if t.strip().startswith(".")]
                            widgets.append(
                                gr.File(label=label, file_count="multiple" if many else "single", type="filepath", file_types=accept or None)
                            )
                        elif role in CODE_ROLES:
                            gr.HTML(
                                f'<div class="db-field-head"><b>{_e(label)}</b>{f"<span>{_e(hint)}</span>" if hint else ""}</div>',
                                elem_classes=["db-tight"],
                            )
                            widgets.append(gr.Code(language=code_language(spec), show_label=False, lines=8, max_lines=30))
                        else:
                            widgets.append(
                                gr.Textbox(label=label, info=hint, lines=4 if role in LONG_TEXT_ROLES else 2, max_lines=24)
                            )
                    with gr.Row(equal_height=True):
                        title = gr.Textbox(
                            label="Title (optional)",
                            placeholder="Leave empty to use the start of your task",
                            max_lines=1,
                            max_length=120,
                            scale=3,
                            min_width=260,
                        )
                        method = gr.Radio(GENERATION_METHODS, value="human", label="How did you make it?", scale=2, min_width=300)
                    send = gr.Button("Submit item", variant="primary")
                    outcome = gr.HTML()

                    def submit(title_value, method_value, session_value, *values):
                        return submit_one(current, title_value, method_value, values, session_value)

                    send.click(submit, [title, method, ui.session, *widgets], outcome, api_visibility="private")

            with gr.Column(visible=False) as ui.file_col:
                gr.HTML(
                    '<div class="db-muted">Add many items at once: one item per row, in a CSV, TSV, JSON or JSONL file. '
                    "Start from the template so the columns match.</div>"
                )
                ui.template_btn = gr.DownloadButton(
                    "Download the CSV template", size="sm", visible=False, elem_classes=["db-quiet", "db-link-btn"]
                )
                ui.bulk_file = gr.File(label="Your file", file_types=[".csv", ".tsv", ".jsonl", ".ndjson", ".json"], type="filepath")
                ui.bulk_summary = gr.HTML(elem_classes=["db-tight"])
                ui.bulk_rows = gr.State([])
                ui.bulk_table = gr.Dataframe(
                    headers=["Send", "Row", "Check", "Title"],
                    datatype=["bool", "number", "str", "str"],
                    type="array",
                    interactive=True,
                    static_columns=[1, 2, 3],
                    wrap=True,
                    max_height=360,
                    visible=False,
                    show_label=False,
                    elem_classes=["db-table"],
                )
                ui.bulk_method = gr.Radio(GENERATION_METHODS, value="human", label="How did you make these?")
                ui.bulk_send = gr.Button("Send the ticked rows", variant="primary", visible=False)
                ui.bulk_out = gr.HTML(elem_classes=["db-tight"])


def build_submissions() -> None:
    """The My submissions tab's contents. Call inside `with gr.Tab(...)` after build()."""
    with gr.Row(equal_height=True):
        ui.subs_note = gr.HTML()
        ui.subs_refresh = gr.Button("Refresh", size="sm", scale=0, min_width=90, elem_classes=["db-quiet"])
    ui.subs_ids = gr.State([])
    ui.subs_table = gr.Dataframe(
        headers=["Status", "Item", "Pool", "Submitted"],
        datatype=["str", "str", "str", "str"],
        interactive=False,
        wrap=True,
        max_height=420,
        show_label=False,
        elem_classes=["db-table"],
    )
    ui.sub_detail = gr.HTML()


def account_outputs() -> list[Any]:
    return [
        ui.session, ui.account_html, ui.signin_col, ui.sign_out_btn, ui.setup_col, ui.setup_html, ui.setup_msg,
        ui.handle_in, ui.check_btn, ui.claim_btn, ui.resend_btn, ui.verified_btn, ui.work_col, ui.pools_state,
        ui.pool_picker, ui.picker_col, ui.pool_col, ui.contract, ui.subs_tab,
    ]


def contract_outputs() -> list[Any]:
    return [
        ui.contract, ui.summary_html, ui.example_acc, ui.example_html, ui.reqs_md, ui.template_btn, ui.mode,
        ui.picker_col, ui.pool_col, ui.bulk_file, ui.bulk_summary, ui.bulk_table, ui.bulk_send, ui.bulk_out,
        *account_outputs(),
    ]


def wire() -> None:
    """Connect the events. Call after build(), build_submissions() and setting ui.subs_tab."""
    private = {"api_visibility": "private"}
    acct = account_outputs()
    ui.sign_out_btn.click(sign_out, ui.session, acct, **private)
    ui.sign_in_btn.click(None, js="() => window.databountySignIn && window.databountySignIn()")
    ui.verified_btn.click(account_view, ui.session, acct, **private)
    ui.check_btn.click(check_handle, [ui.handle_in, ui.session], ui.setup_msg, **private)
    ui.claim_btn.click(finish_setup, [ui.handle_in, ui.session], ui.setup_msg, **private).then(
        account_view, ui.session, acct, **private
    )
    ui.resend_btn.click(resend_verification, ui.session, ui.setup_msg, **private)
    ui.pool_search.change(filter_pools, [ui.pool_search, ui.pools_state], ui.pool_picker, **private)
    ui.pool_picker.input(load_contract, [ui.pool_picker, ui.session], contract_outputs(), **private)
    ui.change_btn.click(change_pool, None, [ui.contract, ui.pool_picker, ui.picker_col, ui.pool_col], **private)
    ui.mode.change(switch_mode, ui.mode, [ui.one_col, ui.file_col], **private)
    ui.bulk_file.change(
        parse_upload, [ui.bulk_file, ui.contract], [ui.bulk_rows, ui.bulk_table, ui.bulk_summary, ui.bulk_send], **private
    )
    ui.bulk_send.click(submit_rows, [ui.bulk_table, ui.bulk_rows, ui.bulk_method, ui.contract, ui.session], ui.bulk_out, **private)
    subs_outputs = [ui.subs_table, ui.subs_ids, ui.subs_note, ui.sub_detail]
    ui.subs_refresh.click(load_submissions, ui.session, subs_outputs, **private)
    ui.subs_tab.select(load_submissions, ui.session, subs_outputs, **private)
    ui.subs_table.select(show_submission, [ui.subs_ids, ui.session], ui.sub_detail, **private)


def open_pool(pool_id: str | None, session_id: str | None, pools: list[dict[str, Any]]):
    """From the Open pools tab: go to Contribute with that pool open (or the sign-in, when signed out)."""
    if not pool_id or account.session_for(session_id) is None:
        return {ui.pool_picker: gr.update()}
    return {ui.pool_picker: gr.update(choices=_pool_choices(pools or _pools()), value=pool_id)}


def select_tab(request: gr.Request):
    """Open on the tab the URL names, e.g. Contribute when sign-in sends the browser back."""
    tab = request.query_params.get("tab") if request else None
    return gr.Tabs(selected=tab) if tab in {"contribute", "pools", "datasets", "leaderboard", "profile", "agents", "overview", "mine"} else gr.Tabs()
