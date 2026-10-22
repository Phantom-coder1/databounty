# SPDX-License-Identifier: Apache-2.0
"""DataBounty on Hugging Face: DataBounty's community programme, and a place to contribute to it.

The browsing tabs render live public data from databounty.io's API (see databounty.py).
The Contribute tab signs a person in to DataBounty and adds items as them (see
contribute.py and account.py). The `gr.api` endpoints at the bottom are this Space's
public programmatic surface; served with `mcp_server=True`, each one is also an MCP
tool described by its docstring. UI handlers are registered with
`api_visibility="private"` so they do not show up as duplicate, UI-shaped tools.
"""

from __future__ import annotations

import html
import os
import re
from pathlib import Path
from typing import Any

import gradio as gr
import httpx
import pandas as pd
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import account
import contribute
import databounty as db

HERE = Path(__file__).parent
SPACE_HOST = os.environ.get("SPACE_HOST", "databounty-io-databounty.hf.space")
SPACE_MCP_URL = f"https://{SPACE_HOST}/gradio_api/mcp/"
REPO_URL = "https://github.com/Databounty-io/databounty"
LIME = "#b6ff1c"

ANY_DOMAIN = "Any domain"
ANY_DIFFICULTY = "Any difficulty"
ALL_TIERS = "All tiers"

_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{3,8}$")
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+!|<>])")


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _md(value: Any) -> str:
    # Pool titles and display names are user-supplied; escape Markdown so they
    # render as text and cannot break a table cell or forge a link.
    return _MD_SPECIAL.sub(r"\\\1", str(value or ""))


def _num(value: Any) -> str:
    return f"{value:,}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "—"


def _color(value: Any) -> str:
    return value if isinstance(value, str) and _HEX_COLOR.match(value) else LIME


def _choice(value: str | None, any_label: str) -> str:
    return "" if not value or value == any_label else value


def _unavailable(exc: Exception) -> str:
    return (
        f'<div class="db-note">{_e(exc)} Try again in a minute, or visit '
        f'<a href="{_e(db.SITE_URL)}" target="_blank" rel="noopener">databounty.io</a>.</div>'
    )


# --- Overview ----------------------------------------------------------------------


def render_overview() -> str:
    try:
        stats = db.stats()
        ladder = db.tiers()
    except db.UpstreamError as exc:
        return _unavailable(exc)
    cards = [
        ("Items accepted", stats.get("acceptedItems")),
        ("Dataset programmes", stats.get("programs")),
        ("Contributors", stats.get("contributors")),
        ("Datasets published", stats.get("publishedDatasets")),
        ("Karma awarded", stats.get("totalKarmaAwarded")),
    ]
    cards_html = "".join(
        f'<div class="db-card"><div class="db-v">{_num(value)}</div><div class="db-k">{_e(label)}</div></div>'
        for label, value in cards
    )
    cleared = stats.get("clearedItems") or 0
    target = stats.get("targetItems") or 0
    pct = min(100.0, 100 * cleared / target) if target else 0.0
    tiers_html = "".join(
        f'<div class="db-tier"><span class="db-dot" style="background:{_color(tier.get("color"))}"></span>'
        f'<b>{_e(tier.get("label"))}</b> <span class="db-k">from {_num(tier.get("minKarma"))} karma</span></div>'
        for tier in ladder
    )
    return f"""
<div class="db-cards">{cards_html}</div>
<div class="db-section">
  <div class="db-k">{_num(cleared)} of {_num(target)} target items have cleared the automated checks, across every programme</div>
  <div class="db-bar"><span style="width:{pct:.1f}%"></span></div>
</div>
<div class="db-section">
  <div class="db-k">Karma tiers: standing is public and permanent</div>
  <div class="db-tiers">{tiers_html}</div>
</div>
"""


HOW_IT_WORKS = f"""
### How it works

1. **A sponsor asks for a dataset**, and DataBounty opens a **pool** for it: what each item needs, how many are wanted, and the karma each accepted item earns.
2. **You add items**: on the **Contribute** tab here, on databounty.io, or through an AI agent over MCP.
3. **Every item is checked automatically**, for duplicates and disclosed AI help, and its tests run in a sandbox where the dataset has them.
4. **People review it**, and you earn karma for each item they accept.
5. **Finished datasets are published** under [{db.HF_ORG}](https://huggingface.co/{db.HF_ORG}) on the Hub, crediting every contributor.
"""


# --- Open pools ----------------------------------------------------------------------

