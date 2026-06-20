# Nexus-OSINT — Complete Project Explanation

> The single, in-depth explanation of **what this project is, how it works, and
> how to run, use, and extend it.** If you only read one file, read this one.
> For task-focused detail see the **[User Guide](docs/USER_GUIDE.md)** and the
> **[Developer & Contributor Guide](docs/DEVELOPER_GUIDE.md)**; for a quick start
> see the **[README](README.md)**.

---

## Table of contents

1. [What Nexus-OSINT is](#1-what-nexus-osint-is)
2. [Design philosophy](#2-design-philosophy)
3. [The big picture: how data flows end to end](#3-the-big-picture-how-data-flows-end-to-end)
4. [Every subsystem, explained](#4-every-subsystem-explained)
5. [Architecture & technology](#5-architecture--technology)
6. [The data model](#6-the-data-model)
7. [Running it (three ways)](#7-running-it-three-ways)
8. [Extending it](#8-extending-it)
9. [Security & privacy model](#9-security--privacy-model)
10. [Repository map](#10-repository-map)
11. [FAQ](#11-faq)

---

## 1. What Nexus-OSINT is

Nexus-OSINT is a **local-first open-source intelligence (OSINT) workstation**. It
is a single application you run on your own computer that:

1. **Collects** open-source content — news/RSS, Google News, Reddit, GDELT, and
   (optionally) Telegram, Twitter/X, SERPAPI, Google Custom Search, plus any
   public JSON API and the output of installed OSINT CLI tools.
2. **Stores** everything locally in one SQLite database — your permanent,
   searchable archive.
3. **Analyzes** it (optionally) with a pluggable AI backend — summary,
   translation, threat scoring, entity extraction, credibility flags, and
   relevance scoring against your standing questions.
4. **Presents** it in a unified, dense, dark-themed feed with a full analyst
   workspace: cases, intelligence requirements, an evidence vault, a relationship
   graph, watchlists, reporting, air-gap transfer, and an in-app assistant.

Everything binds to `127.0.0.1`. Nothing is exposed to the network, and your data
never leaves the machine unless **you** explicitly export it.

**Who it's for:** researchers, analysts, journalists, and investigators who want a
private, self-hosted, end-to-end intelligence loop — from collection to a
court-ready report — without depending on a cloud service.

**It runs for free.** RSS, Google News, Reddit search and GDELT need no API key;
Google Gemini has a free AI tier; or run a fully local model with Ollama. Keyed
sources are optional upgrades.

---

## 2. Design philosophy

Five principles shape every part of the codebase:

- **Local-first / OpSec.** The server is loopback-only. Secrets live in a local
  `.env` file and never touch the database, the UI (which shows only set/not-set),
  the logs (a redaction filter scrubs them), or any shipped artifact. The Security
  center makes the app's outbound traffic auditable.
- **Graceful degradation.** Every source and capability is optional. A missing
  key, model, or tool disables exactly one feature — never crashes the app. With
  no AI at all, items are still collected, stored and searchable.
- **Trigger-based execution.** No 24/7 scraping. Collection runs on an explicit
  scan, an optional auto-scan timer, or a quiet boot sync at startup.
- **Provider-agnostic AI.** All model access goes through one interface, so cloud
  (Anthropic Claude, Google Gemini, OpenAI) and a fully local model (Ollama) are
  interchangeable and switchable live.
- **No frontend build step.** The UI is server-rendered Jinja2 + htmx + Tailwind
  (CDN). There is no JavaScript toolchain to install.

---

## 3. The big picture: how data flows end to end

A **scan** is the heartbeat of the system:

```
        ┌─────────┐   ┌──────────────┐   ┌────────────┐   ┌──────────────┐
SOURCES │ collect │ → │  store +     │ → │  watchlist │ → │   AI analyze │ →
 (rss,  │  items  │   │  deduplicate │   │   match    │   │  (prefilter, │
 news,  └─────────┘   └──────────────┘   └────────────┘   │  summary,    │
 reddit,                     │                             │  translate,  │
 custom,             one SQLite write path                 │  score,      │
 tools…)             (idempotent, clusters                 │  entities)   │
                      near-duplicates)                     └──────────────┘
                                                                  │
                                                ┌─────────────────┴─────────────┐
                                                │  score Intelligence            │
                                                │  Requirements (0–100 per PIR)  │
                                                └────────────────────────────────┘
                                                                  │
                          ┌───────────────────────────────────────┴───────────────┐
                          │  PRESENT: feed, intel, cases, brief, graph, reports…    │
                          └─────────────────────────────────────────────────────────┘
```

1. **Collect** — each available source fetches new items. A per-source failure is
   isolated and logged; one bad feed never aborts the scan.
2. **Store & deduplicate** — a single write path inserts items. An exact re-fetch
   is a no-op; near-duplicate content is folded into a *cluster* (an "Echoed N
   times" badge) instead of creating a duplicate row.
3. **Watchlists** — new items are matched against your patterns, recording alerts.
4. **Analyze** — un-analyzed items pass a cheap local prefilter (to avoid spending
   tokens on noise), then the active AI provider, bounded by a per-run budget.
   Results are cached by content, so re-scans never re-pay.
5. **Score requirements** — each item is scored 0–100 against each standing
   question; scores are cached per (item, requirement).

The web layer then reads these rows and renders them, enriching each feed row in
one batched pass with derived fields (typed entities, related sources,
read/bookmark flags, source reliability, relative time, best requirement match).

---

## 4. Every subsystem, explained

**Collection sources.** RSS/Atom is the keyless backbone. Keyless Google News and
Reddit search work via public search feeds; GDELT adds global news. Keyed sources
(SERPAPI, Google Custom Search, Twitter/X, Telegram) activate when their key is
present. You can also add **any public JSON API** with no code — an AI planner
reads the API's docs/sample and writes the field mapping, and **Auto-Adapt**
re-maps fields automatically if the API's shape later drifts.

**AI intelligence core.** A pluggable backend (Claude / Gemini / OpenAI / Ollama)
behind one interface. A local prefilter trims noise first; then items get a
summary, an English translation when needed, a threat level, extracted entities
(people / organizations / locations / identifiers), and a first-party vs.
third-party / contradiction flag. Everything degrades gracefully without a key.

**Intelligence Requirements (PIRs).** You define the standing questions your
investigation needs answered. The AI scores every item 0–100 on how well it
answers each, with a short rationale. The **Intel** feed ranks items by those
scores so the most relevant material rises to the top.

**The feed ("The River").** A reverse-chronological stream of dense cards with
summaries, translations, threat badges, entities, cross-source corroboration, and
per-item actions (read/dismiss/bookmark, add to list, pin to case, capture
evidence, open details). It is topic-scoped by default; search or "Everything"
opens the firehose.

**Cases.** A case is the home for one subject, with tabs: a **Live feed** driven
by its tracking words (also searched across sources each scan), a **Pinned**
dossier of items + notes, a **Questions** tab of per-case PIRs, **sub-cases**, and
a **timeline**. Create a case from a one-line brief and the AI auto-sets its
tracking words and starter questions.

**Daily Brief.** A one-screen morning read: what's new across every open case
since you last opened each, top items first, with one-click pin and a live nav
badge.

**Evidence Vault.** Timestamped, SHA-256-hashed full-page screenshots of a source
at collection time, stored locally so they survive deletion upstream. OCR makes
image-only content searchable.

**Watchlists.** Keyword / regex / crypto-wallet / phone patterns that raise an
alert whenever they appear in newly collected items (with ReDoS guards on regex).

**Relationship graph.** A co-occurrence network of the people, organizations and
places the AI extracts — bigger dots are mentioned more, central dots connect the
most others. Global or scoped to one case; entity aliases merge variants.

**Reporting & export.** Export a case or the feed to PDF/HTML, machine-readable
CSV/JSON (exactly the rows you're viewing), or an **Obsidian vault** (Markdown with
`[[wikilinks]]` between entities). Graceful fallback to HTML if no PDF engine.

**Air-gap transfer.** Move intelligence between two machines with no network: an
online collector exports a portable `.nexusbundle`; an offline station imports it
and sees every item with its analysis and evidence. Idempotent re-imports; **no
secrets ever travel in a bundle**, and imported files are virus/zip-bomb scanned.

**Security center.** A fully local defensive layer: an **egress monitor** that
records every outbound connection and flags anything that isn't an AI provider, a
configured source, or localhost; and a **file scanner** for vetting files you bring
in. An optional VirusTotal check sends only a file's SHA-256 *hash*, never the file.

**Sherlock, the assistant.** A floating chat on the same AI backend. It explains
the product, answers questions about your data, and takes **constructive, local,
reversible** actions (build a case, fill it, generate a report, run a scan, add a
watchlist/requirement/note, capture evidence, navigate). With the **Sources**
toggle it cites the collected items its answer draws on, as clickable sources.
By design it can never delete, change settings/keys/provider, or send data off the
machine — and it has no knowledge of who built the software.

---

## 5. Architecture & technology

| Layer | Choice |
| --- | --- |
| Language | Python 3.11+ |
| Web | FastAPI + uvicorn |
| Templates / UI | Jinja2 + htmx + Tailwind (CDN) — no build step |
| Storage | SQLite (WAL) with an FTS5 full-text index kept in sync by triggers |
| AI | Pluggable: Anthropic Claude · Google Gemini · OpenAI · local Ollama |
| Screenshots | Playwright (headless Chromium) |
| Reporting | xhtml2pdf / reportlab (PDF), Jinja (HTML) |
| Graph | networkx + pyvis |
| Packaging | PyInstaller + Inno Setup (Windows installer); Docker; per-OS launcher |

Execution is trigger-based: a silent boot sync fills gaps at startup, and a manual
scan (or an auto-scan timer) runs collection on demand. The server binds to
`127.0.0.1` only.

---

## 6. The data model

One SQLite database (WAL mode); schema in `nexus/db.py`. Highlights:

- `items` — the canonical collected row (source, url, title, content, language,
  timestamps, dedup/cluster keys, dismissed flag, a mirrored `summary` for FTS).
- `items_fts` — FTS5 virtual table kept in sync by triggers; powers search.
- `clusters` — near-duplicate grouping with a `shared_count`.
- `analyses` — one row per analysed item (threat level, summary, translation,
  entities JSON, party, contradiction, confidence). *Source reliability is
  computed at render time, not stored.*
- `requirements` / `requirement_hits` — standing questions and per-item scores.
- `cases` (with `parent_id`, status, priority), `case_terms` (tracking/alert
  words), `bookmarks` (pins; case-scoped or general), `notes`.
- `lists` / `list_memberships` — triage lanes.
- `watchlists` / `watchlist_hits`; `subscriptions` (collection targets);
  `entity_aliases`; `evidence`; `meta` (runtime settings).

All database access is centralized in `nexus/storage.py`.

---

## 7. Running it (three ways)

- **Desktop app (Windows/Linux)** — the packaged installer bundles Python, the
  OSINT tools, and a headless Chromium. Double-click to run in a chromeless window.
- **Docker** — `docker compose up --build`, then open `http://127.0.0.1:8000`.
  All batteries included.
- **Local (developers)** — a virtualenv + `pip install -r requirements.txt` +
  `uvicorn nexus.web.app:app`.

Full commands are in the [README](README.md); a feature-by-feature walkthrough is
in the [User Guide](docs/USER_GUIDE.md).

---

## 8. Extending it

The codebase is built so adding capability is an *extension*, not a rewrite. The
[Developer Guide](docs/DEVELOPER_GUIDE.md) covers each in detail:

- **Add a collection source** — subclass `Source` (`nexus/sources/base.py`),
  implement `is_available()` + `fetch()`, register it in the collector.
- **Wrap an OSINT CLI tool** — subclass `ToolAdapter` and add it to the registry.
- **Add an AI provider** — subclass `LLMProvider` and wire `get_provider()`.
- **Extend the assistant** — add a constructive tool to the `_ALLOWED_TOOLS`
  allow-list in `nexus/assistant.py`.
- **Add a JSON API source with no code** — use the in-app Sources page.

---

## 9. Security & privacy model

- **Loopback-only.** The server binds to `127.0.0.1`; nothing is exposed.
- **Secrets stay in `.env`.** Never in the database, the UI, the logs, or any
  build artifact. A log-redaction filter is the safety net.
- **Auditable egress.** The Security center shows exactly what the app talks to and
  flags anything unexpected.
- **Optional, and fully local, AI.** With Ollama, analysed content never leaves the
  machine.
- **Deliberate exports only.** Data leaves only when you export a report or a
  transfer bundle — and bundles never contain secrets.
- **Web hardening.** CSRF/DNS-rebinding protection and security headers on every
  response; untrusted (collected) URLs are sanitized before rendering; model output
  is rendered as text, never raw HTML; user-supplied URLs pass an SSRF guard.

---

## 10. Repository map

```
nexus/            # the application package
├── web/app.py    #   FastAPI app: every route
├── web/templates #   the whole UI (Jinja + htmx)
├── storage.py    #   all DB access (the single source of truth for SQL)
├── db.py         #   schema, WAL, FTS5, migrations
├── collector.py  #   the scan pipeline
├── sources/      #   collection sources (rss, news, reddit, custom, …)
├── adapters/     #   Plug & Play OSINT CLI tool wrappers
├── analysis/     #   the AI core (providers, prefilter, prompts, scoring)
├── assistant.py  #   Sherlock (grounded chat + constructive actions)
├── evidence.py   #   screenshots + hashing;  graph.py  obsidian.py  transfer.py
├── security.py   #   egress monitor + file scanner;  netguard.py  logging_safe.py
└── …             #   reporting, casesetup, models, config, envstore, lang, ocr
docs/             # USER_GUIDE.md, DEVELOPER_GUIDE.md, ROADMAP.md
installer/        # PyInstaller spec, Inno Setup script, frozen entry point
launcher/         # one-click desktop installers per OS
tests/            # the pytest suite
README.md · OVERVIEW.md (this file) · SECURITY.md · LICENSE
```

What is **not** in this package (and never published): your `.env` (API keys),
your `data/` database, and any locally built installer artifacts — all local-only.

---

## 11. FAQ

**Do I need to pay for anything?** No. RSS + Google News + Reddit + GDELT are
keyless; Gemini has a free AI tier; or run Ollama fully locally.

**Does any of my data leave my machine?** Only if you connect a cloud AI provider
(analysed text goes to that provider) or you explicitly export a report/bundle.
Use Ollama to keep everything offline.

**What happens without an AI key?** Collection, storage, full-text search, cases,
watchlists, evidence and reports all work — you just don't get AI summaries,
translations or scores.

**Can it run completely offline?** Yes — collection needs the internet, but with a
local Ollama model the analysis is offline, and the air-gap transfer workflow is
designed for fully disconnected analysis machines.

**Where is my data?** A single SQLite file under the app's data directory (the
desktop app uses the OS app-data folder; a dev run uses the project's `data/`).
Back up that file to back up everything.

**How do I get help inside the app?** Open **Sherlock** (the chat) or the **Guide**
page — both explain every feature in plain language.
