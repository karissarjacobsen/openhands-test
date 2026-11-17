# AGENTS.md

Repository notes for AI agents and contributors working on Agreement Manager.

## What this is

A FastAPI + Jinja + SQLite web app for managing agreements and sending them for
signature through the Docusign eSignature REST API. See `README.md` for the
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
| `app/main.py` | All FastAPI routes: pages, OAuth, envelope actions, JSON API |
| `app/config.py` | Settings from env/`.env`; Docusign environment URL map |
| `app/docusign_client.py` | eSignature REST client (OAuth, envelopes, recipients, documents) |
| `app/db.py` | SQLite access layer, schema, agreement cache |
| `app/templates_data.py` | Sample agreement templates |
| `app/pdf.py` | Dependency-free PDF generator |
| `app/templates/*.html` | Jinja templates |
| `app/static/` | `css/app.css`, `js/app.js`, `js/agreement_form.js` |
| `tests/` | pytest suite |

## Conventions

- Python: type hints on public functions, `from __future__ import annotations`,
  dataclasses for structured config. Keep comments for non-obvious reasoning
  only; do not narrate the code.
- Templates: extend `base.html`; page routes pass an `active` key for nav state.
- Front-end: plain JavaScript, no build step. Shared helpers live on
  `window.App` (`toast`, `setLoading`, `postJSON`) in `static/js/app.js`.
- Data: SQLite via `app/db.py`. The `data/` directory (DB + uploads) is
  git-ignored. Delete `data/` to reset the local cache; agreements are reloaded
  from Docusign on the next sign-in.

## Docusign integration notes

- Two OAuth flows were supported before; only Authorization Code Grant
  (`authorization_url` / `exchange_code_for_token`) remains. JWT Grant was
  removed. Users sign in via `/login`; the session token is required for every
  Docusign call.
- Access tokens are stored server-side in `_SESSIONS` in `main.py`. The browser
  cookie is an itsdangerous-signed opaque session id only. Never put tokens in
  cookies, responses, or logs.
- Agreements are loaded from the signed-in user's Docusign account
  (`_sync_envelopes` calls `list_envelopes`, i.e. Envelopes:listStatusChanges).
  There is no local sample/dummy data.
- Envelopes use a `SignHere` tab with `anchorString="/sn1/"`. Generated template
  PDFs contain that anchor; uploaded PDFs must contain it too (otherwise
  Docusign places the tab at the top-left).
- Envelope statuses from Docusign are mapped to local statuses in
  `_map_envelope_status` (`created` -> `draft`, etc.).
- Document downloads: `combined` (all docs as one PDF), `certificate`
  (certificate of completion), `archive` (ZIP of everything). The download route
  falls back to the locally stored original if Docusign is unreachable.

## Testing guidance

- `tests/test_app.py` drives the real app with `fastapi.testclient.TestClient`
  against an isolated temp database (see the `client` fixture).
- `tests/test_docusign_client.py` exercises the real client code; Docusign HTTP
  is intercepted with `httpx.MockTransport`. Prefer this over mocking the client
  itself, so request construction and error handling stay under test.
- Do not add tests that hit the live Docusign API.

## Security

- Secrets come from env/`.env` only. `.env`, `*.pem` and `data/` are git-ignored.
- Uploaded files must be PDFs (magic-byte check) and <= 25 MB.
- Do not expose raw Docusign error payloads to the UI beyond the message field.
