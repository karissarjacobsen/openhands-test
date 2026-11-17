"""Agreement Manager - FastAPI application.

Phases implemented:
  1. Dashboard, agreement detail/status views, create form.
  2. Docusign OAuth (Authorization Code Grant), PDF upload, envelope create/send.
  3. Envelope status refresh, recipient/completion info, completed document download.
  4. Search/filtering, sample templates, error handling and loading states.

Agreements are loaded from the signed-in user's Docusign account; there is no
local sample data. Signing in requires Authorization Code Grant credentials.
"""

from __future__ import annotations

import base64
import io
import secrets
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import __version__
from .config import load_settings
from .db import STATUSES, TERMINAL_STATUSES, Database
from .docusign_client import DocusignClient, DocusignError, encode_document
from .templates_data import get_template, list_templates

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

# How far back to pull envelopes from the user's Docusign account.
SYNC_LOOKBACK_DAYS = 90


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


def _client_for_session(session: dict[str, Any] | None) -> DocusignClient:
    """Build a Docusign client from the session's Authorization Code token."""
    token = (session or {}).get("access_token")
    return DocusignClient(settings, access_token=token)


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
    if isinstance(exc, DocusignError):
        message = exc.message
        if exc.details and isinstance(exc.details, dict):
            extra = exc.details.get("message") or exc.details.get("error_description")
            if extra and extra not in message:
                message = f"{message} {extra}".strip()
    else:
        message = str(exc) or exc.__class__.__name__
    return JSONResponse({"ok": False, "error": message}, status_code=status)


def _require_connected(session: dict[str, Any] | None) -> None:
    if not _connected(session):
        raise DocusignError(
            "Not connected to Docusign. Sign in with Docusign to continue."
        )


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
    if _connected(session):
        try:
            _sync_envelopes(session)
        except DocusignError as exc:
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
    )


@app.get("/agreements/new", response_class=HTMLResponse)
def new_agreement_form(request: Request) -> HTMLResponse:
    return _render(
        request,
        "agreement_form.html",
        active="new",
        templates_list=list_templates(),
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


# --------------------------------------------------------------- create / CRUD


@app.post("/agreements")
async def create_agreement(
    request: Request,
    title: str = Form(...),
    client_name: str = Form(""),
    agreement_type: str = Form("Sales Agreement"),
    value: str = Form("0"),
    currency: str = Form("USD"),
    owner: str = Form(""),
    effective_date: str = Form(""),
    notes: str = Form(""),
    template_key: str = Form(""),
    recipient_name: list[str] = Form([]),
    recipient_email: list[str] = Form([]),
    recipient_role: list[str] = Form([]),
    document: UploadFile | None = File(None),
    action: str = Form("save"),
):
    title = title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")

    recipients = [
        {
            "name": (name or "").strip(),
            "email": (email or "").strip(),
            "role": (role or "signer").strip(),
            "routing_order": index,
        }
        for index, (name, email, role) in enumerate(
            zip(recipient_name, recipient_email, recipient_role), start=1
        )
        if (name or "").strip() and (email or "").strip()
    ]

    try:
        numeric_value = float(value or 0)
    except ValueError:
        numeric_value = 0.0

    document_name: str | None = None
    document_path: str | None = None

    # Document precedence: explicit upload > generated template.
    if document is not None and document.filename:
        content = await document.read()
        _validate_pdf(content, document.filename)
        stored = settings.upload_dir / f"{uuid.uuid4().hex}.pdf"
        stored.write_bytes(content)
        document_name = document.filename
        document_path = str(stored)
    elif template_key:
        template = get_template(template_key)
        if template is None:
            raise HTTPException(status_code=400, detail="Unknown template")
        content = template.build_pdf(
            client_name=client_name.strip(), effective_date=effective_date
        )
        stored = settings.upload_dir / f"{uuid.uuid4().hex}.pdf"
        stored.write_bytes(content)
        document_name = f"{template.name}.pdf"
        document_path = str(stored)

    agreement = db.create_agreement(
        title=title,
        client_name=client_name.strip(),
        agreement_type=agreement_type.strip() or "Sales Agreement",
        value=numeric_value,
        currency=currency.strip() or "USD",
        owner=owner.strip(),
        notes=notes.strip(),
        effective_date=effective_date.strip(),
        recipients=recipients,
        document_name=document_name,
        document_path=document_path,
    )

    if action == "send":
        session = _get_session(request)
        try:
            _require_connected(session)
            result = _send_envelope(session, agreement["id"])
            return RedirectResponse(
                f"/agreements/{agreement['id']}?sent=1&envelope={result.get('envelopeId','')}",
                status_code=303,
            )
        except DocusignError as exc:
            db.add_event(agreement["id"], "error", exc.message)
            return RedirectResponse(
                f"/agreements/{agreement['id']}?error={quote(exc.message)}",
                status_code=303,
            )

    return RedirectResponse(f"/agreements/{agreement['id']}", status_code=303)


def _validate_pdf(content: bytes, filename: str) -> None:
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    if len(content) > 25 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="PDF must be 25 MB or smaller")
    if not content.startswith(b"%PDF"):
        raise HTTPException(
            status_code=400, detail=f"'{filename}' is not a valid PDF file"
        )


