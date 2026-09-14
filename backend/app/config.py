"""Environment-backed configuration for the Runwise API.

The local application intentionally has a small fixed owner identity. Cloud
mode requires an explicit Supabase/Auth configuration before the application
can accept traffic; an incomplete deployment fails during startup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from urllib.parse import urlparse


def _first_env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def _parse_positive_int(value: str | None, *, default: int, field_name: str) -> int:
    try:
        parsed = int(value) if value is not None else default
    except ValueError as exc:
        raise RuntimeError(f"{field_name} must be an integer") from exc
    if parsed <= 0:
        raise RuntimeError(f"{field_name} must be greater than zero")
    return parsed


def _split_values(value: str | None) -> tuple[str, ...]:
    return tuple(item.strip().rstrip("/") for item in (value or "").split(",") if item.strip())


def _is_https_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings shared by SQLite development and cloud production."""

    # Keep the first five fields in their original order. A few local callers
    # construct Settings positionally, so new deployment settings have
    # defaults and remain backwards compatible.
    mode: str
    database_url: str
    cors_origins: tuple[str, ...]
    max_import_bytes: int
    max_import_rows: int
    supabase_url: str | None = None
    supabase_publishable_key: str | None = None
    supabase_jwks_url: str | None = None
    jwt_issuer: str | None = None
    jwt_audience: str = "authenticated"
    frontend_url: str | None = None
    public_base_url: str | None = None
    allowed_hosts: tuple[str, ...] = field(default_factory=tuple)
    token_encryption_key: str | None = None
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_redirect_uri: str | None = None
    google_jwks_url: str | None = None
    google_id_token_issuer: str = "https://accounts.google.com"
    owner_google_sub: str | None = None
    owner_google_emails: tuple[str, ...] = field(default_factory=tuple)
    personal_owner_id: str = "00000000-0000-0000-0000-000000000001"

    @classmethod
    def from_env(cls) -> "Settings":
        raw_mode = _first_env("RUNWISE_MODE", "APP_ENV", default="dev") or "dev"
        mode_aliases = {
            "development": "dev",
            "local": "dev",
            "testing": "test",
            "production": "production",
            "prod": "production",
        }
        mode = mode_aliases.get(raw_mode.lower(), raw_mode.lower())

        configured_database = _first_env("DATABASE_URL")
        if configured_database is None:
            configured_database = "sqlite:///./data/runwise.db" if mode in {"dev", "test"} else ""
        render_host = _first_env("RENDER_EXTERNAL_HOSTNAME")
        render_base_url = _first_env("RENDER_EXTERNAL_URL")
        configured_hosts = _first_env("RUNWISE_ALLOWED_HOSTS", "ALLOWED_HOSTS")
        if configured_hosts is None and render_host:
            configured_hosts = render_host
        origins_value = _first_env(
            "RUNWISE_CORS_ORIGINS",
            "CORS_ORIGINS",
            default=render_base_url or "http://localhost:5173,http://127.0.0.1:5173",
        )
        origins = _split_values(origins_value)
        if not origins:
            origins = ("http://localhost:5173", "http://127.0.0.1:5173")

        supabase_url = (_first_env("SUPABASE_URL", "RUNWISE_SUPABASE_URL") or "").rstrip("/") or None
        supabase_key = _first_env(
            "SUPABASE_PUBLISHABLE_KEY",
            "SUPABASE_ANON_KEY",
            "SUPABASE_KEY",
            "RUNWISE_SUPABASE_PUBLISHABLE_KEY",
        )
        jwks_url = _first_env("SUPABASE_JWKS_URL", "RUNWISE_SUPABASE_JWKS_URL")
        issuer = _first_env("SUPABASE_JWT_ISSUER", "RUNWISE_JWT_ISSUER")
        if issuer is None and supabase_url:
            issuer = f"{supabase_url}/auth/v1"
        if jwks_url is None and supabase_url:
            jwks_url = f"{supabase_url}/auth/v1/.well-known/jwks.json"

        frontend_url = _first_env("RUNWISE_FRONTEND_URL", "FRONTEND_URL", default=render_base_url)
        if frontend_url is None and origins:
            frontend_url = origins[0]
        public_base_url = _first_env("RUNWISE_PUBLIC_BASE_URL", "PUBLIC_BASE_URL", default=render_base_url)
        google_redirect = _first_env("RUNWISE_GOOGLE_REDIRECT_URI", "GOOGLE_REDIRECT_URI")
        if google_redirect is None and public_base_url:
            google_redirect = f"{public_base_url.rstrip('/')}/api/integrations/google-health/callback"

        return cls(
            mode=mode,
            database_url=configured_database,
            cors_origins=origins,
            max_import_bytes=_parse_positive_int(
                _first_env("RUNWISE_MAX_IMPORT_BYTES"),
                default=5 * 1024 * 1024,
                field_name="RUNWISE_MAX_IMPORT_BYTES",
            ),
            max_import_rows=_parse_positive_int(
                _first_env("RUNWISE_MAX_IMPORT_ROWS"),
                default=10_000,
                field_name="RUNWISE_MAX_IMPORT_ROWS",
            ),
            supabase_url=supabase_url,
            supabase_publishable_key=supabase_key,
            supabase_jwks_url=jwks_url,
            jwt_issuer=issuer,
            jwt_audience=_first_env("SUPABASE_JWT_AUDIENCE", "RUNWISE_JWT_AUDIENCE", default="authenticated") or "authenticated",
            frontend_url=frontend_url,
            public_base_url=public_base_url,
            allowed_hosts=_split_values(configured_hosts),
            token_encryption_key=_first_env("RUNWISE_TOKEN_ENCRYPTION_KEY", "TOKEN_ENCRYPTION_KEY"),
            google_client_id=_first_env("GOOGLE_CLIENT_ID", "RUNWISE_GOOGLE_CLIENT_ID"),
            google_client_secret=_first_env("GOOGLE_CLIENT_SECRET", "RUNWISE_GOOGLE_CLIENT_SECRET"),
            google_redirect_uri=google_redirect,
            google_jwks_url=_first_env("GOOGLE_JWKS_URL", "RUNWISE_GOOGLE_JWKS_URL", default="https://www.googleapis.com/oauth2/v3/certs"),
            google_id_token_issuer=_first_env("GOOGLE_ID_TOKEN_ISSUER", "RUNWISE_GOOGLE_ID_TOKEN_ISSUER", default="https://accounts.google.com") or "https://accounts.google.com",
            owner_google_sub=_first_env("RUNWISE_OWNER_GOOGLE_SUB", "OWNER_GOOGLE_SUB"),
            owner_google_emails=tuple(item.lower() for item in _split_values(_first_env("RUNWISE_OWNER_GOOGLE_EMAILS", "RUNWISE_OWNER_GOOGLE_EMAIL_ALLOWLIST", "OWNER_GOOGLE_EMAILS"))),
            personal_owner_id=_first_env("RUNWISE_PERSONAL_OWNER_ID", "RUNWISE_OWNER_ID", default="00000000-0000-0000-0000-000000000001") or "00000000-0000-0000-0000-000000000001",
        )

    @property
    def auth_required(self) -> bool:
        return self.mode in {"personal", "production"}

    def validate_runtime(self) -> None:
        if self.mode not in {"dev", "test", "personal", "production"}:
            raise RuntimeError("RUNWISE_MODE must be one of: dev, test, personal, production")
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is required outside local/test mode")

        if self.mode == "personal":
            if not self.google_client_id or not self.google_client_secret:
                raise RuntimeError("personal authentication requires GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET")
            if not self.owner_google_sub and not self.owner_google_emails:
                raise RuntimeError("personal authentication requires RUNWISE_OWNER_GOOGLE_SUB or an email allowlist")
            try:
                from uuid import UUID

                UUID(self.personal_owner_id)
            except (ValueError, TypeError) as exc:
                raise RuntimeError("RUNWISE_PERSONAL_OWNER_ID must be a UUID") from exc
            if not self.google_jwks_url or not _is_https_url(self.google_jwks_url):
                raise RuntimeError("Google ID token JWKS URL must use HTTPS")
            if not self.google_id_token_issuer or not _is_https_url(self.google_id_token_issuer):
                raise RuntimeError("Google ID token issuer must use HTTPS")
            if not self.frontend_url or not _is_https_url(self.frontend_url):
                raise RuntimeError("personal frontend URL must use HTTPS")
            if not self.public_base_url or not _is_https_url(self.public_base_url):
                raise RuntimeError("personal public base URL must use HTTPS")
            if not self.google_redirect_uri or not _is_https_url(self.google_redirect_uri):
                raise RuntimeError("personal Google redirect URI must use HTTPS")
            if not self.token_encryption_key:
                raise RuntimeError("personal token encryption key is required")
            if not self.allowed_hosts or any(host in {"*", ""} for host in self.allowed_hosts):
                raise RuntimeError("personal allowed hosts must be explicit")
            if any(not _is_https_url(origin) for origin in self.cors_origins):
                raise RuntimeError("personal CORS origins must use HTTPS")
            return

        if self.mode != "production":
            return

        # Keep this error explicit: a previous release allowed production to
        # start without any authentication and could expose a local database.
        if not self.supabase_url or not self.supabase_publishable_key:
            raise RuntimeError(
                "production authentication requires SUPABASE_URL and "
                "SUPABASE_PUBLISHABLE_KEY"
            )
        if not _is_https_url(self.supabase_url):
            raise RuntimeError("SUPABASE_URL must use HTTPS in production")
        if not self.database_url.lower().startswith(("postgresql://", "postgres://", "postgresql+")):
            raise RuntimeError("production DATABASE_URL must use PostgreSQL")
        if not self.jwt_issuer or not _is_https_url(self.jwt_issuer):
            raise RuntimeError("production JWT issuer must use HTTPS")
        if not self.supabase_jwks_url or not _is_https_url(self.supabase_jwks_url):
            raise RuntimeError("production JWKS URL must use HTTPS")
        if not self.jwt_audience.strip():
            raise RuntimeError("production JWT audience is required")
        if not self.frontend_url or not _is_https_url(self.frontend_url):
            raise RuntimeError("production frontend URL must use HTTPS")
        if not self.public_base_url or not _is_https_url(self.public_base_url):
            raise RuntimeError("production public base URL must use HTTPS")
        if not self.google_redirect_uri or not _is_https_url(self.google_redirect_uri):
            raise RuntimeError("production Google redirect URI must use HTTPS")
        if not self.token_encryption_key:
            raise RuntimeError("production token encryption key is required")
        if not self.allowed_hosts or any(host in {"*", ""} for host in self.allowed_hosts):
            raise RuntimeError("production allowed hosts must be explicit")
        if any(not _is_https_url(origin) for origin in self.cors_origins):
            raise RuntimeError("production CORS origins must use HTTPS")
