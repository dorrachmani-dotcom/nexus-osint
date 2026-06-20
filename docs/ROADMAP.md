# Nexus-OSINT — Roadmap

> Status: a project in progress, not a finished product. This document tracks
> what is built, the principles that constrain how it grows, and the planned
> direction. It is intentionally high-level; implementation detail lives in the
> code and in `README.md`.

## Vision

A 100% local, open-source OSINT intelligence platform for researchers,
analysts, and journalists. The whole pipeline runs on the operator's own
machine:

```
collect open sources → store locally (SQLite) → AI analysis → unified English feed
```

The unified feed always reads in English regardless of source language, with an
analyst workspace on top (triage, cases, evidence, reporting).

## Operating principles (non-negotiable)

These constrain every feature and every contribution.

- **Local-only.** The server binds to `127.0.0.1` only. Nothing is exposed to a
  network by default. Data lives in a local SQLite database and the `data/`
  directory.
- **Graceful degradation.** A source with no API key is skipped, not failed. No
  AI provider means items are stored raw without analysis. A missing CLI tool is
  skipped. Nothing crashes the run.
- **Secrets stay in `.env`.** API keys live only in the local, git-ignored
  `.env` file — never in the database, never in logs, and never rendered back to
  the UI (the dashboard shows "set / not set" only). The sole exception is the
  keyless-translation endpoint URL, which is a non-secret address shown back so
  it can be edited from the dashboard.
- **English-only feed.** Source content in any language is rendered to English
  for the feed; the original is always one click away on the item.
- **Outbound-URL safety.** Any URL derived from a source item or operator input
  must pass the SSRF guard (`nexus.netguard.safe_http_url`) before it is
  fetched. Loopback/private/file addresses are refused.
- **Official APIs only for social platforms.** No scraping of social platforms
  and no account creation. Access is via official APIs (bring-your-own-key) or an
  optional third-party commercial provider the operator opts into.
- **Trigger-based collection.** Collection runs on a manual scan or a silent
  boot-sync gap-fill — never 24/7 background scraping.

## Architecture (current)

- **Backend:** Python + FastAPI, served locally. No build step on the frontend
  (Jinja2 + htmx + Tailwind via CDN), dark "terminal" UI.
- **Storage:** a single SQLite database with WAL mode and an FTS5 full-text
  index. Idempotent migrations.
- **Sources:** a `Source` ABC (`is_available()`, `fetch(since)`) with per-source
  watermarks for incremental pulls. CLI OSINT tools are wrapped as adapters that
  self-skip when the binary is absent.
- **AI:** a provider-agnostic engine (Anthropic / Gemini / OpenAI / local
  Ollama). A cheap local prefilter runs before any paid call; results are cached
  by content hash; a per-run budget cap bounds cost.
- **Language handling:** one module resolves the query language (so a search
  returns results in that language) and detects content language (to decide
  whether to translate). A keyless translation fallback (LibreTranslate over
  HTTP, optional offline Argos) keeps the feed English with no AI key.

## Built (v1)

- Collection backbone: RSS (always on), keyless search (Google News, Reddit
  search), GDELT worldwide multilingual monitoring, and key-gated official APIs
  (SerpApi, Google CSE, Reddit, Twitter/X, Telegram). User-defined custom API
  sources with an AI source planner and an Auto-Adapt self-healing pass.
- CLI tool adapters (username/email/phone/Google footprint and more) that map
  their JSON output into the entity record, skipping cleanly when not installed.
- Intelligence core: prefilter → batch analysis → threat scoring, summary,
  English translation, contradiction/credibility flags, entity typing — with
  caching and a budget guard. Standing intelligence-requirement scoring.
- Unified English feed with FTS5 search and time/source/threat filters,
  near-duplicate clustering ("seen N times"), read/unread, and quick relative-
  time filters.
- Analyst workspace: bookmarks, notes, multi-case management (status/priority,
  active case, membership), custom triage lanes.
- Evidence Vault (screenshot + hash + timestamp), watchlists with alerts,
  reporting (PDF/HTML), a persistent topic/entity relationship graph, OCR, and a
  first-run onboarding wizard.
- Packaging: a single local server (uvicorn) and a containerized run, with
  full-setup installers for Windows and Linux.

## Near-term

- **Source-pipeline parity.** Ensure every source flows through the same
  watermark + translation + scoring path uniformly, and populate the language
  field at the source where the provider exposes it (RSS feed/entry language,
  etc.) so translation is more accurate and detection is a fallback, not the
  primary signal.
- **Social sources via official APIs.** Solidify the bring-your-own-key path for
  the platforms that offer official APIs; keep the no-scraping, opt-in stance for
  everything else.
- **Feed and triage polish.** Continued work on the analyst surface for
  non-technical operators (clear empty/degraded states, plain-language labels).

## Mid-term

- A clearer plugin contract for third-party sources and tool adapters, so new
  sources can be added without touching the core.
- Richer entity resolution and cross-source linking in the relationship graph.
- Optional integration with an operator-supplied third-party commercial data
  provider (opt-in, key-gated) for platforms without a usable official API.

## Long-term

- Collaboration features for teams while preserving the local-first, operator-
  controlled model.
- Enterprise tiers and deployment options that do not compromise the
  local-only / no-scraping principles.

## Explicitly deferred

These are out of scope for now; the architecture leaves room to add them as
extensions rather than rewrites:

- Stealth/scraping collection paths (the project uses official APIs only).
- Proxy rotation / user-agent spoofing.
- Any feature that would require creating accounts or solving bot-detection.

## Where contributions help most

- New `Source` implementations and CLI tool adapters that follow the ABC and the
  graceful-degradation rule.
- Additional keyless or self-hosted translation/analysis backends.
- Reporting templates and export formats.
- Test coverage for sources, the prefilter, dedup, and the language layer.
