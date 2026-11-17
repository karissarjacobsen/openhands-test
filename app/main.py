"""Agreement Manager - FastAPI application.

A demo of the Docusign Agreement Manager (IAM) API. Every workflow the API
exposes is available from the UI or the JSON API:

  * Agreements list (``GET /agreements``) and single agreement (``GET /agreements/{id}``)
  * Patch agreement, change agreement type, delete agreement
  * Agreement types list and document download
  * Bulk upload (create job -> upload to pre-signed URLs -> complete -> status)
  * Custom provisions (create / list / update / delete, add / remove on a type)
  * OAuth: JWT Grant and Authorization Code Grant

Docusign is the system of record; the local SQLite database caches agreements
the user imported from Agreement Manager and the bulk jobs they started.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import __version__
from .agreement_manager_client import AgreementManagerClient, AgreementManagerError
from .config import AGM_SCOPES, load_settings
from .db import STATUSES, TERMINAL_STATUSES, Database, seed_sample_data
from .pdf import build_pdf

settings = load_settings()
db = Database(settings.db_path)
seed_sample_data(db)

app = FastAPI(title="Agreement Manager", version=__version__)
app.mount("/static", StaticFiles(directory=settings.base_dir / "app" / "static"), name="static")
templates = Jinja2Templates(directory=str(settings.base_dir / "app" / "templates"))

serializer = URLSafeTimedSerializer(settings.session_secret, salt="agreement-session")

# Server-side session store: the cookie only carries an opaque session id, so
# access tokens never leave the server.
_SESSIONS: dict[str, dict[str, Any]] = {}
_SESSION_LOCK = threading.Lock()
SESSION_COOKIE = "am_session"
SESSION_MAX_AGE = 8 * 60 * 60


# --------------------------------------------------------------------- session


def _create_session() -> str:
    session_id = secrets.token_urlsafe(24)
    with _SESSION_LOCK:
        _SESSIONS[session_id] = {"created": time.time()}
    return session_id


def _get_session(request: Request) -> dict[str, Any] | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        session_id = serializer.loads(raw, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None
    with _SESSION_LOCK:
        return _SESSIONS.get(session_id)


def _save_session(session_id: str, data: dict[str, Any]) -> None:
    with _SESSION_LOCK:
        _SESSIONS[session_id] = data


def _client_for_session(session: dict[str, Any] | None) -> AgreementManagerClient:
    """Build a client. Uses the session token if present, else JWT Grant."""
    token = (session or {}).get("access_token")
    return AgreementManagerClient(settings, access_token=token)


def _connected(session: dict[str, Any] | None) -> bool:
    return bool(session and session.get("access_token"))


def _templates_context(request: Request, **extra: Any) -> dict[str, Any]:
    session = _get_session(request)
    context: dict[str, Any] = {
        "request": request,
        "statuses": STATUSES,
        "docusign_env": settings.environment,
        "docusign_connected": _connected(session),
        "docusign_user": (session or {}).get("user_email", ""),
        "jwt_configured": settings.jwt_configured,
        "auth_code_configured": settings.auth_code_configured,
        "agm_scopes": AGM_SCOPES.split(),
        "version": __version__,
    }
    context.update(extra)
    return context


def _render(request: Request, name: str, **extra: Any) -> HTMLResponse:
    return templates.TemplateResponse(request, name, _templates_context(request, **extra))


# ------------------------------------------------------------- helpers / errors


def _agreement_or_404(agreement_id: str) -> dict[str, Any]:
    agreement = db.get_agreement(agreement_id)
    if agreement is None:
        raise HTTPException(status_code=404, detail="Agreement not found")
    return agreement


def _error_response(exc: Exception, status: int = 400) -> JSONResponse:
    if isinstance(exc, AgreementManagerError):
        message = exc.message
        details = exc.details
        if isinstance(details, dict):
            extra = details.get("detail") or details.get("message") or details.get("error_description")
            if extra and extra not in message:
                message = f"{message} {extra}".strip()
        status = exc.status_code or status
    else:
        message = str(exc) or exc.__class__.__name__
    return JSONResponse({"ok": False, "error": message}, status_code=status)


def _require_connected(session: dict[str, Any] | None) -> None:
    if not _connected(session) and not settings.jwt_configured:
        raise AgreementManagerError(
            "Not connected to Docusign. Connect on the Settings page or configure "
            "JWT Grant credentials in .env."
        )


def _agm_id_or_error(agreement: dict[str, Any]) -> str:
    agm_id = agreement.get("agm_id")
    if not agm_id:
        raise AgreementManagerError(
            "This agreement is not linked to an Agreement Manager record. Import it first."
        )
    return agm_id


# ------------------------------------------------------------ AGM data mapping

_STATUS_FROM_AGM = {
    "PENDING": "pending",
    "COMPLETE": "complete",
    "INACTIVE": "inactive",
}


def _map_agm_status(agm_status: str | None) -> str:
    if not agm_status:
        return "draft"
    return _STATUS_FROM_AGM.get(agm_status.upper(), "draft")


def _agm_to_local_fields(agm: dict[str, Any]) -> dict[str, Any]:
    """Map an Agreement Manager agreement onto our local column set."""
    provisions = agm.get("provisions") or {}
    parties = agm.get("parties") or []
    party_names = [
        p.get("name_in_agreement") or p.get("name", "") for p in parties if isinstance(p, dict)
    ]
    local_parties = [
        {"name": name, "role": "party", "routing_order": index}
        for index, name in enumerate([n for n in party_names if n], start=1)
    ]
    value = provisions.get("total_agreement_value") or 0
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = 0.0
    effective_date = provisions.get("effective_date") or ""
    if isinstance(effective_date, str) and "T" in effective_date:
        effective_date = effective_date.split("T", 1)[0]
    return {
        "agm_id": agm.get("id"),
        "title": agm.get("title") or agm.get("file_name") or "Untitled agreement",
        "client_name": (party_names[0] if party_names else "") or "",
        "agreement_type": agm.get("type") or "",
        "value": value,
        "currency": provisions.get("total_agreement_value_currency_code") or "USD",
        "status": _map_agm_status(agm.get("status")),
        "agm_status": agm.get("status") or "",
        "summary": agm.get("summary") or "",
        "effective_date": effective_date,
        "document_name": agm.get("file_name"),
        "provisions": provisions,
        "parties": local_parties,
    }


def _upsert_agm_agreement(agm: dict[str, Any]) -> dict[str, Any]:
    """Insert or update the local cache record for an Agreement Manager agreement."""
    fields = _agm_to_local_fields(agm)
    parties = fields.pop("parties")
    provisions = fields.pop("provisions")

    existing = db.get_agreement_by_agm_id(fields["agm_id"])
    if existing is None:
        agreement = db.create_agreement(parties=parties, provisions=provisions, **fields)
    else:
        fields["provisions_json"] = json.dumps(provisions, default=str)
        agreement = db.update_agreement(existing["id"], **fields)
        db.replace_parties(existing["id"], parties)
    assert agreement is not None
    return agreement


# --------------------------------------------------------------------- pages


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    q: str = "",
    status: str = "",
    type: str = "",
) -> HTMLResponse:
    agreements = db.list_agreements(
        search=q or None, status=status or None, agreement_type=type or None
    )
    counts = db.status_counts()
    total_value = sum(a["value"] for a in agreements)
    return _render(
        request,
        "dashboard.html",
        active="dashboard",
        agreements=agreements,
        counts=counts,
        total_value=total_value,
        types=db.distinct_types(),
        filters={"q": q, "status": status, "type": type},
    )


@app.get("/agm", response_class=HTMLResponse)
def agm_list_page(request: Request, search: str = "") -> HTMLResponse:
    """Live Agreement Manager agreement list (Agreements:getAgreementsList)."""
    session = _get_session(request)
    agreements: list[dict[str, Any]] = []
    error = ""
    total = 0
    try:
        _require_connected(session)
        client = _client_for_session(session)
        payload = client.list_agreements(search=search or None)
        agreements = payload.get("data", []) if isinstance(payload, dict) else []
        total = (payload.get("metadata") or {}).get("total_items", len(agreements))
    except AgreementManagerError as exc:
        error = exc.message
    return _render(
        request,
        "agm_list.html",
        active="agm",
        agm_agreements=agreements,
        total=total,
        search=search,
        error=error,
    )


@app.get("/bulk", response_class=HTMLResponse)
def bulk_page(request: Request) -> HTMLResponse:
    return _render(
        request,
        "bulk.html",
        active="bulk",
        bulk_jobs=db.list_bulk_jobs(),
    )


@app.get("/custom-provisions", response_class=HTMLResponse)
def custom_provisions_page(request: Request) -> HTMLResponse:
    session = _get_session(request)
    provisions: list[dict[str, Any]] = []
    error = ""
    try:
        _require_connected(session)
        client = _client_for_session(session)
        payload = client.list_custom_provisions()
        provisions = payload.get("data", []) if isinstance(payload, dict) else []
    except AgreementManagerError as exc:
        error = exc.message
    return _render(
        request,
        "custom_provisions.html",
        active="provisions",
        provisions=provisions,
        error=error,
    )


@app.get("/agreements/{agreement_id}", response_class=HTMLResponse)
def agreement_detail(request: Request, agreement_id: str) -> HTMLResponse:
    agreement = _agreement_or_404(agreement_id)
    return _render(
        request,
        "agreement_detail.html",
        active="dashboard",
        agreement=agreement,
        terminal_statuses=TERMINAL_STATUSES,
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    session = _get_session(request)
    return _render(
        request,
        "settings.html",
        active="settings",
        session=session or {},
        account_id=settings.account_id,
        redirect_uri=settings.redirect_uri,
        integration_key_set=bool(settings.integration_key),
    )


# ------------------------------------------------------------- Docusign: connect


@app.get("/oauth/connect")
def oauth_connect(request: Request):
    """Start Authorization Code Grant, or fall back to JWT Grant for the session."""
    session_id = request.cookies.get(SESSION_COOKIE)
    session = _get_session(request)
    if session is None:
        session_id = _create_session()
        session = {"created": time.time()}

    if settings.auth_code_configured:
        state = secrets.token_urlsafe(16)
        session["oauth_state"] = state
        _save_session(session_id, session)  # type: ignore[arg-type]
        client = AgreementManagerClient(settings)
        response = RedirectResponse(client.authorization_url(state), status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            serializer.dumps(session_id),
            httponly=True,
            samesite="lax",
            max_age=SESSION_MAX_AGE,
        )
        return response

    if settings.jwt_configured:
        return _connect_via_jwt(request, session_id, session)

    return RedirectResponse(
        "/settings?error="
        + quote(
            "Docusign is not configured. Add credentials to .env or configure an "
            "integration key with a client secret for Authorization Code Grant."
        ),
        status_code=303,
    )


def _connect_via_jwt(request: Request, session_id: str | None, session: dict[str, Any]):
    client = AgreementManagerClient(settings)
    try:
        token = client.request_jwt_token()
        info = client.get_user_info()
    except AgreementManagerError as exc:
        return RedirectResponse(
            "/settings?error=" + quote(exc.message), status_code=303
        )
    session_id = session_id or _create_session()
    session.update(
        {
            "access_token": token.access_token,
            "expires_at": token.expires_at,
            "auth_type": "jwt",
            "account_id": info.account_id,
            "user_email": info.email,
            "user_name": info.name,
        }
    )
    _save_session(session_id, session)
    response = RedirectResponse("/settings?connected=1", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        serializer.dumps(session_id),
        httponly=True,
        samesite="lax",
        max_age=SESSION_MAX_AGE,
    )
    return response


@app.get("/oauth/callback")
def oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return RedirectResponse(
            "/settings?error=" + quote(f"Docusign returned an error: {error}"),
            status_code=303,
        )
    session = _get_session(request)
    if session is None:
        return RedirectResponse(
            "/settings?error=" + quote("Session expired. Please try connecting again."),
            status_code=303,
        )
    if not state or state != session.get("oauth_state"):
        return RedirectResponse(
            "/settings?error=" + quote("OAuth state mismatch. Please try again."),
            status_code=303,
        )
    if not code:
        return RedirectResponse(
            "/settings?error=" + quote("Missing authorization code."),
            status_code=303,
        )
    client = AgreementManagerClient(settings)
    try:
        token = client.exchange_code_for_token(code)
        info = client.get_user_info()
    except AgreementManagerError as exc:
        return RedirectResponse(
            "/settings?error=" + quote(exc.message), status_code=303
        )
    session.update(
        {
            "access_token": token.access_token,
            "expires_at": token.expires_at,
            "auth_type": "authorization_code",
            "account_id": info.account_id,
            "user_email": info.email,
            "user_name": info.name,
            "oauth_state": "",
        }
    )
    session_id = serializer.loads(request.cookies[SESSION_COOKIE])
    _save_session(session_id, session)
    return RedirectResponse("/settings?connected=1", status_code=303)


@app.post("/oauth/disconnect")
def oauth_disconnect(request: Request):
    raw = request.cookies.get(SESSION_COOKIE)
    if raw:
        try:
            session_id = serializer.loads(raw, max_age=SESSION_MAX_AGE)
            with _SESSION_LOCK:
                _SESSIONS.pop(session_id, None)
        except BadSignature:
            pass
    response = RedirectResponse("/settings", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/api/connection")
def api_connection(request: Request):
    session = _get_session(request)
    return {
        "connected": _connected(session),
        "auth_type": (session or {}).get("auth_type", ""),
        "user_email": (session or {}).get("user_email", ""),
        "account_id": (session or {}).get("account_id", ""),
        "environment": settings.environment,
        "jwt_configured": settings.jwt_configured,
        "auth_code_configured": settings.auth_code_configured,
        "scopes": AGM_SCOPES.split(),
    }


# ------------------------------------------------ Agreement Manager: agreements


@app.get("/api/agreements")
def api_list_agreements(q: str = "", status: str = "", type: str = ""):
    """List the locally cached agreements."""
    agreements = db.list_agreements(
        search=q or None, status=status or None, agreement_type=type or None
    )
    return {"ok": True, "agreements": agreements, "count": len(agreements)}


@app.get("/api/agreements/{agreement_id}")
def api_get_agreement(agreement_id: str):
    agreement = db.get_agreement(agreement_id)
    if agreement is None:
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
    return {"ok": True, "agreement": agreement}


@app.get("/api/agm/agreements")
def api_agm_list(request: Request, search: str = "", filter: str = "", select: str = ""):
    """Agreements:getAgreementsList - live list from Agreement Manager."""
    session = _get_session(request)
    try:
        _require_connected(session)
        client = _client_for_session(session)
        payload = client.list_agreements(
            search=search or None, filter_expr=filter or None, select=select or None
        )
        if isinstance(payload, list):
            return {"ok": True, "data": payload, "metadata": {}}
        return {
            "ok": True,
            "data": payload.get("data", []),
            "metadata": payload.get("metadata", {}),
        }
    except AgreementManagerError as exc:
        return _error_response(exc)


@app.get("/api/agm/agreements/{agm_id}")
def api_agm_get(request: Request, agm_id: str):
    """Agreements:getAgreement - full detail from Agreement Manager."""
    session = _get_session(request)
    try:
        _require_connected(session)
        client = _client_for_session(session)
        return {"ok": True, "agreement": client.get_agreement(agm_id)}
    except AgreementManagerError as exc:
        return _error_response(exc)


@app.post("/api/agm/agreements/{agm_id}/import")
def api_agm_import(request: Request, agm_id: str):
    """Fetch an agreement from Agreement Manager and cache it locally."""
    session = _get_session(request)
    try:
        _require_connected(session)
        client = _client_for_session(session)
        agm = client.get_agreement(agm_id)
        agreement = _upsert_agm_agreement(agm)
        db.add_event(
            agreement["id"], "imported", f"Imported agreement {agm_id} from Agreement Manager"
        )
        return {"ok": True, "agreement": db.get_agreement(agreement["id"])}
    except AgreementManagerError as exc:
        return _error_response(exc)


@app.get("/api/agm/agreement-types")
def api_agm_agreement_types(request: Request):
    """Agreements:getAgreementTypes - configured agreement types for the account."""
    session = _get_session(request)
    try:
        _require_connected(session)
        client = _client_for_session(session)
        return {"ok": True, "agreement_types": client.get_agreement_types()}
    except AgreementManagerError as exc:
        return _error_response(exc)


@app.post("/api/agreements/{agreement_id}/refresh")
def refresh_from_agm(request: Request, agreement_id: str):
    """Re-fetch the linked agreement from Agreement Manager and update the cache."""
    agreement = _agreement_or_404(agreement_id)
    session = _get_session(request)
    try:
        _require_connected(session)
        agm_id = _agm_id_or_error(agreement)
        client = _client_for_session(session)
        agm = client.get_agreement(agm_id)
        updated = _upsert_agm_agreement(agm)
        db.add_event(agreement_id, "status", "Refreshed from Agreement Manager")
        return {"ok": True, "agreement": db.get_agreement(updated["id"])}
    except AgreementManagerError as exc:
        db.add_event(agreement_id, "error", exc.message)
        return _error_response(exc)


@app.patch("/api/agreements/{agreement_id}")
async def patch_agreement(request: Request, agreement_id: str):
    """Agreements:patchAgreement - update fields/provisions of the linked agreement."""
    agreement = _agreement_or_404(agreement_id)
    session = _get_session(request)
    try:
        _require_connected(session)
        agm_id = _agm_id_or_error(agreement)
        body = await request.json()
        if not isinstance(body, dict):
            raise AgreementManagerError("Request body must be a JSON object.")
        client = _client_for_session(session)
        client.patch_agreement(agm_id, body)

        local_updates: dict[str, Any] = {}
        if "title" in body:
            local_updates["title"] = body["title"]
        if "summary" in body:
            local_updates["summary"] = body["summary"]
        if "status" in body:
            local_updates["status"] = _map_agm_status(body["status"])
            local_updates["agm_status"] = body["status"]
        if isinstance(body.get("provisions"), dict):
            local_updates["provisions_json"] = json.dumps(body["provisions"], default=str)
        if local_updates:
            db.update_agreement(agreement_id, **local_updates)
        db.add_event(agreement_id, "patched", "Agreement patched via Agreement Manager")
        return {"ok": True, "agreement": db.get_agreement(agreement_id)}
    except (AgreementManagerError, ValueError) as exc:
        db.add_event(agreement_id, "error", getattr(exc, "message", str(exc)))
        return _error_response(exc)


@app.post("/api/agreements/{agreement_id}/change-type")
async def change_agreement_type(request: Request, agreement_id: str):
    """Agreements:changeAgreementType - change the agreement's type."""
    agreement = _agreement_or_404(agreement_id)
    session = _get_session(request)
    try:
        _require_connected(session)
        agm_id = _agm_id_or_error(agreement)
        body = await request.json()
        new_type = (body or {}).get("type", "").strip()
        if not new_type:
            raise AgreementManagerError("A target agreement type is required.")
        client = _client_for_session(session)
        result = client.change_agreement_type(agm_id, new_type)
        db.update_agreement(agreement_id, agreement_type=new_type)
        db.add_event(agreement_id, "type", f"Agreement type changed to '{new_type}'")
        return {"ok": True, "agreement": db.get_agreement(agreement_id), "result": result}
    except (AgreementManagerError, ValueError) as exc:
        db.add_event(agreement_id, "error", getattr(exc, "message", str(exc)))
        return _error_response(exc)