@app.post("/agreements/{agreement_id}/delete")
def delete_agreement(agreement_id: str) -> RedirectResponse:
    _agreement_or_404(agreement_id)
    db.delete_agreement(agreement_id)
    return RedirectResponse("/", status_code=303)


# ------------------------------------------------------------- Docusign: connect


@app.get("/oauth/connect")
def oauth_connect(request: Request):
    """Start Authorization Code Grant sign-in."""
    session_id = request.cookies.get(SESSION_COOKIE)
    session = _get_session(request)
    if session is None:
        session_id = _create_session()
        session = {"created": time.time()}

    if settings.auth_code_configured:
        state = secrets.token_urlsafe(16)
        session["oauth_state"] = state
        _save_session(session_id, session)  # type: ignore[arg-type]
        client = DocusignClient(settings)
        response = RedirectResponse(client.authorization_url(state), status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            serializer.dumps(session_id),
            httponly=True,
            samesite="lax",
            max_age=SESSION_MAX_AGE,
        )
        return response

    return RedirectResponse(
        "/settings?error="
        + quote(
            "Docusign is not configured. Add an integration key and client secret "
            "for Authorization Code Grant to .env."
        ),
        status_code=303,
    )


@app.get("/login")
def login(request: Request):
    """Login entry point: begin the Docusign Authorization Code Grant flow."""
    return oauth_connect(request)


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
    client = DocusignClient(settings)
    try:
        token = client.exchange_code_for_token(code)
        info = client.get_user_info()
    except DocusignError as exc:
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
        "auth_code_configured": settings.auth_code_configured,
    }


# --------------------------------------------------- Docusign: load agreements


def _sync_envelopes(session: dict[str, Any]) -> int:
    """Import/refresh the signed-in user's Docusign envelopes into the local DB.

    Agreements come from Docusign only; nothing is seeded locally. Returns the
    number of envelopes seen.
    """
    client = _client_for_session(session)
    from_date = (
        datetime.now(timezone.utc) - timedelta(days=SYNC_LOOKBACK_DAYS)
    ).isoformat(timespec="seconds")
    payload = client.list_envelopes(from_date=from_date, include_recipients=True)
    envelopes = payload.get("envelopes", []) or []
    for envelope in envelopes:
        _upsert_envelope(envelope)
    return len(envelopes)


def _upsert_envelope(envelope: dict[str, Any]) -> None:
    envelope_id = envelope.get("envelopeId") or envelope.get("envelope_id")
    if not envelope_id:
        return
    subject = envelope.get("emailSubject") or "Untitled agreement"
    docusign_status = (envelope.get("status") or "").lower()
    sender = envelope.get("sender") or {}
    owner = sender.get("email") or sender.get("userName") or ""
    completed_at = envelope.get("completedDateTime") or None

    existing = db.get_agreement_by_envelope(envelope_id)
    fields: dict[str, Any] = {"docusign_status": docusign_status}
    if docusign_status:
        fields["status"] = _map_envelope_status(docusign_status)
    if completed_at:
        fields["completed_at"] = completed_at

    if existing is None:
        agreement = db.create_agreement(
            title=subject,
            owner=owner,
            status=fields.get("status", "sent"),
            envelope_id=envelope_id,
            docusign_status=docusign_status,
            completed_at=completed_at,
            agreement_id=envelope_id,
        )
        db.add_event(
            agreement["id"], "imported", f"Loaded envelope {envelope_id} from Docusign"
        )
        agreement_id = agreement["id"]
    else:
        # Keep any local title/owner edits; only refresh the envelope state.
        agreement_id = existing["id"]
        db.update_agreement(agreement_id, **fields)

    recipient_groups = (
        ("signers", "signer"),
        ("carbonCopies", "carbonCopy"),
        ("certifiedDeliveries", "certifiedDelivery"),
    )
    envelope_recipients = envelope.get("recipients") or {}
    for key, role in recipient_groups:
        for index, recipient in enumerate(envelope_recipients.get(key) or [], start=1):
            email = recipient.get("email", "")
            if not email:
                continue
            signed_at = recipient.get("signedDateTime") or recipient.get(
                "completedDateTime"
            )
            db.upsert_recipient(
                agreement_id,
                name=recipient.get("name", ""),
                email=email,
                role=role,
                routing_order=int(recipient.get("routingOrder", index) or index),
                status=(recipient.get("status") or "sent").lower(),
                signed_at=signed_at,
            )


