"""Server-side Fernet encryption for OAuth secrets."""

from __future__ import annotations

try:  # Keep local SQLite startup usable before optional cloud dependencies install.
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - exercised only in minimal dev envs.
    Fernet = None  # type: ignore[assignment,misc]

    class InvalidToken(Exception):
        pass


def _fernet(key: str) -> Fernet:
    if Fernet is None:
        raise ValueError("cryptography is required for OAuth secret encryption")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError, UnicodeEncodeError) as exc:
        raise ValueError("RUNWISE_TOKEN_ENCRYPTION_KEY is not a valid Fernet key") from exc


def encrypt_secret(value: str, key: str | None) -> str:
    if not key:
        raise ValueError("server token encryption key is not configured")
    return _fernet(key).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str, key: str | None) -> str:
    if not key:
        raise ValueError("server token encryption key is not configured")
    try:
        return _fernet(key).decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError) as exc:
        raise ValueError("stored OAuth secret could not be decrypted") from exc
