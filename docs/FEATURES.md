# Nexus-OSINT — Feature catalogue and configuration

The full feature list, scan pipeline, configuration reference and local-model
setup. For the short version see the [README](../README.md); for the conceptual
walkthrough see the [Overview](OVERVIEW.md); for task-oriented usage see the
[User Guide](USER_GUIDE.md).

## Features

- **Investigate across all sources** — enter one term (a name, alias, handle,
  wallet, CVE, organisation) and it is searched on every keyword-capable source
  at once — Google News and Reddit (both keyless, via public search RSS), plus
  Google Custom Search, SERPAPI, Twitter/X, and Telegram when their keys are
  set — on each scan, with results landing in the unified feed and ranked in
  Intel. A configurable time window (`NEWS_WINDOW_DAYS`) bounds news to the last
  N days, keeping each scan fresh and its volume in check.
- **Add sources and keys from the dashboard** — manage collection targets on the
  Topics page and paste any API key on the Settings page; keys are written to
  `.env` only (never the database, never shown back) and take effect with no
  restart. No file editing required.
- **Add any JSON API as a source** — on the Sources page, plug in any public REST
  API without writing code. When an AI backend is connected it can read the API's
  docs and/or a sample response and auto-fill the configuration (base URL, auth,
  which field holds the text); with no AI you fill the same fields by hand. A
  per-source **Test** button does a live fetch so you can confirm it works before
  it joins the next scan. Each source's API key lives in `.env` only (the key name
  is derived server-side from the source id, so nothing arbitrary can be written).
- **Unified feed (The River)** — a dense, dark, terminal-style stream of items
  with threat badges, AI summaries, optional translation, and echo/virality
  counts for near-duplicate content.
- **AI intelligence core** — a local prefilter trims noise before any token is
  spent; items are then scored (threat level), summarized, translated, flagged
  for possible disinformation, and tagged first-party vs. third-party. The
  backend is pluggable (Google Gemini, OpenAI, Anthropic, or local Ollama) and
  switchable live from Settings. Results are cached by content and capped per run by a budget
  guard.
- **Intelligence Requirements** — define the questions your investigation needs
  answered; the AI scores every collected item (0–100) for relevance to each
  requirement, and a dedicated Intel feed ranks items by how well they answer
  what you actually care about.
- **Full-text search & facets** — SQLite FTS5 across the whole local history,
  filtered by source, threat level, and date range.
- **Daily brief** — a one-screen morning read: what's new across every open case
  since you last looked at each, top items first, with one-click pin. Cases show
  a **"+N new"** badge, **mark-all-read** clears a case's queue, and the feed
  supports **keyboard triage** (`j/k` move, `o` open, `b` bookmark, `r` read). A
  set-up checklist surfaces any configuration gaps with one-click fixes.
- **Case hub** — a Case is the single home for one subject. Each case has tabs:
  a **Live feed** driven by its tracking words (its "word capsule" — also searched
  across your sources on each scan), a **Pinned** dossier of items + researcher
  notes, a **Questions** tab (per-case intelligence requirements with scored
  matches), and **sub-cases**. Create a case from a one-line brief and the AI sets
  up its tracking words and starter questions for you.
- **Evidence Vault** — timestamped, SHA-256-hashed full-page screenshots of a
  source at collection time, stored locally so they survive deletion upstream.
  Visible text in each screenshot is read by OCR (when the Tesseract engine is
  present — bundled in the Docker image) so image-only sources stay searchable.
- **Watchlists & alerts** — keyword / regex / wallet / phone patterns matched
  against new items on every scan.
- **Plug & Play OSINT tools** — wraps installed CLI tools (Sherlock, Maigret,
  holehe, GHunt, PhoneInfoga, theHarvester, SpiderFoot, Toutatis, OnionSearch)
  and pulls their findings into a local entity graph (a lightweight
  link-analysis alternative built on networkx + pyvis).
- **Custom API sources (self-service + self-healing)** — point Nexus at any
  public JSON API; the AI planner reads its docs/sample response and writes the
  field mapping for you. **Auto-Adapt** then watches each custom source: if the
  API keeps responding but its response shape drifts (so nothing is collected),
  the AI automatically re-maps the fields from the latest response — bounded by
  a threshold and a cooldown, and only when an AI provider is connected.
