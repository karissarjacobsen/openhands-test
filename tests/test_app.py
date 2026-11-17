"""End-to-end tests for the Agreement Manager app.

Run with:  python -m pytest tests/ -v

The app talks to the real Docusign Agreement Manager REST endpoints. In these
tests `httpx.request` / `httpx.post` are intercepted with httpx MockTransport so
the real request-building and error-handling code is exercised end to end.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Use an isolated database/upload dir before importing the app.
os.environ["APP_SESSION_SECRET"] = "test-secret"
os.environ["DOCUSIGN_ENVIRONMENT"] = "demo"


@pytest.fixture()
def main_module(tmp_path, monkeypatch):
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
    main.settings.account_id = "acct-1"
    main.settings.ensure_dirs()

    from app.db import Database, seed_sample_data

    main.db = Database(main.settings.db_path)
    seed_sample_data(main.db)
    return main


@pytest.fixture()
def client(main_module):
    from fastapi.testclient import TestClient

    with TestClient(main_module.app) as test_client:
        yield test_client


def connect(main_module, client) -> None:
    """Give the test client a server-side session with an access token."""
    session_id = main_module._create_session()
    main_module._save_session(
        session_id,
        {
            "access_token": "test-token",
            "auth_type": "jwt",
            "account_id": "acct-1",
            "user_email": "tester@example.com",
        },
    )
    client.cookies.set(
        main_module.SESSION_COOKIE, main_module.serializer.dumps(session_id)
    )


def install_transport(monkeypatch, handler):
    """Route every httpx call the client makes through a MockTransport handler."""

    def patched_request(method, url, **kwargs):
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            return http.request(method, url, **kwargs)

    def patched_post(url, **kwargs):
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            return http.post(url, **kwargs)

    monkeypatch.setattr(httpx, "request", patched_request)
    monkeypatch.setattr(httpx, "post", patched_post)


# --------------------------------------------------------------------- pages


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_dashboard_lists_sample_data(client):
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "Agreements" in body
    assert "Acme Corp - Master Services Agreement" in body
    assert "Northwind Traders" in body


def test_dashboard_search_filter(client):
    response = client.get("/", params={"q": "Globex"})
    assert response.status_code == 200
    assert "Globex" in response.text
    assert "Northwind Traders - Sales Agreement" not in response.text


def test_dashboard_status_filter(client):
    response = client.get("/", params={"status": "complete"})
    assert response.status_code == 200
    assert "Acme Corp" in response.text
    assert "Globex" not in response.text


def test_agreement_detail_page(client):
    listing = client.get("/api/agreements").json()
    agreement_id = listing["agreements"][0]["id"]
    response = client.get(f"/agreements/{agreement_id}")
    assert response.status_code == 200
    assert "Parties" in response.text
    assert "Agreement Manager actions" in response.text
    assert "Provisions" in response.text


def test_agreement_detail_404(client):
    response = client.get("/agreements/does-not-exist")
    assert response.status_code == 404


def test_settings_page_renders(client):
    response = client.get("/settings")
    assert response.status_code == 200
    assert "Connection" in response.text
    assert "Agreement Manager scopes" in response.text


def test_agm_list_page_renders_when_not_connected(client):
    response = client.get("/agm")
    assert response.status_code == 200
    assert "Agreement Manager" in response.text


def test_bulk_page_renders(client):
    response = client.get("/bulk")
    assert response.status_code == 200
    assert "Bulk upload" in response.text


def test_custom_provisions_page_renders(client):
    response = client.get("/custom-provisions")
    assert response.status_code == 200
    assert "Custom provisions" in response.text


# --------------------------------------------------------------- local API


def test_local_agreements_api(client):
    response = client.get("/api/agreements")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["count"] >= 1


def test_connection_endpoint_reports_not_connected(client):
    response = client.get("/api/connection")
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is False
    assert "adm_store_unified_repo_read" in body["scopes"]


def test_oauth_connect_without_credentials_redirects_with_error(client):
    response = client.get("/oauth/connect", follow_redirects=False)
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_delete_local_agreement(client):
    listing = client.get("/api/agreements").json()
    target = listing["agreements"][0]["id"]
    response = client.post(f"/agreements/{target}/delete", follow_redirects=False)
    assert response.status_code == 303
    assert client.get(f"/api/agreements/{target}").status_code == 404


# ---------------------------------------------- Agreement Manager: not connected


def test_agm_list_requires_connection(client):
    response = client.get("/api/agm/agreements")
    assert response.status_code == 400
    assert response.json()["ok"] is False


def test_bulk_job_requires_connection(client):
    response = client.post(
        "/api/agm/bulk/jobs",
        data={"job_name": "Job"},
        files={"files": ("a.pdf", b"%PDF-1.4 x", "application/pdf")},
    )
    assert response.status_code == 400
    assert response.json()["ok"] is False


# ------------------------------------------- Agreement Manager: live endpoints


AGM_AGREEMENT = {
    "id": "agm-111",
    "title": "Live Imported Agreement",
    "file_name": "live.pdf",
    "type": "Statement of Work",
    "category": "BusinessServices",
    "status": "COMPLETE",
    "summary": "A live agreement from Agreement Manager.",
    "parties": [
        {"id": "p1", "name_in_agreement": "ACME, INC"},
        {"id": "p2", "name_in_agreement": "TALLY, LTD"},
    ],
    "provisions": {
        "effective_date": "2025-01-01T00:00:00Z",
        "total_agreement_value": 150000,
        "total_agreement_value_currency_code": "USD",
    },
    "_links": {"document": {"href": "https://api-d.docusign.com/v1/documents/doc-1"}},
}


def test_agm_list_endpoint(main_module, client, monkeypatch):
    connect(main_module, client)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={"data": [AGM_AGREEMENT], "metadata": {"total_items": 1}},
        )

    install_transport(monkeypatch, handler)
    response = client.get("/api/agm/agreements", params={"search": "acme"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["data"][0]["id"] == "agm-111"
    assert "/accounts/acct-1/agreements" in captured["url"]
    assert "search=acme" in captured["url"]


def test_agm_get_and_import_endpoint(main_module, client, monkeypatch):
    connect(main_module, client)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/agreements/agm-111")
        return httpx.Response(200, json=AGM_AGREEMENT)

    install_transport(monkeypatch, handler)
    response = client.post("/api/agm/agreements/agm-111/import")
    assert response.status_code == 200
    agreement = response.json()["agreement"]
    assert agreement["agm_id"] == "agm-111"
    assert agreement["title"] == "Live Imported Agreement"
    assert agreement["status"] == "complete"
    assert agreement["agreement_type"] == "Statement of Work"
    assert len(agreement["parties"]) == 2
    assert agreement["provisions"]["total_agreement_value"] == 150000

    # Importing again updates the same local record rather than duplicating it.
    response = client.post("/api/agm/agreements/agm-111/import")
    assert response.status_code == 200
    listing = client.get("/api/agreements").json()["agreements"]
    assert sum(1 for a in listing if a["agm_id"] == "agm-111") == 1


def test_agm_agreement_types_endpoint(main_module, client, monkeypatch):
    connect(main_module, client)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/agreement-types")
        return httpx.Response(200, json=["Statement of Work", "Non-disclosure Agreement"])

    install_transport(monkeypatch, handler)
    response = client.get("/api/agm/agreement-types")
    assert response.status_code == 200
    assert response.json()["agreement_types"] == [
        "Statement of Work",
        "Non-disclosure Agreement",
    ]


def test_refresh_and_patch_and_change_type(main_module, client, monkeypatch):
    connect(main_module, client)
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json=AGM_AGREEMENT)
        if request.url.path.endswith("/actions/change-type"):
            body = json.loads(request.content.decode())
            assert body == {"type": "Non-disclosure Agreement"}
            return httpx.Response(200, json={**AGM_AGREEMENT, "type": "Non-disclosure Agreement"})
        # PATCH
        body = json.loads(request.content.decode())
        assert body["title"] == "Patched Title"
        return httpx.Response(200, json={**AGM_AGREEMENT, "title": "Patched Title"})

    install_transport(monkeypatch, handler)
    imported = client.post("/api/agm/agreements/agm-111/import").json()["agreement"]
    local_id = imported["id"]

    refreshed = client.post(f"/api/agreements/{local_id}/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["agreement"]["agm_id"] == "agm-111"

    patched = client.patch(
        f"/api/agreements/{local_id}",
        json={"title": "Patched Title", "status": "INACTIVE"},
    )
    assert patched.status_code == 200
    updated = patched.json()["agreement"]
    assert updated["title"] == "Patched Title"
    assert updated["status"] == "inactive"

    changed = client.post(
        f"/api/agreements/{local_id}/change-type",
        json={"type": "Non-disclosure Agreement"},
    )
    assert changed.status_code == 200
    assert changed.json()["agreement"]["agreement_type"] == "Non-disclosure Agreement"

    assert ("PATCH", "/v1/accounts/acct-1/agreements/agm-111") in calls


def test_patch_requires_agm_link(main_module, client):
    connect(main_module, client)
    local_only = main_module.db.create_agreement(title="Local only agreement")
    response = client.patch(f"/api/agreements/{local_only['id']}", json={"title": "x"})
    assert response.status_code == 400
    assert "not linked" in response.json()["error"].lower()


def test_delete_agreement_from_agm(main_module, client, monkeypatch):
    connect(main_module, client)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=AGM_AGREEMENT)
        if request.method == "DELETE":
            captured["method"] = request.method
            captured["path"] = request.url.path
            return httpx.Response(204)
        return httpx.Response(404)

    install_transport(monkeypatch, handler)
    imported = client.post("/api/agm/agreements/agm-111/import").json()["agreement"]
    local_id = imported["id"]

    response = client.delete(f"/api/agreements/{local_id}")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert captured["method"] == "DELETE"
    assert captured["path"].endswith("/agreements/agm-111")
    assert client.get(f"/api/agreements/{local_id}").status_code == 404


def test_delete_from_agm_form_route(main_module, client, monkeypatch):
    connect(main_module, client)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=AGM_AGREEMENT)
        captured["path"] = request.url.path
        return httpx.Response(204)

    install_transport(monkeypatch, handler)
    imported = client.post("/api/agm/agreements/agm-111/import").json()["agreement"]

    response = client.post(
        f"/api/agreements/{imported['id']}/delete-from-agm", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/?deleted=1"
    assert captured["path"].endswith("/agreements/agm-111")
    assert client.get(f"/api/agreements/{imported['id']}").status_code == 404


def test_download_agreement_document(main_module, client, monkeypatch):
    connect(main_module, client)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agreements/agm-111"):
            return httpx.Response(200, json=AGM_AGREEMENT)
        if request.url.path.endswith("/documents/doc-1"):
            return httpx.Response(200, content=b"%PDF-1.4 live document")
        return httpx.Response(404)

    install_transport(monkeypatch, handler)
    imported = client.post("/api/agm/agreements/agm-111/import").json()["agreement"]
    response = client.get(f"/agreements/{imported['id']}/download")
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


# ------------------------------------------- Agreement Manager: custom provisions


def test_custom_provisions_crud(main_module, client, monkeypatch):
    connect(main_module, client)
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        assert request.url.path.endswith("/custom-provisions")
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"key": "my-prov", "label": "My Prov"}]})
        if request.method == "POST":
            body = json.loads(request.content.decode())
            assert body["label"] == "My Custom Provision"
            assert body["category"] == "FINANCIAL"
            return httpx.Response(201, json={"key": "my-prov", "label": "My Custom Provision"})
        return httpx.Response(200, json={"data": []})

    install_transport(monkeypatch, handler)
    listing = client.get("/api/agm/custom-provisions")
    assert listing.status_code == 200
    assert listing.json()["data"][0]["key"] == "my-prov"

    created = client.post(
        "/api/agm/custom-provisions",
        json={"label": "My Custom Provision", "category": "FINANCIAL", "type": "CURRENCY"},
    )
    assert created.status_code == 200
    assert created.json()["provision"]["key"] == "my-prov"

    updated = client.patch("/api/agm/custom-provisions", json={"keys": ["my-prov"]})
    assert updated.status_code == 200

    deleted = client.request(
        "DELETE", "/api/agm/custom-provisions", json={"keys": ["my-prov"]}
    )
    assert deleted.status_code == 200

    methods = [m for m, _ in seen]
    assert "GET" in methods and "POST" in methods and "PATCH" in methods and "DELETE" in methods


def test_agreement_type_provisions_add_and_remove(main_module, client, monkeypatch):
    connect(main_module, client)
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        body = json.loads(request.content.decode())
        assert body["agreement_types"][0]["key"] == "nda"
        return httpx.Response(200, json=["Non-disclosure Agreement"])

    install_transport(monkeypatch, handler)
    add = client.post(
        "/api/agm/agreement-types/provisions",
        json={"action": "add", "agreement_types": [{"key": "nda", "custom_provisions": ["x"]}]},
    )
    assert add.status_code == 200
    remove = client.post(
        "/api/agm/agreement-types/provisions",
        json={"action": "remove", "agreement_types": [{"key": "nda", "custom_provisions": ["x"]}]},
    )
    assert remove.status_code == 200
    assert any(p.endswith("/agreement-types/actions/add") for p in paths)
    assert any(p.endswith("/agreement-types/actions/remove") for p in paths)


# --------------------------------------------- Agreement Manager: bulk upload


def test_bulk_upload_full_flow(main_module, client, monkeypatch):
    connect(main_module, client)
    requests: list[tuple[str, str]] = []
    uploaded: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path.endswith("/upload/jobs"):
            body = json.loads(request.content.decode())
            assert body["job_name"] == "Q4 Contract Migration"
            assert body["expected_number_of_docs"] == 2
            return httpx.Response(
                201,
                json={
                    "id": "job-1",
                    "status": "OPEN",
                    "_embedded": {
                        "documents": [
                            {"id": "d1", "_actions": {"upload_document": {"href": "https://blob/d1"}}},
                            {"id": "d2", "_actions": {"upload_document": {"href": "https://blob/d2"}}},
                        ]
                    },
                },
            )
        if request.url.host == "blob":
            uploaded.append(request.content)
            return httpx.Response(201)
        if request.url.path.endswith("/actions/complete"):
            return httpx.Response(200, json={"status": "UPLOAD_COMPLETE"})
        return httpx.Response(404)

    install_transport(monkeypatch, handler)
    response = client.post(
        "/api/agm/bulk/jobs",
        data={"job_name": "Q4 Contract Migration", "expected_number_of_docs": "2"},
        files=[
            ("files", ("one.pdf", b"%PDF-1.4 one", "application/pdf")),
            ("files", ("two.pdf", b"%PDF-1.4 two", "application/pdf")),
        ],
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["job"]["job_id"] == "job-1"
    assert payload["job"]["status"] == "UPLOAD_COMPLETE"
    assert len(uploaded) == 2

    methods = [m for m, _ in requests]
    assert "POST" in methods
    assert any(p.endswith("/actions/complete") for _, p in requests)

    # Poll the job status.
    local_id = payload["job"]["id"]

    def status_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/upload/jobs/job-1")
        return httpx.Response(200, json={"id": "job-1", "status": "COMPLETE"})

    install_transport(monkeypatch, status_handler)
    status = client.get(f"/api/agm/bulk/jobs/{local_id}")
    assert status.status_code == 200
    assert status.json()["job"]["status"] == "COMPLETE"


def test_bulk_upload_requires_files(main_module, client, monkeypatch):
    connect(main_module, client)
    response = client.post(
        "/api/agm/bulk/jobs",
        data={"job_name": "Empty job"},
    )
    assert response.status_code == 400
    assert "document" in response.json()["error"].lower()


def test_sample_document_is_pdf(client):
    response = client.get("/api/agm/sample-document")
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")
