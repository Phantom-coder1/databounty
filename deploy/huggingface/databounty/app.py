# SPDX-License-Identifier: Apache-2.0
"""DataBounty on Hugging Face: DataBounty's community programme, and a place to contribute to it.

The page people see is web/ (index.html, app.css, app.js): a small web app drawn like the
DataBounty console, talking to webapi.py. Public data comes from databounty.io's API (see
databounty.py); signing in and contributing go through account.py and contribute.py.

Gradio is mounted underneath for one job: the `gr.api` endpoints at the bottom are this Space's
public, read-only programmatic surface, and with `mcp_server=True` each is also an MCP tool
described by its docstring (served under /gradio_api/mcp/).
"""

from __future__ import annotations

import html
import os
from pathlib import Path
from typing import Any

import gradio as gr
import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import account
import databounty as db
import webapi

HERE = Path(__file__).parent
WEB = HERE / "web"


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


# --- API and MCP tools ----------------------------------------------------------------------------


def _upstream(exc: db.UpstreamError) -> gr.Error:
    return gr.Error(str(exc))


def get_platform_stats() -> dict:
    """Get live totals for DataBounty's community dataset programme.

    Returns:
        The number of dataset programmes, target and accepted item counts, items that have
        cleared the automated checks, contributors, published datasets, total karma awarded,
        and the karma tier ladder.
    """
    try:
        stats = db.stats()
        ladder = db.tiers()
    except db.UpstreamError as exc:
        raise _upstream(exc) from exc
    return {
        "dataset_programmes": stats.get("programs"),
        "target_items": stats.get("targetItems"),
        "accepted_items": stats.get("acceptedItems"),
        "items_cleared_automated_checks": stats.get("clearedItems"),
        "contributors": stats.get("contributors"),
        "published_datasets": stats.get("publishedDatasets"),
        "total_karma_awarded": stats.get("totalKarmaAwarded"),
        "karma_tiers": [{"tier": t.get("label"), "min_karma": t.get("minKarma")} for t in ladder],
        "source": db.SITE_URL,
    }


def search_open_pools(query: str = "", domain: str = "", difficulty: str = "", limit: int = 20) -> list[dict]:
    """Search DataBounty's open community pools: datasets currently accepting contributions.

    Args:
        query: Free-text filter on pool title, dataset type, language and description. Leave empty to list all.
        domain: Optional domain filter: coding, legal, healthcare, finance or science.
        difficulty: Optional difficulty filter: beginner, intermediate or advanced.
        limit: Maximum number of pools to return, from 1 to 50.

    Returns:
        Open pools, each with its id, title, dataset type, difficulty, accepted and target item counts,
        karma per accepted item, licence, and links to view the pool or contribute to it.
    """
    domain = (domain or "").strip().lower()
    difficulty = (difficulty or "").strip().lower()
    if domain and domain not in db.DOMAINS:
        raise gr.Error(f"Unknown domain {domain!r}. Use one of: {', '.join(db.DOMAINS)}.")
    if difficulty and difficulty not in db.DIFFICULTIES:
        raise gr.Error(f"Unknown difficulty {difficulty!r}. Use one of: {', '.join(db.DIFFICULTIES)}.")
    try:
        pools = db.open_pools(query or "", domain, difficulty)
    except db.UpstreamError as exc:
        raise _upstream(exc) from exc
    return pools[: max(1, min(int(limit or 20), 50))]


def get_pool_details(pool_id: str) -> dict:
    """Get one DataBounty pool's public details: description, field contract, karma pricing and progress.

    Args:
        pool_id: The pool id, as returned by search_open_pools.

    Returns:
        The pool's description, every field an item must supply, up to three approved example items,
        karma per accepted and per audited item, audit coverage, progress and links, or an "error" key
        when no pool has that id.
    """
    try:
        pool = db.pool_details(pool_id)
    except ValueError as exc:
        return {"error": str(exc)}
    except db.UpstreamError as exc:
        raise _upstream(exc) from exc
    return pool if pool is not None else {"error": f"No pool with id {pool_id!r}."}


