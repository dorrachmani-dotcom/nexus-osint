# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres
to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Opt-in encryption at rest for stored files (Settings › Encryption at rest, off
  by default): uploaded originals and evidence screenshots are stored AES-256-GCM
  encrypted (chunked, tamper-evident). The data key is wrapped by a passphrase
  (scrypt), a one-time recovery code and, optionally, Windows DPAPI for automatic
  unlock; it is never stored in the database, `.env`, logs or the UI. Without
  automatic unlock the app starts locked and asks for the passphrase on `/unlock`
  (rate-limited). Existing files are converted in the background; evidence
  hashes stay those of the original images. CLI: `python -m nexus.security.atrest`.

- Backup & restore (Settings › Backup & restore): one archive with the
  database (consistent copy via SQLite's backup API), uploaded documents,
  evidence, saved reports, the encryption keystore and the non-secret settings;
  API keys, tokens and passwords are never included. Optionally protected with a
  passphrase (AES-256-GCM). Restore validates everything before touching data
  (format and version check, zip-slip and ZIP-bomb safe, per-file SHA-256,
  database integrity check), saves a pre-restore backup, then swaps the data in.

### Changed
- Image OCR (uploads and evidence) reads from memory instead of writing a
  temporary copy of the image.

### Fixed
- Deleting a write-up no longer leaves what was published from it behind
  without asking. The delete dialog lists where it was published, with
  checkboxes: the knowledge-pool document is removed; the Obsidian note is
  removed only if Nexus created it in its own folder and nobody changed it
  (otherwise it is left and the dialog says why); from Confluence only Nexus's
  own section is removed, never the page, only with publishing allowed and
  only when ticked (it is not ticked by default).
  The result is shown per target; anything left or unreachable keeps its
  publish record.
- The packaged app now honours `NEXUS_HOME` (when set and non-empty) as its
  data folder, as its own documentation said; otherwise it still uses
  `%LOCALAPPDATA%\Nexus`.

## [1.3.0] - 2026-11-03

Bring your own data in, and send your analysis back out: uploads, watched pages,
custom APIs, Confluence and Obsidian on the way in; edited items and write-ups
published to Confluence, Obsidian or the knowledge pool on the way out.

### Added
- Edit and publish (write-back). Edit an item's title, summary and an analyst
  note; the edit is a separate layer (with an "edited" badge) and the collected
  original stays unchanged and visible. Compose a write-up from several pinned
  items (case ⋯ menu → Write-ups & publish). Publish an item or write-up to:
  Confluence (new page in a space, optionally under a parent page, or a section
  appended to an existing page; storage format fully escaped; source link,
  collected date, case and a "Collected by Nexus-OSINT" footer; republish
  updates the same page or only its own section; version conflicts retried
  once, HTTP 429 after `Retry-After`); an Obsidian vault (only inside a
  configurable `Nexus/` subfolder, path-validated with no traversal, symlink or
  junction escape, frontmatter via `yaml.safe_dump`, entity `[[wikilinks]]`,
  user notes never overwritten, text below the end marker kept on republish);
  or the knowledge pool, so Sherlock answers from the edited version. Every
  publish shows an exact preview and needs a click, and is recorded as
  "Published to …". Confluence ("Allow publishing to Confluence") and the vault
  ("Allow writing to the vault") each need an explicit opt-in, off by default;
  Test connection reports write permission per space. The vault importer and
  zipped-vault uploads skip notes Nexus wrote. Sherlock can prepare a publish
  (to Confluence, Obsidian or the knowledge pool) only as a confirmation card
  the analyst clicks, and only when the analyst asked for it. See
  `docs/PUBLISHING.md`.
- Upload documents into a case or a shared knowledge pool (Sources › Documents,
  or a case's Documents tab): PDF, Word, Excel, PowerPoint, OpenDocument, CSV,
  HTML, saved pages (MHTML), Markdown/text, JSON/NDJSON, XML, e-mail, RTF,
  images (OCR) and ZIP archives, including exported wiki/docs sites. Unknown
  formats are read best-effort. Each page/section/sheet/slide becomes a
  searchable item; Sherlock answers from the knowledge pool. AI enrichment is
  opt-in per upload, with a warning when the AI provider is a cloud service.
  ZIP-bomb, path-traversal and XML-entity protections, size limits, and
  originals stored locally and never served inline.
- Watch web pages and documentation sites (Sources › Web pages): optional
  same-site crawl with depth/page limits, `robots.txt` honoured, polite delays,
  and an item for each new page or a summary of what changed on a known one.
- Closed-network support: an opt-in internal allow-list
  (`INTERNAL_ALLOWED_HOSTS`, `INTERNAL_ALLOWED_CIDRS`, Settings › Internal
  network) applied centrally to every server-side fetch and every redirect hop;
  loopback, link-local/cloud metadata and the app's own port stay blocked. Per
  source: trust an internal CA (`INTERNAL_CA_BUNDLE`) or, explicitly, skip TLS
  verification; Basic/bearer/header/cookie logins kept in `.env`. Allowed
  internal connections show as "Internal network (allowed)" in the Security
  center.
- Obsidian: import vault (Sources › Obsidian): read a local vault folder
  (read-only; hidden folders skipped, symlinks never followed out of the
  vault) into a case or the knowledge pool. Frontmatter (`yaml.safe_load`),
  tags, `[[wikilinks]]` and `![[embeds]]` are kept; embedded PDFs/images/Office
  files go through the document readers; long notes split at headings.
  Re-scans pick up new, changed (with a "what changed" item) and deleted notes;
  optional include/exclude folders and a required tag. A zipped vault uploaded
  under Documents is parsed the same way. New dependency: PyYAML 6.0.3 (MIT),
  optional at runtime.
- Confluence connector (Sources › Confluence): Confluence Cloud (e-mail + API
  token) or Data Center / Server (Personal Access Token), also on closed
  networks through the internal allow-list and internal CA. Pick spaces after
  "Test connection" and/or enter a CQL query; pages (optionally blog posts,
  comments and attachments) are stored in a case or the knowledge pool with
  title, space, labels, author, version, last-modified time, link and
  breadcrumb. Later scans read only new and changed pages (watermark + version),
  a changed page gets a "what changed" item, and HTTP 429 is retried after
  `Retry-After`. The token stays in `.env`.
- Custom API sources read JSON, NDJSON, XML, CSV, RSS/Atom and HTML
  (auto-detected), page through results (page number, offset/limit, cursor or
  next link, capped per scan), authenticate with Basic or OAuth2 client
  credentials besides header/query/bearer, send several secret headers, and POST
  a JSON body template with `{query}`. Without a field mapping each record is
  stored as readable text. Requests use the shared SSRF guard (internal
  allow-list, per-source TLS, every redirect re-checked). The AI planner and
  Auto-Adapt understand non-JSON samples. Existing sources keep working
  unchanged; the database migrates automatically.

### Security
- The web app no longer serves the whole data folder under `/data`. Before,
  anyone who could reach the local port could download the SQLite database,
  logs and backups. Now only evidence screenshots are served, by one explicit
  route (`/data/evidence/<image>`) that accepts a flat image file name and
  rejects sub-paths, traversal and every other file type.
- RSS / search-feed fetches now re-check every redirect hop with the SSRF guard,
  so a public feed URL can no longer redirect the request to a loopback,
  private or link-local address.
- Dependency updates: Pillow 13.0.0 (security fixes), playwright 1.64.0,
  anthropic 1.15.0, pydantic-settings 2.16.0, networkx 3.6.1, ruff 0.17.1.

### Changed
- New guide `docs/INGESTION.md`: every way to bring your own data in (uploads,
  watched pages, closed networks, custom APIs, Confluence, Obsidian), with
  step-by-step setup, the `.env` keys, the case vs knowledge-pool choice, the
  cloud-AI / "no AI" privacy note and the security limits. README gains a
  "Bring your own data" section and lists API sources as their own feature.
- The case export is now labelled "Obsidian: export case", matching
  "Obsidian: import vault".
- Development tools (pytest) moved out of `requirements.txt`; install
  `requirements-dev.txt` to run the test suite. The runtime install is unchanged.
- README and `.env.example` explain that Reddit works keyless via its search feed, and that the official Reddit Data API is optional and needs approval from Reddit (since 2026).
- Sherlock keeps the current conversation (and whether its panel is open)
  when you move to another page. It lasts for the browser tab only and is
  cleared by "End chat".

### Fixed
- Auto-Adapt no longer fails when the AI answers that the response itself is
  the list of items (an empty items path).
- Sherlock's answers read as clean text: Markdown markers such as `**bold**`
  and `- ` bullets no longer show up literally (bullets become "•"). Answers
  are still displayed as text only, never as HTML.
- AI summaries state the news directly in one neutral sentence instead of
  commentary such as "The text claims…" or "The headline discusses…";
  headline-only items are summarised from what the headline reports.
- Item details no longer show the "aggregator" badge, matching the feed cards.
- Sherlock's "End chat" button is hidden while no AI model is set up.

## [1.2.0] - 2026-10-20

A clearer interface: calmer pages, obvious primary actions, and a Setup checklist
that makes configuring sources, AI, email and the daily brief straightforward.

### Changed
- Header: the row of ~25 source chips is now one "N/M sources" control that
  opens a list with plain names and what each source needs.
- The scan action is called "Scan now" everywhere.
- Feed cards show three actions (Open, Pin to case, Dismiss); the rest moved
  into a "more" menu. "Single source" reads "1 source", the "aggregator" badge
  is gone, and a body line that only repeats the headline is hidden.
- Item details: content first, one close button, Internet Archive below.
- Case cards say "N collected · M pinned".
- Settings lists the Gemini key first (recommended).
- Case page: tracking words collapse to a one-line summary with an Edit
  button; Close case moved into a "more" menu; clearer rename/edit links.
- Cases page: the create form opens from a "+ New case" button.
- Narrow screens get a Menu button for the navigation.
- Sherlock explains that it needs an AI model (with a Set up AI link) instead
  of offering a chat that cannot act.

### Added
- Setup checklist (Setup → Setup checklist): five steps with live status and
  one button each, including a daily-brief card with an on/off switch, local
  time, "scan first", what is included, a test send and "Next / Last sent".
- Per-case daily reports can be switched on from the Setup page, and each
  case's Reports tab shows the next and last report and whether it is emailed.
- The README demo video now shows the redesigned interface.
- README screenshots re-captured on the redesigned interface, plus the Setup checklist.

### Fixed
- The Brief page's Scan now button no longer wraps the navigation onto two lines.

## [1.1.0] - 2026-10-17

### Added
- Sherlock can schedule the daily email brief ("open a case on X and email me a
  summary every day"); allowed only once the email connection passed a test.
- Gmail setup: upload the OAuth client file from Google ("Download JSON")
  instead of copying the client ID and secret by hand.
- A 65-second demo video at the top of the README.

### Changed
- Sherlock answers small talk (greetings, thanks, "who are you") locally with no
  model call, and refuses prompt-injection / jailbreak attempts and "who built
  you" questions before anything reaches the model.
- Default AI models favour cost, since every analysed item is one call:
  Claude `claude-haiku-4-5`, Gemini `gemini-3.5-flash-lite`, OpenAI
  `gpt-5.4-nano`, Grok `grok-4.3`. Any model can still be set per provider.
- The daily brief's recipient defaults to the connected Gmail account.
- Every test runs against an empty, temporary `.env` and database, so the suite
  can never read the developer's real keys or send real email.

### Fixed
- Security: the issues raised by the first CodeQL scan (server-side fetches
  re-validate every redirect hop, the evidence browser blocks private
  addresses, log lines escape CR/LF) and two regular expressions made linear.
- Sherlock's links to pages inside the app open in the same window instead of
  a new tab.
- The Gmail settings card shows clearly that the Google client file was
  accepted.

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

[Unreleased]: https://github.com/dorrachmani-dotcom/nexus-osint/compare/v1.3.0...HEAD
[1.3.0]: https://github.com/dorrachmani-dotcom/nexus-osint/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/dorrachmani-dotcom/nexus-osint/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/dorrachmani-dotcom/nexus-osint/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/dorrachmani-dotcom/nexus-osint/releases/tag/v1.0.0
