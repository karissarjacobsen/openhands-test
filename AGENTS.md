# AGENTS.md

Repository notes for AI agents and contributors working on Agreement Manager.

## What this is

A FastAPI + Jinja + SQLite web app that reads agreements from the Docusign
**Agreement Manager** API and shows them in a dashboard. See `README.md` for the
product overview and user-facing setup.

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
| `app/main.py` | All FastAPI routes: pages, OAuth, sync/refresh actions, JSON API |
| `app/config.py` | Settings from env/`.env`; OAuth host and Agreement Manager base URL map |
| `app/agm_client.py` | Agreement Manager REST client (OAuth, agreements, documents) |
| `app/db.py` | SQLite cache of agreements and parties |
| `app/templates/*.html` | Jinja templates |
| `app/static/` | `css/app.css`, `js/app.js` |
| `tests/` | pytest suite |

## Conventions

- Python: type hints on public functions, `from __future__ import annotations`,
  dataclasses for structured config. Keep comments for non-obvious reasoning
  only; do not narrate the code.
- Templates: extend `base.html`; page routes pass an `active` key for nav state.
- Front-end: plain JavaScript, no build step. Shared helpers live on
  `window.App` (`toast`, `setLoading`, `postJSON`) in `static/js/app.js`.
- Data: SQLite via `app/db.py`. The `data/` directory is git-ignored. Delete
  `data/` to reset the cache.

## Agreement Manager integration notes

- The API base URL is `https://api-d.docusign.com/v1` (demo) or
  `https://api.docusign.com/v1` (prod), built from `Settings.agm_base_url`.
- Two OAuth flows are supported: JWT Grant (`request_jwt_token`) and
  Authorization Code Grant (`authorization_url` / `exchange_code_for_token`).
  Both request the Agreement Manager scopes `adm_store_unified_repo_read` and
  `public_dms_document_read` (plus `impersonation` for JWT).
- Access tokens are stored server-side in `_SESSIONS` in `main.py`. The browser
  cookie is an itsdangerous-signed opaque session id only. Never put tokens in
  cookies, responses, or logs.
- `AgreementManagerClient.list_agreements` follows the `_links.next` pagination
  link and returns a flat list.
- Agreement statuses from Agreement Manager are mapped to local statuses in
  `_map_agm_status` (`COMPLETE` -> `complete`, `INACTIVE` -> `inactive`, etc.).
- Document downloads use the agreement's `download_url`. The bearer token is
  attached only for `docusign.com` / `docusign.net` hosts, never for external
  (e.g. pre-signed object store) URLs.
- The SQLite database is a cache, not a source of truth. The dashboard syncs on
  load; the detail page refreshes one agreement (with parties).

## Testing guidance

- `tests/test_app.py` drives the real app with `fastapi.testclient.TestClient`
  against an isolated temp database (see the `client` fixture) and a fake
  Agreement Manager HTTP surface (see the `agm` fixture).
- `tests/test_agm_client.py` exercises the real client code; Docusign HTTP is
  intercepted with `httpx.MockTransport`. Prefer this over mocking the client
  itself, so request construction and error handling stay under test.
- Do not add tests that hit the live Docusign API.

## Security

- Secrets come from env/`.env` only. `.env`, `*.pem` and `data/` are git-ignored.
- Do not expose raw Docusign error payloads to the UI beyond the message field.
