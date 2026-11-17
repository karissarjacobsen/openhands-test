# Agreement Manager

A small web app for browsing agreements held in the Docusign **Agreement
Manager** repository. Built with FastAPI, server-rendered Jinja templates and a
SQLite cache.

## What it does

- Dashboard listing every agreement in your Agreement Manager account, with
  status counters and search/filtering by text, status and type.
- Agreement detail view showing the agreement metadata, its parties and the
  provisions Docusign AI extracted from the document.
- Download the agreement document using the `download_url` Agreement Manager
  returns for the agreement.
- OAuth against Docusign: JWT Grant (service integration) and Authorization Code
  Grant (user consent). Requests the Agreement Manager scopes
  (`adm_store_unified_repo_read`, `public_dms_document_read`).

Every agreement shown in the UI comes from the Agreement Manager API. There is no
sample or dummy agreement data; before you sign in the dashboard is empty.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in your Docusign credentials (see below)
./run.sh                    # serves on http://localhost:12000
```

Open http://localhost:12000 and click **Connect to Agreement Manager**. The app
syncs your agreements from the Agreement Manager API on each dashboard load.

## Docusign setup

1. Create an app at <https://developers.docusign.com/platform/apps-and-keys>.
2. Copy the **integration key** (client ID) into `DOCUSIGN_INTEGRATION_KEY`.
3. Add a **redirect URI** matching `DOCUSIGN_REDIRECT_URI`
   (default `http://localhost:12000/oauth/callback`).
4. Add the Agreement Manager scopes to the app's requested scopes:
   `adm_store_unified_repo_read` (read agreements) and
   `public_dms_document_read` (download documents).

Then pick one OAuth flow:

**Authorization Code Grant** (interactive, recommended for hosted apps)
- Copy the **secret key** into `DOCUSIGN_CLIENT_SECRET`.
- Click *Connect to Agreement Manager* on the Settings page and grant consent.

**JWT Grant** (non-interactive, for service integrations)
- Add an RSA key pair to the app, save the private key, and set
  `DOCUSIGN_PRIVATE_KEY_PATH`.
- Set `DOCUSIGN_USER_ID` to the API user GUID from *My Account Information*.
- Grant consent once for the Agreement Manager scopes.

Credentials live in `.env` (git-ignored) or the environment. They are never
stored in the browser or committed. Access tokens are held server-side in an
in-memory session; the browser cookie carries only an opaque session id.

## Agreement Manager API

The app talks to the Agreement Manager REST API:

| Environment | Base URL |
|---|---|
| demo | `https://api-d.docusign.com/v1` |
| prod | `https://api.docusign.com/v1` |

Endpoints used:

| Method | Path | Purpose |
|---|---|---|
| GET | `/accounts/{accountId}/agreements` | List agreements (paginated) |
| GET | `/accounts/{accountId}/agreements/{agreementId}` | One agreement with parties |
| GET | `/accounts/{accountId}/agreement-types` | Configured agreement types |
| GET | `download_url` | Download the agreement document |

See <https://developers.docusign.com/docs/agreement-manager-api/> for the full
reference.

## How it is put together

```
app/
  main.py        FastAPI routes (pages, OAuth, sync/refresh actions, JSON API)
  config.py      Environment / .env loading and Agreement Manager base URLs
  agm_client.py  Agreement Manager REST client (OAuth, agreements, documents)
  db.py          SQLite cache of agreements and parties
  templates/     Jinja templates
  static/        CSS and JavaScript
tests/           pytest suite
data/            SQLite cache (git-ignored)
```

The SQLite database is a **cache**, not a source of truth. A dashboard load calls
`list_agreements` and upserts each agreement; opening an agreement refreshes it
(with parties) from `get_agreement`.

Key endpoints:

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Dashboard with search/filter |
| GET | `/agreements/{id}` | Detail, parties, provisions |
| POST | `/agreements/sync` | Pull all agreements from Agreement Manager |
| POST | `/agreements/{id}/refresh` | Refresh one agreement from Agreement Manager |
| GET | `/agreements/{id}/download` | Download the agreement document |
| POST | `/agreements/{id}/delete` | Remove an agreement from the local cache |
| GET | `/login`, `/oauth/connect`, `/oauth/callback` | OAuth flows |
| GET | `/settings` | Connection status |

The JSON API (`/api/agreements`, `/api/agreement-types`, `/api/connection`) is
used by the UI and is available for scripting.

## Tests

```bash
python -m pytest tests/ -v
```

The suite covers the dashboard, detail page, sync/refresh, downloads, the JSON
API and the client's URL building, pagination and status mapping. Docusign HTTP
calls go through httpx's `MockTransport`, so the real client code runs without
touching the network.

## Notes and limitations

- The in-memory session store suits a single-process demo. Use a shared store
  (Redis, database) and a fixed `APP_SESSION_SECRET` for multi-process hosting.
- The local cache is refreshed on demand. Add a scheduled sync or subscribe to
  Docusign Connect webhooks for near-real-time updates.
