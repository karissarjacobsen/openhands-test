"""Docusign eSignature REST API client.

Implements the parts of the API this app needs, directly against the REST
endpoints so the behaviour is easy to follow and there is no SDK to pin:

* OAuth 2.0 Authorization Code Grant -> user-interactive consent
* Envelopes:listStatusChanges     -> the signed-in user's envelopes
* Envelopes:create                -> upload a PDF and create/send an envelope
* Envelopes:get                   -> envelope status
* EnvelopeRecipients              -> per-recipient status
* EnvelopeDocuments:get           -> download the completed (combined) PDF

Docs: https://developers.docusign.com/docs/esign-rest-api/
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import (
    AUTH_CODE_SCOPES,
    Settings,
)

API_VERSION = "v2.1"
TOKEN_REFRESH_MARGIN_SECONDS = 15 * 60


class DocusignError(Exception):
    """Raised when Docusign returns an error or the client is misconfigured."""

    def __init__(self, message: str, status_code: int | None = None, details: Any = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details


@dataclass
class Token:
    access_token: str
    expires_at: float
    token_type: str = "Bearer"
    scope: str = ""

    @property
    def is_valid(self) -> bool:
        return bool(self.access_token) and time.time() < (
            self.expires_at - TOKEN_REFRESH_MARGIN_SECONDS
        )


@dataclass
class UserInfo:
    account_id: str
    base_uri: str
    user_id: str = ""
    name: str = ""
    email: str = ""
    accounts: list[dict[str, Any]] = field(default_factory=list)


class DocusignClient:
    """Thin, explicit wrapper around the eSignature REST API."""

    def __init__(self, settings: Settings, access_token: str | None = None):
        self.settings = settings
        self._token: Token | None = None
        self._user_info: UserInfo | None = None
        if access_token:
            # Token supplied by the Authorization Code Grant flow (stored in session).
            self._token = Token(
                access_token=access_token,
                expires_at=time.time() + 3600,
            )

    # ------------------------------------------------------- auth: authorization code

    def authorization_url(self, state: str) -> str:
        if not self.settings.auth_code_configured:
            raise DocusignError(
                "Authorization Code Grant is not configured. Set "
                "DOCUSIGN_INTEGRATION_KEY and DOCUSIGN_CLIENT_SECRET."
            )
        params = httpx.QueryParams(
            {
                "response_type": "code",
                "scope": AUTH_CODE_SCOPES,
                "client_id": self.settings.integration_key,
                "redirect_uri": self.settings.redirect_uri,
                "state": state,
            }
        )
        return f"https://{self.settings.auth_server}/oauth/auth?{params}"

    def exchange_code_for_token(self, code: str) -> Token:
        url = f"https://{self.settings.auth_server}/oauth/token"
        data = {"grant_type": "authorization_code", "code": code}
        auth = (self.settings.integration_key, self.settings.client_secret)
        try:
            response = httpx.post(url, data=data, auth=auth, timeout=30)
        except httpx.HTTPError as exc:
            raise DocusignError(f"Could not reach Docusign token endpoint: {exc}") from exc
        if response.status_code != 200:
            raise DocusignError(
                _auth_error_message(response),
                status_code=response.status_code,
                details=_safe_json(response),
            )
        payload = response.json()
        self._token = Token(
            access_token=payload["access_token"],
            expires_at=time.time() + int(payload.get("expires_in", 3600)),
            token_type=payload.get("token_type", "Bearer"),
            scope=payload.get("scope", AUTH_CODE_SCOPES),
        )
        return self._token

    # ------------------------------------------------------------- identity

    def get_user_info(self) -> UserInfo:
        if self._user_info is not None:
            return self._user_info
        payload = self._request("GET", f"https://{self.settings.auth_server}/oauth/userinfo")
        accounts = payload.get("accounts", []) or []
        account_id = self.settings.account_id
        base_uri = self.settings.default_base_uri
        if accounts:
            chosen = None
            if account_id:
                chosen = next(
                    (a for a in accounts if a.get("account_id") == account_id), None
                )
            chosen = chosen or accounts[0]
            account_id = chosen.get("account_id", account_id)
            base_uri = (chosen.get("base_uri") or base_uri).rstrip("/")
        if not account_id:
            raise DocusignError(
                "No Docusign account id available. Set DOCUSIGN_ACCOUNT_ID or "
                "ensure the authenticated user has at least one account."
            )
        self._user_info = UserInfo(
            account_id=account_id,
            base_uri=base_uri,
            user_id=payload.get("sub", ""),
            name=payload.get("name", ""),
            email=payload.get("email", ""),
            accounts=accounts,
        )
        return self._user_info

    def account_id(self) -> str:
        if self.settings.account_id:
            return self.settings.account_id
        return self.get_user_info().account_id

    # ------------------------------------------------------------ transport

    def _ensure_token(self) -> Token:
        if self._token and self._token.is_valid:
            return self._token
        raise DocusignError(
            "Not connected to Docusign. Sign in with Docusign to obtain an access token."
        )

    def _base_url(self) -> str:
        base_uri = self.settings.default_base_uri
        try:
            base_uri = self.get_user_info().base_uri
        except DocusignError:
            # Fall back to the environment default; the request will surface any error.
            pass
        return f"{base_uri.rstrip('/')}/restapi/{API_VERSION}"

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        expect_binary: bool = False,
        auth_required: bool = True,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if auth_required:
            token = self._ensure_token()
            headers["Authorization"] = f"Bearer {token.access_token}"
        try:
            response = httpx.request(
                method,
                url,
                headers=headers,
                json=json_body,
                params=params,
                timeout=60,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise DocusignError(f"Network error calling Docusign: {exc}") from exc

        if response.status_code >= 400:
            raise DocusignError(
                _api_error_message(response),
                status_code=response.status_code,
                details=_safe_json(response),
            )
        if expect_binary:
            return response.content
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {"raw": response.text}

    # ------------------------------------------------------------- envelopes

    def create_envelope(
        self,
        *,
        subject: str,
        document_base64: str,
        document_name: str,
        recipients: list[dict[str, Any]],
        status: str = "sent",
        email_blurb: str = "",
        anchor_string: str = "/sn1/",
    ) -> dict[str, Any]:
        """Create an envelope from a single PDF and optionally send it.

        Recipients are dicts with name/email/role/routing_order. Signers get a
        SignHere tab anchored on `anchor_string`, which must appear in the PDF.
        """
        account_id = self.account_id()
        signers = []
        carbon_copies = []
        for index, recipient in enumerate(recipients, start=1):
            role = (recipient.get("role") or "signer").lower()
            base = {
                "email": recipient["email"],
                "name": recipient["name"],
                "recipientId": str(index),
                "routingOrder": str(recipient.get("routing_order", index)),
            }
            if role in {"carboncopy", "cc", "carbon_copy"}:
                base["recipientId"] = str(index)
                carbon_copies.append(base)
            else:
                base["tabs"] = {
                    "signHereTabs": [
                        {
                            "anchorString": anchor_string,
                            "anchorUnits": "pixels",
                            "anchorXOffset": "20",
                            "anchorYOffset": "10",
                        }
                    ]
                }
                signers.append(base)

        if not signers and carbon_copies:
            # An envelope needs at least one signer; promote the first CC.
            promoted = carbon_copies.pop(0)
            promoted["tabs"] = {
                "signHereTabs": [{"anchorString": anchor_string, "anchorUnits": "pixels"}]
            }
            signers.append(promoted)

        if not signers:
            raise DocusignError("At least one signer recipient is required.")

        envelope_definition: dict[str, Any] = {
            "emailSubject": subject,
            "status": status,
            "documents": [
                {
                    "documentBase64": document_base64,
                    "name": document_name,
                    "fileExtension": "pdf",
                    "documentId": "1",
                }
            ],
            "recipients": {
                "signers": signers,
                "carbonCopies": carbon_copies,
            },
        }
        if email_blurb:
            envelope_definition["emailBlurb"] = email_blurb

        url = f"{self._base_url()}/accounts/{account_id}/envelopes"
        return self._request("POST", url, json_body=envelope_definition)

    def get_envelope(self, envelope_id: str) -> dict[str, Any]:
        account_id = self.account_id()
        url = f"{self._base_url()}/accounts/{account_id}/envelopes/{envelope_id}"
        return self._request("GET", url)

    def get_recipients(self, envelope_id: str) -> dict[str, Any]:
        account_id = self.account_id()
        url = (
            f"{self._base_url()}/accounts/{account_id}/envelopes/"
            f"{envelope_id}/recipients"
        )
        return self._request("GET", url)

    def list_envelopes(
        self,
        *,
        from_date: str | None = None,
        to_date: str | None = None,
        status: str | None = None,
        include_recipients: bool = False,
    ) -> dict[str, Any]:
        account_id = self.account_id()
        params: dict[str, Any] = {}
        if from_date:
            params["from_date"] = from_date
        if to_date:
            params["to_date"] = to_date
        if status:
            params["status"] = status
        if include_recipients:
            params["include"] = "recipients"
        url = f"{self._base_url()}/accounts/{account_id}/envelopes"
        return self._request("GET", url, params=params or None)

    def download_document(self, envelope_id: str, document_id: str = "combined") -> bytes:
        """Download envelope documents. `combined` yields all docs as one PDF."""
        account_id = self.account_id()
        url = (
            f"{self._base_url()}/accounts/{account_id}/envelopes/"
            f"{envelope_id}/documents/{document_id}"
        )
        content = self._request("GET", url, expect_binary=True)
        if isinstance(content, bytes) and content.lstrip().startswith(b"{"):
            # Some errors still return 200 with a JSON body.
            raise DocusignError("Docusign returned an error payload instead of a PDF.")
        return content

    def void_envelope(self, envelope_id: str, reason: str) -> dict[str, Any]:
        account_id = self.account_id()
        url = f"{self._base_url()}/accounts/{account_id}/envelopes/{envelope_id}"
        return self._request("PUT", url, json_body={"status": "voided", "voidedReason": reason})


# --------------------------------------------------------------------- helpers


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text[:500]


def _auth_error_message(response: httpx.Response) -> str:
    payload = _safe_json(response)
    if isinstance(payload, dict):
        error = payload.get("error", "")
        description = payload.get("error_description") or payload.get("message", "")
        if error == "consent_required":
            return (
                "Consent required: the impersonated user has not granted consent "
                "to this integration key. Grant consent once via the Docusign "
                "consent URL, then retry."
            )
        if error or description:
            return f"Docusign authentication failed: {error} {description}".strip()
    return f"Docusign authentication failed (HTTP {response.status_code})."


def _api_error_message(response: httpx.Response) -> str:
    payload = _safe_json(response)
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("errorCode")
        if message:
            return f"Docusign API error ({response.status_code}): {message}"
    return f"Docusign API error (HTTP {response.status_code})."


def encode_document(path: str) -> str:
    with open(path, "rb") as handle:
        return base64.b64encode(handle.read()).decode("ascii")