@app.delete("/api/agreements/{agreement_id}")
def api_delete_agreement(request: Request, agreement_id: str):
    """Agreements:deleteAgreement - delete from Agreement Manager and locally."""
    agreement = _agreement_or_404(agreement_id)
    session = _get_session(request)
    try:
        _require_connected(session)
        agm_id = _agm_id_or_error(agreement)
        client = _client_for_session(session)
        client.delete_agreement(agm_id)
        db.delete_agreement(agreement_id)
        return {"ok": True}
    except AgreementManagerError as exc:
        return _error_response(exc)


@app.post("/api/agreements/{agreement_id}/delete-from-agm")
def delete_agreement_from_agm(request: Request, agreement_id: str):
    """Form-friendly variant of the DELETE endpoint (HTML forms cannot send DELETE)."""
    agreement = _agreement_or_404(agreement_id)
    session = _get_session(request)
    try:
        _require_connected(session)
        agm_id = _agm_id_or_error(agreement)
        client = _client_for_session(session)
        client.delete_agreement(agm_id)
        db.delete_agreement(agreement_id)
        return RedirectResponse("/?deleted=1", status_code=303)
    except AgreementManagerError as exc:
        return RedirectResponse(
            f"/agreements/{agreement_id}?error={quote(exc.message)}", status_code=303
        )


