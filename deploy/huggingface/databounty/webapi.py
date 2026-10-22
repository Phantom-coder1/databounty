# SPDX-License-Identifier: Apache-2.0
"""The JSON API behind the Space's web page (web/app.js).

Public reads come straight from databounty.py. Anything done as a person goes through account.py
with the session handle the page sends as `Authorization: Bearer <handle>`. The page keeps that
handle in its own storage (account.py explains why not a cookie), and a handle only ever reaches
the page through claim_sign_in, which only the page that started the sign-in can complete.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

import account
import contribute
import databounty as db
import items as checks

NO_STORE = {"Cache-Control": "no-store"}
MAX_FILE_BYTES = 100 * 1024 * 1024

router = APIRouter(prefix="/api")


class ApiError(Exception):
    """An answer for the page: an HTTP status and a message written for people."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def api_error(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse({"error": exc.message}, status_code=exc.status, headers=NO_STORE)


def _ok(data: Any) -> JSONResponse:
    return JSONResponse(data, headers=NO_STORE)


def _public(load):
    try:
        return load()
    except db.UpstreamError as exc:
        raise ApiError(502, str(exc)) from exc


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


def _session(request: Request) -> tuple[str, account.Session]:
    session_id = _bearer(request)
    session = account.session_for(session_id) if session_id else None
    if session is None:
        raise ApiError(401, "Sign in to continue.")
    return session_id, session


def _call(session: account.Session, name: str, arguments: dict[str, Any] | None = None) -> Any:
    try:
        return account.call_tool(session, name, arguments)
    except account.NotSignedIn as exc:
        raise ApiError(401, "Your sign-in ended. Sign in again to continue.") from exc
    except account.ToolError as exc:
        raise ApiError(400, str(exc)) from exc


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError as exc:
        raise ApiError(400, "Send a JSON body.") from exc
    return body if isinstance(body, dict) else {}


# --- Public ---------------------------------------------------------------------------------------


@router.get("/overview")
def overview():
    return _ok({"stats": _public(db.stats), "tiers": _public(db.tiers), "hfOrg": db.HF_ORG, "siteUrl": db.SITE_URL, "appUrl": db.APP_URL})


@router.get("/pools")
def pools(q: str = "", domain: str = "", difficulty: str = ""):
    return _ok({"pools": _public(lambda: db.open_pools(q, domain.lower(), difficulty.lower()))})


@router.get("/datasets")
def datasets():
    return _ok({"datasets": _public(db.published_datasets)})


@router.get("/leaderboard")
def leaderboard(tier: str = "", q: str = ""):
    return _ok({"rows": _public(lambda: db.leaderboard(limit=100, tier=tier.lower(), query=q))})


@router.get("/profiles/{handle}")
def profile(handle: str):
    try:
        person = _public(lambda: db.contributor(handle))
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    if person is None:
        raise ApiError(404, "There's no public profile with that handle. Members can keep their profile private.")
    return _ok(person)


@router.get("/tools")
def tools():
    return _ok({"tools": _public(db.platform_mcp_tools), "mcpUrl": db.MCP_URL})


# --- Session and account ------------------------------------------------------------------------------


@router.post("/session/claim")
async def claim_session(request: Request):
    nonce = str((await _json(request)).get("nonce") or "")
    session_id = account.claim_sign_in(nonce)
    if not session_id:
        raise ApiError(404, "That sign-in isn't ready, or it was already used.")
    return _ok({"session": session_id})


@router.post("/session/sign-out")
def sign_out(request: Request):
    account.sign_out(_bearer(request) or None)
    return _ok({"ok": True})


@router.get("/me")
def me(request: Request):
    _, session = _session(request)
    who = _call(session, "whoami")
    session.me = who
    scopes = set(session.scope.split())
    step = (
        "permission" if "contribute" not in scopes
        else "verify_email" if not who.get("emailVerified")
        else "choose_handle" if not who.get("handle")
        else "finish_setup" if not who.get("onboarded")
        else "ready"
    )
    suggestion = ""
    if step == "choose_handle" and "account" in scopes:
        try:
            suggested = account.call_tool(session, "suggest_handles", {"baseName": who.get("displayName") or "", "count": 3})
            suggestion = suggested[0] if isinstance(suggested, list) and suggested else ""
        except (account.ToolError, account.NotSignedIn):
            suggestion = ""
    return _ok({
        "displayName": who.get("displayName") or "",
        "handle": who.get("handle") or "",
        "email": who.get("email") or "",
        "karma": who.get("karmaTotal") if isinstance(who.get("karmaTotal"), (int, float)) else 0,
        "step": step,
        "suggestedHandle": suggestion,
        "profileUrl": f"{db.SITE_URL}/{who.get('handle')}/" if who.get("handle") else "",
    })


