# AGENTS.md

Repository notes for AI agents and contributors working on Agreement Manager.

## What this is

A FastAPI + Jinja + SQLite web app that demonstrates the Docusign **Agreement
Manager** (IAM) API: list/get/patch/delete agreements, bulk upload, custom
provisions, and document download. See `README.md` for the product overview and
user-facing setup.

## Commands

```bash
pip install -r requirements.txt     # install dependencies
./run.sh                            # run the app (port 12000, override with APP_PORT)
python -m pytest tests/ -q          # run tests
python -m pytest tests/test_app.py -q   # UI/route tests only
```

To run manually without the script:

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 12000
```

## Layout

| File | Responsibility |
|---|---|
| `app/main.py` | All FastAPI routes: pages, OAuth, Agreement Manager actions, JSON API |
| `app/config.py` | Settings from env/`.env`; Docusign environment URL map and AGM scopes |
| `app/agreement_manager_client.py` | Agreement Manager (IAM) REST client: agreements, bulk jobs, provisions |
| `app/db.py` | SQLite cache layer, schema, sample seed data |
| `app/pdf.py` | Dependency-free PDF generator (sample documents) |
| `app/templates/*.html` | Jinja templates |
| `app/static/` | `css/app.css`, `js/app.js`, `js/agreement_detail.js` |
| `tests/` | pytest suite |

## Conventions

- Python: type hints on public functions, `from __future__ import annotations`,
  dataclasses for structured config. Keep comments for non-obvious reasoning
  only; do not narrate the code.
- Templates: extend `base.html`; page routes pass an `active` key for nav state.
- Front-end: plain JavaScript, no build step. Shared helpers live on
  `window.App` (`toast`, `setLoading`, `postJSON`) in `static/js/app.js`.
- Data: SQLite via `app/db.py`. The `data/` directory is git-ignored. Delete
  `data/` to reset to fresh sample data.

## Docusign Agreement Manager notes

- The app targets the Agreement Manager (IAM) API at
  `https://api-d.docusign.com/v1` (demo) / `https://api.docusign.com/v1` (prod).
- Docusign's IAM SDK ships for C# and TypeScript only. This project calls the
  REST endpoints directly from `agreement_manager_client.py`; the paths and
  request shapes mirror the IAM SDK operations.
- OAuth tokens must request the Agreement Manager scopes (`AGM_SCOPES` in
  `config.py`) or the API returns an error. The same token works for both the
  eSignature and Agreement Manager APIs.
- Two OAuth flows are supported: JWT Grant (`request_jwt_token`) and
  Authorization Code Grant (`authorization_url` / `exchange_code_for_token`).
  The session token takes precedence; JWT is the fallback for a session that has
  none.
- Access tokens are stored server-side in `_SESSIONS` in `main.py`. The browser
  cookie is an itsdangerous-signed opaque session id only. Never put tokens in
  cookies, responses, or logs.
- Bulk upload is a three-step flow: create a job (`POST /upload/jobs`), PUT each
  document to its pre-signed URL, then mark the job complete
  (`POST /upload/jobs/{id}/actions/complete`). Poll `GET /upload/jobs/{id}` for
  status.
- Agreement Manager statuses (`PENDING`, `COMPLETE`, `INACTIVE`) are mapped to
  local statuses in `_map_agm_status` (`COMPLETE` -> `complete`, etc.).
- Downloads use the agreement's `_links.document.href`. If Agreement Manager is
  unreachable the download route falls back to the locally cached original.

## Testing guidance

- `tests/test_app.py` drives the real app with `fastapi.testclient.TestClient`
  against an isolated temp database (see the `client` fixture). Helpers
  `connect()` and `install_transport()` attach a session token and route all
  Docusign HTTP through `httpx.MockTransport`.
- `tests/test_agreement_manager_client.py` exercises the real client code;
  Docusign HTTP is intercepted with `httpx.MockTransport`. Prefer this over
  mocking the client itself, so request construction and error handling stay
  under test.
- Do not add tests that hit the live Docusign API.

## Security

- Secrets come from env/`.env` only. `.env`, `*.pem` and `data/` are git-ignored.
- Uploaded files for bulk upload are size-checked (<= 25 MB in this demo).
- Do not expose raw Docusign error payloads to the UI beyond the message field.

