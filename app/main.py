"""Agreement Manager - FastAPI application.

Agreements are read from the Docusign Agreement Manager API and cached locally so
the dashboard can render them. OAuth (JWT Grant + Authorization Code Grant) is
used to obtain the access token; every agreement shown in the UI comes from
Agreement Manager, never from local sample data.
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import __version__
from .agm_client import AgreementManagerClient, AgreementManagerError
from .config import load_settings
from .db import STATUSES, Database

settings = load_settings()
db = Database(settings.db_path)

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

# How many agreements to pull from Agreement Manager on a dashboard sync.
SYNC_LIMIT = 100


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
    """Build an Agreement Manager client, using the session token when present."""
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
        if exc.details and isinstance(exc.details, dict):
            extra = exc.details.get("message") or exc.details.get("error_description")
            if extra and extra not in message:
                message = f"{message} {extra}".strip()
    else:
        message = str(exc) or exc.__class__.__name__
    return JSONResponse({"ok": False, "error": message}, status_code=status)


def _require_connected(session: dict[str, Any] | None) -> None:
    if not _connected(session) and not settings.jwt_configured:
        raise AgreementManagerError(
            "Not connected to Agreement Manager. Connect on the Settings page or "
            "configure JWT Grant credentials in .env."
        )


# --------------------------------------------------- Agreement Manager: sync


def _agreement_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """Map an Agreement Manager agreement object onto our cache columns."""
    provisions = raw.get("provisions") or {}
    if not isinstance(provisions, dict):
        provisions = {}
    value = provisions.get("total_agreement_value") or 0
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = 0.0
    return {
        "id": str(raw.get("id") or raw.get("agreement_id") or ""),
        "title": raw.get("title") or raw.get("file_name") or "",
        "file_name": raw.get("file_name") or "",
        "agreement_type": raw.get("type") or "",
        "category": raw.get("category") or "",
        "status": _map_agm_status(raw.get("status")),
        "value": value,
        "currency": provisions.get("total_agreement_value_currency_code") or "",
        "effective_date": _date_only(provisions.get("effective_date")),
        "expiration_date": _date_only(provisions.get("expiration_date")),
        "source_name": raw.get("source_name") or "",
        "source_id": raw.get("source_id") or "",
        "download_url": raw.get("download_url") or "",
        "provisions": provisions,
        "created_at": (raw.get("metadata") or {}).get("created_at") or "",
    }


def _party_fields(raw: dict[str, Any]) -> list[dict[str, Any]]:
    parties = raw.get("parties") or []
    mapped: list[dict[str, Any]] = []
    for party in parties:
        if not isinstance(party, dict):
            continue
        mapped.append(
            {
                "party_id": str(party.get("id") or party.get("party_id") or ""),
                "name": party.get("name_in_agreement")
                or party.get("name")
                or party.get("party_name")
                or "",
                "email": party.get("email") or "",
                "role": party.get("role") or "party",
            }
        )
    return mapped


def _map_agm_status(status: Any) -> str:
    value = (status or "").strip().lower()
    mapping = {
        "pending": "pending",
        "in_progress": "pending",
        "complete": "complete",
        "completed": "complete",
        "inactive": "inactive",
        "expired": "inactive",
        "voided": "inactive",
    }
    return mapping.get(value, value or "pending")


def _date_only(value: Any) -> str:
    if not value:
        return ""
    text = str(value)
    if text.startswith("0001-01-01"):
        return ""
    return text[:10]


def _sync_agreements(session: dict[str, Any] | None, limit: int = SYNC_LIMIT) -> int:
    """Pull agreements from Agreement Manager into the local cache."""
    client = _client_for_session(session)
    agreements = client.list_agreements(limit=limit)
    synced = 0
    for raw in agreements:
        fields = _agreement_fields(raw)
        if not fields["id"]:
            continue
        # The list endpoint returns summaries without parties; keep any parties
        # already cached from a detail fetch.
        db.upsert_agreement(fields)
        synced += 1
    return synced


def _refresh_agreement(session: dict[str, Any] | None, agreement_id: str) -> dict[str, Any]:
    """Fetch one agreement (with parties) from Agreement Manager and cache it."""
    client = _client_for_session(session)
    raw = client.get_agreement(agreement_id)
    fields = _agreement_fields(raw)
    if not fields["id"]:
        fields["id"] = agreement_id
    return db.upsert_agreement(fields, parties=_party_fields(raw))


# --------------------------------------------------------------------- pages


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    q: str = "",
    status: str = "",
    type: str = "",
) -> HTMLResponse:
    session = _get_session(request)
    sync_error = ""
    synced = 0
    if _connected(session) or settings.jwt_configured:
        try:
            synced = _sync_agreements(session)
        except AgreementManagerError as exc:
            sync_error = exc.message

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
        sync_error=sync_error,
        synced=synced,
    )


@app.get("/agreements/{agreement_id}", response_class=HTMLResponse)
def agreement_detail(request: Request, agreement_id: str) -> HTMLResponse:
    session = _get_session(request)
    detail_error = ""
    if _connected(session) or settings.jwt_configured:
        try:
            _refresh_agreement(session, agreement_id)
        except AgreementManagerError as exc:
            detail_error = exc.message
    agreement = _agreement_or_404(agreement_id)
    return _render(
        request,
        "agreement_detail.html",
        active="dashboard",
        agreement=agreement,
        detail_error=detail_error,
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
        agm_base_url=settings.agm_base_url,
    )


@app.post("/agreements/{agreement_id}/delete")
def delete_agreement(agreement_id: str) -> RedirectResponse:
    """Remove an agreement from the local cache (does not touch Agreement Manager)."""
    _agreement_or_404(agreement_id)
    db.delete_agreement(agreement_id)
    return RedirectResponse("/", status_code=303)


# --------------------------------------------------- Agreement Manager: connect


@app.get("/login")
def login(request: Request):
    """Start the sign-in flow."""
    return oauth_connect(request)


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
            "base_uri": info.base_uri,
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
            "base_uri": info.base_uri,
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
        "agm_base_url": settings.agm_base_url,
        "jwt_configured": settings.jwt_configured,
        "auth_code_configured": settings.auth_code_configured,
    }


# ----------------------------------------------------- Agreement Manager: read


@app.post("/agreements/{agreement_id}/refresh")
def refresh_agreement(request: Request, agreement_id: str):
    _agreement_or_404(agreement_id)
    session = _get_session(request)
    try:
        _require_connected(session)
        updated = _refresh_agreement(session, agreement_id)
        return {"ok": True, "agreement": updated}
    except AgreementManagerError as exc:
        return _error_response(exc)


@app.post("/agreements/sync")
def sync_agreements(request: Request):
    session = _get_session(request)
    try:
        _require_connected(session)
        synced = _sync_agreements(session)
        return {"ok": True, "synced": synced}
    except AgreementManagerError as exc:
        return _error_response(exc)


@app.get("/agreements/{agreement_id}/download")
def download_document(request: Request, agreement_id: str):
    """Download the agreement document using its Agreement Manager download URL."""
    agreement = _agreement_or_404(agreement_id)
    session = _get_session(request)
    try:
        _require_connected(session)
        if not agreement.get("download_url"):
            _refresh_agreement(session, agreement_id)
            agreement = _agreement_or_404(agreement_id)
        download_url = agreement.get("download_url")
        if not download_url:
            raise AgreementManagerError(
                "Agreement Manager did not return a download URL for this agreement."
            )
        client = _client_for_session(session)
        content = client.download_document(download_url)
        filename = agreement.get("file_name") or f"{agreement['id']}.pdf"
        if not filename.lower().endswith(".pdf"):
            filename = f"{filename}.pdf"
        return Response(
            content=content,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except AgreementManagerError as exc:
        return _error_response(exc)


# ------------------------------------------------------------------- API (JSON)


@app.get("/api/agreements")
def api_list_agreements(
    request: Request, q: str = "", status: str = "", type: str = "", sync: int = 0
):
    if sync:
        session = _get_session(request)
        try:
            _require_connected(session)
            _sync_agreements(session)
        except AgreementManagerError as exc:
            return _error_response(exc)
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


@app.get("/api/agreement-types")
def api_agreement_types(request: Request):
    session = _get_session(request)
    try:
        _require_connected(session)
        client = _client_for_session(session)
        return {"ok": True, "agreement_types": client.list_agreement_types()}
    except AgreementManagerError as exc:
        return _error_response(exc)


@app.get("/health")
def health():
    return {"ok": True, "version": __version__, "environment": settings.environment}
