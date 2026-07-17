# Contributing to Job Squire

Thanks for considering a contribution. Job Squire is a small, volunteer-maintained
project, so please read this guide before opening a pull request. It will save
you a review round-trip.

By participating, you're expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

---

## Ways to contribute

- **Bug reports** — use the [bug report template](.github/ISSUE_TEMPLATE/bug_report.md).
  Include the running version (shown in the app footer), steps to reproduce, and
  the container logs (`docker logs <instance>` — web, worker, and mcp all share
  one container's log).
- **Feature requests** — use the [feature request template](.github/ISSUE_TEMPLATE/feature_request.md).
- **Security vulnerabilities** — do **not** open a public issue. See [SECURITY.md](SECURITY.md)
  for the private disclosure process.
- **Code, docs, and provider adapters** — pull requests welcome. See below.

---

## Local development setup (no Docker)

```bash
git clone https://github.com/dellipse/job-squire.git
cd job-squire

python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

export SECRET_KEY=dev \
       ADMIN_PASSWORD=devpass \
       USER_PASSWORD=devpass \
       DATA_DIR=./data \
       SESSION_COOKIE_SECURE=false

python wsgi.py   # http://localhost:8000
```

`requirements-dev.txt` installs `requirements.txt` plus `pytest`, `pytest-cov`,
`ruff`, and `pip-audit` — everything the CI pipeline runs locally.

The app targets **Python 3.14**. Match that locally where possible; a couple of
tests assume CPython 3.14 semantics.

---

## Running tests

```bash
coverage run --source=app -m pytest -q
coverage report --show-missing
```

Tests live in `tests/` (`conftest.py` sets up an isolated `SECRET_KEY`, temp
`DATA_DIR`, and seed accounts — no real secrets needed to run the suite).

CI enforces two coverage floors with `coverage report --fail-under`:

| Scope | Floor |
|---|---|
| Whole `app/` package | 33% |
| Critical modules (`crypto.py`, `auth.py`, `mcp_server.py`, `providers.py`, `search.py`) | 58% |

These are **ratchet baselines**, not targets — they only go up over time as
coverage improves elsewhere (`ai.py`, `main.py`, `worker.py`, `notify.py`,
`websearch.py` are still thin). A PR that lowers either number below its floor
will fail CI; a PR that raises real coverage is always welcome, even without a
matching feature.

Priority order for new tests, if you're looking for where to help: migrations
(schema changes run against real user data on every boot), crypto round-trips,
auth/rate-limiting, MCP OAuth edge cases, then provider adapters.

---

## Linting

```bash
ruff check .
```

The ruleset is intentionally small (`E4`, `E7`, `E9`, `F` — real bugs and syntax
errors, not style opinions) so it stays high-signal. `ruff format` is **not**
gated yet; the codebase hasn't been swept for formatting, so please don't
reformat unrelated lines in a functional PR — it makes the diff hard to review.

---

## Adding a job-board provider adapter

Provider adapters live in `app/providers.py`. To add one:

1. Add an entry to the `PROVIDERS` dict describing the provider: `label`,
   `signup_url`, a short `note` shown in the UI, and a `fields` list (each
   field has `name`, `label`, `secret` (bool, encrypted at rest if `True`),
   `required` (bool), and optionally `input_type`/`placeholder`). Fields with no
   credentials at all (e.g. Jobicy) use an empty `fields` list.
2. Write `search_<provider>(creds, title, cfg) -> list[dict]` following the
   existing adapters as a template. Each result dict should include the fields
   the rest of the app expects (`title`, `company`, `location`, `url`,
   `description`, `external_id`, etc. — see an existing adapter for the exact
   shape). Use `_clean()` for description text and `_iso_date()` for dates.
3. Register the function in the dispatch dict inside `search_provider()`.
4. Add a test in `tests/test_providers.py`: mock the HTTP response, assert the
   parsed shape, and cover the missing-credentials and empty-results paths.
5. If the provider needs a nginx/SWAG note or a rate-limit caveat, mention it in
   [`docs/wiki/03-job-sources.md`](docs/wiki/03-job-sources.md).

`search_provider()` never raises — adapter errors are caught and recorded on the
`SearchRun` so one bad provider can't take down a search pass. Follow that
pattern in new adapters (raise internally if you like, but don't let an
exception escape `search_<provider>`).

---

## Commit style

`TYPE: Short description`, imperative mood, one logical change per commit:

```
NEW: add MCP OAuth and provider adapter tests
FIX: resolve html-module shadowing bug breaking search/error digest emails
DOCS: add contributing guide and changelog
REFACTOR: clean up unused imports/vars and one ambiguous name
CHORE: update SBOM
```

Common types: `NEW`, `FIX`, `DOCS`, `REFACTOR`, `CHORE`. Match whichever is
closest; don't invent a new one without a good reason.

---

## Pull requests

1. Fork the repo and branch from `main`.
2. Keep the change focused — one concern per PR is much easier to review than a
   bundle of unrelated fixes.
3. Make sure `ruff check .` and the test suite pass locally, and that neither
   coverage floor regresses.
4. Update the relevant doc under `docs/` if behavior changes, and add a
   [CHANGELOG.md](CHANGELOG.md) entry under `[Unreleased]`.
5. Open the PR against `main`. CI runs lint, tests, and `pip-audit`
   automatically; all three must pass before merge.

Database schema changes are additive `ALTER TABLE` statements in
`_run_migrations()` (`app/__init__.py`) — there's no Flask-Migrate. See
`docs/code-reference.md` before touching this function.

---

## Questions

Open a [discussion or issue](https://github.com/dellipse/job-squire/issues) if
something here is unclear — that's useful feedback on this document too.