def render_pools(query: str, domain: str, difficulty: str):
    try:
        pools = db.open_pools(query or "", _choice(domain, ANY_DOMAIN), _choice(difficulty, ANY_DIFFICULTY))
    except db.UpstreamError as exc:
        return gr.update(choices=[], value=None), f'<div class="db-notice db-warn">{_e(exc)}</div>'
    count = len(pools)
    summary = (
        f"{count} open pool{'' if count == 1 else 's'}. Choose one to see what it needs."
        if count
        else "No open pool matches those filters."
    )
    choices = [(contribute.pool_card_label(pool), pool["id"]) for pool in pools]
    return gr.update(choices=choices, value=None), f'<div class="db-muted">{_e(summary)}</div>'


def show_pool(pool_id: str | None):
    """Open one pool's card in place of the grid on the Open pools tab."""
    card, go = render_pool(pool_id)
    return card, go, gr.update(visible=not pool_id), gr.update(visible=bool(pool_id))


def render_pool(pool_id: str | None):
    """The pool card for the Open pools tab, and whether to offer "Contribute to this pool"."""
    if not pool_id:
        return "", gr.update(visible=False)
    try:
        pool = db.pool_details(pool_id)
    except (ValueError, db.UpstreamError) as exc:
        return f'<div class="db-notice db-warn">{_e(exc)}</div>', gr.update(visible=False)
    if pool is None:
        return '<div class="db-notice db-warn">That pool is no longer listed.</div>', gr.update(visible=False)
    accepted, target = pool["accepted_items"] or 0, pool["target_items"] or 0
    facts = [f"{accepted:,} of {target:,} accepted"]
    if pool["license"]:
        facts.append(f"licence {_e(pool['license'])}")
    facts.append(f'<a href="{_e(pool["page_url"])}" target="_blank" rel="noopener">View on databounty.io</a>')
    card = contribute.pool_summary_html(
        title=pool["title"],
        type_name=pool["dataset_type"],
        difficulty=pool["difficulty"],
        karma=pool["karma_per_accepted_item"],
        wanted=max(0, target - accepted),
        language=pool.get("language") or "",
        field_labels=[field["label"] or field["key"] for field in pool["fields"]],
        note=f'<div class="db-muted db-facts">{" · ".join(facts)}</div>',
    )
    return card, gr.update(visible=True)


# --- Published datasets ------------------------------------------------------------------

DATASET_COLUMNS = ["Dataset", "Items", "Licence", "Published", "Downloads", "Likes", "Also on"]
DATASET_TYPES = ["markdown", "str", "str", "str", "str", "str", "markdown"]


def render_datasets():
    try:
        datasets = db.published_datasets()
    except db.UpstreamError as exc:
        return pd.DataFrame(columns=DATASET_COLUMNS), str(exc)
    rows = [
        {
            "Dataset": f"[{_md(d['title'])}]({d['hugging_face_url']})" if d["hugging_face_url"] else _md(d["title"]),
            "Items": f"{d['items']:,}",
            "Licence": d["license"] or "—",
            "Published": d["published_on"] or "—",
            "Downloads": _num(d["downloads"]),
            "Likes": _num(d["likes"]),
            "Also on": f"[GitHub]({d['github_url']})" if d["github_url"] else "—",
        }
        for d in datasets
    ]
    example = next((d["hugging_face_id"] for d in datasets if d["hugging_face_id"]), None)
    snippet = (
        f"Every dataset ships `data/items.jsonl` plus a `manifest.json` with contributor credit. "
        f"Load one with 🤗 Datasets:\n\n```python\nfrom datasets import load_dataset\n\n"
        f'ds = load_dataset("{example}")\n```'
        if example
        else ""
    )
    return pd.DataFrame(rows, columns=DATASET_COLUMNS), snippet


# --- Leaderboard and profiles --------------------------------------------------------------

BOARD_COLUMNS = ["Rank", "Contributor", "Tier", "Karma", "Accepted items"]
BOARD_TYPES = ["str", "markdown", "str", "str", "str"]


def render_leaderboard(tier: str, query: str):
    try:
        board = db.leaderboard(limit=100, tier=_choice(tier, ALL_TIERS).lower(), query=query or "")
    except db.UpstreamError as exc:
        gr.Warning(str(exc))
        return pd.DataFrame(columns=BOARD_COLUMNS)
    rows = [
        {
            "Rank": str(row["rank"] or "—"),
            "Contributor": f"[{_md(row['display_name'])}]({row['profile_url']}) @{_md(row['handle'])}",
            "Tier": row["tier"] or "—",
            "Karma": _num(row["karma"]),
            "Accepted items": _num(row["accepted_items"]),
        }
        for row in board
    ]
    return pd.DataFrame(rows, columns=BOARD_COLUMNS)


