---
title: DataBounty
emoji: 🧭
colorFrom: green
colorTo: blue
sdk: gradio
sdk_version: 6.30.0
python_version: "3.12"
app_file: app.py
short_description: Browse open pools, contribute items, karma and MCP tools
license: apache-2.0
pinned: true
tags:
  - datasets
  - data-collection
  - crowdsourcing
  - mcp-server
datasets:
  - databounty-io/graphql-schema-resolver-execution-validation-cmskdigf
  - databounty-io/build-competitive-programming-problem-dataset-cmsokff2
  - databounty-io/regex-pattern-generation-with-verified-match-sets-cmskdvm7
  - databounty-io/extract-metrics-from-log-fixtures-cmskdlip
  - databounty-io/python-execution-trace-output-prediction-cmskdimp
  - databounty-io/python-data-transformation-scripts-cmskdijz
---

# DataBounty

A live window onto [DataBounty](https://databounty.io), the open-source, karma-based platform where the community
builds datasets, and a place to contribute to it. Sponsors request a dataset, contributors submit items, automated checks validate each
submission and human validators review the results. Finished datasets are published here on the Hub under
[databounty-io](https://huggingface.co/databounty-io), with contributor credit.

## What's inside

- **Overview**: live programme totals and the karma tier ladder.
- **Contribute**: sign in to DataBounty and add items to an open pool, then follow them through validation. Add
  items one at a time from a form built from the pool's fields, or upload a JSONL, JSON, CSV or TSV file and review
  it row by row first. Rows that would be refused are flagged before anything is sent.
- **Open pools**: every dataset currently accepting contributions, with what each item needs and the karma it earns.
- **Published datasets**: DataBounty releases on the Hub, with download counts.
- **Leaderboard** and **Contributor profile**: public karma standings. Private profiles, and any field a member hides, are never shown.
- **For agents (MCP)**: connect Claude, Cursor, VS Code or ChatGPT to DataBounty's own MCP server.

## Contributing from the Space

Press **Sign in to contribute** and sign in with your DataBounty account, then approve the Space. On the Space's
own page, sign-in happens in the same tab. On the huggingface.co page, a small sign-in window opens and closes itself
when you're done, and the page signs you in where you are. The Space asks for four permissions: read pools and your
own items, contribute items, upload files for file fields, and set up your account on first sign-in. You can untick
any of them, but the Space can't contribute without *contribute*.

Then pick a pool, and add one item with the form or many at once from a file (there's a template to start from).
Each pool shows an example item when it has one, and its full requirements one click away. **My submissions**
shows where each item stands, check by check.

- DataBounty takes items only from accounts with a verified email and a public username. The Space walks you through
  both on first sign-in. Choosing a username makes your profile public at `databounty.io/<username>` straight away.
- Your DataBounty tokens stay in the Space's memory and are never written to disk or sent to your browser, which
  keeps only an encrypted session handle. Signing out revokes the tokens. A restart of the Space signs everyone out.
- Revisions, disputes and validating other people's items happen on [databounty.io](https://databounty.io).

## Use it from an agent

This Space is also a read-only MCP server that needs no sign-in. Contributing is not available over it:

```text
https://databounty-io-databounty.hf.space/gradio_api/mcp/
```

The Hub prefixes each tool name with the Space name, so the tools are `DataBounty_get_platform_stats`,
`DataBounty_search_open_pools`, `DataBounty_get_pool_details`, `DataBounty_list_published_datasets`,
`DataBounty_get_karma_leaderboard` and `DataBounty_get_contributor_profile`.

To contribute or validate through an agent, use DataBounty's own MCP server at `https://console.databounty.io/mcp`.
It signs in with OAuth; setup for each client is on [databounty.io/agents](https://databounty.io/agents/).

## Data

Browsing reads DataBounty's public API (`api.databounty.io`) and the Hub, caching responses for about two minutes,
and stores nothing about visitors. When you sign in, the Space acts for you through DataBounty's MCP server, and only
when you press a button. Files you upload are deleted from the Space within an hour.

## Source

Apache-2.0. The code lives in [Databounty-io/databounty](https://github.com/Databounty-io/databounty) under
`deploy/huggingface/databounty`.
