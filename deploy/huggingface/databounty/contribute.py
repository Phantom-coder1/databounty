# SPDX-License-Identifier: Apache-2.0
"""The Contribute tab: sign in to DataBounty and add items to an open pool, from the Space.

Everything here acts as the person who signed in, through account.py, and only when
they press a button. The Space's other tabs and its MCP tools stay public and read-only.
"""

from __future__ import annotations

import json
import mimetypes
import re
from pathlib import Path
from typing import Any

import gradio as gr

import account
import databounty as db
import items as checks

GENERATION_METHODS = [
    ("I wrote it", "human"),
    ("I wrote it with AI help", "ai_assisted"),
    ("An AI generated it", "ai_generated"),
]
MAX_BULK_ROWS = 500
CODE_ROLES = {"input_code", "solution_code", "tests", "expected_output"}
TITLE_KEYS = ("title", "prompt", "name", "question", "task", "instruction")
ROLE_HINTS = {
    "instruction": "The task, in plain words.",
    "input_context": "Context the task needs.",
    "input_code": "The code the task starts from.",
    "solution_code": "The working solution.",
    "tests": "Tests that prove the solution works.",
    "expected_output": "The output a correct answer produces.",
    "rationale": "Why the answer is right.",
    "list": "A list: one entry per line.",
    "reference": "A source or link.",
}
STATUS_LABELS = {
    "draft": "Draft",
    "submitted": "Received, waiting for checks",
    "duplicate_check": "In automated checks",
    "running_tests": "In automated checks",
    "llm_validation": "In automated checks",
    "provisionally_accepted": "Passed checks, with reviewers",
    "in_audit": "With human reviewers",
    "in_sponsor_review": "With the pool's sponsor",
    "accepted_pending_sample": "Passed checks, with reviewers",
    "disputed": "Disputed, waiting for a decision",
    "needs_fixes": "Needs your fix",
    "flagged": "Needs your fix",
    "tests_failed": "Needs your fix (tests failed)",
    "accepted": "Accepted",
    "rejected": "Rejected",
}

SIGNED_OUT = (
    "**Sign in to contribute from here.** Sign-in opens in a new tab on databounty.io: use any of its "
    "sign-in options, approve the Space, and you'll land back in the Space, signed in. The Space acts for "
    "you only when you press a button, and only within what you approve."
)
SIGN_IN_AGAIN = "⚠️ Your DataBounty sign-in has ended. Sign in again to continue."

_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+!|<>])")


def _md(value: Any) -> str:
    return _MD_SPECIAL.sub(r"\\\1", str(value or ""))


