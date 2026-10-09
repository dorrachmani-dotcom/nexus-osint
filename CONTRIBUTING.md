# Contributing to Nexus-OSINT

Thanks for helping. This project is small and opinionated; the rules below keep
it that way. Architecture and extension points are covered in the
[Developer Guide](docs/DEVELOPER_GUIDE.md).

## Ground rules

1. **Local-only.** The server binds to `127.0.0.1`. Do not add a bind to any
   other address, and do not send collected data to an endpoint the user has not
   configured. Any new outbound call must be visible to the egress monitor.
2. **Graceful degradation.** A missing key, binary, model or network disables one
   feature; it never crashes the app. New sources and adapters implement
   `is_available()` and skip cleanly.
3. **Never commit secrets.** `.env` is git-ignored; keep it that way. Do not put
   keys in code, tests, fixtures, logs or screenshots. Add new settings to
   `.env.example` with an empty value.
4. **Untrusted input stays untrusted.** Collected text and model output render as
   text, not HTML. URLs from items or user input go through
   `nexus.netguard.safe_http_url`. Data embedded in inline scripts goes through
   `|tojson`.
5. **Assistant tools are constructive and local.** New Sherlock actions
   must be reversible, must not delete data or touch settings/keys, and must be
   added to the `_ALLOWED_TOOLS` allow-list with a test.
6. **Reuse the provider abstraction.** LLM calls go through
   `nexus/analysis/providers.py`; do not inline new provider calls elsewhere.
7. **Fictional examples only.** No real people, organizations or incidents in
   tests, fixtures or docs.

## Development setup

```bash
git clone <your fork>
cd nexus-osint
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                 # keys are optional
uvicorn nexus.web.app:app --host 127.0.0.1 --port 8000 --reload
```

Python 3.11 or 3.12. Playwright and the OSINT CLI tools are optional.

**Changing styles.** The compiled stylesheet and htmx are committed, so running
the app needs no Node. If you add or change Tailwind classes in a template,
rebuild and commit the result (CI fails otherwise):

```bash
npm install
npm run build        # writes nexus/web/static/css/app.css and vendor/htmx.min.js
```

## Tests

```bash
python -m pytest -q                          # whole suite
python -m pytest tests/test_assistant.py -q  # one file
```

Tests use temporary SQLite databases (see `tests/conftest.py`) and must not hit
the network. A bug fix needs a regression test; a new storage query or route needs
a test against a seeded database.

## Linting and typing

Both are configured in `pyproject.toml` and enforced in CI (the `lint` and
`types` jobs), so run them before pushing:

```bash
pip install -r requirements-dev.txt
ruff check .          # lint (add --fix for the safe auto-fixes)
mypy                  # type-check the nexus package
pre-commit install    # optional: run ruff and basic hygiene hooks on every commit
```

- The ruff rule set is pycodestyle, pyflakes, isort, bugbear, pyupgrade,
  simplify, comprehensions, ruff-specific, pylint, bandit (security) and
  datetimez. Each global or per-file ignore in `pyproject.toml` carries a reason.
- Prefer fixing a finding over silencing it. If a `# noqa: CODE` or
  `# type: ignore[code]` is genuinely needed, scope it to the one code and say
  why on the same line.
- Do not swallow exceptions silently: catch the narrowest exception you can,
  and where a broad catch is needed to keep a feature degrading gracefully, log
  it with `logger.debug(..., exc_info=True)`.
- `ruff format` is not applied to the whole codebase yet; match the surrounding
  style and keep diffs focused. Add type hints to new code and keep functions small.

## Commits and pull requests

- Use [Conventional Commits](https://www.conventionalcommits.org/): `feat:`,
  `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, `ci:`, `perf:`, optionally
  scoped, e.g. `fix(storage): guard empty entity names`.
- Keep commits focused and the subject under about 72 characters; explain *why* in
  the body when it is not obvious.
- Fork the repository, branch from `main`, keep PRs small, and fill in the pull
  request template.
- `main` is protected: nothing is pushed to it directly. Every change lands
  through a pull request that passes CI and is approved by the maintainer
  (see `.github/CODEOWNERS`). CI on pull requests from forks runs only after a
  maintainer approves it.
- Update `CHANGELOG.md` under `Unreleased` for user-visible changes, and the docs
  when behaviour changes.

## Reporting security issues

Do not open a public issue. Follow [SECURITY.md](SECURITY.md).
