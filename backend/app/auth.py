"""Supabase Auth JWT verification and owner dependency.

JWT signature and claim validation is delegated to the maintained PyJWT JOSE
implementation. The application still controls the algorithm allow-list,
JWKS retrieval, issuer, audience, expiry, and UUID subject checks explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Any
from uuid import UUID

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings
from .config import _is_https_url
from .token_crypto import decrypt_secret, encrypt_secret


_BEARER = HTTPBearer(auto_error=False)
_ALLOWED_ALGORITHMS = frozenset({"RS256", "ES256"})
PERSONAL_SESSION_COOKIE = "runwise_session"
PERSONAL_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """Claims accepted after successful JWT verification."""

    subject: UUID
    email: str | None = None
    role: str | None = None
    claims: dict[str, Any] | None = None


def _normalise_jwks(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = value.get("keys")
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("JWKS response is invalid")
    return value


class JWTVerifier:
    """Explicit Supabase JWKS verifier for RS256 and ES256 access tokens."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def _fetch_jwks(self, request: Request, *, force: bool = False) -> list[dict[str, Any]]:
        # Controlled tests may inject a static JWKS. Signatures and claims are
        # still verified by PyJWT; this only replaces the network transport.
        injected = getattr(request.app.state, "jwks_override", None)
        if injected is not None:
            return _normalise_jwks(injected)
        cache = getattr(request.app.state, "jwks_cache", None)
        now = time.time()
        if cache and not force and cache.get("expires_at", 0) > now:
            return cache["keys"]
        jwks_url = self.settings.supabase_jwks_url
        if not jwks_url or (self.settings.mode == "production" and not _is_https_url(jwks_url)):
            raise ValueError("JWKS URL is not configured securely")
        response = httpx.get(jwks_url, timeout=5.0)
        response.raise_for_status()
        keys = _normalise_jwks(response.json())
        request.app.state.jwks_cache = {"keys": keys, "expires_at": now + 300}
        return keys

    def verify(self, token: str, request: Request) -> CurrentUser:
        # get_unverified_header reads only the JOSE header so we can choose the
        # key. Claims are never read before the signature is checked by decode.
        try:
            import jwt

            header = jwt.get_unverified_header(token)
        except Exception as exc:
            # ImportError is kept in this branch so local mode can boot without
            # the optional cloud dependency; production auth then returns 401.
            raise ValueError("JWT parser is unavailable or the token is malformed") from exc
        algorithm = header.get("alg")
        if algorithm not in _ALLOWED_ALGORITHMS:
            raise ValueError("JWT algorithm is not allowed")
        kid = header.get("kid")
        keys = self._fetch_jwks(request)
        matching = [key for key in keys if (kid is None or key.get("kid") == kid)]
        if not matching:
            keys = self._fetch_jwks(request, force=True)
            matching = [key for key in keys if (kid is None or key.get("kid") == kid)]
        if len(matching) != 1:
            raise ValueError("JWT signing key was not found")
        key = matching[0]
        expected_kty = "RSA" if algorithm == "RS256" else "EC"
        if key.get("kty") != expected_kty or (key.get("alg") and key.get("alg") != algorithm):
            raise ValueError("JWT signing key type or algorithm is invalid")
        try:
            signing_key = jwt.PyJWK.from_dict(key)
            claims = jwt.decode(
                token,
                key=signing_key,
                algorithms=[algorithm],
                audience=self.settings.jwt_audience,
                issuer=self.settings.jwt_issuer,
                options={"require": ["exp", "sub", "iss", "aud"]},
                leeway=30,
            )
        except (jwt.PyJWTError, TypeError, ValueError) as exc:
            raise ValueError("JWT signature or claims are invalid") from exc
        if not isinstance(claims, dict):
            raise ValueError("JWT claims are invalid")
        try:
            subject = UUID(str(claims["sub"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("JWT subject is not a UUID") from exc
        email = claims.get("email")
        role = claims.get("role")
        return CurrentUser(
            subject=subject,
            email=email if isinstance(email, str) else None,
            role=role if isinstance(role, str) else None,
            claims=claims,
        )


def _fetch_google_jwks(request: Request, settings: Settings, *, force: bool = False) -> list[dict[str, Any]]:
    """Fetch Google's signing keys with a short in-process cache."""

    injected = getattr(request.app.state, "google_jwks_override", None)
    if injected is not None:
        return _normalise_jwks(injected)
    cache = getattr(request.app.state, "google_jwks_cache", None)
    now = time.time()
    if cache and not force and cache.get("expires_at", 0) > now:
        return cache["keys"]
    if not settings.google_jwks_url or not _is_https_url(settings.google_jwks_url):
        raise ValueError("Google ID token JWKS URL is not configured securely")
    response = httpx.get(settings.google_jwks_url, timeout=5.0)
    response.raise_for_status()
    keys = _normalise_jwks(response.json())
    request.app.state.google_jwks_cache = {"keys": keys, "expires_at": now + 300}
    return keys


def verify_google_identity(id_token: str, request: Request) -> CurrentUser:
    """Verify a Google OAuth ID token and enforce the personal owner allowlist."""

    settings: Settings = request.app.state.settings
    if settings.mode != "personal":
        raise ValueError("Google personal identity verification is only enabled in personal mode")
    try:
        import jwt

        header = jwt.get_unverified_header(id_token)
    except Exception as exc:
        raise ValueError("Google ID token is malformed") from exc
    algorithm = header.get("alg")
    if algorithm not in _ALLOWED_ALGORITHMS:
        raise ValueError("Google ID token algorithm is not allowed")
    kid = header.get("kid")
    keys = _fetch_google_jwks(request, settings)
    matching = [key for key in keys if (kid is None or key.get("kid") == kid)]
    if not matching:
        keys = _fetch_google_jwks(request, settings, force=True)
        matching = [key for key in keys if (kid is None or key.get("kid") == kid)]
    if len(matching) != 1:
        raise ValueError("Google ID token signing key was not found")
    key = matching[0]
    if key.get("kty") != ("RSA" if algorithm == "RS256" else "EC") or (key.get("alg") and key.get("alg") != algorithm):
        raise ValueError("Google ID token signing key is invalid")
    try:
        signing_key = jwt.PyJWK.from_dict(key)
        claims = jwt.decode(
            id_token,
            key=signing_key,
            algorithms=[algorithm],
            audience=settings.google_client_id,
            issuer=[settings.google_id_token_issuer, "accounts.google.com"],
            options={"require": ["exp", "sub", "iss", "aud"]},
            leeway=30,
        )
    except (jwt.PyJWTError, TypeError, ValueError) as exc:
        raise ValueError("Google ID token signature or claims are invalid") from exc
    if not isinstance(claims, dict):
        raise ValueError("Google ID token claims are invalid")
    subject = claims.get("sub")
    email = claims.get("email")
    subject_match = bool(settings.owner_google_sub and subject == settings.owner_google_sub)
    email_match = bool(
        settings.owner_google_emails
        and isinstance(email, str)
        and email.lower() in settings.owner_google_emails
        and claims.get("email_verified") is True
    )
    if not (subject_match or email_match):
        raise ValueError("Google account is not allowed for this personal app")
    try:
        owner = UUID(settings.personal_owner_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Configured personal owner ID is invalid") from exc
    return CurrentUser(subject=owner, email=email if isinstance(email, str) else None, role="personal", claims=claims)


def issue_personal_session(user: CurrentUser, settings: Settings) -> str:
    """Encrypt a short identity session; only the fixed owner UUID is stored."""

    payload = {
        "owner_id": str(user.subject),
        "google_sub": (user.claims or {}).get("sub"),
        "email": user.email,
        "email_verified": (user.claims or {}).get("email_verified") is True,
        "exp": int(time.time()) + PERSONAL_SESSION_TTL_SECONDS,
    }
    return encrypt_secret(json.dumps(payload, separators=(",", ":")), settings.token_encryption_key)


def _personal_session_user(request: Request) -> CurrentUser:
    settings: Settings = request.app.state.settings
    value = request.cookies.get(PERSONAL_SESSION_COOKIE)
    if not value:
        raise _unauthorized()
    try:
        payload = json.loads(decrypt_secret(value, settings.token_encryption_key))
        if not isinstance(payload, dict) or payload.get("owner_id") != settings.personal_owner_id:
            raise ValueError("session owner is invalid")
        if not isinstance(payload.get("exp"), (int, float)) or time.time() >= float(payload["exp"]):
            raise ValueError("session expired")
        session_sub = payload.get("google_sub")
        session_email = payload.get("email")
        subject_match = bool(settings.owner_google_sub and session_sub == settings.owner_google_sub)
        email_match = bool(
            settings.owner_google_emails
            and isinstance(session_email, str)
            and session_email.lower() in settings.owner_google_emails
            and payload.get("email_verified") is True
        )
        if not (subject_match or email_match):
            raise ValueError("session identity is no longer allowed")
        owner = UUID(settings.personal_owner_id)
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        raise _unauthorized("Session is missing or expired")
    email = payload.get("email")
    return CurrentUser(subject=owner, email=email if isinstance(email, str) else None, role="personal", claims=payload)


def _unauthorized(detail: str = "Authentication is required") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
) -> CurrentUser:
    settings: Settings = request.app.state.settings
    if settings.mode == "personal":
        return _personal_session_user(request)
    if not settings.auth_required:
        from .owner import DEV_OWNER_ID

        return CurrentUser(subject=UUID(DEV_OWNER_ID), role="local")
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()
    try:
        return JWTVerifier(settings).verify(credentials.credentials, request)
    except (ValueError, KeyError, TypeError, httpx.HTTPError, json.JSONDecodeError):
        raise _unauthorized("Invalid authentication token")


def get_current_owner(request: Request, user: CurrentUser = Depends(get_current_user)) -> str:
    """Return the immutable owner UUID used by all private queries."""

    owner_id = str(user.subject)
    request.state.owner_id = owner_id
    return owner_id


def get_optional_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
) -> CurrentUser | None:
    """Resolve an existing session/token without requiring one.

    This dependency is used only by the personal OAuth entry point, which must
    be reachable before the first session exists. Private routes use
    :func:`get_current_user` and remain fail-closed for anonymous requests.
    """

    settings: Settings = request.app.state.settings
    if settings.mode == "personal":
        # The login entry point is the one anonymous route that needs a
        # deterministic owner anchor before a session exists.
        request.state.owner_id = settings.personal_owner_id
        if not request.cookies.get(PERSONAL_SESSION_COOKIE):
            return None
        try:
            return _personal_session_user(request)
        except HTTPException:
            return None
    if not settings.auth_required:
        from .owner import DEV_OWNER_ID

        request.state.owner_id = DEV_OWNER_ID
        return CurrentUser(subject=UUID(DEV_OWNER_ID), role="local")
    if credentials is None or credentials.scheme.lower() != "bearer":
        return None
    try:
        user = JWTVerifier(settings).verify(credentials.credentials, request)
        request.state.owner_id = str(user.subject)
        return user
    except Exception:
        return None
