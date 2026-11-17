# Agreement Manager

A small web app for managing agreements and sending them for signature with the
Docusign eSignature REST API. Built with FastAPI, server-rendered Jinja
templates and a SQLite database.

## What it does

**Phase 1 — Basic UI**
- Dashboard with agreement status counters.
- Agreement detail view with recipients, status and an activity timeline.
- Form for creating agreements (upload a PDF or generate one from a template).

**Phase 2 — Docusign integration**
- OAuth: Authorization Code Grant (user sign-in and consent).
- Connects to the Docusign developer (demo) environment.
- Uploads a PDF and creates an envelope, then sends it to recipients.

**Phase 3 — Agreement tracking**
- Loads agreements from the signed-in user's Docusign account (no dummy data).
- Retrieves envelope status and per-recipient status from Docusign.
- Shows recipient and completion information on the detail page.
- Downloads completed documents (combined PDF, certificate, or full archive).

**Phase 4 — Polish**
- Search and filtering on the dashboard (by text, status and type).
- Four sample agreement templates that generate ready-to-send PDFs.
- Error handling and loading states throughout the UI.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in your Docusign credentials (see below)
./run.sh                    # serves on http://localhost:12000
```

Open http://localhost:12000. Sign in with Docusign to load your agreements from
your account — the app reads envelopes from Docusign and does not ship any sample
data. You can create and browse agreements before signing in, but you need
credentials to sign in, load your account, and send envelopes.

## Docusign setup

1. Create an app at <https://developers.docusign.com/platform/apps-and-keys>.
2. Copy the **integration key** (client ID) into `DOCUSIGN_INTEGRATION_KEY`.
3. Copy the **secret key** into `DOCUSIGN_CLIENT_SECRET`.
4. Add a **redirect URI** matching `DOCUSIGN_REDIRECT_URI`
   (default `http://localhost:12000/oauth/callback`).

Then sign in:

**Authorization Code Grant** (interactive)
- Click *Sign in with Docusign* (or the *Sign in with Docusign* button in the
  header) and grant consent. The app stores the access token server-side and
  loads your envelopes from the account.

Credentials live in `.env` (git-ignored) or the environment. They are never
stored in the browser or committed. Access tokens are held server-side in an
in-memory session; the browser cookie carries only an opaque session id.

### Sending an envelope

The envelope includes a `SignHere` tab anchored on the text `/sn1/`. Documents
must contain that string where the signature should appear. Template-generated
PDFs include it automatically; when uploading your own PDF, add `/sn1/` on the
signature line or Docusign places the tab at the top-left of the first page.

## How it is put together

```
app/
  main.py             FastAPI routes (pages, OAuth, envelope actions, JSON API)
  config.py           Environment / .env loading and Docusign environment URLs
  docusign_client.py  eSignature REST client (OAuth, envelopes, documents)
  db.py               SQLite persistence for the Docusign agreement cache
  templates_data.py   Sample agreement templates
  pdf.py              Minimal dependency-free PDF generator
  templates/          Jinja templates
  static/             CSS and JavaScript
tests/                pytest suite
data/                 SQLite DB and uploaded documents (git-ignored)
```

Key endpoints:

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Dashboard with search/filter |
| GET | `/agreements/new` | Create form |
| GET | `/agreements/{id}` | Detail, recipients, activity |
| POST | `/agreements` | Create agreement (upload/template) |
| POST | `/agreements/{id}/send` | Create + send envelope |
| POST | `/agreements/{id}/refresh` | Pull status from Docusign |
| GET | `/agreements/{id}/download` | Download combined/signed PDF |
| GET | `/agreements/{id}/download/archive` | Download ZIP archive |
| GET | `/login` | Start Docusign sign-in (Authorization Code Grant) |
| GET | `/oauth/connect`, `/oauth/callback` | OAuth flow |
| GET | `/settings` | Connection status |

The JSON API (`/api/agreements`, `/api/templates`, `/api/connection`) is used by
the UI and is available for scripting.

## Tests

```bash
python -m pytest tests/ -v
```

The suite covers the UI flows, the create form (template and upload paths),
error handling, loading agreements from the Docusign account, and the Docusign
client's Authorization Code exchange, envelope construction and API error
parsing. Docusign HTTP calls in the client tests go through httpx's
`MockTransport`, so the real client code runs without touching the network.

## Notes and limitations

- The in-memory session store suits a single-process demo. Use a shared store
  (Redis, database) and a fixed `APP_SESSION_SECRET` for multi-process hosting.
- Status refresh is on demand. For production, subscribe to Docusign Connect
  webhooks instead of polling.
- The demo environment sends real signing emails to real addresses, so use
  addresses you control when testing.
