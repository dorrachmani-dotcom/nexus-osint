<div align="center">

# Nexus-OSINT

**A local-first OSINT workstation: collect open sources, enrich them with AI, and triage everything in one private, auditable feed on your own machine.**

[![CI](https://github.com/dorrachmani-dotcom/nexus-osint/actions/workflows/ci.yml/badge.svg)](https://github.com/dorrachmani-dotcom/nexus-osint/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.11 | 3.12](https://img.shields.io/badge/python-3.11%20%7C%203.12-3776AB?logo=python&logoColor=white)

[Features](docs/FEATURES.md) · [Overview](docs/OVERVIEW.md) · [User Guide](docs/USER_GUIDE.md) · [Developer Guide](docs/DEVELOPER_GUIDE.md) · [Roadmap](docs/ROADMAP.md) · [Security](SECURITY.md)

</div>

<!-- TODO: record a 20-30 s demo (scan -> feed -> case -> report) and save it as docs/media/demo.gif -->
![Nexus-OSINT demo](docs/media/demo.gif)

## The problem

Open-source material is scattered across news feeds, social platforms, search
engines and one-off CLI tools. An analyst ends up with forty tabs, no memory of
what was already seen, and no record of where a claim came from. Hosted
platforms solve the aggregation but want your queries and your findings on their
servers, which is a non-starter for a lot of investigative work.

Nexus-OSINT is the opposite trade: everything runs on `127.0.0.1`, the data is
one SQLite file you own, and every outbound connection can be audited.

## What it does

- **Collects** from RSS, Google News, Reddit and GDELT with no API keys, plus
  optional Twitter/X, Telegram, SERPAPI and Google CSE, any user-defined JSON API,
  and wrapped OSINT CLI tools.
- **Deduplicates and searches**: exact re-fetches are no-ops, near-duplicates
  collapse into one row with an "echoed N times" badge, and the whole history is
  searchable through SQLite FTS5.
- **Enriches** items with a pluggable AI provider (Gemini, OpenAI, Anthropic, or a
  fully local Ollama model): summary, translation, threat level, entity
  extraction, and 0-100 relevance scores against your standing questions.
- **Organizes** work into cases with tracking terms, pinned items, notes,
  sub-cases, a daily brief, watchlists and a "Focus" view of the few items that
  most need attention.
- **Preserves and exports**: hashed, timestamped page screenshots (Evidence
  Vault), PDF/HTML/CSV/JSON/Obsidian exports, and an air-gap bundle format for
  moving data between machines.
- **Audits itself**: an egress monitor records every outbound connection and a
  file scanner vets anything you import.

The full catalogue is in [docs/FEATURES.md](docs/FEATURES.md).

## Architecture

![Nexus-OSINT architecture: sources, collect, store (SQLite), analyze (AI), investigate, all on 127.0.0.1](docs/architecture.svg)

A scan runs collect -> deduplicate -> watchlist match -> AI analysis -> requirement
scoring. All SQL lives in `nexus/storage.py`; the UI is server-rendered Jinja2 +
htmx with no JavaScript build step. See [docs/OVERVIEW.md](docs/OVERVIEW.md) for
the end-to-end walk-through and [docs/DEVELOPER_GUIDE.md](docs/DEVELOPER_GUIDE.md)
for the module map and extension points.

## Engineering decisions and trade-offs

- **SQLite (WAL) + FTS5 instead of a database server.** One user, one machine,
  read-heavy workload: a single file gives atomic writes, trigger-maintained
  full-text search, trivial backup and zero install friction. The cost is a
  single writer and no multi-user access; collaboration is explicitly out of
  scope for now (see the roadmap).
- **Trigger-based collection instead of a 24/7 daemon.** Scans run on demand, from
  an optional timer, or as a quiet gap-fill at startup. Nothing polls in the
  background, which keeps the resource footprint and the outbound-traffic surface
  small and predictable. The cost is that data is only as fresh as the last scan.
- **Graceful degradation as a rule.** Every source, tool and AI provider is
  optional and reports `is_available()`. A missing key or binary disables exactly
  one feature; with no AI configured, items are still collected, stored and
  searchable. A per-run budget cap and content-hash caching bound AI cost, and
  quota errors pause a scan instead of failing it.
- **AI agent safety by allow-list.** The in-app Nexus Assistant can act on your
  data, but only through a fixed set of constructive local tools
  (`_ALLOWED_TOOLS` in `nexus/assistant.py`), with a hard cap on actions per turn.
  Anything else the model asks for is dropped and logged. It has no delete,
  settings, key or network tools. Model output and collected text are rendered as
  text, never HTML, because collected content is untrusted input and a prompt
  injection must not be able to become script execution.
- **A real entity index.** Entities extracted by the AI are stored as JSON on the
  analysis row *and* normalized into an `item_entities` table (indexed by
  canonical name, backfilled on upgrade). This turns "everything mentioning X"
  from a full scan into an index lookup and backs the entity dossier, the
  relationship graph and cross-linking.
- **Safe script embedding.** Where data must be embedded in an inline `<script>`,
  it goes through Jinja's `|tojson`, which escapes `<`, `>` and `&`. An earlier
  `json.dumps | safe` path allowed a `</script>` in a collected title to break
  out; it was fixed and is the pattern to follow.

## Quickstart

**From source** (Python 3.11+):

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium-headless-shell           # optional, for the Evidence Vault
cp .env.example .env                                 # Windows: copy .env.example .env
uvicorn nexus.web.app:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. Missing CLI tools and API keys are skipped, not fatal.

**Docker** (bundles the OSINT CLI tools and headless Chromium):

```bash
cp .env.example .env
docker compose up --build
```

**Windows installer**: download the latest installer from the repository's
**Releases** page and run it; it installs a desktop shortcut that opens the app in
its own window and stops the local server when closed. Linux has a script under
`launcher/linux/`.

On first run an onboarding wizard walks through adding topics and (optionally) an
AI provider.

## Security model

- The server binds to `127.0.0.1` only. Requests get CSRF/DNS-rebinding checks and
  security headers; user-supplied URLs pass an SSRF guard before being fetched.
- **Collected data stays on your machine unless you configure a cloud AI
  provider.** In that case the text of items being analyzed is sent to that
  provider. Choose the local Ollama backend, or no AI, to keep analysis fully
  offline. Collection itself necessarily contacts the sources you enable.
- Reports and transfer bundles leave the machine only when you export them, and
  bundles never contain secrets.
- Secrets live in `.env` only (git-ignored). They are never written to the
  database, shown back in the UI, or emitted in logs (a redaction filter scrubs
  configured keys). The browser loads the Tailwind CDN script for styling; that is
  a request to a CDN, not an upload of your data.
- The Security center lists every outbound destination the app has contacted and
  flags any that is not a configured source or AI provider.

Reporting vulnerabilities and the full policy: [SECURITY.md](SECURITY.md).

## Testing and CI

```bash
pip install -r requirements.txt
pytest -q
```

The suite (roughly 370 tests) uses temporary SQLite databases and avoids the
network; it covers storage and dedup, source parsing, the assistant allow-list,
web hardening, export/transfer round-trips and route smoke tests. GitHub Actions
runs it on every push to `main` and on pull requests (`.github/workflows/ci.yml`).
Contribution guidelines are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Roadmap

See [docs/ROADMAP.md](docs/ROADMAP.md): contextual one-click tool pivots, triage
tuning, richer entity resolution, and lint/type-check gates in CI.

## About the author

Built by <!-- AUTHOR_NAME -->.
[LinkedIn](<!-- LINKEDIN_URL -->) · [Contact](<!-- CONTACT_URL -->)

Available for consulting on OSINT tooling and AI-agent systems (local-first
architectures, agent safety, LLM-backed data pipelines).

## License

[MIT](LICENSE)
