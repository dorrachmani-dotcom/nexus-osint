# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres
to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `CONTRIBUTING.md`, issue templates and a pull request template.

### Changed
- Documentation reorganized: the feature catalogue moved to `docs/FEATURES.md`,
  the project overview to `docs/OVERVIEW.md`, and the README rewritten around
  architecture and engineering trade-offs.
- In documentation, the in-app assistant is referred to as "Nexus Assistant".

### Removed
- `docs/INVESTIGATOR_PLAN.md`; its remaining items moved to `docs/ROADMAP.md`.

## [1.0.0]

First tagged release.

### Added
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
- **Nexus Assistant**: in-app chat that answers from collected data and performs
  constructive local actions through a fixed allow-list.
- **Security center**: egress monitor, file scanner, optional hash-only
  VirusTotal lookup, CSRF/DNS-rebinding protection, security headers, SSRF guard,
  secret-redacting log filter.
- **Packaging and quality**: Docker image, Windows installer (PyInstaller + Inno
  Setup), per-OS launcher scripts, pytest suite and GitHub Actions CI.

### Security
- Inline script embedding of collected data uses Jinja `|tojson` to prevent
  `</script>` breakout.

### Fixed
- Dark-theme text contrast raised to meet WCAG AA; dialog semantics and focus
  return added to the item drawer.

[Unreleased]: https://github.com/dorrachmani-dotcom/nexus-osint/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/dorrachmani-dotcom/nexus-osint/releases/tag/v1.0.0
