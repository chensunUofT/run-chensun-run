"""Owner-bound Google Health OAuth and bounded synchronization routes."""

from __future__ import annotations

import base64
from datetime import date, datetime, timedelta, timezone
import hashlib
import hmac
import importlib
import json
import math
import re
import secrets
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, status
from starlette.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .auth import (
    PERSONAL_SESSION_COOKIE,
    PERSONAL_SESSION_TTL_SECONDS,
    get_current_owner,
    get_optional_current_user,
    issue_personal_session,
    verify_google_identity,
)
from .config import Settings
from .db import GoogleConnection, OAuthState, Run, RunStream, get_db
from .schemas import GoogleDataInspection, GoogleSyncRequest
from .token_crypto import decrypt_secret, encrypt_secret


OAUTH_STATE_TTL = timedelta(minutes=10)
GOOGLE_CALLBACK_PATH = "/api/integrations/google-health/callback"
# Route exports are deliberately bounded per sync.  A full-history import can
# contain thousands of exercises; summaries are imported for all pages, while
# detailed TCX streams are attached to a bounded batch and retried on the next
# sync.  This keeps a Render instance from retaining an account's entire route
# history in memory in one request.
MAX_ROUTE_EXPORTS_PER_SYNC = 50
MAX_SAMPLE_PAGES_PER_SYNC = 50
OAUTH_STATE_COOKIE = "runwise_oauth_state"
MAX_OUTSTANDING_OAUTH_STATES = 5


def _google_module():
    return importlib.import_module(".google_health", package=__package__)


def _hash_state(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _callback_redirect(settings: Settings, outcome: str, reason: str | None = None) -> RedirectResponse:
    target = (settings.frontend_url or "http://localhost:5173").rstrip("/")
    params = {"google": outcome}
    if reason:
        params["reason"] = reason[:120]
    # The only values sent to the browser are a coarse outcome and safe reason.
    return RedirectResponse(f"{target}?{urlencode(params)}", status_code=status.HTTP_303_SEE_OTHER)


def _secure_cookie(settings: Settings) -> bool:
    """Use Secure cookies whenever the configured browser/API origin is HTTPS."""

    return settings.mode == "production" or any(
        isinstance(value, str) and value.lower().startswith("https://")
        for value in (settings.frontend_url, settings.public_base_url)
    )


def _config_error(settings: Settings) -> str | None:
    if not settings.google_client_id:
        return "Google OAuth client ID is not configured"
    if not settings.google_redirect_uri:
        return "Google OAuth redirect URI is not configured"
    if not settings.token_encryption_key:
        return "Server token encryption is not configured"
    return None


def _authorization_url(settings: Settings, state: str, challenge: str) -> str:
    google = _google_module()
    builder = getattr(google, "build_authorization_url", None)
    if builder is None:
        raise RuntimeError("Google OAuth client is unavailable")
    url = str(builder(settings.google_client_id, settings.google_redirect_uri, state, challenge))
    # The Health client owns its documented data scopes. Personal mode also
    # uses Google as the app login, so request identity scopes in the same
    # consent flow; no identity claim is trusted until the callback verifies
    # the returned ID token.
    if settings.mode == "personal":
        parsed = urlsplit(url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        scopes = set((params.get("scope") or [""])[0].split())
        scopes.update({"openid", "email", "profile"})
        params["scope"] = [" ".join(sorted(scopes))]
        query = urlencode(params, doseq=True)
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))
    return url


def _exchange(settings: Settings, code: str, verifier: str) -> dict[str, Any]:
    google = _google_module()
    exchanger = getattr(google, "exchange_code", None)
    if exchanger is None:
        raise RuntimeError("Google OAuth client is unavailable")
    result = exchanger(
        code,
        settings.google_client_id,
        settings.google_redirect_uri,
        settings.google_client_secret,
        verifier,
    )
    if not isinstance(result, dict):
        raise RuntimeError("Google OAuth token response is invalid")
    return result


def _error_message(error: Exception) -> str:
    # Provider exceptions can contain response bodies. Keep API messages short
    # and do not echo credentials, authorization codes, or token payloads.
    return error.__class__.__name__.replace("GoogleHealth", "Google ")


_RAW_OMIT_KEYS = {
    "access_token",
    "authorization",
    "code",
    "cookie",
    "data_source_id",
    "datasourceid",
    "end_location",
    "id",
    "latitude",
    "location",
    "longitude",
    "lon",
    "lat",
    "name",
    "notes",
    "provider_id",
    "refresh_token",
    "resource_name",
    "resourceid",
    "route",
    "source_id",
    "start_location",
    "state",
    "token",
}


def _safe_raw_value(value: Any, *, depth: int = 0) -> Any:
    """Keep the provider response shape while dropping identifying/sensitive data.

    Google normalized sessions retain the provider's actual exercise object in
    ``raw``.  The inspection endpoint is useful for debugging field mapping,
    but it must not become a way to expose resource IDs or route coordinates.
    This bounded recursive projection preserves metric and interval names while
    limiting what can be persisted in connection metadata.
    """

    if depth > 5:
        return None
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:80]:
            normalized = "".join(character for character in str(key).lower() if character.isalnum() or character == "_")
            if normalized in _RAW_OMIT_KEYS or normalized.endswith("token") or normalized.endswith("secret"):
                continue
            safe = _safe_raw_value(item, depth=depth + 1)
            if safe is not None:
                result[str(key)[:80]] = safe
        return result
    if isinstance(value, list):
        return [_safe_raw_value(item, depth=depth + 1) for item in value[:40]]
    if isinstance(value, (str, int, float, bool)):
        return value if not isinstance(value, str) else value[:300]
    return None