# ------------------------------------------------------------- Docusign: send


def _send_envelope(session: dict[str, Any] | None, agreement_id: str) -> dict[str, Any]:
    agreement = db.get_agreement(agreement_id)
    if agreement is None:
        raise DocusignError("Agreement not found")
    if not agreement.get("document_path"):
        raise DocusignError("This agreement has no document. Add a PDF or template first.")
    signers = [r for r in agreement["recipients"] if r["role"].lower() == "signer"]
    if not signers:
        raise DocusignError("Add at least one signer recipient before sending.")
    if agreement.get("envelope_id"):
        raise DocusignError("This agreement has already been sent to Docusign.")

    path = Path(agreement["document_path"])
    if not path.exists():
        raise DocusignError("The stored document file is missing. Re-create the agreement.")

    client = _client_for_session(session)
    document_base64 = encode_document(str(path))
    envelope = client.create_envelope(
        subject=f"Please sign: {agreement['title']}",
        email_blurb=agreement.get("notes", ""),
        document_base64=document_base64,
        document_name=agreement.get("document_name") or "Agreement.pdf",
        recipients=[
            {
                "name": r["name"],
                "email": r["email"],
                "role": r["role"],
                "routing_order": r["routing_order"],
            }
            for r in agreement["recipients"]
        ],
        status="sent",
    )
    envelope_id = envelope.get("envelopeId") or envelope.get("envelope_id")
    if not envelope_id:
        raise DocusignError("Docusign did not return an envelope id.", details=envelope)
    db.update_agreement(
        agreement_id,
        status="sent",
        envelope_id=envelope_id,
        docusign_status="sent",
    )
    db.add_event(
        agreement_id,
        "sent",
        f"Envelope {envelope_id} created and sent via Docusign",
    )
    return {"envelopeId": envelope_id}


@app.post("/agreements/{agreement_id}/send")
def send_agreement(request: Request, agreement_id: str):
    session = _get_session(request)
    try:
        _require_connected(session)
        result = _send_envelope(session, agreement_id)
        return {"ok": True, "envelope_id": result["envelopeId"]}
    except DocusignError as exc:
        db.add_event(agreement_id, "error", exc.message)
        return _error_response(exc)


@app.post("/agreements/{agreement_id}/void")
def void_agreement(request: Request, agreement_id: str, reason: str = Form("Voided by sender")):
    agreement = _agreement_or_404(agreement_id)
    session = _get_session(request)
    try:
        _require_connected(session)
        if not agreement.get("envelope_id"):
            raise DocusignError("This agreement has no Docusign envelope to void.")
        client = _client_for_session(session)
        client.void_envelope(agreement["envelope_id"], reason)
        db.update_agreement(agreement_id, status="voided", docusign_status="voided")
        db.add_event(agreement_id, "voided", f"Envelope voided: {reason}")
        return {"ok": True}
    except DocusignError as exc:
        db.add_event(agreement_id, "error", exc.message)
        return _error_response(exc)


# ----------------------------------------------------- Docusign: status / download


