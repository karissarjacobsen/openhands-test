"""End-to-end tests for the Agreement Manager app.

Run with:  python -m pytest tests/ -v
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Use an isolated database/upload dir before importing the app.
os.environ["APP_SESSION_SECRET"] = "test-secret"
os.environ["DOCUSIGN_ENVIRONMENT"] = "demo"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")

    import importlib

    from app import main

    importlib.reload(main)
    main.settings.data_dir = tmp_path
    main.settings.upload_dir = tmp_path / "uploads"
    main.settings.db_path = tmp_path / "test.db"
    main.settings.ensure_dirs()
    from app.db import Database

    main.db = Database(main.settings.db_path)

    from fastapi.testclient import TestClient

    with TestClient(main.app) as test_client:
        yield test_client


def _sign_in(client, *, token="tok-123", account_id="acct-1", email="user@example.com"):
    """Attach an authenticated Authorization Code session to the test client."""
    from app import main

    session_id = main._create_session()
    main._save_session(
        session_id,
        {
            "created": time.time(),
            "access_token": token,
            "expires_at": time.time() + 3600,
            "auth_type": "authorization_code",
            "account_id": account_id,
            "base_uri": "https://demo.docusign.net",
            "user_email": email,
            "user_name": "Test User",
        },
    )
    client.cookies.set(main.SESSION_COOKIE, main.serializer.dumps(session_id))


def _mock_docusign(envelopes):
    """Patch httpx.request so Docusign calls return `envelopes` from the account."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/userinfo"):
            return httpx.Response(
                200,
                json={
                    "sub": "user-1",
                    "name": "Test User",
                    "email": "user@example.com",
                    "accounts": [
                        {
                            "account_id": "acct-1",
                            "base_uri": "https://demo.docusign.net",
                        }
                    ],
                },
            )
        if request.url.path.endswith("/envelopes"):
            return httpx.Response(200, json={"envelopes": envelopes})
        return httpx.Response(404, json={"message": "not found"})

    def patched_request(method, url, **kwargs):
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            return http.request(method, url, **kwargs)

    return patched_request


def _create_agreement(client, title="Test Agreement", **overrides):
    data = {
        "title": title,
        "client_name": "Widget Co",
        "agreement_type": "Sales Agreement",
        "value": "1000",
        "currency": "USD",
        "owner": "tester@example.com",
        "effective_date": "2026-03-01",
        "notes": "Please sign promptly.",
        "template_key": "sales-agreement",
        "recipient_name": ["Ada Lovelace"],
        "recipient_email": ["ada@example.com"],
        "recipient_role": ["signer"],
        "action": "save",
    }
    data.update(overrides)
    return client.post("/agreements", data=data, follow_redirects=False)


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_dashboard_has_login_button_when_signed_out(client):
    response = client.get("/")
    assert response.status_code == 200
    assert 'href="/login"' in response.text
    assert "Sign in with Docusign" in response.text
    # No dummy data is shown before signing in.
    assert "Acme Corp" not in response.text
    assert "Northwind Traders" not in response.text


def test_settings_page_has_login_button_when_signed_out(client):
    response = client.get("/settings")
    assert response.status_code == 200
    assert "Connection" in response.text
    assert 'href="/login"' in response.text
    assert "Sign in with Docusign" in response.text


def test_login_without_credentials_redirects_with_error(client):
    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_login_redirects_to_docusign_authorization_url(client):
    from app import main

    main.settings.integration_key = "test-key"
    main.settings.client_secret = "test-secret"
    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("https://account-d.docusign.com/oauth/auth?")
    assert "client_id=test-key" in location
    assert "response_type=code" in location


def test_no_jwt_support_remains():
    from app.config import Settings

    settings = Settings(integration_key="k", client_secret="s")
    assert settings.auth_code_configured is True
    assert not hasattr(settings, "jwt_configured")

    from app.docusign_client import DocusignClient

    assert not hasattr(DocusignClient, "request_jwt_token")
    assert not hasattr(DocusignClient, "_build_jwt")