def render_profile(handle: str) -> str:
    if not (handle or "").strip():
        return "Enter a handle to look someone up."
    try:
        handle = db.normalize_handle(handle)
        person = db.contributor(handle)
    except (ValueError, db.UpstreamError) as exc:
        return str(exc)
    if person is None:
        return f"There is no public profile for **@{_md(handle)}**. Members can keep their profile private."
    facts = [
        f"**{_md(person['tier'])}** tier" if person["tier"] else "",
        f"**{person['karma']:,}** karma" if isinstance(person["karma"], int) else "",
        f"**{person['accepted_items']:,}** accepted items" if isinstance(person["accepted_items"], int) else "",
        f"**{person['audits']:,}** audits" if person["audits"] else "",
        f"member since {person['member_since']}" if person["member_since"] else "",
    ]
    parts = [f"## {_md(person['display_name'])} &nbsp;`@{person['handle']}`", " · ".join(f for f in facts if f)]
    if person["badges"]:
        parts.append("**Badges:** " + ", ".join(_md(b) for b in person["badges"]))
    if person["published_credits"]:
        parts.append(
            "**Credited on published datasets**\n\n"
            + "\n".join(f"- [{_md(c['title'])}]({c['hugging_face_url']})" for c in person["published_credits"])
        )
    contributed = sorted(person["datasets_contributed_to"] or [], key=lambda d: d.get("items") or 0, reverse=True)
    if contributed:
        more = f"\n- …and {len(contributed) - 8} more" if len(contributed) > 8 else ""
        parts.append(
            "**Most accepted items in**\n\n"
            + "\n".join(
                f"- {_md(d['title'])}: {_num(d['items'])} item{'' if d['items'] == 1 else 's'}" for d in contributed[:8]
            )
            + more
        )
    parts.append(f"[Full profile on databounty.io →]({person['profile_url']})")
    return "\n\n".join(p for p in parts if p)


def example_handles(limit: int = 3) -> list[str]:
    """Top leaderboard handles that have a public profile, for the examples row."""
    handles: list[str] = []
    try:
        top = db.stats().get("leaderboard") or []
        for row in top:
            handle = row.get("handle")
            if handle and db.contributor(handle):
                handles.append(handle)
            if len(handles) == limit:
                break
    except (ValueError, db.UpstreamError):
        pass
    return handles


# --- Agents ------------------------------------------------------------------------------------

TOOL_COLUMNS = ["Tool", "Scope", "What it does"]

AGENTS_INTRO = f"""
### Connect your agent to DataBounty

DataBounty runs its own MCP server. Agents can browse pools, submit items, run validator audits and track
karma. Sign-in is OAuth, so you never paste a key anywhere.

**Endpoint:** `{db.MCP_URL}` (streamable HTTP)

**Claude Code**: run this, then `/mcp` in the session and choose *authenticate*.

```bash
claude mcp add --transport http databounty {db.MCP_URL}
```

**Cursor**: `~/.cursor/mcp.json`

```json
{{ "mcpServers": {{ "databounty": {{ "url": "{db.MCP_URL}" }} }} }}
```

**VS Code / Copilot**: `.vscode/mcp.json`

```json
{{ "servers": {{ "databounty": {{ "type": "http", "url": "{db.MCP_URL}" }} }} }}
```

ChatGPT, Codex CLI, Gemini CLI and raw JSON-RPC: see [databounty.io/agents]({db.SITE_URL}/agents/).

### This Space is an MCP server too

Read-only and no sign-in, for agents that only need to read the catalogue:

```text
{SPACE_MCP_URL}
```

Tools, each prefixed `DataBounty_` by the Hub: `get_platform_stats`, `search_open_pools`,
`get_pool_details`, `list_published_datasets`, `get_karma_leaderboard`, `get_contributor_profile`.

### DataBounty's MCP tools

*Scope* is the permission a tool needs. **public** tools work without signing in.
"""


def render_tools() -> pd.DataFrame:
    try:
        tools = db.platform_mcp_tools()
    except db.UpstreamError:
        return pd.DataFrame(columns=TOOL_COLUMNS)
    order = {"public": 0, "read": 1, "account": 2, "contribute": 3, "validate": 4, "artifact": 5, "sponsor": 6}
    tools.sort(key=lambda t: (order.get(t["scope"], 9), t["name"]))
    rows = [
        {
            "Tool": tool["name"],
            "Scope": tool["scope"],
            "What it does": tool["description"][:220] + ("…" if len(tool["description"]) > 220 else ""),
        }
        for tool in tools
    ]
    return pd.DataFrame(rows, columns=TOOL_COLUMNS)


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
        The pool's description, every field an item must supply, karma per accepted and per audited item,
        audit coverage, progress and links, or an "error" key when no pool has that id.
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