@router.post("/me/handle-check")
async def handle_check(request: Request):
    _, session = _session(request)
    handle = str((await _json(request)).get("handle") or "").strip().lower().lstrip("@")
    if not handle:
        raise ApiError(400, "Type a username first.")
    result = _call(session, "get_handle_availability", {"handle": handle})
    return _ok({"handle": handle, "available": bool(result.get("available")), "reason": result.get("reason") or ""})


@router.post("/me/finish-setup")
async def finish_setup(request: Request):
    _, session = _session(request)
    handle = str((await _json(request)).get("handle") or "").strip().lower().lstrip("@")
    who = session.me or _call(session, "whoami")
    if not who.get("handle"):
        if not handle:
            raise ApiError(400, "Type the username you want first.")
        result = _call(session, "get_handle_availability", {"handle": handle})
        if not result.get("available"):
            raise ApiError(409, f"@{handle} isn't available: {result.get('reason') or 'it is taken'}.")
        _call(session, "claim_handle", {"handle": handle})
    _call(session, "complete_onboarding", {"persona": "contributor"})
    session.me = None
    return _ok({"ok": True})


@router.post("/me/resend-verification")
def resend_verification(request: Request):
    _, session = _session(request)
    result = _call(session, "resend_email_verification") or {}
    return _ok({"alreadyVerified": result.get("status") == "already_verified"})


# --- Pools ---------------------------------------------------------------------------------------------


def _contract(session: account.Session, pool_id: str) -> dict[str, Any]:
    return contribute.contract_view(_call(session, "get_pool_contract", {"bountyId": pool_id}), pool_id)


@router.get("/pools/{pool_id}/contract")
def contract(pool_id: str, request: Request):
    session = account.session_for(_bearer(request)) if _bearer(request) else None
    if session is not None:
        return _ok(_contract(session, pool_id))
    try:
        details = _public(lambda: db.pool_details(pool_id))
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    if details is None:
        raise ApiError(404, "That pool is no longer listed.")
    return _ok(contribute.public_contract_view(details))


@router.get("/pools/{pool_id}/template.csv")
def template(pool_id: str):
    try:
        details = _public(lambda: db.pool_details(pool_id))
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    if details is None:
        raise ApiError(404, "That pool is no longer listed.")
    text = contribute.template_csv(details.get("fields") or [])
    if not text:
        raise ApiError(404, "This pool takes files, so add its items one at a time.")
    slug = re.sub(r"[^a-z0-9]+", "-", str(details.get("title") or "pool").lower()).strip("-")[:48] or "pool"
    return Response(
        text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{slug}-template.csv"', **NO_STORE},
    )


async def _save(upload: Any) -> tuple[str, str, str]:
    """An uploaded file, to a temporary path: (path, filename, content type)."""
    data = await upload.read()
    if len(data) > MAX_FILE_BYTES:
        raise ApiError(413, f"{upload.filename} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
    suffix = Path(upload.filename or "upload").suffix[:16]
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, prefix="databounty-space-") as handle:
        handle.write(data)
    return handle.name, Path(upload.filename or "upload").name, upload.content_type or "application/octet-stream"


@router.post("/pools/{pool_id}/items")
async def submit_item(pool_id: str, request: Request):
    _, session = _session(request)
    # Closing the form deletes the uploads' temporary files.
    async with request.form() as form:
        return await _submit_item(session, pool_id, form)


async def _submit_item(session: account.Session, pool_id: str, form: Any) -> JSONResponse:
    method = str(form.get("method") or "")
    if method not in contribute.GENERATION_METHODS:
        raise ApiError(400, "Choose how you made this item.")
    try:
        values = json.loads(str(form.get("values") or "{}"))
    except ValueError as exc:
        raise ApiError(400, "The form's values didn't arrive in one piece. Try again.") from exc
    files: dict[str, list[Any]] = {}
    for name, value in form.multi_items():
        if name.startswith("file:") and getattr(value, "filename", None):
            files.setdefault(name[5:], []).append(value)

    contract_data = _contract(session, pool_id)
    if not contract_data["open"]:
        raise ApiError(409, "This pool isn't taking items right now.")
    payload, problems = contribute.item_payload(contract_data["fields"], values if isinstance(values, dict) else {}, {k: len(v) for k, v in files.items()})
    if problems:
        raise ApiError(400, " ".join(problems))

    saved: list[str] = []
    try:
        for spec in contract_data["fields"]:
            uploads = files.get(spec["key"]) or []
            if spec["role"] != "file" or not uploads:
                continue
            ids = []
            for upload in uploads:
                path, filename, content_type = await _save(upload)
                saved.append(path)
                try:
                    ids.append(account.upload_file(session, pool_id, path, filename, content_type))
                except account.NotSignedIn as exc:
                    raise ApiError(401, "Your sign-in ended. Sign in again to continue.") from exc
                except account.ToolError as exc:
                    raise ApiError(400, str(exc)) from exc
            payload[spec["key"]] = ids if spec["maxCount"] > 1 else ids[0]
    finally:
        for path in saved:
            Path(path).unlink(missing_ok=True)

    title = " ".join(str(form.get("title") or "").split())[:120] or contribute.title_for(payload)
    if len(title) < 3:
        title = f"{title} item".strip()
    result = _call(
        session,
        "submit_pool_items",
        {"bountyId": pool_id, "items": [{"title": title, "payloadJson": payload, "generationMethod": method}]},
    )
    subs = result.get("submissions") or []
    duplicate = bool(subs) and all(s.get("status") == "rejected" and s.get("duplicateOfSubmissionId") for s in subs)
    return _ok({"duplicate": duplicate, "submissions": [{"id": s.get("id"), "status": s.get("status")} for s in subs], "karma": contract_data["karma"]})