def test_dashboard_loads_agreements_from_docusign(client, monkeypatch):
    _sign_in(client)
    envelopes = [
        {
            "envelopeId": "env-100",
            "emailSubject": "Signed from Docusign",
            "status": "completed",
            "completedDateTime": "2026-02-01T10:00:00Z",
            "sender": {"email": "user@example.com", "userName": "Test User"},
            "recipients": {
                "signers": [
                    {
                        "name": "Dana Whitfield",
                        "email": "dana@example.com",
                        "status": "completed",
                        "signedDateTime": "2026-02-01T09:00:00Z",
                        "routingOrder": "1",
                    }
                ]
            },
        },
        {
            "envelopeId": "env-200",
            "emailSubject": "Awaiting signature",
            "status": "sent",
            "sender": {"email": "user@example.com"},
            "recipients": {
                "signers": [
                    {"name": "Priya Raman", "email": "priya@example.com", "status": "sent"}
                ]
            },
        },
    ]
    monkeypatch.setattr(httpx, "request", _mock_docusign(envelopes))

    response = client.get("/")
    assert response.status_code == 200
    assert "Signed from Docusign" in response.text
    assert "Awaiting signature" in response.text

    listing = client.get("/api/agreements").json()["agreements"]
    titles = {a["title"] for a in listing}
    assert titles == {"Signed from Docusign", "Awaiting signature"}
    completed = next(a for a in listing if a["title"] == "Signed from Docusign")
    assert completed["status"] == "completed"
    assert completed["envelope_id"] == "env-100"
    assert completed["recipients"][0]["email"] == "dana@example.com"


def test_dashboard_sync_refreshes_existing_envelope(client, monkeypatch):
    _sign_in(client)
    first = [
        {
            "envelopeId": "env-300",
            "emailSubject": "Refresh me",
            "status": "sent",
            "sender": {"email": "user@example.com"},
            "recipients": {"signers": []},
        }
    ]
    monkeypatch.setattr(httpx, "request", _mock_docusign(first))
    client.get("/")
    listing = client.get("/api/agreements").json()["agreements"]
    assert listing[0]["status"] == "sent"

    second = [
        {
            "envelopeId": "env-300",
            "emailSubject": "Refresh me",
            "status": "completed",
            "completedDateTime": "2026-02-02T10:00:00Z",
            "sender": {"email": "user@example.com"},
            "recipients": {
                "signers": [
                    {
                        "name": "Sam",
                        "email": "sam@example.com",
                        "status": "completed",
                        "signedDateTime": "2026-02-02T09:00:00Z",
                    }
                ]
            },
        }
    ]
    monkeypatch.setattr(httpx, "request", _mock_docusign(second))
    client.get("/")

    listing = client.get("/api/agreements").json()["agreements"]
    assert len(listing) == 1
    assert listing[0]["status"] == "completed"
    assert listing[0]["recipients"][0]["status"] == "completed"


def test_dashboard_sync_includes_carbon_copies(client, monkeypatch):
    _sign_in(client)
    envelopes = [
        {
            "envelopeId": "env-400",
            "emailSubject": "With a CC",
            "status": "sent",
            "sender": {"email": "user@example.com"},
            "recipients": {
                "signers": [
                    {"name": "Signer", "email": "signer@example.com", "status": "sent"}
                ],
                "carbonCopies": [
                    {"name": "Watcher", "email": "cc@example.com", "status": "sent"}
                ],
            },
        }
    ]
    monkeypatch.setattr(httpx, "request", _mock_docusign(envelopes))
    client.get("/")

    listing = client.get("/api/agreements").json()["agreements"]
    recipients = {r["email"]: r["role"] for r in listing[0]["recipients"]}
    assert recipients == {"signer@example.com": "signer", "cc@example.com": "carbonCopy"}


def test_dashboard_shows_sync_error(client, monkeypatch):
    _sign_in(client)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/userinfo"):
            return httpx.Response(
                200,
                json={
                    "accounts": [
                        {"account_id": "acct-1", "base_uri": "https://demo.docusign.net"}
                    ]
                },
            )
        return httpx.Response(400, json={"message": "Bad from_date"})

    def patched_request(method, url, **kwargs):
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            return http.request(method, url, **kwargs)

    monkeypatch.setattr(httpx, "request", patched_request)
    response = client.get("/")
    assert response.status_code == 200
    assert "Could not load agreements from Docusign" in response.text