# --- Layout ----------------------------------------------------------------------------------------

LOGO_SVG = (HERE / "assets" / "logo.svg").read_text(encoding="utf-8")

HEADER = f"""
<div class="db-header">
  <div class="db-logo" aria-hidden="true">{LOGO_SVG}</div>
  <div>
    <h1>DataBounty</h1>
    <p>Open datasets built by the community, checked by people and credited with karma.
       Live from <a href="{_e(db.SITE_URL)}" target="_blank" rel="noopener">databounty.io</a>.</p>
  </div>
</div>
<div class="db-cta">
  <a class="db-btn db-btn-primary" href="?tab=contribute" target="_self">Start contributing →</a>
  <a class="db-btn" href="{_e(db.APP_URL)}/sponsor/create/" target="_blank" rel="noopener">Request a dataset</a>
  <a class="db-btn" href="https://huggingface.co/{_e(db.HF_ORG)}" target="_blank" rel="noopener">Datasets on the Hub</a>
  <a class="db-btn" href="{REPO_URL}" target="_blank" rel="noopener">Source (Apache-2.0)</a>
</div>
"""

FOOTER = (
    "Browsing needs no account. When you sign in, your DataBounty sign-in stays in this Space's memory only, "
    f"and signing out ends it. Reviewing and requesting datasets happen on [databounty.io]({db.SITE_URL})."
)