- **Reporting & data export** — export a case to PDF (or HTML) including items,
  AI summaries, evidence screenshots, and notes. Every data view also offers a
  one-click **Export** menu: PDF/HTML reports plus machine-readable **CSV and
  JSON** of exactly the rows you're looking at (filters and search applied) — on
  the main feed, the relevance-ranked Intel view (with each item's score), and
  inside a case. The Security center adds a downloadable data-handling audit
  report. A case also exports as an **Obsidian vault** (a Markdown folder with
  `[[wikilinks]]` to every entity) so Obsidian's graph view shows the case's web
  of people, organisations and places.
- **Relationship graph** — a co-occurrence network of the people, organisations
  and places the AI extracts: bigger dots are mentioned more, central dots
  connect the most others. Available globally **or scoped to a single case**
  (its tracked items only) from the case's **Graph** button.
- **Air-gap transfer** — move collected intelligence between two machines without
  a network (`/transfer`). An online collector exports a portable `.nexusbundle`
  file (everything, a single case, or just what's *new since the last export*) to
  a USB stick; an offline analysis station imports it and instantly sees every
  item with its summary, translation, entities, scores and evidence screenshots.
  Items are keyed on a stable content hash, so importing is idempotent and never
  collides with the receiving machine's own data. **No secrets ever travel in a
  bundle** — only collected open-source data and its analysis; `.env` and keys
  stay on each machine. Imported files are vetted by the security scanner first
  and written safely (evidence by basename only — no path traversal).
- **Nexus Assistant, the in-app chat** — a floating chat (bottom-right) backed by
  the same AI provider. It explains how to use the workstation, answers questions
  about your collected data, and can *act* on your behalf with plain-language
  requests: run a scan, search the feed, open a case and add the matching items,
  generate a report, capture evidence of an item, add a watchlist term, add an
  intelligence requirement, write a case note, or open a page. By design it only
  ever performs **constructive, local** actions — it cannot delete anything,
  change settings/keys/the AI provider, or send your data anywhere.
- **Security center** — a built-in, fully local defensive layer (`/security`):
  an **egress monitor** that records every outbound connection the app makes and
  flags any destination that isn't an AI provider, a configured source, or the
  local machine; and a **file scanner** for vetting files you bring in (disguised
  executables by real signature, zip-slip / zip-bomb archives, embedded
  installers, macro-bearing documents). ClamAV is used automatically if present,
  and — only if you opt in with `VIRUSTOTAL_API_KEY` — a file's SHA-256 *hash*
  (never the file itself) is checked against VirusTotal. That hash lookup is the
  only part that ever touches the network.

## How a scan works

1. **Collect** — each available source fetches new items.
2. **Deduplicate** — exact re-fetches are ignored; near-duplicates collapse into
   one feed row with an "Echoed N times" badge.
3. **Watchlists** — new items are matched against your patterns, recording alerts.
4. **Analyze** — un-analyzed items pass the local prefilter, then the active AI
   backend (within the per-run budget); results are cached so re-scans never
   re-pay.
5. **Score requirements** — each item is scored for relevance against your
   defined Intelligence Requirements, feeding the ranked Intel feed. Scores are
   cached per (item, requirement) pair.

## Configuration

All configuration lives in `.env` (see `.env.example` for the full list).
Everything is optional:

| Variable | Enables | Key? |
| --- | --- | --- |
| *(capsule search terms)* | Keyless Google News + Reddit search | **free** |
| `RSS_FEEDS` | RSS collection | **free** |
| `NEWS_WINDOW_DAYS` | Time window for keyless Google News (default 7) | **free** |
| `AI_PROVIDER` | Chooses the AI backend: `anthropic`, `gemini`, `openai`, `ollama`, or `off` | — |
| `GEMINI_API_KEY` | Gemini analysis, translation, threat scoring | free tier |
| `ANTHROPIC_API_KEY` | Claude analysis, translation, threat scoring | paid |
| `OPENAI_API_KEY` | ChatGPT analysis, translation, threat scoring | paid |
| `LIBRETRANSLATE_URL` | Keyless English-translation fallback when no AI is set | **free** |
| `GOOGLE_CSE_KEY` + `GOOGLE_CSE_CX` | Google Custom Search (wider web) | free tier |
| `SERPAPI_KEY` + `SERPAPI_QUERIES` | Richer Google News via SERPAPI | paid |
| `REDDIT_CLIENT_ID/SECRET` + `REDDIT_SUBREDDITS` | Reddit subreddit "new" feeds | free |
| `TWITTER_BEARER_TOKEN` + `TWITTER_QUERIES` | Twitter/X collection | paid |
| `TELEMETRY_API_KEY` + `TELEGRAM_CHANNELS` | Telegram collection | paid |
| `INSTAGRAM_SESSIONID` | Toutatis adapter (Instagram account lookup) | free |
| `VIRUSTOTAL_API_KEY` | Security center: hash-only VirusTotal lookup (opt-in) | free tier |
| `ANALYSIS_MAX_ITEMS_PER_RUN` | Budget guard: items analyzed per scan (default 100) | — |

Secrets are read from `.env` only and are never written to the database — and a
log-redaction filter scrubs any configured key from the logs as a safety net.
Keys can be pasted on the Settings page (saved to `.env`, never shown back), and
the AI provider can be switched live there too (the choice is stored in the
database, but the keys themselves always stay in `.env`). If the AI provider
hits its rate/quota limit (e.g. Gemini's free tier) the scan pauses cleanly and
the remaining items are analyzed on the next run.

## Run a fully local model (no cloud, no data leaves your machine)

Some organisations cannot send investigation content to a third-party cloud API
(Google, Anthropic, OpenAI) for data-security or compliance reasons. Nexus
supports a **fully local** AI backend via [Ollama](https://ollama.com): the model
runs on your own machine, so analysed content **never leaves it**. No API key,
no account, no network calls to anyone.

### Setup

1. **Install Ollama** — download it for Windows, macOS, or Linux from
   <https://ollama.com/download> (on Linux: `curl -fsSL https://ollama.com/install.sh | sh`).
   It runs a local server at `http://localhost:11434` automatically.
2. **Pull a model** — in a terminal:
   ```bash
   ollama pull gemma4:e4b      # the default; ~6.6 GB download
   ```
3. **Point Nexus at it** — set `AI_PROVIDER=ollama` in `.env` (and optionally
   `OLLAMA_MODEL` / `OLLAMA_BASE_URL`), **or** just pick **"Local model (Ollama)"**
   from the AI-provider dropdown on the Settings page. That's it — scans now
   analyse everything locally.

If the Ollama server isn't running, analysis simply pauses for that scan
(graceful degradation) — collection and search keep working, and analysis
resumes automatically once Ollama is back.

### Hardware requirements

Pick a model to match your hardware. CPU-only works everywhere but is slower; a
GPU (NVIDIA, or Apple-Silicon unified memory) makes analysis much faster.

| Use case | Model | RAM (CPU-only) | GPU VRAM (fast) | Disk |
| --- | --- | --- | --- | --- |
| Lightweight / older laptops | `gemma4:e2b` | 8 GB | 4 GB | ~4.6 GB |
| **Recommended balance** | `gemma4:e4b` *(default)* | 16 GB | 8 GB | ~6.6 GB |
| Sharper, still mid-range | `gemma4:12b` | 24 GB | 12 GB | ~7.7 GB |
| Fast on a GPU (mixture-of-experts) | `gemma4:26b` | 32 GB | 16 GB | ~16 GB |
| Strongest reasoning and tool use | `qwen3.8:27b` | 32 GB | 24 GB | ~18 GB |
| Best (workstation/server) | `gemma4:31b` | 64 GB+ | 24 GB+ | ~19 GB |

Model list checked against [ollama.com/library](https://ollama.com/library) in
October 2026. Any other Ollama model also works: download it, then type its name
in Settings or set `OLLAMA_MODEL`. Very large "open" models such as Kimi K3
(2.8T parameters) are not practical on a single machine, and Ollama's `:cloud`
variants run on remote servers, so they are not private.

Notes:
- **Apple-Silicon Macs** (M-series) run these models well thanks to unified memory.
- The cheap local **prefilter** and the **budget guard**
  (`ANALYSIS_MAX_ITEMS_PER_RUN`) keep the workload manageable on modest hardware —
  lower the cap if a scan is too heavy for your machine.
- **Quality trade-off:** a 4-12B local model gives solid summaries and threat
  scoring; the cloud models are still stronger for the most nuanced analysis.
  Choose the trade-off that fits your security posture — you can switch backends
  any time from the Settings page.

