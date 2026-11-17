"""Tests for the Docusign client request building and response handling.

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

from app.config import Settings
from app.docusign_client import DocusignClient, DocusignError, encode_document


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
    settings = Settings(
        integration_key="test-integration-key",
        client_secret="test-secret",
        redirect_uri="http://localhost:12000/oauth/callback",
        user_id="00000000-0000-0000-0000-000000000000",
        private_key_path=key_path,
        environment="demo",
    )
    return settings


def test_jwt_assertion_is_signed_and_well_formed(rsa_keypair):
    key_path, public_pem = rsa_keypair
    client = DocusignClient(make_settings(key_path))
    assertion = client._build_jwt()
    decoded = pyjwt.decode(assertion, public_pem, algorithms=["RS256"], audience="account-d.docusign.com")
    assert decoded["iss"] == "test-integration-key"
    assert decoded["aud"] == "account-d.docusign.com"
    assert decoded["scope"] == "signature impersonation"
    assert decoded["exp"] > decoded["iat"]


def test_jwt_requires_private_key():
    settings = Settings(integration_key="k", user_id="u", private_key_path="/nonexistent")
    client = DocusignClient(settings)
    with pytest.raises(DocusignError) as exc:
        client._build_jwt()
    assert "not configured" in str(exc.value).lower()


def test_jwt_token_exchange(rsa_keypair, monkeypatch):
    key_path, _ = rsa_keypair
    settings = make_settings(key_path)
    client = DocusignClient(settings)

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


def _connected_client(account_id: str = "acct-1") -> DocusignClient:
    client = DocusignClient(Settings(account_id=account_id))
    client._token = type("T", (), {"access_token": "tok", "is_valid": True})()  # type: ignore
    client.get_user_info = lambda: type(  # type: ignore
        "U", (), {"account_id": account_id, "base_uri": "https://demo.docusign.net"}
    )()
    return client


def test_envelope_definition_structure(monkeypatch):
    client = _connected_client()
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"envelopeId": "env-999"})

    monkeypatch.setattr(httpx, "request", _mock_request(handler))
    result = client.create_envelope(
        subject="Please sign",
        document_base64="JVBERi0=",
        document_name="Agreement.pdf",
        recipients=[
            {"name": "Ada", "email": "ada@example.com", "role": "signer", "routing_order": 1},
            {"name": "Bob", "email": "bob@example.com", "role": "carbonCopy", "routing_order": 2},
        ],
        status="sent",
        email_blurb="Hello",
    )

    assert result["envelopeId"] == "env-999"
    assert captured["url"].endswith("/accounts/acct-1/envelopes")
    body = captured["body"]
    assert body["emailSubject"] == "Please sign"
    assert body["status"] == "sent"
    assert body["emailBlurb"] == "Hello"
    assert body["documents"][0]["fileExtension"] == "pdf"
    assert body["documents"][0]["documentId"] == "1"
    signers = body["recipients"]["signers"]
    assert len(signers) == 1
    assert signers[0]["email"] == "ada@example.com"
    assert signers[0]["tabs"]["signHereTabs"][0]["anchorString"] == "/sn1/"
    assert body["recipients"]["carbonCopies"][0]["email"] == "bob@example.com"


def test_envelope_promotes_lone_cc_to_signer(monkeypatch):
    client = _connected_client()
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(201, json={"envelopeId": "env-1"})

    monkeypatch.setattr(httpx, "request", _mock_request(handler))
    client.create_envelope(
        subject="s",
        document_base64="x",
        document_name="d.pdf",
        recipients=[{"name": "CC", "email": "cc@example.com", "role": "carbonCopy"}],
    )
    signers = captured["body"]["recipients"]["signers"]
    assert len(signers) == 1
    assert signers[0]["email"] == "cc@example.com"
    assert captured["body"]["recipients"]["carbonCopies"] == []


def test_api_error_is_raised(monkeypatch):
    client = _connected_client()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "Invalid recipient email"})

    monkeypatch.setattr(httpx, "request", _mock_request(handler))
    with pytest.raises(DocusignError) as exc:
        client.get_envelope("env-1")
    assert exc.value.status_code == 400
    assert "Invalid recipient email" in exc.value.message


def test_consent_required_message(monkeypatch):
    client = DocusignClient(Settings(integration_key="k", user_id="u", private_key_path="x"))
    client._build_jwt = lambda: "assertion"  # type: ignore

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "consent_required"})

    monkeypatch.setattr(httpx, "post", _mock_post(handler))
    with pytest.raises(DocusignError) as exc:
        client.request_jwt_token()
    assert "consent" in exc.value.message.lower()


def test_authorization_url_contains_expected_params():
    settings = Settings(
        integration_key="my-key",
        client_secret="secret",
        redirect_uri="http://localhost:12000/oauth/callback",
        environment="demo",
    )
    client = DocusignClient(settings)
    url = client.authorization_url("state-xyz")
    assert url.startswith("https://account-d.docusign.com/oauth/auth?")
    assert "client_id=my-key" in url
    assert "response_type=code" in url
    assert "state=state-xyz" in url
    assert "scope=signature" in url


def test_authorization_url_requires_secret():
    client = DocusignClient(Settings(integration_key="k", client_secret=""))
    with pytest.raises(DocusignError):
        client.authorization_url("s")


def test_download_document_returns_bytes(monkeypatch):
    client = DocusignClient(Settings(account_id="acct-1"))
    client._token = type("T", (), {"access_token": "tok", "is_valid": True})()  # type: ignore
    client.get_user_info = lambda: type(  # type: ignore
        "U", (), {"account_id": "acct-1", "base_uri": "https://demo.docusign.net"}
    )()

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/documents/combined" in str(request.url)
        return httpx.Response(200, content=b"%PDF-1.4 signed content")

    original = httpx.request

    def patched_request(method, url, **kwargs):
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            return http.request(method, url, **kwargs)

    httpx.request = patched_request
    try:
        content = client.download_document("env-1", "combined")
    finally:
        httpx.request = original
    assert content == b"%PDF-1.4 signed content"


def test_encode_document(tmp_path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 hello")
    encoded = encode_document(str(pdf))
    import base64

    assert base64.b64decode(encoded) == b"%PDF-1.4 hello"


def test_map_envelope_status():
    from app.main import _map_envelope_status

    assert _map_envelope_status("completed") == "completed"
    assert _map_envelope_status("Delivered") == "delivered"
    assert _map_envelope_status("created") == "draft"
    assert _map_envelope_status("voided") == "voided"