CSS = f"""
#col-container {{ max-width: 1120px; margin: 0 auto; }}
.dark .gradio-container {{ color: var(--body-text-color); }}
.db-header {{ display: flex; gap: 16px; align-items: center; margin: 10px 0 4px; }}
.db-logo svg {{ width: 56px; height: 56px; display: block; }}
.db-header h1 {{ margin: 0; font-size: 1.9rem; line-height: 1.15; }}
.db-header p {{ margin: 4px 0 0; color: var(--body-text-color-subdued); }}
.db-cta {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0 6px; }}
.db-btn {{ padding: 8px 14px; border-radius: 8px; border: 1px solid var(--border-color-primary);
  text-decoration: none !important; color: var(--body-text-color) !important; font-weight: 600; font-size: .9rem; }}
.db-btn-primary {{ background: {LIME}; border-color: {LIME}; color: #0b0b0b !important; }}
.db-cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }}
.db-card {{ border: 1px solid var(--border-color-primary); border-radius: 12px; padding: 14px 16px;
  background: var(--block-background-fill); }}
.db-v {{ font-size: 1.7rem; font-weight: 700; line-height: 1.2; }}
.db-k {{ color: var(--body-text-color-subdued); font-size: .85rem; }}
.db-section {{ margin-top: 18px; }}
.db-bar {{ height: 10px; border-radius: 6px; background: var(--border-color-primary); overflow: hidden; margin-top: 6px; }}
.db-bar > span {{ display: block; height: 100%; background: {LIME}; }}
.db-tiers {{ display: flex; flex-wrap: wrap; gap: 10px 22px; margin-top: 8px; }}
.db-dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }}
.db-note {{ padding: 12px 14px; border-radius: 10px; border: 1px solid var(--border-color-primary); }}
.db-muted {{ color: var(--body-text-color-subdued); font-size: .9rem; }}
.db-hero h2 {{ margin: 8px 0 4px; font-size: 1.4rem; }}
.db-hero p {{ margin: 0 0 4px; color: var(--body-text-color-subdued); }}
.db-signin {{ max-width: 280px; }}
.db-account {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
.db-avatar {{ width: 38px; height: 38px; border-radius: 50%; background: {LIME}; color: #0b0b0b; display: grid;
  place-items: center; font-weight: 800; }}
.db-who span {{ color: var(--body-text-color-subdued); margin-left: 4px; }}
.db-account-row {{ align-items: center; }}
.db-chip {{ display: inline-flex; align-items: center; padding: 3px 10px; border-radius: 999px; font-size: .82rem;
  border: 1px solid var(--border-color-primary); background: var(--background-fill-secondary); white-space: nowrap; }}
.db-chip-accent {{ background: {LIME}; border-color: {LIME}; color: #0b0b0b; font-weight: 600; }}
.db-chips {{ display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0 12px; }}
.db-step {{ margin: 10px 0 2px; font-size: 1.05rem; }}
.db-card, .db-pool, .db-detail {{ border: 1px solid var(--border-color-primary); border-radius: 14px; padding: 16px 18px;
  background: var(--block-background-fill); }}
.db-card h3, .db-pool h3, .db-detail h3 {{ margin: 0; }}
.db-card p {{ margin: 4px 0 0; color: var(--body-text-color-subdued); }}
.db-pool h3 {{ font-size: 1.3rem; margin-top: 2px; }}
.db-pool-type {{ color: var(--body-text-color-subdued); font-size: .78rem; text-transform: uppercase; letter-spacing: .05em; }}
.db-submit-what {{ font-size: .93rem; }}
.db-submit-what span {{ color: var(--body-text-color-subdued); margin-right: 6px; }}
.db-tip {{ margin-top: 6px; font-size: .93rem; color: var(--body-text-color-subdued); }}
.db-facts {{ margin-top: 10px; }}
.db-field-head {{ display: flex; flex-direction: column; gap: 2px; margin: 6px 0 -4px; }}
.db-field-head span {{ color: var(--body-text-color-subdued); font-size: .86rem; }}
.db-notice {{ padding: 10px 14px; border-radius: 10px; border: 1px solid var(--border-color-primary); margin: 6px 0; }}
.db-ok {{ border-color: rgba(34, 197, 94, .5); background: rgba(34, 197, 94, .08); }}
.db-warn {{ border-color: rgba(245, 158, 11, .55); background: rgba(245, 158, 11, .08); }}
.db-bad {{ border-color: rgba(239, 68, 68, .55); background: rgba(239, 68, 68, .08); }}
.db-review {{ border-color: rgba(168, 85, 247, .5); background: rgba(168, 85, 247, .08); }}
.db-info {{ background: var(--background-fill-secondary); }}
.db-link-btn {{ align-self: flex-start; max-width: fit-content; }}
button.db-quiet {{ background: transparent !important; border: 1px solid var(--border-color-primary) !important;
  color: var(--body-text-color) !important; box-shadow: none !important; }}
button.db-quiet:hover {{ border-color: #84cc16 !important; }}
.db-tight {{ margin-bottom: -12px !important; }}
.db-pool-grid .wrap {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 10px; }}
.db-pool-grid label {{ margin: 0; padding: 12px 14px; border-radius: 12px; border: 1px solid var(--border-color-primary);
  background: var(--block-background-fill); cursor: pointer; box-shadow: none; align-items: flex-start; }}
.db-pool-grid label:hover {{ border-color: #84cc16; }}
.db-pool-grid label.selected {{ border-color: #84cc16; box-shadow: 0 0 0 2px rgba(182, 255, 28, .6); }}
.db-pool-grid input {{ display: none; }}
.db-pool-grid label span {{ display: block; margin: 0; white-space: pre-line; color: var(--body-text-color); font-weight: 650;
  font-size: .96rem; line-height: 1.45; }}
.db-pool-grid label span::first-line {{ color: var(--body-text-color-subdued); font-weight: 400; font-size: .8rem; }}
.db-table, .db-table table, .db-table td, .db-table th, .db-table .cell-wrap {{ font-family: var(--font) !important; }}
#db-mode .wrap {{ display: inline-flex; gap: 4px; padding: 4px; border-radius: 12px; width: fit-content;
  background: var(--background-fill-secondary); border: 1px solid var(--border-color-primary); }}
#db-mode label {{ margin: 0; padding: 6px 16px; border-radius: 9px; border: none; background: transparent; box-shadow: none;
  cursor: pointer; }}
#db-mode label.selected {{ background: var(--block-background-fill); box-shadow: 0 1px 3px rgba(0, 0, 0, .15); font-weight: 600; }}
#db-mode input {{ display: none; }}
#db-mode label span {{ margin: 0; }}
.db-example pre {{ white-space: pre-wrap; word-break: break-word; background: var(--background-fill-secondary);
  border-radius: 8px; padding: 10px 12px; font-size: .85rem; margin: 4px 0 12px; }}
.db-example-label {{ font-weight: 600; font-size: .9rem; }}
.db-detail-head {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }}
.db-badge {{ padding: 3px 10px; border-radius: 999px; font-size: .82rem; font-weight: 600; white-space: nowrap;
  border: 1px solid transparent !important; }}
.db-badge.db-ok {{ background: rgba(34, 197, 94, .16); color: #15803d; }}
.db-badge.db-warn {{ background: rgba(245, 158, 11, .2); color: #b45309; }}
.db-badge.db-bad {{ background: rgba(239, 68, 68, .16); color: #b91c1c; }}
.db-badge.db-review {{ background: rgba(168, 85, 247, .16); color: #7e22ce; }}
.db-badge.db-info {{ background: rgba(59, 130, 246, .14); color: #1d4ed8; }}
.dark .db-badge.db-ok, .dark .db-check-pass .db-check-icon {{ color: #4ade80; }}
.dark .db-badge.db-warn, .dark .db-check-note .db-check-icon {{ color: #fbbf24; }}
.dark .db-badge.db-bad, .dark .db-check-fail .db-check-icon {{ color: #f87171; }}
.dark .db-badge.db-review {{ color: #c084fc; }}
.dark .db-badge.db-info {{ color: #60a5fa; }}
.db-checks {{ list-style: none; padding: 0; margin: 14px 0 8px; display: grid; gap: 10px; }}
.db-check {{ display: flex; gap: 10px; align-items: flex-start; }}
.db-check-icon {{ width: 22px; height: 22px; border-radius: 50%; display: grid; place-items: center; font-weight: 800;
  font-size: .8rem; flex: none; }}
.db-check-pass .db-check-icon {{ background: rgba(34, 197, 94, .2); color: #15803d; }}
.db-check-fail .db-check-icon {{ background: rgba(239, 68, 68, .2); color: #b91c1c; }}
.db-check-wait .db-check-icon {{ background: var(--background-fill-secondary); color: var(--body-text-color-subdued); }}
.db-check-note .db-check-icon {{ background: rgba(245, 158, 11, .22); color: #b45309; }}
.db-detail .db-btn {{ display: inline-block; margin-top: 10px; }}
"""