def test_create_agreement_with_template(client):
    response = _create_agreement(client, title="Test Widget Agreement")
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/agreements/")

    agreement_id = location.rsplit("/", 1)[1]
    detail = client.get(f"/api/agreements/{agreement_id}").json()["agreement"]
    assert detail["title"] == "Test Widget Agreement"
    assert detail["status"] == "draft"
    assert detail["document_name"] == "Sales Agreement.pdf"
    assert len(detail["recipients"]) == 1
    assert detail["recipients"][0]["email"] == "ada@example.com"

    # The generated template PDF must be a valid PDF with the sign anchor.
    pdf = client.get(f"/agreements/{agreement_id}/download")
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF")
    assert b"/sn1/" in pdf.content


def test_agreement_detail_page(client):
    response = _create_agreement(client, title="Detail Page Agreement")
    agreement_id = response.headers["location"].rsplit("/", 1)[1]
    detail = client.get(f"/agreements/{agreement_id}")
    assert detail.status_code == 200
    assert "Recipients" in detail.text
    assert "Docusign actions" in detail.text


def test_agreement_detail_404(client):
    response = client.get("/agreements/does-not-exist")
    assert response.status_code == 404


def test_create_agreement_requires_title(client):
    response = client.post(
        "/agreements",
        data={"title": "   ", "action": "save"},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_upload_rejects_non_pdf(client):
    response = client.post(
        "/agreements",
        data={
            "title": "Bad upload",
            "recipient_name": ["X"],
            "recipient_email": ["x@example.com"],
            "recipient_role": ["signer"],
            "action": "save",
        },
        files={"document": ("notes.txt", b"this is not a pdf", "text/plain")},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_upload_accepts_pdf(client):
    from app.pdf import build_pdf

    pdf_bytes = build_pdf("Uploaded Doc", [("Clause", "Sign here /sn1/")])
    response = client.post(
        "/agreements",
        data={
            "title": "Uploaded agreement",
            "recipient_name": ["Grace Hopper"],
            "recipient_email": ["grace@example.com"],
            "recipient_role": ["signer"],
            "action": "save",
        },
        files={"document": ("contract.pdf", pdf_bytes, "application/pdf")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    agreement_id = response.headers["location"].rsplit("/", 1)[1]
    detail = client.get(f"/api/agreements/{agreement_id}").json()["agreement"]
    assert detail["document_name"] == "contract.pdf"


def test_send_without_connection_returns_error(client):
    response = _create_agreement(client, title="Send me")
    agreement_id = response.headers["location"].rsplit("/", 1)[1]
    send = client.post(f"/agreements/{agreement_id}/send")
    assert send.status_code == 400
    payload = send.json()
    assert payload["ok"] is False
    assert "connect" in payload["error"].lower()


def test_refresh_without_envelope_returns_error(client):
    response = _create_agreement(client, title="Refresh me")
    agreement_id = response.headers["location"].rsplit("/", 1)[1]
    refresh = client.post(f"/agreements/{agreement_id}/refresh")
    assert refresh.status_code == 400
    assert refresh.json()["ok"] is False


def test_templates_endpoint(client):
    response = client.get("/api/templates")
    assert response.status_code == 200
    keys = {t["key"] for t in response.json()["templates"]}
    assert {"sales-agreement", "mutual-nda", "statement-of-work"} <= keys


def test_template_preview_is_pdf(client):
    response = client.get("/api/templates/mutual-nda/preview")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    assert b"/sn1/" in response.content


def test_unknown_template_preview_404(client):
    response = client.get("/api/templates/nope/preview")
    assert response.status_code == 404


def test_connection_endpoint_reports_not_connected(client):
    response = client.get("/api/connection")
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is False
    assert "jwt_configured" not in body


def test_delete_agreement(client):
    response = _create_agreement(client, title="Delete me")
    target = response.headers["location"].rsplit("/", 1)[1]
    deleted = client.post(f"/agreements/{target}/delete", follow_redirects=False)
    assert deleted.status_code == 303
    assert client.get(f"/api/agreements/{target}").status_code == 404


def test_download_missing_document_404(client):
    response = _create_agreement(client, title="No document agreement", template_key="")
    agreement_id = response.headers["location"].rsplit("/", 1)[1]
    download = client.get(f"/agreements/{agreement_id}/download")
    assert download.status_code == 404