@app.post("/agreements/{agreement_id}/delete")
def delete_agreement_local(agreement_id: str) -> RedirectResponse:
    """Remove the agreement from the local cache only."""
    _agreement_or_404(agreement_id)
    db.delete_agreement(agreement_id)
    return RedirectResponse("/", status_code=303)


@app.get("/agreements/{agreement_id}/download")
def download_document(request: Request, agreement_id: str):
    """Download the agreement's document from Agreement Manager (or the local copy)."""
    agreement = _agreement_or_404(agreement_id)
    session = _get_session(request)

    if agreement.get("agm_id"):
        try:
            _require_connected(session)
            client = _client_for_session(session)
            content = client.download_document(agreement["agm_id"])
            filename = agreement.get("document_name") or f"{agreement['title']}.pdf"
            return Response(
                content=content,
                media_type="application/pdf",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        except AgreementManagerError as exc:
            db.add_event(agreement_id, "error", f"Download failed: {exc.message}")
            # Fall through to the local copy if we have one.

    if agreement.get("document_path") and Path(agreement["document_path"]).exists():
        content = Path(agreement["document_path"]).read_bytes()
        filename = agreement.get("document_name") or "agreement.pdf"
        return Response(
            content=content,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    raise HTTPException(status_code=404, detail="No document available to download")


# ------------------------------------------- Agreement Manager: custom provisions


@app.get("/api/agm/custom-provisions")
def api_list_custom_provisions(request: Request):
    session = _get_session(request)
    try:
        _require_connected(session)
        client = _client_for_session(session)
        payload = client.list_custom_provisions()
        return {"ok": True, "data": payload.get("data", [])}
    except AgreementManagerError as exc:
        return _error_response(exc)


@app.post("/api/agm/custom-provisions")
async def api_create_custom_provision(request: Request):
    session = _get_session(request)
    try:
        _require_connected(session)
        body = await request.json()
        if not isinstance(body, dict):
            raise AgreementManagerError("Request body must be a JSON object.")
        client = _client_for_session(session)
        result = client.create_custom_provision(body)
        return {"ok": True, "provision": result}
    except (AgreementManagerError, ValueError) as exc:
        return _error_response(exc)


@app.patch("/api/agm/custom-provisions")
async def api_update_custom_provisions(request: Request):
    session = _get_session(request)
    try:
        _require_connected(session)
        body = await request.json()
        client = _client_for_session(session)
        return {"ok": True, "result": client.update_custom_provisions(body)}
    except (AgreementManagerError, ValueError) as exc:
        return _error_response(exc)


@app.delete("/api/agm/custom-provisions")
async def api_delete_custom_provisions(request: Request):
    session = _get_session(request)
    try:
        _require_connected(session)
        body = await request.json()
        client = _client_for_session(session)
        return {"ok": True, "result": client.delete_custom_provisions(body)}
    except (AgreementManagerError, ValueError) as exc:
        return _error_response(exc)


@app.post("/api/agm/agreement-types/provisions")
async def api_agreement_type_provisions(request: Request):
    """Add or remove provisions on one or more agreement types."""
    session = _get_session(request)
    try:
        _require_connected(session)
        body = await request.json()
        action = (body or {}).get("action", "add")
        payload = {k: v for k, v in (body or {}).items() if k != "action"}
        client = _client_for_session(session)
        if action == "remove":
            result = client.remove_agreement_type_provisions(payload)
        else:
            result = client.add_agreement_type_provisions(payload)
        return {"ok": True, "result": result}
    except (AgreementManagerError, ValueError) as exc:
        return _error_response(exc)


# ------------------------------------------------ Agreement Manager: bulk upload


@app.post("/api/agm/bulk/jobs")
async def create_bulk_job(
    request: Request,
    job_name: str = Form(...),
    language: str = Form("en-US"),
    expected_number_of_docs: str = Form("0"),
    files: list[UploadFile] = File(default=[]),
):
    """Create a bulk upload job, upload each document, then mark it complete."""
    session = _get_session(request)
    try:
        _require_connected(session)
        job_name = job_name.strip()
        if not job_name:
            raise AgreementManagerError("A job name is required.")
        uploads: list[UploadFile] = [f for f in files if f and f.filename]
        if not uploads:
            raise AgreementManagerError("Select at least one document to upload.")
        try:
            expected = int(expected_number_of_docs or 0) or len(uploads)
        except ValueError:
            expected = len(uploads)

        contents: list[tuple[str, bytes]] = []
        for upload in uploads:
            data = await upload.read()
            _validate_document(data, upload.filename or "document.pdf")
            contents.append((upload.filename or "document.pdf", data))

        client = _client_for_session(session)
        job = client.create_bulk_upload_job(
            job_name=job_name,
            expected_number_of_docs=expected,
            language=language or "en-US",
        )
        job_id = job.get("id")
        if not job_id:
            raise AgreementManagerError(
                "Agreement Manager did not return a bulk job id.", details=job
            )
        documents = (job.get("_embedded") or {}).get("documents", [])
        if len(documents) < len(contents):
            raise AgreementManagerError(
                "Agreement Manager returned fewer upload URLs than documents."
            )
        for (name, data), document in zip(contents, documents):
            upload_url = _upload_href(document)
            if not upload_url:
                raise AgreementManagerError("A document upload URL was missing.")
            client.upload_document(upload_url, data, file_name=name)

        client.complete_bulk_upload_job(job_id)

        local = db.create_bulk_job(
            name=job_name,
            language=language or "en-US",
            expected_docs=len(contents),
            job_id=job_id,
            status="UPLOAD_COMPLETE",
        )
        return {"ok": True, "job": local, "agm_job": job}
    except AgreementManagerError as exc:
        return _error_response(exc)


@app.get("/api/agm/bulk/jobs/{local_id}")
def get_bulk_job(request: Request, local_id: str):
    """BulkJob:getBulkJobStatus - current status of a bulk upload job."""
    local = db.get_bulk_job(local_id)
    if local is None:
        return JSONResponse({"ok": False, "error": "Bulk job not found"}, status_code=404)
    session = _get_session(request)
    try:
        _require_connected(session)
        if not local.get("job_id"):
            raise AgreementManagerError("This bulk job has no Agreement Manager job id.")
        client = _client_for_session(session)
        status = client.get_bulk_job_status(local["job_id"])
        if status.get("status"):
            db.update_bulk_job(local_id, status=status["status"])
        return {"ok": True, "job": db.get_bulk_job(local_id), "agm_job": status}
    except AgreementManagerError as exc:
        return _error_response(exc)


def _upload_href(document: dict[str, Any]) -> str | None:
    actions = document.get("_actions") or {}
    upload = actions.get("upload_document")
    if isinstance(upload, dict):
        return upload.get("href")
    if isinstance(upload, str):
        return upload
    return None


def _validate_document(content: bytes, filename: str) -> None:
    if not content:
        raise AgreementManagerError(f"'{filename}' is empty.")
    if len(content) > 25 * 1024 * 1024:
        raise AgreementManagerError(f"'{filename}' exceeds the 25 MB limit.")


# ------------------------------------------------------------------- misc API


@app.get("/api/agm/sample-document")
def sample_document():
    """Generate a sample agreement PDF (handy for trying the bulk upload flow)."""
    pdf = build_pdf(
        "Sample Agreement",
        [
            ("Parties", "This sample agreement is between Example Inc. and Sample LLC."),
            ("Term", "The agreement runs for one year from the effective date."),
        ],
        footer="Generated by Agreement Manager for the bulk upload demo.",
    )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": 'attachment; filename="sample-agreement.pdf"',
            "Content-Length": str(len(pdf)),
        },
    )


@app.get("/health")
def health():
    return {"ok": True, "version": __version__, "environment": settings.environment}
