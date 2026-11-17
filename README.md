# Agreement Manager

A small web app that demonstrates the Docusign **Agreement Manager** (IAM) API.
It lists agreements, imports them into a local cache, patches and re-types them,
runs bulk uploads, manages custom provisions, and downloads agreement
documents. Built with FastAPI, server-rendered Jinja templates and a SQLite
cache.

## What it does

Docusign Agreement Manager is the system of record. The app calls every
Agreement Manager workflow:

- **Agreements list** (`GET /agreements`) with `$search`, `$filter` and `$select`.
- **Agreement detail** (`GET /agreements/{agreementId}`) with parties and provisions.
- **Agreement types** (`GET /agreement-types`).
- **Patch agreement** (`PATCH /agreements/{agreementId}`).
- **Patch by document id** (`PATCH /agreements/documents/{documentId}`).
- **Change agreement type** (`PATCH /agreements/{agreementId}/actions/change-type`).
- **Delete agreement** (`DELETE /agreements/{agreementId}`).
- **Download document** via the agreement's `_links.document.href`.
- **Bulk upload**: create a job, upload documents to the pre-signed URLs, mark the
  job complete, then poll job status (`/upload/jobs`).
- **Custom provisions**: create, list, update, delete, and add/remove them on an
  agreement type.

Imported agreements are cached locally so the dashboard is fast and works while
disconnected. The local cache also stores the bulk jobs you start.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in your Docusign credentials (see below)
./run.sh                    # serves on http://localhost:12000
```

Open http://localhost:12000. The app seeds five sample agreements on first run
so the dashboard is never empty. You can browse the cached agreements without
credentials; you need credentials to call Agreement Manager.

## Docusign setup

1. Create an app at <https://developers.docusign.com/platform/apps-and-keys>.
2. Copy the **integration key** (client ID) into `DOCUSIGN_INTEGRATION_KEY`.
3. Add a **redirect URI** matching `DOCUSIGN_REDIRECT_URI`
   (default `http://localhost:12000/oauth/callback`).

Then pick one OAuth flow:

**Authorization Code Grant** (interactive, recommended for hosted apps)
- Copy the **secret key** into `DOCUSIGN_CLIENT_SECRET`.
- Click *Connect to Docusign* on the Settings page and grant consent.

**JWT Grant** (non-interactive, for service integrations)
- Add an RSA key pair to the app, save the private key, and set
  `DOCUSIGN_PRIVATE_KEY_PATH`.
- Set `DOCUSIGN_USER_ID` to the API user GUID from *My Account Information*.
- Grant consent once by visiting the consent URL shown in the Docusign docs
  (`response_type=code&scope=signature%20impersonation&client_id=...`).

### Agreement Manager scopes

The OAuth token must request the Agreement Manager scopes or the API calls fail.
The app requests them automatically (see `AGM_SCOPES` in `app/config.py`):

| Scope | Used for |
|---|---|
| `adm_store_unified_repo_read` | reading agreements |
| `adm_store_unified_repo_write` | patch / change-type / delete |
| `document_uploader_read` | bulk job status |
| `document_uploader_write` | bulk job create + complete |
| `public_dms_document_read` | downloading agreement documents |
| `search_read` | `$search` on the agreement list |

Credentials live in `.env` (git-ignored) or the environment. They are never
stored in the browser or committed. Access tokens are held server-side in an
in-memory session; the browser cookie carries only an opaque session id.

## How it is put together

```
app/
  main.py                      FastAPI routes (pages, OAuth, Agreement Manager actions, JSON API)
  config.py                    Environment / .env loading, Docusign URLs and scopes
  agreement_manager_client.py  Agreement Manager REST client (agreements, bulk, provisions)
  db.py                        SQLite cache + bulk job records + sample data
  pdf.py                       Minimal dependency-free PDF generator
  templates/                   Jinja templates
  static/                      CSS and JavaScript
tests/                         pytest suite
data/                          SQLite cache (git-ignored)
```

The Agreement Manager API is a REST API and Docusign's IAM SDK ships for C# and
TypeScript only, so `agreement_manager_client.py` calls the REST endpoints
directly. The request shapes and paths mirror the IAM SDK operations. Docs:
<https://developers.docusign.com/docs/agreement-manager-api/>.

Key endpoints:

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Dashboard (cached agreements) with search/filter |
| GET | `/agm` | Live Agreement Manager agreement list |
| GET | `/bulk` | Bulk upload jobs |
| GET | `/custom-provisions` | Custom provision definitions |
| GET | `/agreements/{id}` | Cached agreement detail with parties/provisions |
| GET | `/agreements/{id}/download` | Download the agreement document |
| GET | `/api/agm/agreements` | List agreements (Agreement Manager) |
| GET | `/api/agm/agreements/{agm_id}` | Get an agreement (Agreement Manager) |
| POST | `/api/agm/agreements/{agm_id}/import` | Import an agreement into the cache |
| GET | `/api/agm/agreement-types` | List configured agreement types |
| PATCH | `/api/agreements/{id}` | Patch the linked agreement |
| POST | `/api/agreements/{id}/change-type` | Change the agreement type |
| DELETE | `/api/agreements/{id}` | Delete from Agreement Manager |
| GET/POST/PATCH/DELETE | `/api/agm/custom-provisions` | Manage custom provisions |
| POST | `/api/agm/agreement-types/provisions` | Add/remove provisions on a type |
| POST | `/api/agm/bulk/jobs` | Create + run a bulk upload job |
| GET | `/api/agm/bulk/jobs/{id}` | Bulk job status |
| GET | `/oauth/connect`, `/oauth/callback` | OAuth flows |
| GET | `/settings` | Connection status |

The JSON API is used by the UI and is available for scripting.


## Tests

```bash
python -m pytest tests/ -v
```

The suite covers the UI flows, the Agreement Manager client (request building,
paths, bulk upload, error parsing) and the app routes. Docusign HTTP calls go
through httpx's `MockTransport`, so the real client code runs without touching
the network.

## Notes and limitations

- The in-memory session store suits a single-process demo. Use a shared store
  (Redis, database) and a fixed `APP_SESSION_SECRET` for multi-process hosting.
- The local SQLite database is a cache of agreements imported from Agreement
  Manager, plus the bulk jobs you start. Agreement Manager remains the system of
  record.
- Bulk uploads wait for AI extraction asynchronously; poll a job's status to see
  when it reaches `COMPLETE`.
- The demo environment has a 100-agreement limit per account. Bulk jobs support
  up to 10,000 documents but this demo caps each file at 25 MB.