def _cell(value: Any, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _session(request: gr.Request | None) -> tuple[str | None, account.Session | None]:
    session_id = request.cookies.get(account.SESSION_COOKIE) if request is not None else None
    return session_id, account.session_for(session_id)


def _content_type(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def _title_for(payload: dict[str, Any] | None, number: int) -> str:
    """The same rule DataBounty uses when an item comes without a title."""
    payload = payload or {}
    for key in TITLE_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:120]
    for value in payload.values():
        if isinstance(value, str) and len(value.strip()) >= 3:
            return value.strip()[:120]
    return f"Item from row {number}"


# --- Account state ---------------------------------------------------------------------------


def _view(
    status: str,
    *,
    sign_in: bool = False,
    sign_out: bool = False,
    setup: str | None = None,
    handle: str | None = None,
    claim_label: str = "Claim handle and finish setup",
    resend: bool = False,
    work: bool = False,
    pools: list[tuple[str, str]] | None = None,
):
    return (
        status,
        gr.update(visible=sign_in),
        gr.update(visible=sign_out),
        gr.update(visible=setup is not None),
        setup or "",
        gr.update(visible=handle is not None, value=handle or ""),
        gr.update(visible=handle is not None),
        gr.update(visible=setup is not None and not resend, value=claim_label),
        gr.update(visible=resend),
        gr.update(visible=work),
        gr.update(choices=pools or [], value=None) if pools is not None else gr.update(),
    )


def _pool_choices() -> list[tuple[str, str]]:
    try:
        pools = db.open_pools()
    except db.UpstreamError:
        return []
    choices = []
    for pool in pools:
        karma = pool.get("karma_per_accepted_item")
        label = f"{pool['title']} · {karma} karma per item" if karma else pool["title"]
        choices.append((label, pool["id"]))
    return choices


def account_view(request: gr.Request):
    session_id, session = _session(request)
    if session is None:
        return _view(SIGNED_OUT, sign_in=True)
    try:
        me = account.call_tool(session, "whoami")
    except account.NotSignedIn:
        account.sign_out(session_id)
        return _view(SIGN_IN_AGAIN + "\n\n" + SIGNED_OUT, sign_in=True)
    except account.ToolError as exc:
        return _view(f"⚠️ Couldn't reach DataBounty: {exc}", sign_out=True)
    session.me = me

    name = me.get("displayName") or "you"
    handle = me.get("handle")
    who = f"Signed in to DataBounty as **{_md(name)}**" + (f" (@{_md(handle)})" if handle else "")
    karma = me.get("karmaTotal")
    if isinstance(karma, (int, float)):
        who += f" · {karma:,} karma"

    granted = set(session.scope.split())
    if "contribute" not in granted:
        return _view(
            who + "\n\n⚠️ The Space wasn't given permission to **contribute** when you approved it. "
            "Sign out, sign in again, and leave *contribute* ticked.",
            sign_out=True,
        )
    if not me.get("emailVerified"):
        return _view(
            who,
            sign_out=True,
            setup="**Verify your email first.** DataBounty only takes items from verified accounts. Open the "
            "link in the verification email, then press *Check again*. Didn't get it? Send it again.",
            resend=True,
        )
    if not handle:
        suggestion = ""
        if "account" in granted:
            try:
                suggested = account.call_tool(session, "suggest_handles", {"baseName": name, "count": 3})
                suggestion = suggested[0] if isinstance(suggested, list) and suggested else ""
            except account.ToolError:
                suggestion = ""
        return _view(
            who,
            sign_out=True,
            setup="**Choose your public handle to finish setting up.** Contributions are credited to it, and "
            f"your profile becomes public at `{db.SITE_URL}/<handle>` as soon as you claim it. You can hide "
            "sections of your profile later in its settings on databounty.io.",
            handle=suggestion,
        )
    if not me.get("onboarded"):
        return _view(
            who,
            sign_out=True,
            setup="**One step left.** Finish setting up your account to start contributing.",
            claim_label="Finish setup",
        )
    return _view(who, sign_out=True, work=True, pools=_pool_choices())


def check_handle(handle: str, request: gr.Request) -> str:
    _, session = _session(request)
    if session is None:
        return SIGN_IN_AGAIN
    handle = (handle or "").strip().lower().lstrip("@")
    if not handle:
        return "Type a handle to check."
    try:
        result = account.call_tool(session, "get_handle_availability", {"handle": handle})
    except (account.ToolError, account.NotSignedIn) as exc:
        return f"⚠️ {exc or SIGN_IN_AGAIN}"
    if result.get("available"):
        return f"✅ @{_md(handle)} is available."
    return f"@{_md(handle)} isn't available: {_md(result.get('reason') or 'it is taken or reserved')}."


def finish_setup(handle: str, request: gr.Request) -> str:
    _, session = _session(request)
    if session is None:
        return SIGN_IN_AGAIN
    try:
        me = session.me or account.call_tool(session, "whoami")
        if not me.get("handle"):
            handle = (handle or "").strip().lower().lstrip("@")
            if not handle:
                return "Type the handle you want first."
            result = account.call_tool(session, "get_handle_availability", {"handle": handle})
            if not result.get("available"):
                return f"@{_md(handle)} isn't available: {_md(result.get('reason') or 'it is taken or reserved')}."
            account.call_tool(session, "claim_handle", {"handle": handle})
        account.call_tool(session, "complete_onboarding", {"persona": "contributor"})
    except account.NotSignedIn:
        return SIGN_IN_AGAIN
    except account.ToolError as exc:
        return f"⚠️ {_md(exc)}"
    session.me = None
    return "✅ You're all set."


def resend_verification(request: gr.Request) -> str:
    _, session = _session(request)
    if session is None:
        return SIGN_IN_AGAIN
    try:
        result = account.call_tool(session, "resend_email_verification")
    except account.NotSignedIn:
        return SIGN_IN_AGAIN
    except account.ToolError as exc:
        return f"⚠️ {_md(exc)}"
    if (result or {}).get("status") == "already_verified":
        return "Your email is already verified. Press *Check again*."
    return "Sent. Open the link in that email, then press *Check again*."


def sign_out(request: gr.Request):
    session_id, _ = _session(request)
    account.sign_out(session_id)
    return _view("You're signed out of DataBounty in this Space.\n\n" + SIGNED_OUT, sign_in=True)


# --- Pool contract -------------------------------------------------------------------------------


def load_contract(pool_id: str | None, request: gr.Request):
    if not pool_id:
        return None, ""
    _, session = _session(request)
    if session is None:
        return None, SIGN_IN_AGAIN
    try:
        raw = account.call_tool(session, "get_pool_contract", {"bountyId": pool_id})
    except account.NotSignedIn:
        return None, SIGN_IN_AGAIN
    except account.ToolError as exc:
        return None, f"⚠️ Couldn't load that pool: {_md(exc)}"

    bounty = raw.get("bounty") or {}
    capacity = raw.get("capacity") or {}
    limits = raw.get("submitLimits") or {}
    fields = [f for f in (raw.get("datasetType") or {}).get("fields") or [] if isinstance(f, dict) and f.get("key")]
    contract = {
        "bountyId": pool_id,
        "title": bounty.get("title") or "",
        "fields": fields,
        "maxItems": int(limits.get("maxItemsPerRequest") or 20),
        "open": capacity.get("recommendedPath") != "none" and bounty.get("status") == "active",
        "nextStep": capacity.get("nextStep") or "",
    }

    karma = (bounty.get("karmaPricing") or {}).get("contributorPerItem") or bounty.get("karmaPerAcceptedItem")
    lines = [f"### {_md(contract['title'])}"]
    facts = []
    if karma:
        facts.append(f"**{karma} karma** for each accepted item")
    remaining = capacity.get("poolRemaining")
    if isinstance(remaining, int):
        facts.append(f"{remaining:,} more items wanted")
    if facts:
        lines.append(" · ".join(facts))
    guidance = (bounty.get("difficultyRequirement") or {}).get("guidance")
    if guidance:
        lines.append(f"_{_md(guidance)}_")
    if not contract["open"]:
        lines.append(f"⚠️ This pool isn't taking items right now. {_md(contract['nextStep'])}")
    lines += ["", "| Field | Required | What goes here |", "|---|---|---|"]
    for spec in fields:
        role = spec.get("role")
        what = spec.get("help") or ROLE_HINTS.get(role, "")
        if role == "enum" and spec.get("options"):
            what = f"{what} One of: {', '.join(str(o) for o in spec['options'])}.".strip()
        if role == "file":
            count = spec.get("maxCount") or 1
            what = f"{what} {'Up to ' + str(count) + ' files' if count > 1 else 'A file'}{' (' + spec['accept'] + ')' if spec.get('accept') else ''}.".strip()
        lines.append(f"| {_md(spec.get('label') or spec['key'])} | {'yes' if spec.get('required') else ''} | {_md(what)} |")
    return contract, "\n".join(lines)


# --- One item --------------------------------------------------------------------------------


def _submitted_summary(result: dict[str, Any], numbers: list[int] | None = None) -> list[str]:
    lines = []
    for index, sub in enumerate(result.get("submissions") or []):
        status = sub.get("status") or ""
        label = STATUS_LABELS.get(status, status)
        if status == "rejected" and sub.get("duplicateOfSubmissionId"):
            label = "Rejected: the same item is already in this pool"
        prefix = f"Row {numbers[index]}: " if numbers and index < len(numbers) else ""
        lines.append(f"- {prefix}`{sub.get('id')}` · {label}")
    return lines


def submit_one(contract: dict[str, Any] | None, title: str, method: str, values: tuple, request: gr.Request) -> str:
    _, session = _session(request)
    if session is None:
        return SIGN_IN_AGAIN
    if not contract or not contract.get("open"):
        return "⚠️ Choose a pool that is taking items."
    title = (title or "").strip()
    if len(title) < 3:
        return "⚠️ Give the item a title of at least 3 characters."

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

    # Check everything before uploading anything, so no file is sent for an item DataBounty would refuse.
    probe = dict(payload)
    for spec, paths in uploads:
        if len(paths) > (spec.get("maxCount") or 1):
            return f"⚠️ {_md(spec.get('label') or spec['key'])} takes at most {spec.get('maxCount') or 1} file(s)."
        probe[spec["key"]] = ["file"] * len(paths) if (spec.get("maxCount") or 1) > 1 else "file"
    problems = checks.item_problems(contract["fields"], probe)
    if problems:
        return "⚠️ Not sent yet: " + "; ".join(problems) + "."

    try:
        for spec, paths in uploads:
            ids = [account.upload_file(session, contract["bountyId"], p, Path(p).name, _content_type(p)) for p in paths]
            payload[spec["key"]] = ids if (spec.get("maxCount") or 1) > 1 else ids[0]
        result = account.call_tool(
            session,
            "submit_pool_items",
            {"bountyId": contract["bountyId"], "items": [{"title": title, "payloadJson": payload, "generationMethod": method}]},
        )
    except account.NotSignedIn:
        return SIGN_IN_AGAIN
    except account.ToolError as exc:
        return f"⚠️ DataBounty didn't take it: {_md(exc)}"
    lines = ["✅ Sent to DataBounty."] + _submitted_summary(result)
    lines.append("\nAutomated checks run next, then people review it. Follow it under **My submissions**.")
    return "\n".join(lines)


# --- A file of items ---------------------------------------------------------------------------


def parse_upload(path: str | None, contract: dict[str, Any] | None):
    if not path:
        return [], [], ""
    if not contract:
        return [], [], "⚠️ Choose a pool first, so the rows can be checked against its fields."
    try:
        rows = checks.parse_bulk(Path(path).read_bytes(), Path(path).name)
    except ValueError as exc:
        return [], [], f"⚠️ {_md(exc)}"
    if len(rows) > MAX_BULK_ROWS:
        return [], [], (
            f"⚠️ That file has {len(rows):,} rows. The Space takes up to {MAX_BULK_ROWS} at a time: "
            "split the file, or upload it on databounty.io."
        )
    fields = contract["fields"]
    for row in rows:
        if row.payload is not None:
            row.payload = checks.coerce_for_contract(fields, row.payload)
    checks.check_rows(rows, fields)

    state = [{"number": r.number, "payload": r.payload, "ok": r.ok, "title": _title_for(r.payload, r.number)} for r in rows]
    table = [
        [
            r.ok,
            r.number,
            "ready" if r.ok else (r.error or "; ".join(r.problems)),
            _cell(s["title"], 80),
            _cell(json.dumps(r.payload, ensure_ascii=False) if r.payload is not None else "", 160),
        ]
        for r, s in zip(rows, state)
    ]
    ready = sum(1 for r in rows if r.ok)
    summary = [f"**{ready} of {len(rows)} rows are ready to send.** Untick any you don't want, then submit."]
    if ready < len(rows):
        summary.append("Rows that aren't ready are shown with the reason and are never sent.")
    if any(spec.get("role") == "file" for spec in fields):
        summary.append("This pool has file fields: add items with files one at a time under **Add one item**.")
    return state, table, "\n\n".join(summary)


def submit_rows(table: list[list[Any]] | None, state: list[dict[str, Any]], method: str, contract: dict[str, Any] | None, request: gr.Request) -> str:
    _, session = _session(request)
    if session is None:
        return SIGN_IN_AGAIN
    if not contract or not contract.get("open"):
        return "⚠️ Choose a pool that is taking items."
    ticked = set()
    for row in table or []:
        if row and (row[0] is True or str(row[0]).strip().lower() == "true"):
            try:
                ticked.add(int(row[1]))
            except (TypeError, ValueError):
                continue
    rows = [row for row in state if row["number"] in ticked and row["ok"]]
    if not rows:
        return "⚠️ Tick at least one row that is ready."

    size = max(1, contract["maxItems"])
    lines: list[str] = []
    sent = 0
    for start in range(0, len(rows), size):
        batch = rows[start : start + size]
        items = [{"title": row["title"], "payloadJson": row["payload"], "generationMethod": method} for row in batch]
        try:
            result = account.call_tool(session, "submit_pool_items", {"bountyId": contract["bountyId"], "items": items})
        except account.NotSignedIn:
            lines.append(SIGN_IN_AGAIN)
            break
        except account.ToolError as exc:
            lines.append(f"⚠️ Rows {batch[0]['number']}–{batch[-1]['number']} and any after them were not sent: {_md(exc)}")
            break
        created = result.get("submissions") or []
        sent += len(created)
        lines += _submitted_summary(result, [row["number"] for row in batch])
        if result.get("stoppedAtCapacity"):
            lines.append("⚠️ The pool filled up, so the remaining rows were not sent.")
            break
    header = f"✅ Sent {sent} of {len(rows)} ticked rows." if sent else "Nothing was sent."
    return "\n".join([header, *lines, "\nFollow them under **My submissions**."])


# --- My submissions ---------------------------------------------------------------------------


def load_submissions(request: gr.Request):
    _, session = _session(request)
    if session is None:
        return [], gr.update(choices=[], value=None), SIGN_IN_AGAIN
    try:
        data = account.call_tool(session, "list_my_submissions", {"limit": 50})
    except account.NotSignedIn:
        return [], gr.update(choices=[], value=None), SIGN_IN_AGAIN
    except account.ToolError as exc:
        return [], gr.update(choices=[], value=None), f"⚠️ {_md(exc)}"
    rows, choices = [], []
    for item in (data or {}).get("items") or []:
        status = STATUS_LABELS.get(item.get("status"), item.get("status") or "")
        pool = (item.get("bounty") or {}).get("title") or ""
        rows.append([str(item.get("createdAt") or "")[:10], _cell(pool, 60), _cell(item.get("title"), 80), status])
        choices.append((f"{_cell(item.get('title'), 60)} · {status}", item.get("id")))
    total = (data or {}).get("total")
    note = f"Your {len(rows)} most recent submissions" + (f" of {total:,}." if isinstance(total, int) else ".") if rows else "No submissions yet."
    return rows, gr.update(choices=choices, value=None), note


def show_submission(submission_id: str | None, request: gr.Request) -> str:
    if not submission_id:
        return ""
    _, session = _session(request)
    if session is None:
        return SIGN_IN_AGAIN
    try:
        sub = account.call_tool(session, "check_submission", {"submissionId": submission_id})
    except account.NotSignedIn:
        return SIGN_IN_AGAIN
    except account.ToolError as exc:
        return f"⚠️ {_md(exc)}"
    status = sub.get("status") or ""
    lines = [
        f"### {_md(sub.get('title'))}",
        f"**{STATUS_LABELS.get(status, status)}** · pool: {_md((sub.get('bounty') or {}).get('title'))}",
    ]
    results = sub.get("validationResults") or []
    if results:
        lines += ["", "| Check | Result | Detail |", "|---|---|---|"]
        for result in results:
            outcome = "passed" if result.get("passed") else "did not pass"
            detail = result.get("outcome") or (f"score {result['score']:.2f}" if isinstance(result.get("score"), (int, float)) else "")
            lines.append(f"| {_md(result.get('stage'))} | {outcome} | {_md(detail)} |")
    for flag in sub.get("flags") or []:
        note = flag.get("note") or flag.get("validatorNote") or flag.get("message") or ""
        lines.append(f"\n**Reviewer note** ({_md(flag.get('reason'))}): {_md(note)}")
    if status in {"needs_fixes", "flagged", "tests_failed"}:
        lines.append(
            "\nThis item needs a fix. Revise it on databounty.io "
            f"([open it]({db.APP_URL}/contributor/submissions/{submission_id}/)): a revision replaces the whole item."
        )
    recheck = (sub.get("validation") or {}).get("recheckAfterSeconds")
    if isinstance(recheck, (int, float)) and recheck > 0:
        lines.append(f"\n_Checks are still running; look again in about {int(recheck)} seconds._")
    return "\n".join(lines)


# --- Layout ------------------------------------------------------------------------------------


def build() -> list[Any]:
    """The Contribute tab's contents. Call inside `with gr.Tab(...)`. Returns the components
    account_view() updates, so the page can run it on load."""
    status = gr.Markdown(SIGNED_OUT)
    with gr.Row():
        sign_in_btn = gr.Button(
            "Sign in to DataBounty ↗", variant="primary", link="/oauth/start", link_target="_blank", visible=True
        )
        refresh_btn = gr.Button("Check again")
        sign_out_btn = gr.Button("Sign out", visible=False)

    with gr.Column(visible=False) as setup_col:
        setup_md = gr.Markdown()
        handle_in = gr.Textbox(label="Public handle", placeholder="3–20 lowercase letters, digits or hyphens", visible=False)
        with gr.Row():
            check_btn = gr.Button("Check availability")
            claim_btn = gr.Button("Claim handle and finish setup", variant="primary")
            resend_btn = gr.Button("Resend verification email", visible=False)
        setup_msg = gr.Markdown()

    with gr.Column(visible=False) as work_col:
        pool_dd = gr.Dropdown(label="Choose an open pool", choices=[], interactive=True)
        contract = gr.State(None)
        contract_md = gr.Markdown()
        with gr.Tabs():
            with gr.Tab("Add one item"):

                @gr.render(inputs=[contract])
                def item_form(current: dict[str, Any] | None):
                    if not current:
                        gr.Markdown("Choose a pool above and its fields appear here.")
                        return
                    if not current.get("open"):
                        gr.Markdown("This pool isn't taking items right now.")
                        return
                    title = gr.Textbox(label="Title", placeholder="A short name for this item (3–120 characters)", max_length=120)
                    widgets = []
                    for spec in current["fields"]:
                        label = f"{spec.get('label') or spec['key']}{' *' if spec.get('required') else ''}"
                        hint = spec.get("help") or ROLE_HINTS.get(spec.get("role"), None)
                        role = spec.get("role")
                        if role == "enum" and spec.get("options"):
                            widgets.append(gr.Dropdown([str(o) for o in spec["options"]], label=label, info=hint))
                        elif role == "file":
                            many = (spec.get("maxCount") or 1) > 1
                            accept = [t.strip() for t in str(spec.get("accept") or "").split(",") if t.strip().startswith(".")]
                            widgets.append(
                                gr.File(label=label, file_count="multiple" if many else "single", type="filepath", file_types=accept or None)
                            )
                        else:
                            lines = 8 if role in CODE_ROLES else 3
                            widgets.append(gr.Textbox(label=label, info=hint, lines=lines, max_lines=40))
                    method = gr.Radio(GENERATION_METHODS, value="human", label="How was this item made?")
                    send = gr.Button("Submit this item", variant="primary")
                    outcome = gr.Markdown()

                    def submit(title_value, method_value, *values, request: gr.Request):
                        return submit_one(current, title_value, method_value, values, request)

                    send.click(submit, [title, method, *widgets], outcome, api_visibility="private")

            with gr.Tab("Upload a file of items"):
                gr.Markdown(
                    "Upload **JSONL**, a **JSON** array, **CSV** or **TSV**, with one item per row and the pool's field "
                    "keys as columns. Every row is checked against the pool's fields before anything is sent."
                )
                bulk_file = gr.File(
                    label="Items file", file_types=[".jsonl", ".ndjson", ".json", ".csv", ".tsv"], type="filepath"
                )
                bulk_method = gr.Radio(GENERATION_METHODS, value="human", label="How were these items made?")
                bulk_summary = gr.Markdown()
                bulk_rows = gr.State([])
                bulk_table = gr.Dataframe(
                    headers=["Send", "Row", "Check", "Title", "Item"],
                    datatype=["bool", "number", "str", "str", "str"],
                    type="array",
                    interactive=True,
                    static_columns=[1, 2, 3, 4],
                    wrap=True,
                    max_height=420,
                )
                bulk_send = gr.Button("Send the ticked rows", variant="primary")
                bulk_out = gr.Markdown()

            with gr.Tab("My submissions"):
                subs_refresh = gr.Button("Refresh")
                subs_note = gr.Markdown()
                subs_table = gr.Dataframe(
                    headers=["Submitted", "Pool", "Title", "Status"],
                    datatype=["str", "str", "str", "str"],
                    interactive=False,
                    wrap=True,
                    max_height=420,
                )
                sub_pick = gr.Dropdown(label="Open a submission", choices=[], interactive=True)
                sub_detail = gr.Markdown()

    view_outputs = [
        status, sign_in_btn, sign_out_btn, setup_col, setup_md, handle_in, check_btn, claim_btn, resend_btn, work_col, pool_dd
    ]

    refresh_btn.click(account_view, None, view_outputs, api_visibility="private")
    sign_out_btn.click(sign_out, None, view_outputs, api_visibility="private")
    check_btn.click(check_handle, handle_in, setup_msg, api_visibility="private")
    claim_btn.click(finish_setup, handle_in, setup_msg, api_visibility="private").then(
        account_view, None, view_outputs, api_visibility="private"
    )
    resend_btn.click(resend_verification, None, setup_msg, api_visibility="private")
    pool_dd.change(load_contract, pool_dd, [contract, contract_md], api_visibility="private")
    bulk_file.change(parse_upload, [bulk_file, contract], [bulk_rows, bulk_table, bulk_summary], api_visibility="private")
    bulk_send.click(submit_rows, [bulk_table, bulk_rows, bulk_method, contract], bulk_out, api_visibility="private")
    subs_refresh.click(load_submissions, None, [subs_table, sub_pick, subs_note], api_visibility="private")
    sub_pick.change(show_submission, sub_pick, sub_detail, api_visibility="private")

    return view_outputs


def select_tab(request: gr.Request):
    """Open on the Contribute tab when sign-in sends the browser back here."""
    return gr.Tabs(selected="contribute") if request and request.query_params.get("tab") == "contribute" else gr.Tabs()