# Sign-in, started from the page (see account.py for the whole flow). The page keeps a random
# secret in its own sessionStorage and sends only its hash. At its own address the Space signs
# in in the same tab; inside the huggingface.co embed, which may open windows but not leave the
# page, it opens a small sign-in window and checks every 1.5 s whether the sign-in finished.
SIGN_IN_JS = """
<script>
(() => {
  const KEY = "databounty-signin";
  const hex = (buf) => Array.from(new Uint8Array(buf), (b) => b.toString(16).padStart(2, "0")).join("");
  const say = (text) => { const el = document.getElementById("db-signin-note"); if (el) el.textContent = text; };
  const backHere = () => { const url = new URL(location.href); url.searchParams.set("tab", "contribute"); location.replace(url); };
  async function start() {
    const bytes = crypto.getRandomValues(new Uint8Array(32));
    const nonce = btoa(String.fromCharCode(...bytes)).replace(/[+/=]/g, (c) => ({ "+": "-", "/": "_", "=": "" })[c]);
    try { sessionStorage.setItem(KEY, nonce); } catch (e) {
      say("This browser blocked the page's storage, so sign-in can't finish here. Open the Space at its own address and try again.");
      return;
    }
    const n = hex(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(nonce)));
    if (window.top === window.self) { location.assign("/oauth/start?n=" + n); return; }
    const w = 520, h = 720;
    const left = Math.max(0, (window.screenX || 0) + ((window.outerWidth || w) - w) / 2);
    const top = Math.max(0, (window.screenY || 0) + ((window.outerHeight || h) - h) / 2);
    const win = window.open("/oauth/start?popup=1&n=" + n, "databounty-signin", `popup=yes,width=${w},height=${h},left=${left},top=${top}`);
    // huggingface.co serves its page with Cross-Origin-Opener-Policy: same-origin, so the window
    // opens but this frame gets no handle to it (null). Null is not "blocked": wait either way.
    say(win
      ? "Finish signing in in the window that opened. This page updates by itself."
      : "Finish signing in in the sign-in window. This page updates by itself. No window? Allow pop-ups for this page and press Sign in again.");
    const deadline = Date.now() + 10 * 60 * 1000;
    const check = async () => {
      if (Date.now() > deadline) { say("Sign-in timed out. Press Sign in to try again."); return; }
      try {
        const res = await fetch("/oauth/status?n=" + n, { cache: "no-store" });
        if (res.ok && (await res.json()).ready) { backHere(); return; }
      } catch (e) {}
      setTimeout(check, 1500);
    };
    setTimeout(check, 1500);
  }
  window.databountySignIn = () => { start().catch(() => say("Sign-in couldn't start. Try again.")); };
})();
</script>
"""
# Hands the page's sign-in secret to contribute.claim_view on load, then forgets it. Only the
# secret goes through this script: the stored session reaches account_view untouched, after
# Gradio has read it back from the page's storage.
READ_NONCE_JS = "(n) => { let v = ''; try { v = sessionStorage.getItem('databounty-signin') || ''; } catch (e) {} return [v]; }"
FORGET_NONCE_JS = "() => { try { sessionStorage.removeItem('databounty-signin'); } catch (e) {} }"

