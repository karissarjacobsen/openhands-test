"""End-to-end tests for the Agreement Manager app.

Run with:  python -m pytest tests/ -v
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

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
    from app.db import Database, seed_sample_data

    main.db = Database(main.settings.db_path)
    seed_sample_data(main.db)

    from fastapi.testclient import TestClient

    with TestClient(main.app) as test_client:
        yield test_client


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
    response = client.get("/", params={"status": "completed"})
    assert response.status_code == 200
    assert "Acme Corp" in response.text
    assert "Globex" not in response.text


def test_agreement_detail_page(client):
    listing = client.get("/api/agreements").json()
    agreement_id = listing["agreements"][0]["id"]
    response = client.get(f"/agreements/{agreement_id}")
    assert response.status_code == 200
    assert "Recipients" in response.text
    assert "Docusign actions" in response.text


def test_agreement_detail_404(client):
    response = client.get("/agreements/does-not-exist")
    assert response.status_code == 404


def test_create_agreement_with_template(client):
    response = client.post(
        "/agreements",
        data={
            "title": "Test Widget Agreement",
            "client_name": "Widget Co",
            "agreement_type": "Sales Agreement",
            "value": "1234.56",
            "currency": "USD",
            "owner": "tester@example.com",
            "effective_date": "2026-03-01",
            "notes": "Please sign promptly.",
            "template_key": "sales-agreement",
            "recipient_name": ["Ada Lovelace"],
            "recipient_email": ["ada@example.com"],
            "recipient_role": ["signer"],
            "action": "save",
        },
        follow_redirects=False,
    )
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
    listing = client.get("/api/agreements").json()
    draft = next(a for a in listing["agreements"] if a["status"] == "draft")
    response = client.post(f"/agreements/{draft['id']}/send")
    assert response.status_code == 400
    payload = response.json()
    assert payload["ok"] is False
    assert "connect" in payload["error"].lower()


def test_refresh_without_envelope_returns_error(client):
    listing = client.get("/api/agreements").json()
    draft = next(a for a in listing["agreements"] if a["status"] == "draft")
    response = client.post(f"/agreements/{draft['id']}/refresh")
    assert response.status_code == 400
    assert response.json()["ok"] is False


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
    assert response.json()["connected"] is False


def test_delete_agreement(client):
    listing = client.get("/api/agreements").json()
    target = listing["agreements"][0]["id"]
    response = client.post(f"/agreements/{target}/delete", follow_redirects=False)
    assert response.status_code == 303
    assert client.get(f"/api/agreements/{target}").status_code == 404


def test_settings_page_renders(client):
    response = client.get("/settings")
    assert response.status_code == 200
    assert "Connection" in response.text


def test_oauth_connect_without_credentials_redirects_with_error(client):
    response = client.get("/oauth/connect", follow_redirects=False)
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_download_missing_document_404(client):
    response = client.post(
        "/agreements",
        data={
            "title": "No document agreement",
            "recipient_name": ["N"],
            "recipient_email": ["n@example.com"],
            "recipient_role": ["signer"],
            "action": "save",
        },
        follow_redirects=False,
    )
    agreement_id = response.headers["location"].rsplit("/", 1)[1]
    download = client.get(f"/agreements/{agreement_id}/download")
    assert download.status_code == 404