def list_published_datasets() -> list[dict]:
    """List the datasets DataBounty has published to the Hugging Face Hub, newest first.

    Returns:
        Each dataset's title, item count, licence, publication date, Hugging Face repo id and URL,
        GitHub mirror URL when there is one, and Hub download and like counts.
    """
    try:
        return db.published_datasets()
    except db.UpstreamError as exc:
        raise _upstream(exc) from exc


def get_karma_leaderboard(limit: int = 20, tier: str = "", query: str = "") -> list[dict]:
    """Get DataBounty's all-time karma leaderboard.

    Args:
        limit: Number of contributors to return, from 1 to 100.
        tier: Optional tier filter: dharma, bodhi, moksha or nirvana.
        query: Optional search on handle or display name.

    Returns:
        Ranked contributors with handle, display name, karma, accepted item count, tier and public profile URL.
    """
    tier = (tier or "").strip().lower()
    if tier and tier not in db.TIERS:
        raise gr.Error(f"Unknown tier {tier!r}. Use one of: {', '.join(db.TIERS)}.")
    try:
        return db.leaderboard(limit=limit, tier=tier, query=query or "")
    except db.UpstreamError as exc:
        raise _upstream(exc) from exc


def get_contributor_profile(handle: str) -> dict:
    """Get a DataBounty contributor's public profile.

    Only public profiles are returned, and only the fields the member chose to show.

    Args:
        handle: The contributor's public handle, for example "starlord". A leading @ or a databounty.io profile URL also works.

    Returns:
        Display name, tier, karma, badges, accepted items, audits, datasets contributed to and Hugging Face
        datasets they are credited on, or an "error" key when there is no public profile.
    """
    try:
        person = db.contributor(handle)
    except ValueError as exc:
        return {"error": str(exc)}
    except db.UpstreamError as exc:
        raise _upstream(exc) from exc
    return person if person is not None else {"error": "No public profile with that handle."}


with gr.Blocks(title="DataBounty") as demo:
    gr.Markdown("DataBounty's read-only MCP tools. The Space itself is at [/](/).")
    gr.api(get_platform_stats, api_name="get_platform_stats")
    gr.api(search_open_pools, api_name="search_open_pools")
    gr.api(get_pool_details, api_name="get_pool_details")
    gr.api(list_published_datasets, api_name="list_published_datasets")
    gr.api(get_karma_leaderboard, api_name="get_karma_leaderboard")
    gr.api(get_contributor_profile, api_name="get_contributor_profile")

# Every handler is network-bound and mostly answered from cache, so let several
# visitors through at once instead of Gradio's default of one per event.
demo.queue(default_concurrency_limit=8)

# --- The page, its API and sign-in ------------------------------------------------------------------
# All registered before Gradio's mount at "/", so they answer first.

server = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
server.include_router(webapi.router)
server.add_exception_handler(webapi.ApiError, webapi.api_error)


@server.get("/", include_in_schema=False)
def index() -> FileResponse:
    # Always fresh, so a redeploy shows at once; the assets it loads carry a version.
    return FileResponse(WEB / "index.html", media_type="text/html", headers={"Cache-Control": "no-cache"})


server.mount("/static", StaticFiles(directory=WEB), name="static")

SIGN_IN_PAGE_STYLE = (
    "body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f3f4f0;color:#14170f;"
    "font-family:Geist,ui-sans-serif,system-ui,sans-serif}"
    ".card{width:min(380px,calc(100vw - 32px));box-sizing:border-box;padding:32px;background:#fff;border:1px solid #e4e6df;"
    "border-radius:14px;text-align:center}"
    ".logo{width:38px;height:38px;margin:0 auto 14px;display:block}"
    "h1{margin:0 0 8px;font:600 18px/1.3 'Geist Mono',ui-monospace,monospace}"
    "p{margin:0 0 18px;color:#6b7360;font-size:14px;line-height:1.55}"
    "button,a.btn{display:inline-block;width:100%;box-sizing:border-box;padding:11px 14px;border:0;border-radius:8px;"
    "background:#14170f;color:#a3f60a;font:500 13px 'Geist Mono',ui-monospace,monospace;cursor:pointer;text-decoration:none}"
)


