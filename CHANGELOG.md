# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres
to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.0.0] - 2026-10-10

First tagged release.

### Added
- **Connect Gmail** for the daily brief: OAuth 2.0 with PKCE and the
  `gmail.send` scope only (no password, cannot read mail), using the
  operator's own Desktop OAuth client; refresh token kept in `.env`;
  Disconnect revokes it at Google.
- **Internet Archive (Wayback Machine) preservation**: archive an item's source
  from the item drawer (Save Page Now, anonymous or authenticated SPN2 with
  optional `ARCHIVE_ORG_ACCESS_KEY` / `ARCHIVE_ORG_SECRET_KEY`), or find its
  newest existing snapshot. Captures run on a rate-limited background queue
  with htmx status polling; a case can **Archive all pinned items** and opt in
  to **auto-archive on pin**. New `item_archives` table (one row per item, the
  latest attempt) and a `cases.auto_archive` column, both added by migration.
  Archive URL and capture time are included in the evidence manifest, case
  reports and CSV/JSON case exports. A one-time OpSec warning (captures are
  public), a global on/off in Settings, every URL through the SSRF guard, and
  archive.org added to the egress allow-list.
- **Fully offline UI**: the Tailwind stylesheet is prebuilt and bundled, htmx
  and vis-network are vendored, and the CSP allows only the app's own origin.
  A CI job fails if the committed assets drift from their sources.
- `CONTRIBUTING.md`, issue templates and a pull request template.
- AI provider **xAI (Grok)** via its OpenAI-compatible API (`XAI_API_KEY`,
  `GROK_MODEL`, default `grok-4.6`).
- AI provider **Local server** (`local_openai`) for any local OpenAI-compatible
  server (LM Studio, llama.cpp, vLLM, Jan, LocalAI), with a Settings box to set
  the address and model and a **Test connection** button that lists the
  server's models. Both new providers share one httpx-based
  `OpenAICompatibleProvider` (no new dependency).
- **Automatic** AI provider: uses the first ready backend in the order Gemini →
  OpenAI → Anthropic → Grok → local server (once a model is set). Ollama is used
  only when chosen explicitly.
- **Daily email brief written by Sherlock** (opt-in): a "Connect your email"
  wizard in Settings (Gmail app password, Outlook / Microsoft 365, Resend,
  SendGrid or custom SMTP), a required test email before the brief can be
  switched on, a daily time, an optional scan-first, and "Send a test brief
  now". Falls back to a plain brief when no AI is connected.
- **Daily case reports**: a new **Reports** tab on each case to schedule a daily
  PDF/HTML report of newly collected items, generate one now, and open /
  download / delete saved reports; the Daily brief page shows each case's latest
  report, and the email can link or attach it.
- Egress allow-list entries for xAI, the configured local server, the configured
  SMTP host and the Resend / SendGrid APIs.
- **Collection**: RSS, keyless Google News and Reddit search, GDELT; optional
  Twitter/X, Telegram, SERPAPI and Google Custom Search; user-defined JSON API
  sources with an AI source planner and Auto-Adapt self-healing; wrappers for
  installed OSINT CLI tools. Trigger-based scans (manual, optional timer, boot
  gap-fill) with per-source watermarks.
- **Storage**: single SQLite database (WAL) with an FTS5 index maintained by
  triggers, near-duplicate clustering, and an `item_entities` index for fast
  entity lookups.
- **AI enrichment**: pluggable providers (Gemini, OpenAI, Anthropic, local Ollama)
  behind one interface, with a local prefilter, content-hash caching, a per-run
  budget cap, summaries, translation, threat levels, entity extraction,
  contradiction flags and relevance scoring against Intelligence Requirements.
- **Analyst workspace**: unified feed, Intel ranking, Focus (needs-attention) view,
  cases with tracking terms, questions, pins, notes and sub-cases, daily brief,
  watchlists (keyword, regex, wallet, phone), entity dossier and relationship
  graph.
- **Evidence and export**: hashed screenshot Evidence Vault with OCR, PDF/HTML,
  CSV/JSON and Obsidian-vault export, and an idempotent air-gap transfer bundle.
- **Sherlock**: in-app chat that answers from collected data and performs
  constructive local actions through a fixed allow-list.
- **Security center**: egress monitor, file scanner, optional hash-only
  VirusTotal lookup, CSRF/DNS-rebinding protection, security headers, SSRF guard,
  secret-redacting log filter.
- **Packaging and quality**: Docker image, Windows installer (PyInstaller + Inno
  Setup), per-OS launcher scripts, pytest suite and GitHub Actions CI.

### Changed
- Code layout: `nexus/web/app.py` (about 4,800 lines) split into per-domain
  FastAPI routers under `nexus/web/routers/` (app.py is now about 250 lines),
  and `nexus/storage.py` turned into the `nexus/storage/` package with
  re-exports. No behaviour change; a test pins the full route table and the
  matching order of overlapping routes.
- The default `AI_PROVIDER` is now `auto` (was `anthropic`). Explicit choices keep
  working; unknown values fall back to `auto`.
- Documentation reorganized: the feature catalogue moved to `docs/FEATURES.md`,
  the project overview to `docs/OVERVIEW.md`, and the README rewritten around
  architecture and engineering trade-offs.
- **Code-quality gates**: `pyproject.toml` now holds project metadata and the
  ruff, mypy and pytest configuration. CI gains `lint` (ruff) and `types` (mypy)
  jobs, a `.pre-commit-config.yaml` provides local hooks, and Dependabot checks
  pip, npm and GitHub Actions weekly (Tailwind major versions are ignored: v4 is
  a different toolchain). The codebase was brought to zero ruff and mypy
  findings with no behaviour change: import order, `datetime.UTC`, silent
  `except: pass` blocks now log at debug level, a mutable default argument, an
  unresolved `markupsafe` annotation and an unreferenced background task.

### Fixed
- The automatic-scan background thread never ran: `@asynccontextmanager` was
  applied to the scan loop instead of the app lifespan. The scheduler (scans,
  daily reports and the email brief) now runs as intended.
- Dark-theme text contrast raised to meet WCAG AA; dialog semantics and focus
  return added to the item drawer.

### Removed
- `docs/INVESTIGATOR_PLAN.md`; its remaining items moved to `docs/ROADMAP.md`.

### Security
- Inline script embedding of collected data uses Jinja `|tojson` to prevent
  `</script>` breakout.

[Unreleased]: https://github.com/dorrachmani-dotcom/nexus-osint/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/dorrachmani-dotcom/nexus-osint/releases/tag/v1.0.0