@app.post("/agreements/{agreement_id}/refresh")
def refresh_status(request: Request, agreement_id: str):
    agreement = _agreement_or_404(agreement_id)
    session = _get_session(request)
    try:
        _require_connected(session)
        if not agreement.get("envelope_id"):
            raise DocusignError("This agreement has not been sent to Docusign yet.")
        client = _client_for_session(session)
        envelope = client.get_envelope(agreement["envelope_id"])
        docusign_status = (envelope.get("status") or "").lower()
        recipients_payload = client.get_recipients(agreement["envelope_id"])
        recipients = (
            recipients_payload.get("signers", [])
            + recipients_payload.get("carbonCopies", [])
            + recipients_payload.get("certifiedDeliveries", [])
        )
        for recipient in recipients:
            email = recipient.get("email", "")
            status = (recipient.get("status") or "").lower()
            signed_at = recipient.get("signedDateTime") or recipient.get("completedDateTime")
            if email:
                db.set_recipient_status(agreement_id, email, status or "sent", signed_at)

        update: dict[str, Any] = {"docusign_status": docusign_status}
        if docusign_status:
            update["status"] = _map_envelope_status(docusign_status)
        if docusign_status == "completed":
            update["completed_at"] = (
                envelope.get("completedDateTime") or agreement.get("completed_at")
            )
        db.update_agreement(agreement_id, **update)
        db.add_event(
            agreement_id,
            "status",
            f"Docusign status refreshed: {docusign_status or 'unknown'}",
        )
        updated = db.get_agreement(agreement_id)
        return {"ok": True, "agreement": updated}
    except DocusignError as exc:
        db.add_event(agreement_id, "error", exc.message)
        return _error_response(exc)


def _map_envelope_status(docusign_status: str) -> str:
    mapping = {
        "sent": "sent",
        "delivered": "delivered",
        "completed": "completed",
        "declined": "declined",
        "voided": "voided",
        "correct": "corrected",
        "created": "draft",
    }
    return mapping.get(docusign_status.lower(), docusign_status.lower() or "sent")


@app.get("/agreements/{agreement_id}/download")
def download_document(request: Request, agreement_id: str, document_id: str = "combined"):
    agreement = _agreement_or_404(agreement_id)
    session = _get_session(request)

    # Prefer the signed copy from Docusign; fall back to the locally stored original.
    if agreement.get("envelope_id"):
        try:
            _require_connected(session)
            client = _client_for_session(session)
            content = client.download_document(agreement["envelope_id"], document_id)
            filename = _download_filename(agreement, document_id)
            return Response(
                content=content,
                media_type="application/pdf",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        except DocusignError as exc:
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


def _download_filename(agreement: dict[str, Any], document_id: str) -> str:
    safe_title = "".join(
        ch if ch.isalnum() or ch in " -_" else "_" for ch in agreement["title"]
    ).strip()
    suffix = "signed" if document_id == "combined" else document_id
    return f"{safe_title or 'agreement'}-{suffix}.pdf"


@app.get("/agreements/{agreement_id}/download/archive")
def download_archive(request: Request, agreement_id: str):
    """Download all envelope documents plus the Certificate of Completion as a ZIP."""
    agreement = _agreement_or_404(agreement_id)
    session = _get_session(request)
    try:
        _require_connected(session)
        if not agreement.get("envelope_id"):
            raise DocusignError("This agreement has not been sent to Docusign yet.")
        client = _client_for_session(session)
        content = client.download_document(agreement["envelope_id"], "archive")
        return Response(
            content=content,
            media_type="application/zip",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{_download_filename(agreement, "archive").replace(".pdf", ".zip")}"'
                )
            },
        )
    except DocusignError as exc:
        return _error_response(exc)


# ------------------------------------------------------------------- API (JSON)


@app.get("/api/agreements")
def api_list_agreements(
    q: str = "", status: str = "", type: str = ""
):
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


@app.get("/api/templates")
def api_templates():
    return {
        "ok": True,
        "templates": [
            {
                "key": t.key,
                "name": t.name,
                "agreement_type": t.agreement_type,
                "description": t.description,
                "default_value": t.default_value,
            }
            for t in list_templates()
        ],
    }


@app.get("/api/templates/{key}/preview")
def api_template_preview(key: str, client_name: str = "Sample Client", effective_date: str = ""):
    template = get_template(key)
    if template is None:
        return JSONResponse({"ok": False, "error": "Unknown template"}, status_code=404)
    pdf = template.build_pdf(client_name=client_name, effective_date=effective_date)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{template.key}.pdf"',
            "Content-Length": str(len(pdf)),
        },
    )


@app.get("/health")
def health():
    return {"ok": True, "version": __version__, "environment": settings.environment}
