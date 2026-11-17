"""Tests for the Agreement Manager client request building and response handling.

These exercise the real client code paths. Network calls are intercepted with
httpx's MockTransport, which is the same interface the client uses in
production -- no application code is mocked.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agm_client import AgreementManagerClient, AgreementManagerError
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


def make_settings(key_path: str = "") -> Settings:
    return Settings(
        integration_key="test-integration-key",
        client_secret="test-secret",
        redirect_uri="http://localhost:12000/oauth/callback",
        user_id="00000000-0000-0000-0000-000000000000",
        private_key_path=key_path,
        environment="demo",
    )


def _mock_post(handler):
    def patched_post(url, **kwargs):
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            return http.post(url, **kwargs)

    return patched_post


def _mock_request(handler):
    def patched_request(method, url, **kwargs):
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            return http.request(method, url, **kwargs)

    return patched_request


def _connected_client(account_id: str = "acct-1") -> AgreementManagerClient:
    client = AgreementManagerClient(Settings(account_id=account_id))
    client._token = type("T", (), {"access_token": "tok", "is_valid": True})()  # type: ignore
    client.get_user_info = lambda: type(  # type: ignore
        "U", (), {"account_id": account_id, "base_uri": "https://api-d.docusign.com/v1"}
    )()
    return client


# ------------------------------------------------------------------- auth


def test_jwt_assertion_requests_agm_scopes(rsa_keypair):
    key_path, public_pem = rsa_keypair
    client = AgreementManagerClient(make_settings(key_path))
    assertion = client._build_jwt()
    decoded = pyjwt.decode(
        assertion, public_pem, algorithms=["RS256"], audience="account-d.docusign.com"
    )
    assert decoded["iss"] == "test-integration-key"
    assert decoded["aud"] == "account-d.docusign.com"
    assert "adm_store_unified_repo_read" in decoded["scope"]
    assert "impersonation" in decoded["scope"]
    assert decoded["exp"] > decoded["iat"]


def test_jwt_requires_private_key():
    settings = Settings(integration_key="k", user_id="u", private_key_path="/nonexistent")
    client = AgreementManagerClient(settings)
    with pytest.raises(AgreementManagerError) as exc:
        client._build_jwt()
    assert "not configured" in str(exc.value).lower()


def test_authorization_url_contains_agm_scope():
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
    assert "state=state-xyz" in url
    assert "adm_store_unified_repo_read" in url


def test_authorization_url_requires_secret():
    client = AgreementManagerClient(Settings(integration_key="k", client_secret=""))
    with pytest.raises(AgreementManagerError):
        client.authorization_url("s")


def test_api_base_url_per_environment():
    assert make_settings().agm_base_url == "https://api-d.docusign.com/v1"
    prod = Settings(environment="prod")
    assert prod.agm_base_url == "https://api.docusign.com/v1"


# ------------------------------------------------------------- agreements


def test_list_agreements_uses_agm_endpoint_and_returns_data(monkeypatch):
    client = _connected_client()
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "ag-1", "file_name": "NDA.pdf", "status": "COMPLETE"},
                    {"id": "ag-2", "file_name": "SOW.pdf", "status": "PENDING"},
                ],
                "metadata": {"total_items": 2},
            },
        )

    monkeypatch.setattr(httpx, "request", _mock_request(handler))
    agreements = client.list_agreements(limit=50)
    assert [a["id"] for a in agreements] == ["ag-1", "ag-2"]
    assert captured["url"].startswith(
        "https://api-d.docusign.com/v1/accounts/acct-1/agreements"
    )
    assert "page_limit=50" in captured["url"]
    assert captured["auth"] == "Bearer tok"


def test_list_agreements_follows_pagination(monkeypatch):
    client = _connected_client()
    pages = {
        None: {
            "data": [{"id": "ag-1"}],
            "_links": {"next": "https://api-d.docusign.com/v1/accounts/acct-1/agreements?page=2"},
        },
        "https://api-d.docusign.com/v1/accounts/acct-1/agreements?page=2": {
            "data": [{"id": "ag-2"}],
        },
    }
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        payload = pages[None] if len(seen) == 1 else pages[url]
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(httpx, "request", _mock_request(handler))
    agreements = client.list_agreements()
    assert [a["id"] for a in agreements] == ["ag-1", "ag-2"]
    assert len(seen) == 2


def test_list_agreements_accepts_bare_array(monkeypatch):
    client = _connected_client()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "ag-1"}, {"id": "ag-2"}])

    monkeypatch.setattr(httpx, "request", _mock_request(handler))
    assert len(client.list_agreements()) == 2


def test_get_agreement_unwraps_data(monkeypatch):
    client = _connected_client()
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": "ag-1",
                    "type": "Statement of Work",
                    "parties": [{"id": "p1", "name_in_agreement": "ACME, INC"}],
                    "provisions": {"effective_date": "2024-09-01T00:00:00Z"},
                }
            },
        )

    monkeypatch.setattr(httpx, "request", _mock_request(handler))
    agreement = client.get_agreement("ag-1")
    assert agreement["id"] == "ag-1"
    assert agreement["parties"][0]["name_in_agreement"] == "ACME, INC"
    assert captured["url"].endswith("/accounts/acct-1/agreements/ag-1")


def test_list_agreement_types(monkeypatch):
    client = _connected_client()

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/accounts/acct-1/agreement-types")
        return httpx.Response(200, json=["Non-disclosure Agreement", "Statement of Work"])

    monkeypatch.setattr(httpx, "request", _mock_request(handler))
    assert client.list_agreement_types() == [
        "Non-disclosure Agreement",
        "Statement of Work",
    ]


# -------------------------------------------------------------- downloads


def test_download_document_sends_token_for_docusign_host(monkeypatch):
    client = _connected_client()
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, content=b"%PDF-1.4 agreement")

    monkeypatch.setattr(httpx, "request", _mock_request(handler))
    content = client.download_document(
        "https://api-d.docusign.com/v1/accounts/acct-1/agreements/ag-1/document"
    )
    assert content == b"%PDF-1.4 agreement"
    assert captured["auth"] == "Bearer tok"


def test_download_document_skips_token_for_external_host(monkeypatch):
    client = _connected_client()
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, content=b"%PDF-1.4 pre-signed")

    monkeypatch.setattr(httpx, "request", _mock_request(handler))
    client.download_document("https://bucket.s3.amazonaws.com/ag-1.pdf?signature=abc")
    assert captured["auth"] is None


def test_download_document_rejects_json_error_body(monkeypatch):
    client = _connected_client()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"message": "not found"}')

    monkeypatch.setattr(httpx, "request", _mock_request(handler))
    with pytest.raises(AgreementManagerError):
        client.download_document(
            "https://api-d.docusign.com/v1/accounts/acct-1/agreements/ag-1/document"
        )


# ---------------------------------------------------------------- errors


def test_api_error_is_raised(monkeypatch):
    client = _connected_client()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "Invalid agreement id"})

    monkeypatch.setattr(httpx, "request", _mock_request(handler))
    with pytest.raises(AgreementManagerError) as exc:
        client.get_agreement("nope")
    assert exc.value.status_code == 400
    assert "Invalid agreement id" in exc.value.message
    assert "Agreement Manager" in exc.value.message


def test_consent_required_message(rsa_keypair, monkeypatch):
    key_path, _ = rsa_keypair
    client = AgreementManagerClient(make_settings(key_path))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "consent_required"})

    monkeypatch.setattr(httpx, "post", _mock_post(handler))
    with pytest.raises(AgreementManagerError) as exc:
        client.request_jwt_token()
    assert "consent" in exc.value.message.lower()