def _safe_raw_example(session: dict[str, Any], result: dict[str, Any]) -> dict[str, Any] | None:
    """Return an actual provider payload sample, sanitized and size bounded."""

    raw = session.get("raw") if isinstance(session, dict) else None
    if not isinstance(raw, dict):
        raw_pages = result.get("raw") if isinstance(result.get("raw"), dict) else {}
        raw_sessions = raw_pages.get("sessions") if isinstance(raw_pages, dict) else []
        if isinstance(raw_sessions, list):
            for page in raw_sessions:
                if isinstance(page, dict):
                    raw = page
                    break
    safe = _safe_raw_value(raw) if isinstance(raw, dict) else None
    if not isinstance(safe, dict):
        return None
    try:
        encoded = json.dumps(safe, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    if len(encoded.encode("utf-8")) > 30_000:
        # Keep the same provider object shape but truncate oversized nested
        # values through a second, stricter pass rather than storing a partial
        # JSON string that would no longer describe the original response.
        safe = _safe_raw_value(raw, depth=0)
        if not isinstance(safe, dict):
            return None
        encoded = json.dumps(safe, separators=(",", ":"), ensure_ascii=False)
        if len(encoded.encode("utf-8")) > 30_000:
            return None
    return safe


def _token_expiry(now: datetime, value: Any) -> datetime:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = 3600.0
    if seconds < 0:
        seconds = 0.0
    return now + timedelta(seconds=seconds)


def _google_client(settings: Settings, connection: GoogleConnection, now: datetime):
    google = _google_module()
    refresh = getattr(google, "refresh_token", None)
    access: str | None = None
    if connection.access_token_encrypted and connection.access_token_expires_at:
        try:
            if connection.access_token_expires_at.replace(tzinfo=timezone.utc) > now + timedelta(seconds=30):
                access = decrypt_secret(connection.access_token_encrypted, settings.token_encryption_key)
        except ValueError:
            access = None
    if access is None:
        if refresh is None:
            raise RuntimeError("Google OAuth refresh client is unavailable")
        refresh_token_value = decrypt_secret(connection.refresh_token_encrypted, settings.token_encryption_key)
        token_response = refresh(refresh_token_value, settings.google_client_id, settings.google_client_secret)
        access_value = token_response.get("access_token") if isinstance(token_response, dict) else None
        if not isinstance(access_value, str) or not access_value:
            raise RuntimeError("Google OAuth refresh response did not include an access token")
        access = access_value
        connection.access_token_encrypted = encrypt_secret(access, settings.token_encryption_key)
        connection.access_token_expires_at = _token_expiry(now, token_response.get("expires_in", 3600))
    client_class = getattr(google, "GoogleHealthClient", None)
    if client_class is None:
        raise RuntimeError("Google Health client is unavailable")
    return client_class(access), access


_RUNNING_ACTIVITY_TYPES = frozenset(
    {
        "RUN",
        "RUNNING",
        "JOG",
        "JOGGING",
        "TREADMILL",
        "TREADMILL_RUN",
        "TRAIL_RUN",
        "INCLINE_RUN",
        "DISTANCE_RUN",
    }
)
_GENERIC_EXERCISE_TITLES = frozenset({"activity", "exercise", "workout"})


def _canonical_activity_type(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return re.sub(r"[^A-Z0-9]+", "_", value.strip().upper()).strip("_") or None


def _is_running_activity_type(value: Any) -> bool:
    canonical = _canonical_activity_type(value)
    if canonical is None:
        return False
    if canonical in _RUNNING_ACTIVITY_TYPES:
        return True
    # The enum is explicitly extensible. Accept future run/jog variants while
    # keeping WALKING, BIKING, and generic WORKOUT distinct.
    tokens = set(canonical.split("_"))
    return bool(tokens & {"RUN", "RUNNING", "JOG", "JOGGING"}) and "WALK" not in tokens


def _has_running_label(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    label = value.strip().casefold()
    if label in {"google health run", "google health running", "google health treadmill"}:
        return False
    return bool(re.search(r"\b(?:run|running|jog|jogging|treadmill)\b", value, flags=re.IGNORECASE))


def _route_sports(route: dict[str, Any] | None) -> set[str]:
    if not isinstance(route, dict):
        return set()
    sports: set[str] = set()
    values = route.get("activity_sports")
    if isinstance(values, list):
        sports.update(canonical for value in values if (canonical := _canonical_activity_type(value)))
    activities = route.get("activities")
    if isinstance(activities, list):
        for activity in activities:
            if isinstance(activity, dict):
                canonical = _canonical_activity_type(activity.get("sport"))
                if canonical:
                    sports.add(canonical)
    canonical = _canonical_activity_type(route.get("sport"))
    if canonical:
        sports.add(canonical)
    return sports


def _route_is_running(route: dict[str, Any] | None) -> bool:
    sports = _route_sports(route)
    return any(_is_running_activity_type(sport) or "RUN" in sport for sport in sports)


def _session_component_values(session: dict[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for key in ("children", "childExercises", "activities", "activitySegments", "segments", "laps"):
        collection = session.get(key)
        if not isinstance(collection, list):
            continue
        values.extend(item for item in collection if isinstance(item, dict))
    return values


def _session_has_running_activity(session: dict[str, Any], route: dict[str, Any] | None) -> bool:
    activity_type = session.get("activity_type")
    if _is_running_activity_type(activity_type):
        return True
    canonical = _canonical_activity_type(activity_type)
    # An explicit non-running provider enum is authoritative. A route's TCX
    # sport is a fallback for generic/custom summaries, not permission to
    # reinterpret an exercise explicitly reported as WALKING or BIKING.
    if canonical not in {None, "WORKOUT", "EXERCISE", "UNKNOWN"}:
        return False
    if _route_is_running(route):
        return True
    components = _session_component_values(session)
    if any(_is_running_activity_type(component.get("activity_type")) for component in components):
        return True
    # A provider may use WORKOUT for a custom activity while retaining the
    # human name (for example "Distance run"). Only generic/unknown parent
    # types use the name fallback; explicit WALKING/BIKING remain excluded.
    if canonical in {None, "WORKOUT", "EXERCISE", "UNKNOWN"}:
        return _has_running_label(session.get("title")) or _has_running_label(session.get("display_name"))
    return False


def _route_distance_m(route: dict[str, Any] | None) -> float | None:
    if not isinstance(route, dict):
        return None
    for key in ("total_distance_m", "distance_m"):
        value = route.get(key)
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number > 0:
            return number
    laps = route.get("laps")
    if isinstance(laps, list):
        values = []
        for lap in laps:
            if not isinstance(lap, dict):
                continue
            try:
                number = float(lap.get("distance_m"))
            except (TypeError, ValueError):
                continue
            if math.isfinite(number) and number > 0:
                values.append(number)
        if values:
            return sum(values)
    points = route.get("points")
    if isinstance(points, list):
        values = []
        for point in points:
            if not isinstance(point, dict):
                continue
            try:
                number = float(point.get("distance_m"))
            except (TypeError, ValueError):
                continue
            if math.isfinite(number) and number >= 0:
                values.append(number)
        if values and max(values) > 0:
            return max(values)
    return None


def _route_elapsed_seconds(route: dict[str, Any] | None) -> float | None:
    if not isinstance(route, dict):
        return None
    try:
        number = float(route.get("elapsed_seconds"))
    except (TypeError, ValueError):
        number = math.nan
    if math.isfinite(number) and number > 0:
        return number
    points = route.get("points")
    times = [_parse_timestamp(point.get("timestamp")) for point in points if isinstance(point, dict)] if isinstance(points, list) else []
    times = [value for value in times if value is not None]
    if len(times) >= 2:
        elapsed = (max(times) - min(times)).total_seconds()
        return elapsed if math.isfinite(elapsed) and elapsed > 0 else None
    return None


def _session_distance_km(session: dict[str, Any], route: dict[str, Any] | None) -> float | None:
    for key in ("distance_km",):
        try:
            number = float(session.get(key))
        except (TypeError, ValueError):
            number = math.nan
        if math.isfinite(number) and number > 0:
            return number
    try:
        number = float(session.get("distance_m"))
    except (TypeError, ValueError):
        number = math.nan
    if math.isfinite(number) and number > 0:
        return number / 1000.0
    component_distances = []
    for component in _session_component_values(session):
        try:
            value = float(component.get("distance_m"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            component_distances.append(value)
    if component_distances:
        return sum(component_distances) / 1000.0
    route_distance = _route_distance_m(route)
    return route_distance / 1000.0 if route_distance is not None else None


def _session_source_utc_offset_seconds(session: dict[str, Any]) -> int | None:
    """Read the provider civil offset without changing the stored UTC instant."""

    value = session.get("source_utc_offset_seconds")
    if value is None:
        raw = session.get("raw")
        exercise = raw.get("exercise") if isinstance(raw, dict) else None
        interval = exercise.get("interval") if isinstance(exercise, dict) else None
        if isinstance(interval, dict):
            value = interval.get("startUtcOffset") or interval.get("start_utc_offset")
    if isinstance(value, str):
        match = re.fullmatch(r"\s*([+-]?[0-9]+(?:\.[0-9]+)?)s\s*", value)
        if not match:
            return None
        value = match.group(1)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or abs(number) > 86_400:
        return None
    return int(round(number))


def _session_to_run(session: dict[str, Any], route: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if not _session_has_running_activity(session, route):
        # Google Health exposes many exercise types through the same endpoint.
        # Runwise's run table cannot represent walking, cycling, strength, or
        # unknown activities without misclassifying them as runs.
        return None
    started = session.get("started_at")
    distance = _session_distance_km(session, route)
    duration = session.get("duration_seconds")
    if not isinstance(started, str) or not started.strip():
        return None
    try:
        distance_value = float(distance)
    except (TypeError, ValueError):
        return None
    try:
        duration_value = int(round(float(duration))) if duration is not None else 0
    except (TypeError, ValueError, OverflowError):
        duration_value = 0
    if not math.isfinite(distance_value):
        return None
    if duration_value <= 0:
        route_duration = _route_elapsed_seconds(route)
        if route_duration is not None:
            duration_value = int(round(route_duration))
    if distance_value <= 0 or duration_value <= 0:
        return None
    title = str(session.get("title") or session.get("display_name") or "Google Health run").strip()
    if (
        _route_is_running(route)
        and _canonical_activity_type(session.get("activity_type")) in {"WORKOUT", "EXERCISE", "UNKNOWN", None}
        and title.casefold() in _GENERIC_EXERCISE_TITLES
    ):
        # Fitbit-origin custom runs have been observed as WORKOUT/"Workout"
        # summaries while TCX identifies the Activity as Running. Keep a
        # useful stable title for the app while preserving the raw provider
        # display name in the normalized session.
        title = "Distance run"
    title = title[:120]
    average_hr = session.get("avg_hr")
    if isinstance(average_hr, float) and average_hr.is_integer():
        average_hr = int(average_hr)
    if not isinstance(average_hr, int) or not 20 <= average_hr <= 260:
        average_hr = None
    active_duration = session.get("active_duration_seconds")
    try:
        moving_value = float(active_duration) if active_duration is not None else None
    except (TypeError, ValueError):
        moving_value = None
    try:
        duration_bound = float(duration) if duration is not None else float(duration_value)
    except (TypeError, ValueError):
        duration_bound = float(duration_value)
    if moving_value is not None and (moving_value < 0 or moving_value > duration_bound + 1e-6):
        moving_value = None
    source_id = session.get("provider_id") or session.get("source_id") or session.get("id")
    if not isinstance(source_id, str) or not source_id:
        return None
    return {
        "source_id": source_id[:200],
        "title": title or "Google Health run",
        "started_at": started,
        "distance_km": distance_value,
        "duration_seconds": duration_value,
        "source_utc_offset_seconds": _session_source_utc_offset_seconds(session),
        "run_type": "run",
        "avg_hr": average_hr,
        # This is provider-reported active duration. It is stored separately
        # from elapsed duration and is not inferred from summary averages.
        "moving_seconds": int(round(moving_value)) if moving_value is not None else None,
    }


def _session_may_have_route(session: dict[str, Any]) -> bool:
    """Return whether a session should be probed with ``exportExerciseTcx``.

    ``hasGps`` is optional and Fitbit-origin records have been observed to
    omit it even when TCX export succeeds.  Generic WORKOUT records also need
    a route probe because their summary can omit distance until the detail is
    hydrated.  Explicit non-running types remain out of the candidate set.
    """

    if session.get("has_gps") is True:
        return True
    activity_type = _canonical_activity_type(session.get("activity_type"))
    if _is_running_activity_type(activity_type):
        return True
    # Nested run intervals can be hidden below a generic/custom parent, or a
    # provider can label a child with a future run enum. Check those children
    # before excluding an explicit parent type such as WALKING.
    if any(
        _is_running_activity_type(component.get("activity_type"))
        for component in _session_component_values(session)
    ):
        return True
    if activity_type in {None, "WORKOUT", "EXERCISE", "UNKNOWN"}:
        # The summary for a custom run can be an unlabelled WORKOUT with no
        # distance and no ``hasGps`` field. Its TCX sport is the only reliable
        # classification signal, so probe every generic exercise summary.
        return True
    return False


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        current = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _distance_between_points(first: dict[str, Any], second: dict[str, Any]) -> float | None:
    try:
        lat1 = math.radians(float(first["lat"]))
        lon1 = math.radians(float(first["lon"]))
        lat2 = math.radians(float(second["lat"]))
        lon2 = math.radians(float(second["lon"]))
    except (KeyError, TypeError, ValueError):
        return None
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    haversine = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(max(0.0, min(1.0, haversine))))


def _route_for_session(session: dict[str, Any], routes: list[Any]) -> dict[str, Any] | None:
    identifiers = {
        str(value)
        for value in (session.get("id"), session.get("source_id"), session.get("provider_id"), session.get("resource_name"))
        if value
    }
    for route in routes:
        if not isinstance(route, dict):
            continue
        route_id = str(route.get("exercise_id") or "")
        if route_id in identifiers or route_id.rstrip("/").split("/")[-1] in identifiers:
            return route
    return None


def _build_google_stream(session: dict[str, Any], result: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    route = _route_for_session(session, result.get("routes", []))
    route_points = route.get("points", []) if isinstance(route, dict) else []
    if not isinstance(route_points, list) or not route_points:
        return [], []
    parsed_points: list[tuple[datetime, dict[str, Any]]] = []
    for point in route_points:
        if not isinstance(point, dict):
            continue
        timestamp = _parse_timestamp(point.get("timestamp"))
        if timestamp is not None:
            parsed_points.append((timestamp, point))
    if not parsed_points:
        return [], []
    parsed_points.sort(key=lambda item: item[0])
    first_time = _parse_timestamp(session.get("started_at")) or parsed_points[0][0]
    previous_distance = 0.0
    points: list[dict[str, Any]] = []
    previous_point: dict[str, Any] | None = None
    for timestamp, point in parsed_points:
        elapsed = max(0.0, (timestamp - first_time).total_seconds())
        raw_distance = point.get("distance_m")
        try:
            distance = float(raw_distance) if raw_distance is not None else None
        except (TypeError, ValueError):
            distance = None
        if distance is None and previous_point is not None:
            delta = _distance_between_points(previous_point, point)
            distance = previous_distance + (delta or 0.0)
        if distance is None:
            distance = previous_distance
        distance = max(previous_distance, distance)
        sample: dict[str, Any] = {
            "elapsed_seconds": elapsed,
            "distance_m": distance,
        }
        if point.get("unknown_before") is True:
            sample["unknown_before"] = True
        if point.get("lat") is not None:
            sample["latitude"] = point.get("lat")
        if point.get("lon") is not None:
            sample["longitude"] = point.get("lon")
        altitude = point.get("altitude_m")
        if isinstance(altitude, (int, float)) and math.isfinite(altitude):
            sample["altitude_m"] = altitude
        bpm = point.get("heart_rate")
        if isinstance(bpm, (int, float)) and math.isfinite(bpm) and 20 <= bpm <= 260:
            sample["heart_rate"] = int(round(bpm))
        points.append(sample)
        previous_distance = distance
        previous_point = point

    heart_rate = result.get("samples", {}).get("heart-rate", []) if isinstance(result.get("samples"), dict) else []
    heart_rate_times: list[tuple[datetime, int]] = []
    if isinstance(heart_rate, list):
        for sample in heart_rate:
            if not isinstance(sample, dict):
                continue
            timestamp = _parse_timestamp(sample.get("timestamp") or sample.get("sample_time"))
            value = sample.get("heart_rate_bpm", sample.get("bpm"))
            try:
                bpm = int(round(float(value)))
            except (TypeError, ValueError):
                continue
            if timestamp is not None and 20 <= bpm <= 260:
                heart_rate_times.append((timestamp, bpm))
    for point, (timestamp, _) in zip(points, parsed_points):
        if heart_rate_times and "heart_rate" not in point:
            nearest = min(heart_rate_times, key=lambda item: abs((item[0] - timestamp).total_seconds()))
            if abs((nearest[0] - timestamp).total_seconds()) <= 10:
                point["heart_rate"] = nearest[1]

    laps: list[dict[str, Any]] = []
    provider_splits = session.get("splits") if isinstance(session.get("splits"), list) else []
    # TCX exports are authoritative for route-backed WORKOUT records and can
    # contain laps even when the exercise summary omitted distance/splits.
    if not provider_splits and isinstance(route, dict) and isinstance(route.get("laps"), list):
        provider_splits = route.get("laps", [])
    session_start = _parse_timestamp(session.get("started_at")) or parsed_points[0][0]
    for split in provider_splits:
        if not isinstance(split, dict):
            continue
        start_time = _parse_timestamp(split.get("start_time"))
        end_time = _parse_timestamp(split.get("end_time"))
        if start_time is None:
            start_time = _parse_timestamp(split.get("startTime"))
        if end_time is None:
            end_time = _parse_timestamp(split.get("endTime"))
        if start_time is None or end_time is None or end_time <= start_time:
            continue
        start_seconds = max(0.0, (start_time - session_start).total_seconds())
        end_seconds = max(start_seconds, (end_time - session_start).total_seconds())
        elapsed_value = split.get("elapsed_seconds")
        if elapsed_value is None:
            elapsed_value = split.get("elapsedSeconds") or split.get("total_time_seconds")
        moving_value = split.get("active_duration_seconds")
        if moving_value is None:
            moving_value = split.get("activeDuration")
        try:
            if isinstance(moving_value, str) and moving_value.endswith("s"):
                moving_value = float(moving_value[:-1])
        except (TypeError, ValueError):
            moving_value = None
        metadata = split.get("metadata")
        lap: dict[str, Any] = {
            "start_seconds": start_seconds,
            "end_seconds": end_seconds,
            "distance_m": split.get("distance_m"),
            "elapsed_seconds": elapsed_value or (end_seconds - start_seconds),
            "moving_seconds": moving_value if moving_value is not None else (elapsed_value or (end_seconds - start_seconds)),
            "label": split.get("split_type") or split.get("splitType") or split.get("intensity"),
        }
        if split.get("pace_seconds") is not None:
            lap["pace_seconds"] = split.get("pace_seconds")
        if isinstance(metadata, dict):
            lap["provider_metadata"] = metadata
        if "raw" in split:
            lap["provider_lap"] = split.get("raw")
        laps.append(lap)
    return points, laps


def _persist_google_stream(db: Session, owner_id: str, run: Run, session: dict[str, Any], result: dict[str, Any]) -> bool:
    points, laps = _build_google_stream(session, result)
    if not points:
        return False
    from .main import _stream_analysis

    try:
        analysis = _stream_analysis(points, laps or None)
    except HTTPException:
        return False
    raw = json.dumps({"samples": points, "laps": laps}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw) > 12 * 1024 * 1024:
        return False
    compressed = __import__("gzip").compress(raw, compresslevel=6)
    stream = db.scalar(select(RunStream).where(RunStream.owner_id == owner_id, RunStream.run_id == run.id))
    if stream is None:
        stream = RunStream(owner_id=owner_id, run_id=run.id, created_at=datetime.now(timezone.utc))
        db.add(stream)
    else:
        for key in ("weather", "official_race_distance_km"):
            if key in (stream.analysis or {}):
                analysis[key] = stream.analysis[key]
    stream.payload_gzip = compressed
    stream.sample_count = len(points)
    stream.raw_bytes = len(raw)
    stream.compressed_bytes = len(compressed)
    stream.laps = laps
    analysis["provider_active_seconds"] = session.get("active_duration_seconds")
    analysis["provider_exercise_events"] = session.get("exercise_events", [])
    analysis["provider_metrics_summary"] = session.get("metrics_summary", {})
    route = _route_for_session(session, result.get("routes", []))
    if isinstance(route, dict) and route.get("coarse"):
        analysis["telemetry_quality"] = route.get("quality", {})
        analysis["telemetry_provenance"] = route.get("provenance", {})
        analysis["telemetry_interval_seconds"] = route.get("sample_interval_seconds")
    stream.analysis = analysis
    run.stream_available = True
    moving = analysis.get("moving_seconds")
    if analysis.get("moving_time_available") and isinstance(moving, (int, float)):
        run.moving_seconds = int(round(moving))
    return True


def _persist_sync_result(db: Session, owner_id: str, result: dict[str, Any], connection: GoogleConnection) -> tuple[int, int, int]:
    # Importing the canonical key helper avoids accidentally creating a key
    # whose owner prefix differs from normal/manual/CSV writes.
    from .main import _dedupe_key
    from .run_enrichment import enrich_run

    imported = 0
    skipped = 0
    stream_errors = 0
    raw_example: dict[str, Any] | None = None
    for session in result.get("sessions", []):
        if not isinstance(session, dict):
            continue
        route = _route_for_session(session, result.get("routes", []))
        payload = _session_to_run(session, route)
        if payload is None:
            if _session_has_running_activity(session, route):
                coverage = result.setdefault("coverage", {})
                unresolved = coverage.setdefault("unresolved_running_sessions", [])
                if len(unresolved) < 100:
                    unresolved.append({"provider_id": session.get("provider_id"), "started_at": session.get("started_at"), "duration_seconds": session.get("duration_seconds"), "reason": "missing_valid_distance_or_duration"})
                coverage["complete"] = False
            skipped += 1
            continue
        if raw_example is None:
            raw_example = _safe_raw_example(session, result)
        key = _dedupe_key("google_health", payload["source_id"], owner_id=owner_id)
        existing = db.scalar(select(Run.id).where(Run.owner_id == owner_id, Run.dedupe_key == key))
        if existing is not None:
            skipped += 1
            existing_run = db.get(Run, existing)
            if existing_run is not None:
                # Refresh provider metrics without replacing user labels,
                # notes, or shoe locks. Broad old summaries may be incomplete.
                existing_run.distance_km = payload["distance_km"]
                existing_run.duration_seconds = payload["duration_seconds"]
                existing_run.avg_hr = payload["avg_hr"]
                existing_run.source_utc_offset_seconds = payload.get("source_utc_offset_seconds")
                if not existing_run.stream_available:
                    existing_run.moving_seconds = payload.get("moving_seconds")
                try:
                    has_route = route is not None and bool(route.get("points"))
                    if has_route and not _persist_google_stream(db, owner_id, existing_run, session, result):
                        stream_errors += 1
                    enrich_run(db, existing_run, provider_active_seconds=session.get("active_duration_seconds"))
                except Exception:
                    stream_errors += 1
            continue
        try:
            from .schemas import RunCreate

            normalized = RunCreate(
                source="google_health",
                **{key: value for key, value in payload.items() if key not in {"moving_seconds", "source_utc_offset_seconds"}},
            )
        except Exception:
            skipped += 1
            continue
        run = Run(
                owner_id=owner_id,
                title=normalized.title,
                started_at=normalized.started_at,
                source_utc_offset_seconds=payload.get("source_utc_offset_seconds"),
                distance_km=normalized.distance_km,
                duration_seconds=normalized.duration_seconds,
                run_type=normalized.run_type,
                run_type_assignment="unassigned",
                avg_hr=normalized.avg_hr,
                shoe_id=None,
                shoe_assignment="unassigned",
                moving_seconds=payload.get("moving_seconds"),
                notes="",
                rpe=None,
                source=normalized.source,
                source_id=normalized.source_id,
                dedupe_key=key,
        )
        db.add(run)
        db.flush()
        imported += 1
        try:
            has_route = route is not None and bool(route.get("points"))
            if has_route and not _persist_google_stream(db, owner_id, run, session, result):
                stream_errors += 1
            elif not has_route:
                pass
            enrich_run(db, run, provider_active_seconds=session.get("active_duration_seconds"))
        except Exception:
            stream_errors += 1
    coverage = result.get("coverage") if isinstance(result.get("coverage"), dict) else {}
    samples = coverage.get("record_counts", {}).get("samples", {}) if isinstance(coverage.get("record_counts"), dict) else {}
    available_types = ["exercise"] + sorted(str(key) for key in samples if key)
    connection.metadata_json = {
        **(connection.metadata_json or {}),
        "coverage": coverage,
        "available_types": available_types,
        "last_error_count": len(result.get("errors", [])) if isinstance(result.get("errors"), list) else 0,
    }
    if raw_example is not None:
        connection.metadata_json["raw_example"] = raw_example
    connection.last_sync_at = datetime.now(timezone.utc)
    complete = bool(coverage.get("complete"))
    errors = result.get("errors")
    if complete and not errors:
        connection.sync_status = "connected"
        connection.sync_error = None
    else:
        connection.sync_status = "error"
        connection.sync_error = "Google Health sync is partial; page or supporting-stream retrieval failed."
    return imported, skipped, stream_errors


def _attach_bounded_google_routes(
    client: Any,
    result: dict[str, Any],
    db: Session,
    owner_id: str,
    *,
    max_routes: int = MAX_ROUTE_EXPORTS_PER_SYNC,
) -> int:
    """Fetch a bounded set of TCX exports and annotate partial coverage.

    ``GoogleHealthClient.fetch_activity`` intentionally does not request routes
    because route payloads are much larger than exercise summaries.  Fetching
    them here lets us persist actual samples for imported runs while retaining
    a hard per-sync memory bound.  A route failure remains visible in the
    result's error list and makes coverage incomplete for the caller.
    """

    sessions = result.get("sessions") if isinstance(result.get("sessions"), list) else []
    connection = db.scalar(select(GoogleConnection).where(GoogleConnection.owner_id == owner_id, GoogleConnection.provider == "google-health"))
    metadata = dict(connection.metadata_json or {}) if connection is not None else {}
    probe_history = dict(metadata.get("route_probe_history") or {})
    now = datetime.now(timezone.utc)

    def probe_key(session: dict[str, Any]) -> str:
        return str(session.get("provider_id") or session.get("source_id") or session.get("id") or "")

    def probe_signature(session: dict[str, Any]) -> list[Any]:
        return [session.get("activity_type"), session.get("has_gps"), session.get("distance_km")]

    def recently_empty(session: dict[str, Any]) -> bool:
        prior = probe_history.get(probe_key(session), {})
        stamp = _parse_timestamp(prior.get("at"))
        return bool(prior.get("status") == "empty" and prior.get("signature") == probe_signature(session) and stamp is not None and now - stamp < timedelta(days=7))
    completed_sources = {
        str(value)
        for value in db.scalars(
            select(Run.source_id).where(
                Run.owner_id == owner_id,
                Run.source == "google_health",
                Run.stream_available.is_(True),
                Run.source_id.is_not(None),
            )
        ).all()
        if value
    }

    def has_completed_stream(session: dict[str, Any]) -> bool:
        return any(
            str(session.get(key)) in completed_sources
            for key in ("id", "source_id", "provider_id", "resource_name")
            if session.get(key)
        )

    candidates = [
        session
        for session in sessions
        if isinstance(session, dict)
        and _session_may_have_route(session)
        and not has_completed_stream(session)
        and not recently_empty(session)
    ]
    def route_priority(session: dict[str, Any]) -> int:
        activity_type = _canonical_activity_type(session.get("activity_type"))
        # Generic summaries with no usable distance are the records most
        # likely to become importable only after TCX hydration. Prioritize
        # them before already-populated RUNNING summaries when the per-sync
        # export bound is reached.
        distance = session.get("distance_km")
        try:
            has_distance = float(distance) > 0
        except (TypeError, ValueError):
            has_distance = False
        needs_hydration = activity_type in {None, "WORKOUT", "EXERCISE", "UNKNOWN"} or not has_distance
        return 0 if needs_hydration else 1

    candidates.sort(key=lambda session: str(session.get("started_at") or ""), reverse=True)
    candidates.sort(key=route_priority)
    # Unattempted records always precede retries. An empty/failed export must
    # not monopolize every bounded batch and starve older real runs.
    candidates.sort(key=lambda session: probe_history.get(probe_key(session), {}).get("at", ""))
    routes: list[dict[str, Any]] = []
    errors = result.setdefault("errors", [])
    if not isinstance(errors, list):
        errors = []
        result["errors"] = errors
    coverage = result.setdefault("coverage", {})
    if not isinstance(coverage, dict):
        coverage = {}
        result["coverage"] = coverage
    coverage["routes_requested"] = len(candidates)
    for session in candidates[:max_routes]:
        resource_name = session.get("resource_name") or session.get("id")
        if not resource_name:
            errors.append({"stream": "route", "error": "missing_resource_name"})
            continue
        try:
            route = client.export_exercise_tcx(str(resource_name))
            if isinstance(route, dict) and not route.get("points") and _is_running_activity_type(session.get("activity_type")) and hasattr(client, "fetch_supporting_samples"):
                from .google_telemetry import build_telemetry_route
                supporting = {}
                for data_type in ("distance", "heart-rate"):
                    values = []
                    token = None
                    seen_tokens = set()
                    for _ in range(20):
                        page = client.fetch_supporting_samples(data_type, start_time=session.get("started_at"), end_time=session.get("ended_at"), page_size=10000, page_token=token)
                        values.extend(page.get("samples", []))
                        token = page.get("next_page_token")
                        if not token:
                            break
                        if token in seen_tokens:
                            break
                        seen_tokens.add(token)
                    supporting[data_type] = values
                    if token:
                        errors.append({"stream": data_type, "error": "sample_limit_exceeded"})
                        # Never classify a partial stream as complete telemetry.
                        supporting[data_type] = []
                fallback = build_telemetry_route(session, supporting)
                if fallback is not None:
                    route = fallback
            if isinstance(route, dict):
                routes.append(route)
                probe_history[probe_key(session)] = {"at": now.isoformat(), "status": "success" if route.get("points") else "empty", "signature": probe_signature(session)}
        except Exception as exc:
            # Keep provider identifiers out of normal log/CLI output.  The
            # owner-facing sync response may still distinguish route failure
            # from a missing route through this coarse error class.
            errors.append({"stream": "route", "error": exc.__class__.__name__})
            probe_history[probe_key(session)] = {"at": now.isoformat(), "status": "error", "signature": probe_signature(session)}
    if connection is not None:
        metadata["route_probe_history"] = dict(sorted(probe_history.items(), key=lambda item: item[1].get("at", ""))[-2000:])
        connection.metadata_json = metadata
    result["routes"] = routes
    coverage["record_counts"] = coverage.get("record_counts") if isinstance(coverage.get("record_counts"), dict) else {}
    coverage["record_counts"]["routes"] = len(routes)
    route_complete = len(candidates) <= max_routes and not any(
        isinstance(error, dict) and error.get("stream") == "route" for error in errors
    )
    coverage["routes_complete"] = route_complete
    coverage["complete"] = bool(coverage.get("complete")) and route_complete and not errors
    if len(candidates) > max_routes:
        errors.append({"stream": "route", "error": "route_limit_exceeded", "limit": max_routes})
        coverage["routes_complete"] = False
        coverage["complete"] = False
    return len(routes)


def _sync_owner(
    settings: Settings,
    owner_id: str,
    db: Session,
    *,
    start_date: date | None,
    full_history: bool,
) -> dict[str, Any]:
    connection = db.scalar(select(GoogleConnection).where(GoogleConnection.owner_id == owner_id, GoogleConnection.provider == "google-health"))
    if connection is None:
        raise HTTPException(status_code=404, detail="Google Health is not connected")
    now = datetime.now(timezone.utc)
    client, _ = _google_client(settings, connection, now)
    try:
        effective_start: date | None = start_date
        if effective_start is None and not full_history:
            effective_start = (now - timedelta(days=30)).date()
        discovery = None
        if effective_start is None:
            discovery = client.fetch_activity(include_samples=False, include_routes=False, max_pages=MAX_SAMPLE_PAGES_PER_SYNC)
            oldest = discovery.get("coverage", {}).get("oldest_returned")
            parsed_oldest = _parse_timestamp(oldest)
            if parsed_oldest is not None:
                effective_start = (parsed_oldest.date() - timedelta(days=1)).replace(day=1)
        result = client.fetch_activity(
            start_time=effective_start,
            end_time=(now.date() + timedelta(days=1)),
            # Full-history sync imports all exercise summaries but does not
            # retain every lifetime telemetry point in memory.  Detailed route
            # streams are fetched in a bounded batch below.
            include_samples=False,
            include_routes=False,
            sample_page_size=500,
            max_pages=MAX_SAMPLE_PAGES_PER_SYNC,
        )
        if discovery is not None:
            result.setdefault("coverage", {})["history_discovery_complete"] = bool(discovery.get("coverage", {}).get("sessions_complete"))
            if discovery.get("errors"):
                result.setdefault("errors", []).extend(discovery["errors"])
                result["coverage"]["complete"] = False
        _attach_bounded_google_routes(client, result, db, owner_id)
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    streams_before = db.scalar(select(func.count(RunStream.id)).where(RunStream.owner_id == owner_id)) or 0
    imported, skipped, stream_errors = _persist_sync_result(db, owner_id, result, connection)
    db.flush()
    streams_after = db.scalar(select(func.count(RunStream.id)).where(RunStream.owner_id == owner_id)) or 0
    if stream_errors:
        errors = result.setdefault("errors", [])
        if isinstance(errors, list):
            errors.append({"stream": "route_persistence", "error": "stream_persistence_failed", "count": stream_errors})
        connection.sync_status = "error"
        connection.sync_error = "Google Health stream persistence was partial; retry synchronization."
    # Store the page counts as a checkpoint after the bounded aggregate fetch.
    # A subsequent run can safely repeat the provider IDs because dedupe is
    # scoped to owner and source identity.
    coverage = result.get("coverage") if isinstance(result.get("coverage"), dict) else {}
    if stream_errors:
        coverage["routes_complete"] = False
        coverage["complete"] = False
    connection.metadata_json = {
        **connection.metadata_json,
        "coverage": coverage,
        "last_error_count": len(result.get("errors", [])),
    }
    pages = coverage.get("pages") if isinstance(coverage.get("pages"), dict) else {}
    connection.sync_cursor = json_page_checkpoint(pages)
    db.commit()
    return {
        "imported": imported,
        "skipped": skipped,
        "streams_attached": int(streams_after - streams_before),
        "coverage": coverage,
        "errors": result.get("errors", []),
    }


def json_page_checkpoint(pages: dict[str, Any]) -> str:
    # Keep this value opaque and bounded. It is diagnostic metadata, not a
    # provider token or a cursor that can be replayed by a browser.
    import json

    return json.dumps({str(key): int(value) for key, value in pages.items() if isinstance(value, (int, float))}, sort_keys=True)[:500]


def register_google_routes(app: FastAPI, settings: Settings) -> None:
    """Register Google OAuth routes on an application instance."""

    @app.post("/api/integrations/google-health/connect")
    def connect_google(user=Depends(get_optional_current_user), db: Session = Depends(get_db)) -> JSONResponse:
        error = _config_error(settings)
        if error:
            raise HTTPException(status_code=503, detail=error)
        if settings.mode == "personal":
            owner_id = settings.personal_owner_id
        elif user is None:
            raise HTTPException(status_code=401, detail="Authentication is required")
        else:
            owner_id = str(user.subject)
        state = secrets.token_urlsafe(32)
        verifier, challenge = _pkce_pair()
        try:
            encrypted_verifier = encrypt_secret(verifier, settings.token_encryption_key)
            url = _authorization_url(settings, state, challenge)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=503, detail="Google OAuth is not available") from exc
        now = datetime.now(timezone.utc)
        # Expired or already-consumed state is ephemeral authentication data;
        # prune it before applying the small per-owner outstanding-state cap.
        db.execute(
            delete(OAuthState).where(
                OAuthState.owner_id == owner_id,
                (OAuthState.expires_at <= now) | OAuthState.used_at.is_not(None),
            )
        )
        outstanding = db.scalar(
            select(func.count(OAuthState.id)).where(
                OAuthState.owner_id == owner_id,
                OAuthState.used_at.is_(None),
                OAuthState.expires_at > now,
            )
        ) or 0
        if int(outstanding) >= MAX_OUTSTANDING_OAUTH_STATES:
            raise HTTPException(status_code=429, detail="Too many pending Google authorization attempts")
        db.add(OAuthState(owner_id=owner_id, state_hash=_hash_state(state), expires_at=now + OAUTH_STATE_TTL, created_at=now, code_verifier_encrypted=encrypted_verifier))
        db.commit()
        response = JSONResponse({"url": url})
        response.set_cookie(
            OAUTH_STATE_COOKIE,
            state,
            max_age=int(OAUTH_STATE_TTL.total_seconds()),
            httponly=True,
            secure=_secure_cookie(settings),
            samesite="lax",
            path=GOOGLE_CALLBACK_PATH,
        )
        return response

    @app.get("/api/integrations/google-health/callback")
    def google_callback(request: Request, state: str | None = None, code: str | None = None, error: str | None = None, db: Session = Depends(get_db)) -> RedirectResponse:
        if not state:
            return _callback_redirect(settings, "error", "missing_state")
        browser_state = request.cookies.get(OAUTH_STATE_COOKIE)
        if not browser_state or not hmac.compare_digest(browser_state, state):
            # Do not consume a valid state when the callback arrives in a
            # different browser/session. The initiating browser can retry.
            return _callback_redirect(settings, "error", "invalid_state")
        row = db.scalar(select(OAuthState).where(OAuthState.state_hash == _hash_state(state)))
        now = datetime.now(timezone.utc)
        if row is None or row.used_at is not None or row.expires_at.replace(tzinfo=timezone.utc) <= now:
            return _callback_redirect(settings, "error", "invalid_state")
        # In JWT mode the callback itself is a capability route. Once the
        # single-use state is validated, bind every subsequent transaction to
        # the owner recorded in that state before writing credentials.
        request.state.owner_id = row.owner_id
        db.info["runwise_owner_id"] = row.owner_id
        # Mark state used before contacting Google. A replay therefore cannot
        # exchange the same authorization code twice, even if exchange fails.
        row.used_at = now
        db.commit()
        if error or not code:
            response = _callback_redirect(settings, "error", "authorization_denied")
            response.delete_cookie(OAUTH_STATE_COOKIE, path=GOOGLE_CALLBACK_PATH)
            return response
        try:
            verifier = decrypt_secret(row.code_verifier_encrypted or "", settings.token_encryption_key)
            tokens = _exchange(settings, code, verifier)
            personal_user = None
            if settings.mode == "personal":
                id_token = tokens.get("id_token")
                if not isinstance(id_token, str) or not id_token:
                    raise RuntimeError("Google OAuth did not return an ID token")
                personal_user = verify_google_identity(id_token, request)
            refresh = tokens.get("refresh_token")
            if not isinstance(refresh, str) or not refresh:
                existing = db.scalar(select(GoogleConnection).where(GoogleConnection.owner_id == row.owner_id, GoogleConnection.provider == "google-health"))
                refresh = decrypt_secret(existing.refresh_token_encrypted, settings.token_encryption_key) if existing is not None else None
            if not isinstance(refresh, str) or not refresh:
                raise RuntimeError("Google OAuth did not return an offline refresh token")
            connection = db.scalar(select(GoogleConnection).where(GoogleConnection.owner_id == row.owner_id, GoogleConnection.provider == "google-health"))
            if connection is None:
                connection = GoogleConnection(owner_id=row.owner_id, provider="google-health", connected_at=now, refresh_token_encrypted=encrypt_secret(refresh, settings.token_encryption_key), scopes=[])
                db.add(connection)
            else:
                connection.refresh_token_encrypted = encrypt_secret(refresh, settings.token_encryption_key)
                connection.connected_at = now
                connection.sync_status = "connected"
                connection.sync_error = None
            access = tokens.get("access_token")
            if isinstance(access, str) and access:
                connection.access_token_encrypted = encrypt_secret(access, settings.token_encryption_key)
                connection.access_token_expires_at = _token_expiry(now, tokens.get("expires_in", 3600))
            scopes = tokens.get("scope")
            if isinstance(scopes, str):
                connection.scopes = [item for item in scopes.split() if item][:30]
            db.commit()
        except Exception as exc:
            # The browser receives no code, token, provider payload, or raw
            # exception. Server logs can correlate the coarse error class.
            response = _callback_redirect(settings, "error", _error_message(exc))
            response.delete_cookie(OAUTH_STATE_COOKIE, path=GOOGLE_CALLBACK_PATH)
            return response
        response = _callback_redirect(settings, "connected")
        response.delete_cookie(OAUTH_STATE_COOKIE, path=GOOGLE_CALLBACK_PATH)
        if settings.mode == "personal" and personal_user is not None:
            # Fernet authenticates and encrypts the owner-bound session. The
            # cookie is HttpOnly and Lax so only same-site frontend requests
            # can use it; browser scripts never see the value.
            response.set_cookie(
                PERSONAL_SESSION_COOKIE,
                issue_personal_session(personal_user, settings),
                max_age=PERSONAL_SESSION_TTL_SECONDS,
                httponly=True,
                secure=_secure_cookie(settings),
                samesite="lax",
                path="/",
            )
        return response

    @app.post("/api/integrations/google-health/sync")
    def sync_google(
        payload: GoogleSyncRequest | None = Body(default=None),
        start_date_query: date | None = Query(default=None, alias="start_date"),
        full_history_query: bool | None = Query(default=None, alias="full_history"),
        owner_id: str = Depends(get_current_owner),
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        if _config_error(settings):
            raise HTTPException(status_code=503, detail="Google OAuth is not configured")
        # Accept the JSON contract used by the current frontend and retain
        # query compatibility for older clients during rollout.  A body value
        # always wins when both forms are supplied.
        options = payload or GoogleSyncRequest(start_date=start_date_query, full_history=bool(full_history_query))
        try:
            return _sync_owner(settings, owner_id, db, start_date=options.start_date, full_history=options.full_history)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Google Health synchronization failed") from exc

    @app.get("/api/integrations/google-health/data-inspection", response_model=GoogleDataInspection)
    def inspect_google_data(owner_id: str = Depends(get_current_owner), db: Session = Depends(get_db)) -> GoogleDataInspection:
        connection = db.scalar(select(GoogleConnection).where(GoogleConnection.owner_id == owner_id, GoogleConnection.provider == "google-health"))
        if connection is None:
            raise HTTPException(status_code=404, detail="Google Health is not connected")
        metadata = connection.metadata_json if isinstance(connection.metadata_json, dict) else {}
        coverage = metadata.get("coverage") if isinstance(metadata.get("coverage"), dict) else {}
        types = metadata.get("available_types")
        if not isinstance(types, list):
            types = ["exercise"]
        earliest = db.scalar(select(func.min(Run.started_at)).where(Run.owner_id == owner_id, Run.source == "google_health"))
        complete = bool(coverage.get("complete"))
        note = (
            "Coverage is complete for the requested sync after pagination finished with no provider errors; it is still bounded by the requested sync window."
            if complete
            else "Earliest imported date is only a lower bound. Coverage is partial until every activity and supporting stream page completes without errors."
        )
        example = metadata.get("raw_example") if isinstance(metadata.get("raw_example"), dict) else None
        normalized: dict[str, Any] | None = None
        sample = db.scalar(select(Run).where(Run.owner_id == owner_id, Run.source == "google_health").order_by(Run.started_at.asc()))
        if sample is not None:
            # Keep the normalized projection separate from the actual provider
            # object in raw_example.  This lets the client compare field
            # mapping without mistaking a derived summary for provider JSON.
            normalized = {
                "data_type": "exercise",
                "title": sample.title,
                "started_at": sample.started_at.isoformat(),
                "distance_km": float(sample.distance_km),
                "duration_seconds": int(sample.duration_seconds),
                "avg_hr": sample.avg_hr,
            }
        return GoogleDataInspection(
            raw_example=example,
            normalized_example=normalized,
            available_types=sorted({str(item) for item in types}),
            earliest_imported_date=earliest.date() if isinstance(earliest, datetime) else None,
            source_coverage_note=note,
        )