THEME = gr.themes.Citrus(
    primary_hue=gr.themes.colors.lime,
    font=[gr.themes.GoogleFont("Hanken Grotesk"), "ui-sans-serif", "system-ui", "sans-serif"],
    font_mono=[gr.themes.GoogleFont("JetBrains Mono"), "ui-monospace", "monospace"],
)

# Uploaded files can contain someone's work in progress: clear them out hourly.
with gr.Blocks(title="DataBounty", delete_cache=(3600, 3600)) as demo:
    with gr.Column(elem_id="col-container"):
        gr.HTML(HEADER)

        contribute.build_state()
        with gr.Tabs() as tabs:
            with gr.Tab("Overview", id="overview"):
                overview = gr.HTML('<div class="db-k">Loading live totals…</div>')
                gr.Markdown(HOW_IT_WORKS)

            with gr.Tab("Contribute", id="contribute"):
                contribute.build()

            with gr.Tab("My submissions", id="mine", visible=False) as subs_tab:
                contribute.build_submissions()
            contribute.ui.subs_tab = subs_tab

            with gr.Tab("Open pools", id="pools"):
                with gr.Row(equal_height=True):
                    pool_query = gr.Textbox(
                        placeholder="Search pools: try sql, cache or git",
                        show_label=False,
                        container=False,
                        scale=4,
                    )
                    pool_domain = gr.Dropdown(
                        [ANY_DOMAIN, *db.DOMAINS], value=ANY_DOMAIN, show_label=False, container=False, scale=1
                    )
                    pool_difficulty = gr.Dropdown(
                        [ANY_DIFFICULTY, *db.DIFFICULTIES], value=ANY_DIFFICULTY, show_label=False, container=False, scale=1
                    )
                    pool_search = gr.Button("Search", variant="primary", scale=1)
                with gr.Column() as browse_col:
                    pools_summary = gr.HTML()
                    pool_pick = gr.Radio(choices=[], show_label=False, container=False, elem_classes=["db-pool-grid"], interactive=True)
                with gr.Column(visible=False) as pool_col:
                    pool_back = gr.Button("← All pools", size="sm", elem_classes=["db-link-btn", "db-quiet"])
                    pool_detail = gr.HTML()
                    pool_go = gr.Button("Contribute to this pool", variant="primary", visible=False, elem_classes=["db-signin"])

            with gr.Tab("Published datasets", id="datasets"):
                datasets_table = gr.Dataframe(
                    headers=DATASET_COLUMNS,
                    datatype=DATASET_TYPES,
                    interactive=False,
                    wrap=True,
                    show_label=False,
                )
                datasets_snippet = gr.Markdown()

            with gr.Tab("Leaderboard", id="leaderboard"):
                with gr.Row(equal_height=True):
                    board_query = gr.Textbox(
                        placeholder="Search by handle or name", show_label=False, container=False, scale=4
                    )
                    board_tier = gr.Dropdown(
                        [ALL_TIERS, *(t.capitalize() for t in db.TIERS)],
                        value=ALL_TIERS,
                        show_label=False,
                        container=False,
                        scale=1,
                    )
                board_table = gr.Dataframe(
                    headers=BOARD_COLUMNS,
                    datatype=BOARD_TYPES,
                    interactive=False,
                    wrap=True,
                    max_height=560,
                    show_label=False,
                )

            with gr.Tab("Contributor profile", id="profile"):
                with gr.Row(equal_height=True):
                    profile_handle = gr.Textbox(
                        placeholder="A handle such as starlord, or a databounty.io profile URL",
                        show_label=False,
                        container=False,
                        scale=4,
                    )
                    profile_go = gr.Button("Look up", variant="primary", scale=1)
                profile_card = gr.Markdown()
                handles = example_handles()
                if handles:
                    # Not cached: a profile is live data, and a cached example
                    # would keep showing yesterday's karma forever.
                    gr.Examples(
                        examples=[[h] for h in handles],
                        inputs=[profile_handle],
                        outputs=[profile_card],
                        fn=render_profile,
                        cache_examples=False,
                        run_on_click=True,
                        label="Try a top contributor",
                    )

            with gr.Tab("For agents (MCP)", id="agents"):
                gr.Markdown(AGENTS_INTRO)
                tools_table = gr.Dataframe(
                    headers=TOOL_COLUMNS,
                    datatype=["str", "str", "str"],
                    interactive=False,
                    wrap=True,
                    max_height=520,
                    show_label=False,
                )

        gr.Markdown(FOOTER, elem_classes=["db-k"])

    pool_inputs = [pool_query, pool_domain, pool_difficulty]
    pool_outputs = [pool_pick, pools_summary]
    for trigger in (pool_search.click, pool_query.submit, pool_domain.change, pool_difficulty.change):
        trigger(render_pools, pool_inputs, pool_outputs, api_visibility="private")
    pool_pick.input(show_pool, pool_pick, [pool_detail, pool_go, browse_col, pool_col], api_visibility="private")
    pool_back.click(
        lambda: (gr.update(value=None), gr.update(visible=True), gr.update(visible=False)),
        None,
        [pool_pick, browse_col, pool_col],
        api_visibility="private",
    )
    contribute.wire()
    ui = contribute.ui
    pool_go.click(lambda: gr.Tabs(selected="contribute"), None, tabs, api_visibility="private").then(
        contribute.open_pool, [pool_pick, ui.session, ui.pools_state], ui.pool_picker, api_visibility="private"
    ).then(contribute.load_contract, [ui.pool_picker, ui.session], contribute.contract_outputs(), api_visibility="private")

    board_inputs = [board_tier, board_query]
    for trigger in (board_tier.change, board_query.submit):
        trigger(render_leaderboard, board_inputs, board_table, api_visibility="private")

    for trigger in (profile_go.click, profile_handle.submit):
        trigger(render_profile, profile_handle, profile_card, api_visibility="private")

    demo.load(render_overview, None, overview, api_visibility="private")
    demo.load(render_pools, pool_inputs, pool_outputs, api_visibility="private")
    demo.load(render_datasets, None, [datasets_table, datasets_snippet], api_visibility="private")
    demo.load(render_leaderboard, board_inputs, board_table, api_visibility="private")
    demo.load(render_tools, None, tools_table, api_visibility="private")
    demo.load(contribute.claim_view, ui.nonce, ui.session, js=READ_NONCE_JS, api_visibility="private").then(
        contribute.account_view, ui.session, contribute.account_outputs(), api_visibility="private"
    ).then(None, js=FORGET_NONCE_JS)
    demo.load(contribute.select_tab, None, tabs, api_visibility="private")

    gr.api(get_platform_stats, api_name="get_platform_stats")
    gr.api(search_open_pools, api_name="search_open_pools")
    gr.api(get_pool_details, api_name="get_pool_details")
    gr.api(list_published_datasets, api_name="list_published_datasets")
    gr.api(get_karma_leaderboard, api_name="get_karma_leaderboard")
    gr.api(get_contributor_profile, api_name="get_contributor_profile")

