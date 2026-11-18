"""Application configuration loaded from environment variables / .env file."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "agreements.db"

ENVIRONMENTS = {
    "demo": {
        "auth_server": "account-d.docusign.com",
        "agm_base_uri": "https://api-d.docusign.com",
    },
    "prod": {
        "auth_server": "account.docusign.com",
        "agm_base_uri": "https://api.docusign.com",
    },
}

# OAuth scopes for the Agreement Manager (IAM) API. These are requested in
# addition to the standard Docusign scopes; the access token is the same one
# used across Docusign APIs.
AGM_SCOPES = " ".join(
    [
        "adm_store_unified_repo_read",   # read agreements
        "adm_store_unified_repo_write",  # patch / change-type / delete agreements
        "document_uploader_read",        # bulk job status
        "document_uploader_write",       # bulk job create + complete
        "public_dms_document_read",      # download agreement documents
        "search_read",                   # $search on the agreement list
    ]
)

# `impersonation` is only used by JWT Grant; Authorization Code Grant must not
# request it.
JWT_SCOPES = f"{AGM_SCOPES} impersonation"
AUTH_CODE_SCOPES = AGM_SCOPES


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so we do not need an extra dependency."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Real environment variables win over the file.
        os.environ.setdefault(key, value)


@dataclass
class Settings:
    integration_key: str = ""
    client_secret: str = ""
    redirect_uri: str = "http://localhost:12000/oauth/callback"
    user_id: str = ""
    private_key_path: str = ""
    environment: str = "demo"
    account_id: str = ""
    session_secret: str = ""
    port: int = 12000
    base_dir: Path = BASE_DIR
    data_dir: Path = DATA_DIR
    upload_dir: Path = UPLOAD_DIR
    db_path: Path = DB_PATH
    _private_key_cache: str | None = field(default=None, repr=False)

    @property
    def auth_server(self) -> str:
        env = ENVIRONMENTS.get(self.environment, ENVIRONMENTS["demo"])
        return env["auth_server"]

    @property
    def agm_base_uri(self) -> str:
        env = ENVIRONMENTS.get(self.environment, ENVIRONMENTS["demo"])
        return env["agm_base_uri"]

    @property
    def jwt_configured(self) -> bool:
        return bool(self.integration_key and self.user_id and self._private_key())

    @property
    def auth_code_configured(self) -> bool:
        return bool(self.integration_key and self.client_secret)

    def _private_key(self) -> str:
        if self._private_key_cache is not None:
            return self._private_key_cache
        if not self.private_key_path:
            return ""
        path = Path(self.private_key_path)
        if not path.is_absolute():
            path = self.base_dir / path
        if not path.exists():
            return ""
        self._private_key_cache = path.read_text(encoding="utf-8").strip()
        return self._private_key_cache

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    _load_dotenv(BASE_DIR / ".env")
    settings = Settings(
        integration_key=os.environ.get("DOCUSIGN_INTEGRATION_KEY", "").strip(),
        client_secret=os.environ.get("DOCUSIGN_CLIENT_SECRET", "").strip(),
        redirect_uri=os.environ.get(
            "DOCUSIGN_REDIRECT_URI", "http://localhost:12000/oauth/callback"
        ).strip(),
        user_id=os.environ.get("DOCUSIGN_USER_ID", "").strip(),
        private_key_path=os.environ.get("DOCUSIGN_PRIVATE_KEY_PATH", "").strip(),
        environment=os.environ.get("DOCUSIGN_ENVIRONMENT", "demo").strip().lower(),
        account_id=os.environ.get("DOCUSIGN_ACCOUNT_ID", "").strip(),
        session_secret=os.environ.get("APP_SESSION_SECRET", "").strip(),
        port=int(os.environ.get("APP_PORT", "12000")),
    )
    if settings.environment not in ENVIRONMENTS:
        settings.environment = "demo"
    if not settings.session_secret:
        # Ephemeral secret: sessions simply reset on restart. Never used in prod.
        settings.session_secret = secrets.token_hex(32)
    settings.ensure_dirs()
    return settings
