"""Docusign Agreement Manager (IAM) REST client.

Docusign's IAM SDK ships for C# and TypeScript only, so this module calls the
same Agreement Manager REST endpoints that the IAM SDK wraps. The base path is
`https://api-d.docusign.com/v1` in the demo environment and
`https://api.docusign.com/v1` in production.

Endpoints covered (all account-scoped):

* Agreements:list            -> GET    /accounts/{id}/agreements
* Agreements:get             -> GET    /accounts/{id}/agreements/{agreementId}
* Agreements:getTypes        -> GET    /accounts/{id}/agreement-types
* Agreements:patch           -> PATCH  /accounts/{id}/agreements/{agreementId}
* Agreements:patchByDocId    -> PATCH  /accounts/{id}/agreements/documents/{documentId}
* Agreements:changeType      -> PATCH  /accounts/{id}/agreements/{agreementId}/actions/change-type
* Agreements:delete          -> DELETE /accounts/{id}/agreements/{agreementId}
* Agreements:download        -> GET    {_links.document.href}
* CustomProvisions:create    -> POST   /accounts/{id}/custom-provisions
* CustomProvisions:get       -> GET    /accounts/{id}/custom-provisions
* CustomProvisions:update    -> PATCH  /accounts/{id}/custom-provisions
* CustomProvisions:delete    -> DELETE /accounts/{id}/custom-provisions
* CustomProvisions:addToType -> POST   /accounts/{id}/agreement-types/actions/add
* CustomProvisions:removeFromType -> POST /accounts/{id}/agreement-types/actions/remove
* BulkJob:create             -> POST   /accounts/{id}/upload/jobs
* BulkJob:uploadDocument     -> PUT    {presigned upload url}
* BulkJob:complete           -> POST   /accounts/{id}/upload/jobs/{jobId}/actions/complete
* BulkJob:getStatus          -> GET    /accounts/{id}/upload/jobs/{jobId}

Docs: https://developers.docusign.com/docs/agreement-manager-api/
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt

from .config import (
    AUTH_CODE_SCOPES,
    JWT_SCOPES,
    Settings,
)

API_VERSION = "v1"
TOKEN_REFRESH_MARGIN_SECONDS = 15 * 60


class AgreementManagerError(Exception):
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
    name: str = ""
    email: str = ""
    user_id: str = ""
    accounts: list[dict[str, Any]] = field(default_factory=list)


class AgreementManagerClient:
    """Thin, explicit wrapper around the Agreement Manager REST API."""

    def __init__(self, settings: Settings, access_token: str | None = None):
        self.settings = settings
        self._token: Token | None = None
        self._user_info: UserInfo | None = None
        if access_token:
            # Token supplied by the Authorization Code Grant flow (stored in session).
            self._token = Token(access_token=access_token, expires_at=time.time() + 3600)

    # ------------------------------------------------------------- auth: JWT

    def _build_jwt(self) -> str:
        private_key = self.settings._private_key()
        if not (self.settings.integration_key and self.settings.user_id and private_key):
            raise AgreementManagerError(
                "JWT Grant is not configured. Set DOCUSIGN_INTEGRATION_KEY, "
                "DOCUSIGN_USER_ID and DOCUSIGN_PRIVATE_KEY_PATH."
            )
        now = int(time.time())
        payload = {
            "iss": self.settings.integration_key,
            "sub": self.settings.user_id,
            "aud": self.settings.auth_server,
            "iat": now,
            "exp": now + 3600,
            "scope": JWT_SCOPES,
        }
        return jwt.encode(payload, private_key, algorithm="RS256", headers={"typ": "JWT"})

    def request_jwt_token(self) -> Token:
        """Exchange a signed JWT assertion for an access token."""
        assertion = self._build_jwt()
        url = f"https://{self.settings.auth_server}/oauth/token"
        data = {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        }
        try:
            response = httpx.post(url, data=data, timeout=30)
        except httpx.HTTPError as exc:
            raise AgreementManagerError(
                f"Could not reach Docusign token endpoint: {exc}"
            ) from exc
        if response.status_code != 200:
            raise AgreementManagerError(
                _auth_error_message(response),
                status_code=response.status_code,
                details=_safe_json(response),
            )
        payload = response.json()
        self._token = Token(
            access_token=payload["access_token"],
            expires_at=time.time() + int(payload.get("expires_in", 3600)),
            token_type=payload.get("token_type", "Bearer"),
            scope=payload.get("scope", JWT_SCOPES),
        )
        return self._token

    # --------------------------------------------------- auth: authorization code

    def authorization_url(self, state: str) -> str:
        if not self.settings.auth_code_configured:
            raise AgreementManagerError(
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
            raise AgreementManagerError(
                f"Could not reach Docusign token endpoint: {exc}"
            ) from exc
        if response.status_code != 200:
            raise AgreementManagerError(
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
        if accounts:
            chosen = None
            if account_id:
                chosen = next(
                    (a for a in accounts if a.get("account_id") == account_id), None
                )
            chosen = chosen or accounts[0]
            account_id = chosen.get("account_id", account_id)
        if not account_id:
            raise AgreementManagerError(
                "No Docusign account id available. Set DOCUSIGN_ACCOUNT_ID or "
                "ensure the authenticated user has at least one account."
            )
        self._user_info = UserInfo(
            account_id=account_id,
            name=payload.get("name", ""),
            email=payload.get("email", ""),
            user_id=payload.get("sub", ""),
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
        return self.request_jwt_token()

    def _base_url(self) -> str:
        return f"{self.settings.agm_base_uri.rstrip('/')}/{API_VERSION}"

    def _account_url(self, path: str) -> str:
        return f"{self._base_url()}/accounts/{self.account_id()}{path}"

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        expect_binary: bool = False,
        auth_required: bool = True,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if auth_required:
            token = self._ensure_token()
            headers["Authorization"] = f"Bearer {token.access_token}"
        if extra_headers:
            headers.update(extra_headers)
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
            raise AgreementManagerError(f"Network error calling Docusign: {exc}") from exc

        if response.status_code >= 400:
            raise AgreementManagerError(
                _api_error_message(response),
                status_code=response.status_code,
                details=_safe_json(response),
            )
        if expect_binary:
            return response.content
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {"raw": response.text}

    # ------------------------------------------------------------- agreements

    def list_agreements(
        self,
        *,
        filter_expr: str | None = None,
        select: str | None = None,
        search: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """List agreements. Supports OData `$filter`, `$select` and `$search`."""
        params: dict[str, Any] = {}
        if filter_expr:
            params["$filter"] = filter_expr
        if select:
            params["$select"] = select
        if search:
            params["$search"] = search
        if extra:
            params.update(extra)
        url = self._account_url("/agreements")
        return self._request("GET", url, params=params or None)

    def get_agreement(self, agreement_id: str) -> dict[str, Any]:
        url = self._account_url(f"/agreements/{agreement_id}")
        return self._request("GET", url)

    def get_agreement_types(self) -> list[str]:
        url = self._account_url("/agreement-types")
        payload = self._request("GET", url)
        return payload if isinstance(payload, list) else payload.get("data", [])

    def patch_agreement(self, agreement_id: str, body: dict[str, Any]) -> dict[str, Any]:
        url = self._account_url(f"/agreements/{agreement_id}")
        return self._request("PATCH", url, json_body=body)

    def patch_agreement_by_document_id(
        self, document_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        url = self._account_url(f"/agreements/documents/{document_id}")
        return self._request("PATCH", url, json_body=body)

    def change_agreement_type(self, agreement_id: str, agreement_type: str) -> dict[str, Any]:
        url = self._account_url(f"/agreements/{agreement_id}/actions/change-type")
        return self._request("PATCH", url, json_body={"type": agreement_type})

    def delete_agreement(self, agreement_id: str) -> dict[str, Any]:
        url = self._account_url(f"/agreements/{agreement_id}")
        return self._request("DELETE", url)

    def download_document(self, agreement_id: str) -> bytes:
        """Download the agreement's original document via its `_links.document` href."""
        agreement = self.get_agreement(agreement_id)
        href = _document_download_href(agreement)
        if not href:
            raise AgreementManagerError(
                "This agreement has no downloadable document link."
            )
        return self._request("GET", href, expect_binary=True)

    # -------------------------------------------------------- custom provisions

    def create_custom_provision(self, body: dict[str, Any]) -> dict[str, Any]:
        url = self._account_url("/custom-provisions")
        return self._request("POST", url, json_body=body)

    def list_custom_provisions(self) -> dict[str, Any]:
        url = self._account_url("/custom-provisions")
        return self._request("GET", url)

    def update_custom_provisions(self, body: Any) -> dict[str, Any]:
        url = self._account_url("/custom-provisions")
        return self._request("PATCH", url, json_body=body)

    def delete_custom_provisions(self, body: Any) -> dict[str, Any]:
        url = self._account_url("/custom-provisions")
        return self._request("DELETE", url, json_body=body)

    def add_agreement_type_provisions(self, body: dict[str, Any]) -> Any:
        url = self._account_url("/agreement-types/actions/add")
        return self._request("POST", url, json_body=body)

    def remove_agreement_type_provisions(self, body: dict[str, Any]) -> Any:
        url = self._account_url("/agreement-types/actions/remove")
        return self._request("POST", url, json_body=body)

    # -------------------------------------------------------------- bulk jobs

    def create_bulk_upload_job(
        self,
        *,
        job_name: str,
        expected_number_of_docs: int,
        language: str = "en-US",
        agreement_set_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "job_name": job_name,
            "expected_number_of_docs": expected_number_of_docs,
            "language": language,
        }
        if agreement_set_ids:
            body["agreement_set_ids"] = agreement_set_ids
        url = self._account_url("/upload/jobs")
        return self._request("POST", url, json_body=body)

    def upload_document(
        self,
        upload_url: str,
        content: bytes,
        *,
        file_name: str,
        content_type: str = "application/pdf",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """PUT a document binary to a pre-signed upload URL from a bulk job."""
        headers = {
            "x-ms-blob-type": "BlockBlob",
            "Content-Type": content_type,
            "x-ms-meta-filename": file_name,
        }
        if metadata is not None:
            import json

            headers["x-ms-meta-metadata"] = json.dumps(metadata)
        try:
            response = httpx.request(
                "PUT", upload_url, content=content, headers=headers, timeout=300
            )
        except httpx.HTTPError as exc:
            raise AgreementManagerError(f"Could not upload document: {exc}") from exc
        if response.status_code >= 400:
            raise AgreementManagerError(
                f"Document upload failed (HTTP {response.status_code}).",
                status_code=response.status_code,
                details=response.text[:500],
            )

    def complete_bulk_upload_job(self, job_id: str) -> dict[str, Any]:
        url = self._account_url(f"/upload/jobs/{job_id}/actions/complete")
        return self._request("POST", url)

    def get_bulk_job_status(self, job_id: str) -> dict[str, Any]:
        url = self._account_url(f"/upload/jobs/{job_id}")
        return self._request("GET", url)


# --------------------------------------------------------------------- helpers


def _document_download_href(agreement: dict[str, Any]) -> str | None:
    links = agreement.get("_links") or {}
    document = links.get("document")
    if isinstance(document, dict):
        href = document.get("href")
        if href:
            return href
    for key in ("download_url", "document_url"):
        if agreement.get(key):
            return agreement[key]
    return None


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
        # RFC 9457 problem+json uses `title`/`detail`; older payloads use `message`.
        message = (
            payload.get("message")
            or payload.get("detail")
            or payload.get("title")
            or payload.get("errorCode")
        )
        if message:
            return f"Docusign Agreement Manager error ({response.status_code}): {message}"
    return f"Docusign Agreement Manager error (HTTP {response.status_code})."
