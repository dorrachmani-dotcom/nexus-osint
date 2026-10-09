# Security Policy

Nexus-OSINT is a **local-first** intelligence platform: it runs entirely on the
analyst's own machine, binds only to the loopback interface, and never sends
collected content to anyone except the AI provider the analyst explicitly
configures. Because it handles sensitive investigative material, security is a
first-class design goal rather than an afterthought.

This document describes the threat model, the defenses that are implemented in
the code today, the limitations you should be aware of, and how to report a
vulnerability.

> **Honest scope note.** The defenses below were designed and tested in-house.
> Nexus-OSINT has **not** undergone an independent third-party security audit or
> penetration test. Do not interpret this document as a certification. It is an
> accurate description of the controls that exist in the codebase.

---

## Design principles

1. **Local-only by default.** The server binds to `127.0.0.1` only. It is never
   exposed to the local network or the internet unless the operator deliberately
   places a reverse proxy in front of it (not recommended).
2. **Secrets never touch the database or the UI.** API keys live only in the
   git-ignored `.env` file. They are never written to SQLite, never rendered back
   to the browser (the Settings page shows *Set / Not set*, never the value), and
   are scrubbed from logs.
3. **Graceful degradation.** A missing key, missing library, or missing CLI tool
   disables that one feature instead of crashing the platform. There is no code
   path where an absent secret produces a stack trace containing sensitive data.
4. **Untrusted input is treated as untrusted.** Everything collected from a
   source (titles, bodies, URLs, author names) is data, never an instruction or a
   trusted value. It is escaped on output and validated before use in any
   sensitive operation (subprocess arguments, headless-browser navigation).

---

## Threat model

| Asset | Threat | Primary mitigation |
|-------|--------|--------------------|
| API keys / credentials | Leak via DB, UI, logs, or tracebacks | `.env`-only storage; never rendered; log + traceback redaction filter |
| The host machine | SSRF / local file read via collected URLs | Scheme allowlist + private-IP block before headless capture |
| The host machine | Command/argument injection via OSINT tool targets | `argv`-list subprocess (no shell); leading-dash target rejected |
| The local database | SQL / FTS injection via search and filters | Parameterized queries; FTS query tokenized and quoted |
| The browser session | CSRF from a malicious website the operator visits | Same-origin (Origin/Referer) check on all state-changing requests |
| The browser session | DNS rebinding to reach the loopback server | `Host` header validated against loopback names |
| The browser session | XSS via collected content / AI output | Jinja autoescape; sandboxed graph iframe; CSP |
| Availability | ReDoS via an analyst-authored watchlist regex | Pattern-length and haystack-length caps |
| Supply chain | Known CVEs in dependencies | Pinned, `pip-audit`-clean dependency set |

Out of scope for the current design (see *Known limitations*): multi-user
authentication, encryption of data at rest, and the security of the external
OSINT CLI tools the platform can orchestrate.

---

## Implemented defenses

### Secret handling
- API keys are read from `.env` via `pydantic-settings` and never persisted to
  the database.
- The Settings UI exposes only a boolean *Set / Not set* status per key.
- A logging filter (`nexus/logging_safe.py`) scrubs every configured secret value
  from log records **and** from rendered exception tracebacks before they are
  written, so an accidental leak becomes `***REDACTED***`.
- Noisy HTTP client loggers are raised to `WARNING` so request URLs (which can
  carry `?api_key=...`) are not logged at `INFO`.

### Network exposure
- The application binds to `127.0.0.1` only.
- A `Host`-header check (`nexus/web/security.py`) rejects any request whose host
  is not a loopback name, closing the DNS-rebinding vector.

### CSRF / cross-origin protection
- All state-changing methods (`POST`/`PUT`/`PATCH`/`DELETE`) are verified to be
  same-origin: any `Origin`/`Referer` present must resolve to a loopback host,
  otherwise the request is refused with `403`. This needs no per-form token and
  works cleanly with htmx.

### Response hardening
- Every response carries `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`,
  `Cross-Origin-Opener-Policy: same-origin`, and a Content-Security-Policy that
  restricts scripts/styles/connections to `self` only (every asset ships with
  the app; nothing is loaded from a CDN), with `object-src 'none'`, `base-uri 'self'`,
  `form-action 'self'`, and `frame-ancestors 'none'`.

### Injection protection
- **SQL/FTS:** all queries use parameterized placeholders; the full-text search
  string is tokenized to word characters and each token is quoted, so no operator
  or quote from user input reaches the FTS parser.
- **Command execution:** OSINT CLI adapters run via an `argv` list with
  `subprocess.run` and never `shell=True`, so shell metacharacters are inert. A
  target beginning with `-` is rejected to prevent it being parsed as a flag
  (argument injection).

### SSRF / local-file-read protection
- Before the Evidence Vault captures a screenshot, the item URL is validated
  (`nexus/evidence.py`): only `http`/`https` schemes are allowed, and the host is
  resolved and refused if it maps to a private, loopback, link-local, reserved,
  multicast, or unspecified address. This blocks `file://` disclosure and SSRF
  against internal/cloud-metadata endpoints.

### XSS protection
- Jinja2 autoescaping is on for all templates.
- The entity-graph iframe is rendered with escaped `srcdoc` and a
  `sandbox="allow-scripts"` attribute, isolating it from the parent document.

### Denial-of-service guardrails
- Analyst-authored watchlist regexes are bounded: the stored pattern is capped in
  length, and the text it runs against is capped, so catastrophic backtracking
  cannot hang the scan loop.

### Supply chain
- Dependencies are pinned in `requirements.txt` to releases that pass
  [`pip-audit`](https://pypi.org/project/pip-audit/) with no known
  vulnerabilities. Re-run `pip-audit -r requirements.txt` after any dependency
  change.

---

## Known limitations

These are deliberate trade-offs for a single-user, local-first tool. Operators
who need a stronger posture should account for them:

- **No authentication / authorization.** Anyone with access to the loopback
  interface on the host has full access to the application. This is by design for
  a single-user desktop tool. Do not expose the port to a network.
- **Data at rest is not encrypted.** The SQLite database and captured evidence
  screenshots are stored as plain files under `data/`. Use full-disk encryption
  (BitLocker, FileVault, LUKS) if the host may be lost or shared.
- **Third-party OSINT CLI tools.** When you enable an adapter (Sherlock, Maigret,
  theHarvester, etc.), Nexus orchestrates code maintained by other projects. Their
  security and network behavior are outside our control; install only tools you
  trust.
- **AI provider egress.** If you configure a cloud AI provider, the text of items
  selected for analysis is sent to that provider. Choose the local Ollama backend
  if collected content must never leave the machine.
- **Not independently audited.** See the scope note at the top.

---

## Hardening recommendations for operators

- Keep the server bound to `127.0.0.1`; never put it directly on a network.
- Enable full-disk encryption on the host.
- Prefer the local Ollama backend for the most sensitive investigations.
- Keep dependencies current and re-run `pip-audit` after updates.
- Treat the `.env` file as a secret: it is git-ignored by default — keep it that
  way and never commit it.

---

## Reporting a vulnerability

If you discover a security issue, please report it privately rather than opening
a public issue:

1. Use **GitHub Security Advisories** ("Report a vulnerability") on this
   repository, or
2. Open a minimal issue asking for a private contact channel (do not include
   exploit details in the public issue).

Please include reproduction steps and the affected version/commit. We aim to
acknowledge reports promptly and will credit reporters who wish to be named once
a fix is available.
