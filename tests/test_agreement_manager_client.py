"""Tests for the Agreement Manager client request building and response handling.

These exercise the real client code paths. Network calls are intercepted with
httpx's MockTransport, which is the same interface the client uses in
production -- no application code is mocked.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agreement_manager_client import (
    AgreementManagerClient,
    AgreementManagerError,
    _document_download_href,
)
from app.config import Settings


@pytest.fixture(scope="module")
def rsa_keypair(tmp_path_factory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path_factory.mktemp("keys") / "private_key.txt"
    path.write_bytes(pem)
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return str(path), public_pem


def make_settings(key_path: str) -> Settings:
    return Settings(
        integration_key="test-integration-key",
        client_secret="test-secret",
        redirect_uri="http://localhost:12000/oauth/callback",
        user_id="00000000-0000-0000-0000-000000000000",
        private_key_path=key_path,
        environment="demo",
    )


def _mock_request(handler):
    def patched_request(method, url, **kwargs):
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            return http.request(method, url, **kwargs)

    return patched_request


def _mock_post(handler):
    def patched_post(url, **kwargs):
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            return http.post(url, **kwargs)

    return patched_post


def _connected_client(account_id: str = "acct-1") -> AgreementManagerClient:
    client = AgreementManagerClient(Settings(account_id=account_id))
    client._token = type("T", (), {"access_token": "tok", "is_valid": True})()  # type: ignore
    return client


# --------------------------------------------------------------------- auth


def test_jwt_assertion_has_agm_scopes(rsa_keypair):
    key_path, public_pem = rsa_keypair
    client = AgreementManagerClient(make_settings(key_path))
    assertion = client._build_jwt()
    decoded = pyjwt.decode(
        assertion, public_pem, algorithms=["RS256"], audience="account-d.docusign.com"
    )
    assert decoded["iss"] == "test-integration-key"
    scope = decoded["scope"]
    assert "adm_store_unified_repo_read" in scope
    assert "document_uploader_write" in scope
    assert "impersonation" in scope


def test_jwt_requires_private_key():
    settings = Settings(integration_key="k", user_id="u", private_key_path="/nonexistent")
    client = AgreementManagerClient(settings)
    with pytest.raises(AgreementManagerError) as exc:
        client._build_jwt()
    assert "not configured" in str(exc.value).lower()


def test_jwt_token_exchange(rsa_keypair, monkeypatch):
    key_path, _ = rsa_keypair
    client = AgreementManagerClient(make_settings(key_path))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oauth/token"
        body = request.content.decode()
        assert "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer" in body
        return httpx.Response(
            200,
            json={"access_token": "abc123", "token_type": "Bearer", "expires_in": 3600},
        )

    monkeypatch.setattr(httpx, "post", _mock_post(handler))
    token = client.request_jwt_token()
    assert token.access_token == "abc123"
    assert token.is_valid


def test_authorization_url_requests_agm_scopes():
    settings = Settings(
        integration_key="my-key",
        client_secret="secret",
        redirect_uri="http://localhost:12000/oauth/callback",
        environment="demo",
    )
    client = AgreementManagerClient(settings)
    url = client.authorization_url("state-xyz")
    assert url.startswith("https://account-d.docusign.com/oauth/auth?")
    assert "client_id=my-key" in url
    assert "response_type=code" in url
    assert "adm_store_unified_repo_read" in url
    assert "impersonation" not in url


def test_authorization_url_requires_secret():
    client = AgreementManagerClient(Settings(integration_key="k", client_secret=""))
    with pytest.raises(AgreementManagerError):
        client.authorization_url("s")


def test_consent_required_message(monkeypatch):
    client = AgreementManagerClient(
        Settings(integration_key="k", user_id="u", private_key_path="x")
    )
    client._build_jwt = lambda: "assertion"  # type: ignore

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "consent_required"})

    monkeypatch.setattr(httpx, "post", _mock_post(handler))
    with pytest.raises(AgreementManagerError) as exc:
        client.request_jwt_token()
    assert "consent" in exc.value.message.lower()


# --------------------------------------------------------------- agreements


def test_base_url_uses_agm_host():
    demo = AgreementManagerClient(Settings(environment="demo", account_id="a"))
    prod = AgreementManagerClient(Settings(environment="prod", account_id="a"))
    assert demo._base_url() == "https://api-d.docusign.com/v1"
    assert prod._base_url() == "https://api.docusign.com/v1"


def test_list_agreements_builds_query(monkeypatch):
    client = _connected_client()
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"data": [], "metadata": {"total_items": 0}})

    monkeypatch.setattr(httpx, "request", _mock_request(handler))
    client.list_agreements(search="acme", filter_expr="status eq 'COMPLETE'", select="id,title")
    url = captured["url"]
    assert url.startswith("https://api-d.docusign.com/v1/accounts/acct-1/agreements")
    assert "search=acme" in url
    assert "select=id%2Ctitle" in url
    assert "filter=" in url


def test_get_agreement_path(monkeypatch):
    client = _connected_client()
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"id": "agm-1"})

    monkeypatch.setattr(httpx, "request", _mock_request(handler))
    result = client.get_agreement("agm-1")
    assert result["id"] == "agm-1"
    assert captured["url"].endswith("/accounts/acct-1/agreements/agm-1")


def test_patch_agreement_sends_body(monkeypatch):
    client = _connected_client()
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"id": "agm-1"})

    monkeypatch.setattr(httpx, "request", _mock_request(handler))
    client.patch_agreement("agm-1", {"title": "New", "provisions": {"governing_law": "NY"}})
    assert captured["method"] == "PATCH"
    assert captured["body"]["provisions"]["governing_law"] == "NY"


def test_patch_agreement_by_document_id_path(monkeypatch):
    client = _connected_client()
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"id": "agm-1"})

    monkeypatch.setattr(httpx, "request", _mock_request(handler))
    client.patch_agreement_by_document_id("doc-9", {"status": "COMPLETE"})
    assert captured["url"].endswith("/accounts/acct-1/agreements/documents/doc-9")


def test_change_agreement_type(monkeypatch):
    client = _connected_client()
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"id": "agm-1", "type": "Statement of Work"})

    monkeypatch.setattr(httpx, "request", _mock_request(handler))
    client.change_agreement_type("agm-1", "Statement of Work")
    assert captured["method"] == "PATCH"
    assert captured["url"].endswith("/agreements/agm-1/actions/change-type")
    assert captured["body"] == {"type": "Statement of Work"}


def test_delete_agreement(monkeypatch):
    client = _connected_client()
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        return httpx.Response(204)

    monkeypatch.setattr(httpx, "request", _mock_request(handler))
    result = client.delete_agreement("agm-1")
    assert captured["method"] == "DELETE"
    assert result == {}


def test_get_agreement_types_returns_list(monkeypatch):
    client = _connected_client()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["Statement of Work", "Non-disclosure Agreement"])

    monkeypatch.setattr(httpx, "request", _mock_request(handler))
    assert client.get_agreement_types() == ["Statement of Work", "Non-disclosure Agreement"]


def test_download_document_uses_link_href(monkeypatch):
    client = _connected_client()
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        if request.url.path.endswith("/agreements/agm-1"):
            return httpx.Response(
                200,
                json={
                    "id": "agm-1",
                    "_links": {"document": {"href": "https://api-d.docusign.com/v1/documents/doc-1"}},
                },
            )
        return httpx.Response(200, content=b"%PDF-1.4 signed")

    monkeypatch.setattr(httpx, "request", _mock_request(handler))
    content = client.download_document("agm-1")
    assert content == b"%PDF-1.4 signed"
    assert captured["url"].endswith("/documents/doc-1")


def test_document_download_href_variants():
    assert _document_download_href({"_links": {"document": {"href": "h"}}}) == "h"
    assert _document_download_href({"download_url": "d"}) == "d"
    assert _document_download_href({}) is None


def test_api_error_is_raised(monkeypatch):
    client = _connected_client()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"title": "Not Found", "detail": "Agreement missing"})

    monkeypatch.setattr(httpx, "request", _mock_request(handler))
    with pytest.raises(AgreementManagerError) as exc:
        client.get_agreement("missing")
    assert exc.value.status_code == 404
    assert "Agreement missing" in exc.value.message


# --------------------------------------------------------- custom provisions


def test_custom_provision_endpoints(monkeypatch):
    client = _connected_client()
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(httpx, "request", _mock_request(handler))
    client.create_custom_provision({"label": "P", "category": "FINANCIAL", "type": "CURRENCY"})
    client.list_custom_provisions()
    client.update_custom_provisions({"keys": ["p"]})
    client.delete_custom_provisions({"keys": ["p"]})
    methods = [m for m, _ in seen]
    assert methods == ["POST", "GET", "PATCH", "DELETE"]
    assert all(path.endswith("/custom-provisions") for _, path in seen)


def test_agreement_type_provision_paths(monkeypatch):
    client = _connected_client()
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=[])

    monkeypatch.setattr(httpx, "request", _mock_request(handler))
    client.add_agreement_type_provisions({"agreement_types": []})
    client.remove_agreement_type_provisions({"agreement_types": []})
    assert paths[0].endswith("/agreement-types/actions/add")
    assert paths[1].endswith("/agreement-types/actions/remove")


# ---------------------------------------------------------------- bulk jobs


def test_create_bulk_upload_job(monkeypatch):
    client = _connected_client()
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(201, json={"id": "job-1", "status": "OPEN"})

    monkeypatch.setattr(httpx, "request", _mock_request(handler))
    job = client.create_bulk_upload_job(
        job_name="Migration", expected_number_of_docs=3, language="en-US"
    )
    assert job["id"] == "job-1"
    assert captured["url"].endswith("/accounts/acct-1/upload/jobs")
    assert captured["body"]["expected_number_of_docs"] == 3


def test_upload_document_sets_blob_headers(monkeypatch):
    client = _connected_client()
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["headers"] = dict(request.headers)
        captured["content"] = request.content
        return httpx.Response(201)

    monkeypatch.setattr(httpx, "request", _mock_request(handler))
    client.upload_document("https://blob.example/d1", b"%PDF-1.4", file_name="a.pdf")
    assert captured["method"] == "PUT"
    assert captured["headers"]["x-ms-blob-type"] == "BlockBlob"
    assert captured["headers"]["x-ms-meta-filename"] == "a.pdf"
    assert captured["content"] == b"%PDF-1.4"


def test_complete_and_status_bulk_job(monkeypatch):
    client = _connected_client()
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"id": "job-1", "status": "UPLOAD_COMPLETE"})

    monkeypatch.setattr(httpx, "request", _mock_request(handler))
    client.complete_bulk_upload_job("job-1")
    client.get_bulk_job_status("job-1")
    assert seen[0] == ("POST", "/v1/accounts/acct-1/upload/jobs/job-1/actions/complete")
    assert seen[1] == ("GET", "/v1/accounts/acct-1/upload/jobs/job-1")