# Every handler is network-bound and mostly answered from cache, so let several
# visitors through at once instead of Gradio's default of one per event.
demo.queue(default_concurrency_limit=8)

# --- Sign-in routes ------------------------------------------------------------------------------
# Plain routes beside the Gradio app: DataBounty fetches the client document and
# redirects the browser to the callback, and neither may go through Gradio.

server = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def _sign_in_page(message: str, status: int, *, popup: bool = False, title: str = "Sign-in didn't finish", close: bool = False) -> HTMLResponse:
    back = (
        '<p><button onclick="window.close()" style="font:inherit;padding:.5rem 1rem;border-radius:8px;border:1px solid #ccc;'
        'background:#b6ff1c;font-weight:600;cursor:pointer">Close this window</button></p>'
        if popup
        else '<p><a href="/?tab=contribute">Back to the Space</a></p>'
    )
    body = (
        '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_e(title)}</title>"
        '<body style="font-family:system-ui,sans-serif;max-width:34rem;margin:15vh auto;padding:0 1rem;line-height:1.5">'
        f'<h1 style="font-size:1.3rem">{_e(title)}</h1><p>{_e(message)}</p>{back}'
        + ("<script>setTimeout(() => window.close(), 800)</script>" if close else "")
        + "</body>"
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
        return _sign_in_page("Start signing in from the Space's Contribute tab.", 400, popup=in_window)
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
            raise account.SignInError("Start signing in from the Space's Contribute tab.")
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
            response = RedirectResponse("/?tab=contribute", status_code=302, headers=NO_STORE)
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
    theme=THEME,
    css=CSS,
    favicon_path=str(HERE / "assets" / "logo.svg"),
    max_file_size="100mb",
    head=SIGN_IN_JS,
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
