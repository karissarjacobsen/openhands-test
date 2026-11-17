"""End-to-end tests for the Agreement Manager app.

Run with:  python -m pytest tests/ -v

Agreements are read from the Docusign Agreement Manager API. Docusign HTTP is
intercepted with httpx's MockTransport so the real client code paths (URL
building, pagination, status mapping, caching) stay under test without any live
API calls.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Use an isolated database before importing the app.
os.environ["APP_SESSION_SECRET"] = "test-secret"
os.environ["DOCUSIGN_ENVIRONMENT"] = "demo"


SAMPLE_AGREEMENTS = [
    {
        "id": "ag-100",
        "file_name": "Acme Master Services Agreement.pdf",
        "title": "Acme Corp - Master Services Agreement",
        "type": "Master Services Agreement",
        "category": "BusinessServices",
        "status": "COMPLETE",
        "provisions": {
            "effective_date": "2026-01-15T00:00:00Z",
            "total_agreement_value": 125000,
            "total_agreement_value_currency_code": "USD",
        },
        "source_name": "ESign",
        "source_id": "src-100",
        "download_url": "https://api-d.docusign.com/v1/accounts/acct-1/agreements/ag-100/document",
        "metadata": {"created_at": "2026-01-16T10:00:00Z"},
    },
    {
        "id": "ag-200",
        "file_name": "Northwind Sales Agreement.pdf",
        "title": "Northwind Traders - Sales Agreement",
        "type": "Sales Agreement",
        "category": "Sales",
        "status": "PENDING",
        "provisions": {
            "effective_date": "2026-02-01T00:00:00Z",
            "total_agreement_value": 48900,
            "total_agreement_value_currency_code": "USD",
        },
        "source_name": "ESign",
        "source_id": "src-200",
        "metadata": {"created_at": "2026-02-02T10:00:00Z"},
    },
]

SAMPLE_DETAIL = {
    "id": "ag-100",
    "file_name": "Acme Master Services Agreement.pdf",
    "title": "Acme Corp - Master Services Agreement",
    "type": "Master Services Agreement",
    "category": "BusinessServices",
    "status": "COMPLETE",
    "provisions": {
        "effective_date": "2026-01-15T00:00:00Z",
        "expiration_date": "2029-01-15T00:00:00Z",
        "total_agreement_value": 125000,
        "total_agreement_value_currency_code": "USD",
    },
    "parties": [
        {"id": "p-1", "name_in_agreement": "Acme Corp", "email": "dana@acme.example"},
        {"id": "p-2", "name_in_agreement": "Our Company", "email": "karissa@example.com"},
    ],
    "source_name": "ESign",
    "source_id": "src-100",
    "download_url": "https://api-d.docusign.com/v1/accounts/acct-1/agreements/ag-100/document",
    "metadata": {"created_at": "2026-01-16T10:00:00Z"},
}


class FakeAGM:
    """A tiny in-memory stand-in for the Agreement Manager HTTP surface."""

    def __init__(self):
        self.list_calls = 0
        self.fail_list = False
        self.agreements = json.loads(json.dumps(SAMPLE_AGREEMENTS))
        self.detail = json.loads(json.dumps(SAMPLE_DETAIL))

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/oauth/userinfo"):
            return httpx.Response(
                200,
                json={
                    "sub": "user-1",
                    "name": "Karissa Jacobsen",
                    "email": "karissa@example.com",
                    "accounts": [
                        {"account_id": "acct-1", "base_uri": "https://api-d.docusign.com"}
                    ],
                },
            )
        if path.endswith("/agreements"):
            self.list_calls += 1
            if self.fail_list:
                return httpx.Response(500, json={"message": "boom"})
            return httpx.Response(200, json={"data": self.agreements, "metadata": {}})
        if path.endswith("/agreement-types"):
            return httpx.Response(200, json=["Master Services Agreement", "Sales Agreement"])
        if "/agreements/ag-100" in path and path.endswith("/document"):
            return httpx.Response(200, content=b"%PDF-1.4 agreement body")
        if "/agreements/" in path:
            return httpx.Response(200, json={"data": self.detail})
        return httpx.Response(404, json={"message": "not found"})


@pytest.fixture()
def agm(monkeypatch):
    fake = FakeAGM()
    transport = httpx.MockTransport(fake.handler)
    real_request = httpx.request

    def patched_request(method, url, **kwargs):
        with httpx.Client(transport=transport) as http:
            return http.request(method, url, **kwargs)

    monkeypatch.setattr(httpx, "request", patched_request)
    yield fake
    httpx.request = real_request


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")

    import importlib

    from app import main

    importlib.reload(main)
    main.settings.data_dir = tmp_path
    main.settings.db_path = tmp_path / "test.db"
    main.settings.ensure_dirs()
    from app.db import Database

    main.db = Database(main.settings.db_path)
    main._SESSIONS.clear()

    from fastapi.testclient import TestClient

    with TestClient(main.app) as test_client:
        yield test_client


def sign_in(client) -> None:
    """Seed a session with a token so the app treats the user as connected."""
    from app import main

    session_id = main._create_session()
    main._save_session(
        session_id,
        {
            "access_token": "test-token",
            "auth_type": "authorization_code",
            "account_id": "acct-1",
            "user_email": "karissa@example.com",
        },
    )
    client.cookies.set(main.SESSION_COOKIE, main.serializer.dumps(session_id))


# --------------------------------------------------------------------- basics


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_dashboard_empty_before_sign_in(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Agreements" in response.text
    assert "connect first" in response.text.lower() or "Connect to Agreement Manager" in response.text
    # No sample/dummy agreements are shown before signing in.
    assert "Acme Corp - Master Services Agreement" not in response.text


def test_dashboard_loads_agreements_from_agreement_manager(client, agm):
    sign_in(client)
    response = client.get("/")
    assert response.status_code == 200
    assert agm.list_calls == 1
    assert "Acme Corp - Master Services Agreement" in response.text
    assert "Northwind Traders - Sales Agreement" in response.text


def test_dashboard_surfaces_sync_errors(client, agm):
    sign_in(client)
    agm.fail_list = True
    response = client.get("/")
    assert response.status_code == 200
    assert "Could not load agreements from Agreement Manager" in response.text


def test_agreements_are_cached_and_refreshed_on_resync(client, agm):
    sign_in(client)
    client.get("/")
    assert agm.list_calls == 1

    # A second dashboard load syncs again (no local seeding involved).
    client.get("/")
    assert agm.list_calls == 2

    # Simulate the agreement being updated upstream.
    agm.agreements[0]["status"] = "INACTIVE"
    agm.agreements[0]["file_name"] = "Acme Master Services Agreement v2.pdf"
    client.get("/")
    listing = client.get("/api/agreements").json()
    acme = next(a for a in listing["agreements"] if a["id"] == "ag-100")
    assert acme["status"] == "inactive"
    assert acme["file_name"] == "Acme Master Services Agreement v2.pdf"


def test_dashboard_search_filter(client, agm):
    sign_in(client)
    response = client.get("/", params={"q": "Northwind"})
    assert response.status_code == 200
    assert "Northwind" in response.text
    assert "Acme Corp - Master Services Agreement" not in response.text


def test_dashboard_status_filter(client, agm):
    sign_in(client)
    response = client.get("/", params={"status": "complete"})
    assert response.status_code == 200
    assert "Acme Corp" in response.text
    assert "Northwind" not in response.text


# -------------------------------------------------------------- detail page


def test_agreement_detail_loads_parties_from_agreement_manager(client, agm):
    sign_in(client)
    client.get("/")
    response = client.get("/agreements/ag-100")
    assert response.status_code == 200
    assert "Parties" in response.text
    assert "Acme Corp" in response.text
    assert "Agreement Manager actions" in response.text


def test_agreement_detail_404(client):
    response = client.get("/agreements/does-not-exist")
    assert response.status_code == 404


def test_refresh_endpoint_pulls_from_agreement_manager(client, agm):
    sign_in(client)
    client.get("/")
    response = client.post("/agreements/ag-100/refresh")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["agreement"]["parties"][0]["name"] == "Acme Corp"


def test_refresh_without_connection_returns_error(client):
    # Seed the cache so the route is reached, then attempt a refresh with no session.
    from app import main

    main.db.upsert_agreement({"id": "ag-100", "title": "Acme"})
    response = client.post("/agreements/ag-100/refresh")
    assert response.status_code == 400
    assert response.json()["ok"] is False


def test_sync_endpoint_requires_connection(client):
    response = client.post("/agreements/sync")
    assert response.status_code == 400
    assert "connect" in response.json()["error"].lower()


def test_sync_endpoint_loads_agreements(client, agm):
    sign_in(client)
    response = client.post("/agreements/sync")
    assert response.status_code == 200
    assert response.json()["synced"] == 2


# ---------------------------------------------------------------- downloads


def test_download_uses_agreement_manager_download_url(client, agm):
    sign_in(client)
    client.get("/")
    response = client.get("/agreements/ag-100/download")
    assert response.status_code == 200
    assert response.content == b"%PDF-1.4 agreement body"
    assert response.headers["content-type"] == "application/pdf"


def test_download_without_connection_returns_error(client, agm):
    # Seed the cache without a session so the download route is reached.
    from app import main

    main.db.upsert_agreement(
        {"id": "ag-100", "title": "Acme", "download_url": "https://x"}
    )
    response = client.get("/agreements/ag-100/download")
    assert response.status_code == 400
    assert response.json()["ok"] is False


# ------------------------------------------------------------- json API


def test_api_list_agreements_syncs_when_asked(client, agm):
    sign_in(client)
    response = client.get("/api/agreements", params={"sync": 1})
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["count"] == 2


def test_api_agreement_types(client, agm):
    sign_in(client)
    response = client.get("/api/agreement-types")
    assert response.status_code == 200
    assert "Sales Agreement" in response.json()["agreement_types"]


def test_api_get_agreement_404(client):
    assert client.get("/api/agreements/nope").status_code == 404


def test_delete_removes_from_cache(client, agm):
    sign_in(client)
    client.get("/")
    response = client.post("/agreements/ag-100/delete", follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/api/agreements/ag-100").status_code == 404


# ---------------------------------------------------------------- settings


def test_settings_page_renders(client):
    response = client.get("/settings")
    assert response.status_code == 200
    assert "Connection" in response.text
    assert "Agreement Manager" in response.text


def test_login_redirects_to_authorization_url(client):
    from app import main

    main.settings.integration_key = "key"
    main.settings.client_secret = "secret"
    try:
        response = client.get("/login", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith(
            "https://account-d.docusign.com/oauth/auth?"
        )
        assert "adm_store_unified_repo_read" in response.headers["location"]
    finally:
        main.settings.integration_key = ""
        main.settings.client_secret = ""


def test_oauth_connect_without_credentials_redirects_with_error(client):
    response = client.get("/oauth/connect", follow_redirects=False)
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_connection_endpoint_reports_not_connected(client):
    response = client.get("/api/connection")
    assert response.status_code == 200
    payload = response.json()
    assert payload["connected"] is False
    assert payload["agm_base_url"] == "https://api-d.docusign.com/v1"
