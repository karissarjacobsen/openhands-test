"""Docusign Agreement Manager API client.

Implements the Agreement Manager endpoints this app needs directly against the
REST API, so the behaviour is easy to follow and there is no SDK to pin:

* OAuth 2.0 JWT Grant   -> service integrations (no user interaction)
* OAuth 2.0 Auth Code   -> user-interactive consent
* Agreements:list       -> agreements held in the account's AGM repository
* Agreements:get        -> one agreement with its parties and provisions
* AgreementTypes:list   -> configured agreement types for the account
* Document download     -> via an agreement's `download_url`

Docs: https://developers.docusign.com/docs/agreement-manager-api/
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx
import jwt

from .config import (
    AUTH_CODE_SCOPES,
    JWT_SCOPES,
    Settings,
)

TOKEN_REFRESH_MARGIN_SECONDS = 15 * 60


class AgreementManagerError(Exception):
    """Raised when Agreement Manager returns an error or the client is misconfigured."""

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
    base_uri: str = ""
    user_id: str = ""
    name: str = ""
    email: str = ""
    accounts: list[dict[str, Any]] = field(default_factory=list)


class AgreementManagerClient:
    """Thin, explicit wrapper around the Agreement Manager REST API."""

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
        return jwt.encode(
            payload,
            private_key,
            algorithm="RS256",
            headers={"typ": "JWT"},
        )

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
            base_uri=settings_base_uri(self.settings),
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
        return self.request_jwt_token()

    def _api_url(self, path: str) -> str:
        return f"{self.settings.agm_base_url}{path}"

    def _request(
        self,
        method: str,
        url: str,
        *,
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
                params=params,
                timeout=60,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise AgreementManagerError(
                f"Network error calling Agreement Manager: {exc}"
            ) from exc

        if response.status_code >= 400:
            raise AgreementManagerError(
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

    # ------------------------------------------------------------ agreements

    def list_agreements(
        self,
        *,
        limit: int | None = None,
        status: str | None = None,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return agreements from the account's Agreement Manager repository.

        Follows the `_links.next` pagination link until every page is read, so the
        caller receives a flat list regardless of how the API paginates.
        """
        account_id = self.account_id()
        url: str | None = self._api_url(f"/accounts/{account_id}/agreements")
        params: dict[str, Any] | None = {}
        if limit is not None:
            params["page_limit"] = limit
        if status:
            params["status"] = status
        if search:
            params["$search"] = search

        agreements: list[dict[str, Any]] = []
        while url:
            payload = self._request("GET", url, params=params or None)
            if isinstance(payload, list):
                agreements.extend(payload)
                break
            agreements.extend(payload.get("data", []) or [])
            next_link = (payload.get("_links") or {}).get("next")
            if not next_link:
                break
            # Only the first request carries query params; `next` is pre-built.
            url = next_link
            params = None
            if limit is not None and len(agreements) >= limit:
                break
        if limit is not None:
            return agreements[:limit]
        return agreements

    def get_agreement(self, agreement_id: str) -> dict[str, Any]:
        account_id = self.account_id()
        url = self._api_url(f"/accounts/{account_id}/agreements/{agreement_id}")
        payload = self._request("GET", url)
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"] or {}
        return payload

    def list_agreement_types(self) -> list[str]:
        account_id = self.account_id()
        url = self._api_url(f"/accounts/{account_id}/agreement-types")
        payload = self._request("GET", url)
        if isinstance(payload, dict) and "data" in payload:
            return list(payload["data"] or [])
        return list(payload or [])

    def download_document(self, download_url: str) -> bytes:
        """Download an agreement document from its `download_url`.

        Relative URLs are resolved against the Agreement Manager base URL. An
        access token is attached only for Docusign hosts; other hosts (for
        example a pre-signed object store URL) must not receive the bearer token.
        """
        url = download_url
        parsed = urlparse(download_url)
        auth_required = True
        if parsed.scheme and parsed.netloc:
            host = parsed.netloc.lower()
            auth_required = host.endswith("docusign.com") or host.endswith("docusign.net")
        else:
            url = f"{self.settings.agm_base_url.rstrip('/')}/{download_url.lstrip('/')}"
        content = self._request(
            "GET", url, expect_binary=True, auth_required=auth_required
        )
        if isinstance(content, bytes) and content.lstrip().startswith(b"{"):
            # Some errors still return 200 with a JSON body.
            raise AgreementManagerError(
                "Agreement Manager returned an error payload instead of a document."
            )
        return content


def settings_base_uri(settings: Settings) -> str:
    """The OAuth host for the configured environment (used for display only)."""
    return settings.agm_base_url


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
            return f"Agreement Manager API error ({response.status_code}): {message}"
    return f"Agreement Manager API error (HTTP {response.status_code})."
