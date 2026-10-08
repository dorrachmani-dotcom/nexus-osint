# Nexus-OSINT — Developer & Contributor Guide

This guide is for engineers who want to **understand, modify, or extend** the
codebase: add a collection source, wrap a new OSINT CLI tool, plug in another AI
provider, extend the assistant, or build the desktop installer. It assumes you can
read Python and have skimmed the [README](../README.md) and
[User Guide](USER_GUIDE.md).

---

## Table of contents

1. [Design principles](#1-design-principles)
2. [Tech stack](#2-tech-stack)
3. [Repository layout](#3-repository-layout)
4. [Development environment](#4-development-environment)
5. [Architecture & data flow](#5-architecture--data-flow)
6. [Data model](#6-data-model)
7. [The web layer (FastAPI + htmx)](#7-the-web-layer-fastapi--htmx)
8. [Extension points](#8-extension-points)
9. [Configuration & secrets](#9-configuration--secrets)
10. [Testing](#10-testing)
11. [Building the desktop app](#11-building-the-desktop-app)
12. [Coding conventions](#12-coding-conventions)
13. [Security checklist for contributors](#13-security-checklist-for-contributors)

---

## 1. Design principles

These are load-bearing — keep them in mind for every change:

- **Local-first / OpSec.** The server binds to `127.0.0.1` only. Secrets live in
  `.env` and must **never** reach the database, the UI, the logs, or any shipped
  artifact. The egress monitor exists so the app's network behaviour stays
  auditable; don't add silent outbound calls.
- **Graceful degradation.** A missing key, model, binary or network must disable
  exactly one feature, never crash the app. Sources report `is_available()`;
  optional imports are guarded; the AI layer is entirely optional.
- **Trigger-based execution.** No 24/7 scraping. Collection runs on an explicit
  scan, an auto-scan timer, or a quiet boot sync.
- **Provider-agnostic AI.** All model access goes through one interface so cloud
  and local backends are interchangeable.
- **No build step for the frontend.** Jinja2 + htmx + Tailwind (CDN). Avoid adding
  a JS toolchain.

---

## 2. Tech stack

| Layer | Choice |
| --- | --- |
| Language | Python 3.11+ |
| Web framework | FastAPI + uvicorn |
| Templates | Jinja2 |
| Interactivity | htmx 1.9 (+ small vanilla JS); Tailwind via CDN |
| Storage | SQLite (WAL mode) with an FTS5 full-text index kept in sync by triggers |
| AI | Pluggable: Anthropic Claude, Google Gemini, OpenAI, or local Ollama |
| Screenshots | Playwright (headless Chromium) |
| Reporting | xhtml2pdf / reportlab (PDF), Jinja (HTML) |
| Graph | networkx + pyvis |
| Packaging | PyInstaller (one-folder) + Inno Setup (Windows installer); Docker |

---

## 3. Repository layout

```
nexus/
├── config.py            # Settings dataclass: .env parsing + capability flags
├── envstore.py          # safe .env read/write (EDITABLE_KEYS allow-list)
├── db.py                # SQLite schema, WAL, FTS5 triggers, migrations
├── models.py            # RawItem / Analysis / ProcessedItem dataclasses
├── storage.py           # ALL DB read/write helpers (dedup, search, workspace)
├── collector.py         # scan orchestration: sources -> store -> analyze
├── casesetup.py         # auto-configure a new case's terms + questions
├── assistant.py         # Nexus Assistant: grounded chat + constructive action tools
├── lang.py / textclean.py / translate.py / keyless… # language + translation
├── evidence.py          # Playwright screenshot + SHA-256 hashing
├── ocr.py               # Tesseract OCR over evidence screenshots
├── graph.py             # entity co-occurrence graph (networkx + pyvis)
├── obsidian.py          # case -> Obsidian markdown vault
├── reporting.py         # feed/case -> PDF/HTML/CSV/JSON
├── transfer.py          # .nexusbundle export/import (air-gap)
├── netguard.py          # SSRF guard for outbound URLs
├── logging_safe.py      # log-redaction filter for secrets
├── security.py          # egress monitor + file scanner (imported by web)
├── toolguide.py         # catalogue/brief of optional OSINT CLI tools
├── sources/             # collection sources (see §8.1)
│   ├── base.py          #   Source ABC + fetch_feed() helper
│   ├── rss.py, freesearch.py (Google News + Reddit search), gdelt.py,
│   │   google_cse.py, serpapi.py, reddit.py, twitter.py, telegram.py
│   ├── custom.py        #   generic config-driven JSON API source
│   └── auto_adapt.py    #   AI re-maps a drifting custom source
├── adapters/            # Plug & Play CLI tool wrappers (see §8.2)
│   ├── base.py          #   ToolAdapter ABC
│   ├── registry.py      #   the list the web layer asks for
│   └── sherlock.py, maigret.py, holehe.py, … (one per tool)
├── analysis/            # the AI core
│   ├── providers.py     #   LLMProvider ABC + get_provider() factory (see §8.3)
│   ├── prefilter.py     #   cheap local noise filter before any token spend
│   ├── prompts.py       #   prompt templates
│   ├── claude_core.py   #   analysis orchestration
│   ├── requirements.py  #   PIR scoring
│   ├── query_builder.py #   subject -> search terms + questions
│   ├── source_planner.py#   AI auto-config for custom API sources
│   └── ollama_admin.py  #   local model pull/status
└── web/
    ├── app.py           # FastAPI app: every route + lifespan + helpers
    ├── security.py      # CSRF/host hardening + security headers
    └── templates/       # Jinja templates (the whole UI)

installer/               # PyInstaller spec, Inno Setup script, run_nexus.py entry
launcher/                # one-click desktop installers (per OS)
tests/                   # pytest suite (see §10)
docs/                    # overview, features, user guide, this guide, roadmap
```

---

## 4. Development environment

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium-headless-shell           # optional (evidence)
cp .env.example .env                                 # Windows: copy .env.example .env
uvicorn nexus.web.app:app --host 127.0.0.1 --port 8000 --reload
```

- `--reload` picks up Python changes automatically. Template changes are loaded
  per request (no restart needed).
- A dev run stores data under the project's `data/` folder; the packaged app uses
  `%LOCALAPPDATA%\Nexus` (Windows). Both are git-ignored.
- Never commit `.env`, `data/`, or build artifacts — they are git-ignored; keep it
  that way.

---

## 5. Architecture & data flow

A scan is the heart of the system (`Collector.scan()` in `collector.py`):

1. **Collect** — every `Source.is_available()` source is fetched. Per-source
   failures are isolated and logged; one bad feed never aborts the scan.
2. **Store & deduplicate** — `storage.upsert_item()` is the single write path. An
   exact re-fetch is a no-op; near-duplicate content bumps a cluster's
   `shared_count` ("Echoed N times") instead of creating a row.
3. **Watchlists** — new items are matched against user patterns, recording hits.
4. **Analyze** — un-analyzed items pass the local `prefilter`, then the active AI
   provider (bounded by `ANALYSIS_MAX_ITEMS_PER_RUN`). Results are cached by
   content, so re-scans never re-pay.
5. **Score requirements** — each item is scored 0–100 against each enabled PIR;
   scores are cached per `(item, requirement)`.

The web layer reads the resulting rows through `storage.py` helpers and renders
them. `enrich_feed_rows()` is the batched enrichment pass that adds derived
presentation fields (typed entities, related sources, read/bookmark flags, source
reliability, relative time, best requirement match) without N+1 queries.

---

## 6. Data model

SQLite, WAL mode, schema in `db.py`. Key tables:

- `items` — the canonical collected row (source, url, title, content, language,
  timestamps, `dedup_key`, `cluster_id`, `dismissed`, mirrored `summary` for FTS).
- `items_fts` — FTS5 virtual table kept in sync by triggers; powers search.
- `clusters` — near-duplicate grouping with `shared_count`.
- `analyses` — one row per analysed item (threat_level, summary, translation,
  entities JSON, party, contradiction, confidence, model). **Note:** `reliability`
  is *computed at render time* (`storage._classify_reliability`), not a column.
- `requirements` / `requirement_hits` — PIRs and their per-item scores.
- `cases` (with `parent_id` for sub-cases, `status`, `priority`), `case_terms`
  (tracking words, `kind` = required/alert), `bookmarks` (pins; `case_id` nullable
  for general bookmarks), `notes`.
- `lists` / `list_memberships` — triage lists.
- `watchlists` / `watchlist_hits`.
- `subscriptions` — user-chosen collection targets (Topics).
- `entity_aliases`, `evidence`, `meta` (key/value runtime settings).

All reads/writes go through `storage.py` — don't scatter SQL across the codebase.
When you change the schema, add an idempotent migration in `db.py` (the app must
upgrade an existing database in place).

---

## 7. The web layer (FastAPI + htmx)

`nexus/web/app.py` holds every route plus shared helpers (`_paged_feed`,
`_render_card`, `_feed_partial_response`, filters). Patterns to follow:

- **Partial swaps.** Most mutations return a small HTML fragment that htmx swaps
  in. A single feed card re-renders via `_render_card(request, conn, item_id,
  case_id)`; the `case_id` is the *viewing-case context* so the refreshed card
  keeps `current_case` (and its "Pin to this case" button). Forward it from the
  template with `hx-vals='{"case_id": "{{ current_case.id }}"}'`.
- **`hx-target` + `hx-swap="outerHTML"`** when replacing a whole card
  (`#item-<id>`); **`hx-swap="none"` + `hx-on::after-request`** for compact widgets
  (drawer, intel feed) that update themselves client-side.
- **Untrusted content is escaped.** Item URLs are source-derived: render links
  through the `safe_url` Jinja filter (only `http(s)`/`mailto`), and render model
  output as text, never raw HTML.
- **Never crash the user.** A global exception handler turns any unhandled error
  into a calm branded page; routes still handle their own expected failures.

---

## 8. Extension points

### 8.1 Add a collection source

1. Create `nexus/sources/yoursource.py` subclassing `Source`
   (`nexus/sources/base.py`):

   ```python
   from datetime import datetime
   from nexus.sources.base import Source
   from nexus.models import RawItem

   class YourSource(Source):
       name = "yoursource"  # stable id stored on every item

       def __init__(self, settings):
           self.settings = settings

       def is_available(self) -> bool:
           return bool(self.settings.your_key)  # never raise on a missing key

       def fetch(self, since: datetime | None = None) -> list[RawItem]:
           # swallow per-item errors; return what you could collect
           return [...]
   ```

2. Register it in `Collector.__init__` (`nexus/collector.py`) by adding
   `YourSource(self.settings)` to `self.sources`.
3. Add any config knobs to `config.py` / `.env.example`. If it needs a key, add the
   key name to `EDITABLE_KEYS` in `envstore.py` so it's settable from Settings.

The collector handles availability, watermarks, dedup and analysis for you.

> No code needed for one-off APIs: end users can add any JSON API on the **Sources**
> page (`nexus/sources/custom.py`), with AI auto-mapping (`source_planner.py`) and
> Auto-Adapt (`auto_adapt.py`).

### 8.2 Wrap an OSINT CLI tool (Plug & Play adapter)

1. Create `nexus/adapters/yourtool.py` subclassing `ToolAdapter`
   (`nexus/adapters/base.py`): set `name`/`binary`, implement `is_available()`
   (detect the binary) and `run(target)` (shell out, parse, return findings).
2. Add the class to `_ADAPTER_CLASSES` in `nexus/adapters/registry.py`.
3. Add an entry to `nexus/toolguide.py` so its install hint and Nexus Assistant guidance
   show up.

Adapters must degrade gracefully (tool not installed → listed as unavailable, never
an error) and only ever **read** — they are lookups, not mutations.

### 8.3 Add an AI provider

1. Subclass `LLMProvider` in `nexus/analysis/providers.py`, implementing the chat/
   complete methods used by the analysis layer and the assistant.
2. Wire it into the `get_provider(settings)` factory (the `if provider == "…"`
   chain) and add the choice to `Settings.PROVIDER_CHOICES`.
3. Add any key to `EDITABLE_KEYS`. The whole app is provider-agnostic, so nothing
   else needs to change.

### 8.4 Extend Nexus Assistant

Nexus Assistant's constructive actions live in `nexus/assistant.py`:

1. Add a `_do_youraction(...)` handler that performs a **local, constructive,
   reversible** operation and returns a result dict (`type`, `label`, optional
   `url`).
2. Document the action in `_ACTIONS_GUIDE`, add its name to the `_ALLOWED_TOOLS`
   allow-list, and dispatch it in the `act()` tool loop.

**Hard limits:** actions must never delete, change settings/keys/provider, or send
data off the machine, and Nexus Assistant must never learn or reveal who built the
software. The `_ALLOWED_TOOLS` allow-list is the security boundary — anything not
on it is refused even if the model emits it (prompt-injection defense).

---

## 9. Configuration & secrets

- `config.py` exposes a `Settings` object (parsed from `.env`) with capability
  flags like `analysis_enabled` and an `availability_report()`.
- `envstore.py` is the **only** sanctioned way to write `.env`. It enforces an
  `EDITABLE_KEYS` allow-list so the Settings page can never write an arbitrary key,
  and reloads settings live (no restart).
- Secrets are read from `.env` only. They must never be written to the DB, returned
  in a response, or logged — `logging_safe.install_log_redaction()` scrubs known
  key values from every log record as defense-in-depth.

---

## 10. Testing

The suite is `pytest` under `tests/` (see `tests/conftest.py` for fixtures). It
covers sources, the assistant and its action tools, the case hub, security guards
(egress, file scan, SSRF/netguard), transfer, reporting/export, language handling,
and the web routes.

```bash
pytest -q                     # whole suite
pytest tests/test_assistant.py -q
```

Guidelines:
- Add a test alongside any new source/adapter/provider/route.
- Tests must not hit the network or require API keys — mock providers and feeds.
- Use a temporary SQLite database (see existing fixtures) rather than the real one.

---

## 11. Building the desktop app

The Windows desktop build is a PyInstaller **one-folder** app, optionally wrapped
by Inno Setup into a single installer.

```bash
# 1) Freeze the app (templates ship as data; Python is bundled).
#    NEXUS_BUNDLE_CHROMIUM=1 embeds a headless Chromium for offline evidence.
NEXUS_BUNDLE_CHROMIUM=1 pyinstaller installer/nexus.spec --noconfirm \
    --distpath installer/dist --workpath installer/build
# -> installer/dist/Nexus/Nexus.exe  (+ _internal/)

# 2) Wrap it into a single installer (needs Inno Setup's ISCC).
ISCC installer/nexus.iss
# -> installer/Output/Nexus-Setup.exe
```

Notes:
- `installer/run_nexus.py` is the frozen entry point: it runs uvicorn in-process
  (with `log_config=None`, important under a windowed/no-console process) and opens
  a chromeless app window. `NEXUS_NO_WINDOW=1` runs it headless (smoke tests).
- The spec ships `nexus/web/templates` and `.env.example` as data, and bundles
  reportlab/xhtml2pdf data for PDF export. It deliberately **does not** bundle
  `.env` or `data/` — verify this stays true (no secrets or user data in a build).
- `NEXUS_DEBUG_CONSOLE=1` keeps a console window for tracebacks while testing.
- A Linux spec (`installer/nexus_linux.spec`) and a Docker image are also provided.

---

## 12. Coding conventions

- **Match the surrounding code** — comment density, naming, and idiom. The codebase
  favours short, purposeful comments explaining *why*, not *what*.
- **Type hints** on public functions; `from __future__ import annotations` at the
  top of modules.
- **All SQL in `storage.py`** (or the relevant data module), never inline in
  routes. Keep `search_items` and `count_matching_items` filter logic in sync
  (`_item_filter_clauses` is the shared source of truth).
- **English only** in all shipped strings, comments, templates and docs.
- **Fail soft.** Wrap optional/external work in try/except and degrade; log with
  context (`logger.exception(...)`) but never leak secrets.

---

## 13. Security checklist for contributors

Before opening a change, confirm:

- [ ] No secret is read from or written to anywhere but `.env` (via `envstore`).
- [ ] No new outbound network destination beyond AI providers / configured
      sources / localhost (the egress monitor will flag it — that's intended).
- [ ] Any URL fetched from user/collected input passes the `netguard` SSRF guard.
- [ ] Untrusted text is escaped; links use the `safe_url` filter; model output is
      rendered as text.
- [ ] New Nexus Assistant tools are constructive, local, reversible, and on the
      `_ALLOWED_TOOLS` allow-list.
- [ ] The server still binds to `127.0.0.1` only.
- [ ] No personal or identifying information is added to code, docs, or commits.
- [ ] `.env`, `data/`, and build artifacts remain git-ignored.