def _sign_in_page(message: str, status: int, *, popup: bool = False, title: str = "Sign-in didn't finish", close: bool = False) -> HTMLResponse:
    action = (
        '<button onclick="window.close()">close this window</button>'
        if popup
        else '<a class="btn" href="/">back to the Space</a>'
    )
    body = (
        '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_e(title)}</title>"
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600&family=Geist+Mono:wght@500;600&display=swap" rel="stylesheet">'
        f"<style>{SIGN_IN_PAGE_STYLE}</style>"
        '<div class="card"><img class="logo" src="/static/logo.svg" alt="">'
        f"<h1>{_e(title)}</h1><p>{_e(message)}</p>{action}</div>"
        + ("<script>setTimeout(() => window.close(), 800)</script>" if close else "")
    )
    return HTMLResponse(body, status_code=status, headers=NO_STORE)


@server.get("/oauth/client-metadata.json")
def oauth_client_metadata() -> JSONResponse:
    # DataBounty reads this to learn the Space's name and where it may send people back to.
    return JSONResponse(account.client_metadata(), headers={"Cache-Control": "public, max-age=300"})


@server.get("/oauth/start")
def oauth_start(request: Request, n: str = "", popup: str = ""):
    in_window = popup == "1"
    # Only a sign-in the Space's own page started: a link from anywhere else could otherwise
    # sign someone in for the sender to collect (see account.py).
    if not account.valid_nonce_hash(n) or request.headers.get("sec-fetch-site") != "same-origin":
        return _sign_in_page("Start signing in from the Space.", 400, popup=in_window)
    try:
        url, login_cookie = account.begin_sign_in(n, in_window)
    except (account.SignInError, httpx.HTTPError, ValueError):
        return _sign_in_page("DataBounty's sign-in service didn't answer. Try again in a minute.", 503, popup=in_window)
    response = RedirectResponse(url, status_code=302, headers=NO_STORE)
    response.set_cookie(
        account.LOGIN_COOKIE,
        login_cookie,
        max_age=account.LOGIN_TTL_SECONDS,
        path="/oauth",
        httponly=True,
        secure=account.COOKIE_SECURE,
        samesite="lax",
    )
    return response


@server.get("/oauth/callback")
def oauth_callback(request: Request):
    login_cookie = request.cookies.get(account.LOGIN_COOKIE)
    context = account.login_context(login_cookie)
    in_window = context["p"]
    try:
        session_id = account.complete_sign_in(dict(request.query_params), login_cookie)
        if not account.valid_nonce_hash(context["n"]):
            raise account.SignInError("Start signing in from the Space.")
    except account.SignInError as exc:
        response = _sign_in_page(str(exc), 400, popup=in_window)
    except (httpx.HTTPError, ValueError):
        response = _sign_in_page("DataBounty's sign-in service didn't answer. Start again from the Space.", 503, popup=in_window)
    else:
        # The page that started this claims the session with its secret (account.claim_sign_in).
        account.hold_sign_in(context["n"], session_id)
        if in_window:
            response = _sign_in_page(
                "You can close this window. The Space updates by itself.", 200, popup=True, title="You're signed in", close=True
            )
        else:
            response = RedirectResponse("/", status_code=302, headers=NO_STORE)
    response.delete_cookie(account.LOGIN_COOKIE, path="/oauth")
    return response


@server.get("/oauth/status")
def oauth_status(n: str = "") -> JSONResponse:
    # Only "has this sign-in finished": claiming it needs the secret the hash came from.
    return JSONResponse({"ready": account.sign_in_ready(n)}, headers=NO_STORE)


app = gr.mount_gradio_app(
    server,
    demo,
    path="/",
    mcp_server=True,
    favicon_path=str(WEB / "logo.svg"),
    # Spaces set GRADIO_SSR_MODE, and in SSR mode Gradio starts a Node server on
    # the Space's one public port, which uvicorn below must own.
    ssr_mode=False,
)

if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("GRADIO_SERVER_NAME", "0.0.0.0"),
        port=int(os.environ.get("GRADIO_SERVER_PORT", "7860")),
        # Spaces sit behind Hugging Face's proxy; trust its forwarded scheme and host.
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