@router.post("/pools/{pool_id}/bulk/preview")
async def bulk_preview(pool_id: str, request: Request):
    _, session = _session(request)
    async with request.form() as form:
        upload = form.get("file")
        if not getattr(upload, "filename", None):
            raise ApiError(400, "Choose a file first.")
        data = await upload.read()
    fields = _contract(session, pool_id)["fields"]
    try:
        rows = checks.parse_bulk(data, upload.filename)
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    if len(rows) > contribute.MAX_BULK_ROWS:
        raise ApiError(400, f"That file has {len(rows):,} rows. Upload up to {contribute.MAX_BULK_ROWS} at a time.")
    for row in rows:
        if row.payload is not None:
            row.payload = checks.coerce_for_contract(fields, row.payload)
    checks.check_rows(rows, fields)
    return _ok({
        "rows": [
            {
                "number": row.number,
                "ok": row.ok,
                "problem": "" if row.ok else (row.error or "; ".join(row.problems)),
                "title": contribute.title_for(row.payload, row.number),
                "payload": row.payload,
            }
            for row in rows
        ],
        "hasFileFields": any(spec["role"] == "file" for spec in fields),
    })


@router.post("/pools/{pool_id}/bulk/send")
async def bulk_send(pool_id: str, request: Request):
    _, session = _session(request)
    body = await _json(request)
    method = str(body.get("method") or "")
    if method not in contribute.GENERATION_METHODS:
        raise ApiError(400, "Choose how you made these items.")
    contract_data = _contract(session, pool_id)
    if not contract_data["open"]:
        raise ApiError(409, "This pool isn't taking items right now.")
    # Every row is checked again here: the page's copy is only a preview.
    items, skipped = [], 0
    for row in body.get("rows") or []:
        payload = row.get("payload") if isinstance(row, dict) else None
        if not isinstance(payload, dict) or checks.item_problems(contract_data["fields"], payload):
            skipped += 1
            continue
        number = int(row.get("number") or 0)
        title = " ".join(str(row.get("title") or "").split())[:120] or contribute.title_for(payload, number)
        items.append({"title": title, "payloadJson": payload, "generationMethod": method})
    if not items:
        raise ApiError(400, "None of those rows are ready to send.")
    sent = duplicates = 0
    problem = ""
    size = max(1, contract_data["maxItems"])
    for start in range(0, len(items), size):
        try:
            result = account.call_tool(session, "submit_pool_items", {"bountyId": pool_id, "items": items[start : start + size]})
        except account.NotSignedIn:
            problem = "Your sign-in ended, so the rest weren't sent. Sign in again to continue."
            break
        except account.ToolError as exc:
            problem = f"Items {start + 1} onwards weren't sent: {exc}"
            break
        created = result.get("submissions") or []
        sent += len(created)
        duplicates += sum(1 for s in created if s.get("status") == "rejected" and s.get("duplicateOfSubmissionId"))
        if result.get("stoppedAtCapacity"):
            problem = "The pool filled up, so the rest weren't sent."
            break
    return _ok({"sent": sent, "duplicates": duplicates, "skipped": skipped, "problem": problem, "karma": contract_data["karma"]})


# --- Submissions ------------------------------------------------------------------------------------------


@router.get("/submissions")
def submissions(request: Request):
    _, session = _session(request)
    data = _call(session, "list_my_submissions", {"limit": 50}) or {}
    rows = [contribute.submission_row(item) for item in data.get("items") or []]
    return _ok({"items": rows, "total": data.get("total") if isinstance(data.get("total"), int) else len(rows)})


@router.get("/submissions/{submission_id}")
def submission(submission_id: str, request: Request):
    _, session = _session(request)
    return _ok(contribute.submission_view(_call(session, "check_submission", {"submissionId": submission_id}), db.APP_URL))
