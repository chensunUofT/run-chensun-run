"""Owner-scoped weather estimates for recorded runs.

Weather is an optional enrichment.  A provider failure, malformed provider
response, or malformed stream must therefore resolve to a small unavailable
response instead of making the run detail route fail.  The provider request is
kept here, behind a fixed URL, so the core application never accepts an
arbitrary outbound URL from a request.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import gzip
import json
import math
from threading import Lock
import time
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import get_current_owner
from .db import Run, RunStream, get_db


ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
PROVIDER_NAME = "Open-Meteo"
HOURLY_FIELDS = "temperature_2m,relative_humidity_2m,weather_code"

# The cache deliberately stays process-local.  Weather is public provider
# data, but the owner and run lookup is always completed before a cache lookup
# so an inaccessible run can never be used as a cache oracle.
MAX_CACHE_ENTRIES = 512
SUCCESS_CACHE_TTL_SECONDS = 24 * 60 * 60
FAILURE_CACHE_TTL_SECONDS = 15 * 60
REQUEST_TIMEOUT_SECONDS = 10.0
RECENT_DAYS = 5
MAX_READING_DISTANCE_SECONDS = 60 * 60


@dataclass(slots=True)
class _WeatherCacheEntry:
    stored_at: float
    success: bool
    payload: dict[str, Any] | None = None
    reason: str = "provider_unavailable"


def _unavailable(reason: str) -> dict[str, Any]:
    """Return the complete stable response shape for an unavailable result."""

    return {
        "status": "unavailable",
        "reason": reason,
        "temperature_c": None,
        "humidity_percent": None,
        "weather_code": None,
        "provider": PROVIDER_NAME,
        "estimated": True,
    }


def _available(
    temperature_c: float | None,
    humidity_percent: float | None,
    weather_code: int | None,
) -> dict[str, Any]:
    return {
        "status": "available",
        "reason": None,
        "temperature_c": temperature_c,
        "humidity_percent": humidity_percent,
        "weather_code": weather_code,
        "provider": PROVIDER_NAME,
        "estimated": True,
    }


def _as_utc(value: datetime) -> datetime:
    """Normalize a stored run timestamp to UTC.

    Run payload validation stores timestamps as UTC.  SQLite can return a
    timezone-aware value as naive, so a naive value here is already treated as
    UTC rather than as the machine's local timezone.
    """

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
    else:
        return None
    return numeric if math.isfinite(numeric) else None


def _coordinate(sample: dict[str, Any], *names: str) -> float | None:
    for name in names:
        value = _finite_number(sample.get(name))
        if value is not None:
            return value
    return None


def _first_location(samples: Any) -> tuple[float, float] | None:
    """Find and round the first complete, valid latitude/longitude pair."""

    if not isinstance(samples, list):
        return None
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        latitude = _coordinate(sample, "latitude", "lat")
        longitude = _coordinate(sample, "longitude", "lon")
        if latitude is None or longitude is None:
            continue
        if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
            continue
        # Canonicalize negative zero so query parameters and cache keys are
        # stable for points close to the equator or prime meridian.
        rounded_latitude = round(latitude, 2)
        rounded_longitude = round(longitude, 2)
        if rounded_latitude == 0:
            rounded_latitude = 0.0
        if rounded_longitude == 0:
            rounded_longitude = 0.0
        return rounded_latitude, rounded_longitude
    return None


def _parse_provider_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _value_at(values: Any, index: int) -> Any:
    if not isinstance(values, list) or index < 0 or index >= len(values):
        return None
    return values[index]


def _reading(payload: dict[str, Any], target: datetime) -> dict[str, Any] | None:
    """Select the nearest hourly provider observation for ``target``."""

    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        return None
    times = hourly.get("time")
    if not isinstance(times, list):
        return None

    nearest_index: int | None = None
    nearest_delta: float | None = None
    for index, raw_time in enumerate(times):
        parsed_time = _parse_provider_time(raw_time)
        if parsed_time is None:
            continue
        # Open-Meteo is requested in GMT, so a reading from another UTC date
        # cannot be the run's nearest hourly observation.  The one-hour bound
        # also turns truncated or unrelated provider payloads into no_data.
        if parsed_time.date() != target.date():
            continue
        delta = abs((parsed_time - target).total_seconds())
        if delta > MAX_READING_DISTANCE_SECONDS:
            continue
        if nearest_delta is None or delta < nearest_delta:
            nearest_index = index
            nearest_delta = delta
    if nearest_index is None:
        return None

    temperature = _finite_number(_value_at(hourly.get("temperature_2m"), nearest_index))
    humidity = _finite_number(_value_at(hourly.get("relative_humidity_2m"), nearest_index))
    code_value = _finite_number(_value_at(hourly.get("weather_code"), nearest_index))
    weather_code = int(code_value) if code_value is not None else None

    # An hourly timestamp without any reading is an explicit no-data result.
    # Individual missing fields remain null while the available fields are
    # preserved for a partially populated provider response.
    if temperature is None and humidity is None and weather_code is None:
        return None
    return _available(temperature, humidity, weather_code)


def _cache_key(latitude: float, longitude: float, target_date: date) -> tuple[float, float, date]:
    return latitude, longitude, target_date


def _cache_get(
    cache: OrderedDict[tuple[float, float, date], _WeatherCacheEntry],
    cache_lock: Lock,
    key: tuple[float, float, date],
) -> _WeatherCacheEntry | None:
    now = time.monotonic()
    with cache_lock:
        entry = cache.get(key)
        if entry is None:
            return None
        ttl = SUCCESS_CACHE_TTL_SECONDS if entry.success else FAILURE_CACHE_TTL_SECONDS
        if now - entry.stored_at >= ttl:
            cache.pop(key, None)
            return None
        cache.move_to_end(key)
        return entry


def _cache_put(
    cache: OrderedDict[tuple[float, float, date], _WeatherCacheEntry],
    cache_lock: Lock,
    key: tuple[float, float, date],
    entry: _WeatherCacheEntry,
) -> None:
    with cache_lock:
        cache[key] = entry
        cache.move_to_end(key)
        while len(cache) > MAX_CACHE_ENTRIES:
            cache.popitem(last=False)


def _recent_enough(target_date: date, today: date) -> bool:
    """Whether the forecast endpoint can cover a run's UTC calendar date."""

    return today - timedelta(days=RECENT_DAYS) <= target_date <= today


def _provider_params(
    latitude: float,
    longitude: float,
    target_date: date,
    *,
    recent: bool,
) -> tuple[str, dict[str, Any]]:
    common: dict[str, Any] = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": HOURLY_FIELDS,
        "timezone": "GMT",
    }
    if recent:
        # A full five-day lookback keeps this request useful when the process
        # clock moves between requests for runs on adjacent recent dates.
        common.update({"past_days": RECENT_DAYS, "forecast_days": 1})
        return FORECAST_URL, common
    common.update({"start_date": target_date.isoformat(), "end_date": target_date.isoformat()})
    return ARCHIVE_URL, common


def _fetch_hourly(
    latitude: float,
    longitude: float,
    target_date: date,
    *,
    now: datetime,
) -> tuple[dict[str, Any] | None, str | None]:
    """Fetch a provider payload and classify only provider/data failures."""

    url, params = _provider_params(
        latitude,
        longitude,
        target_date,
        recent=_recent_enough(target_date, now.date()),
    )
    try:
        response = httpx.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        # Checking the status code directly keeps controlled test transports
        # useful even when they return a response without an attached request
        # object (``raise_for_status`` requires one in httpx).
        if int(getattr(response, "status_code", 200)) >= 400:
            return None, "provider_unavailable"
        payload = response.json()
    except Exception:
        # Weather is optional and must never make a run unavailable.  The
        # broad provider boundary also handles custom transports used in tests
        # and malformed third-party responses without leaking provider details.
        return None, "provider_unavailable"
    if not isinstance(payload, dict):
        return None, "provider_unavailable"
    return payload, None


def _get_weather(
    app: FastAPI,
    run: Run,
    stream: RunStream | None,
) -> dict[str, Any]:
    if stream is None:
        return _unavailable("missing_location")
    try:
        decoded = gzip.decompress(stream.payload_gzip)
        payload = json.loads(decoded.decode("utf-8"))
    except (OSError, EOFError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return _unavailable("no_data")
    if not isinstance(payload, dict):
        return _unavailable("no_data")
    location = _first_location(payload.get("samples"))
    if location is None:
        return _unavailable("missing_location")

    latitude, longitude = location
    started_at = _as_utc(run.started_at)
    context = [started_at.isoformat(), round(latitude, 5), round(longitude, 5)]
    saved = (stream.analysis or {}).get("weather")
    if isinstance(saved, dict) and saved.get("status") == "available" and saved.get("context") == context:
        return saved
    target_date = started_at.date()
    key = _cache_key(latitude, longitude, target_date)
    cache: OrderedDict[tuple[float, float, date], _WeatherCacheEntry] = app.state.weather_cache
    cache_lock: Lock = app.state.weather_cache_lock
    cached = _cache_get(cache, cache_lock, key)
    if cached is not None:
        if not cached.success:
            return _unavailable(cached.reason)
        reading = _reading(cached.payload or {}, started_at)
        return reading if reading is not None else _unavailable("no_data")

    provider_payload, provider_error = _fetch_hourly(
        latitude,
        longitude,
        target_date,
        now=datetime.now(timezone.utc),
    )
    if provider_error is not None or provider_payload is None:
        _cache_put(
            cache,
            cache_lock,
            key,
            _WeatherCacheEntry(
                stored_at=time.monotonic(),
                success=False,
                reason=provider_error or "provider_unavailable",
            ),
        )
        return _unavailable(provider_error or "provider_unavailable")

    reading = _reading(provider_payload, started_at)
    if reading is None:
        _cache_put(
            cache,
            cache_lock,
            key,
            _WeatherCacheEntry(stored_at=time.monotonic(), success=False, reason="no_data"),
        )
        return _unavailable("no_data")
    _cache_put(
        cache,
        cache_lock,
        key,
        _WeatherCacheEntry(stored_at=time.monotonic(), success=True, payload=provider_payload),
    )
    return reading


def get_and_store_weather(app: FastAPI, db: Session, run: Run, stream: RunStream | None) -> dict[str, Any]:
    """Persist successful historical readings for repeatable coaching inputs."""
    weather = _get_weather(app, run, stream)
    if stream is not None and weather.get("status") == "available":
        payload = json.loads(gzip.decompress(stream.payload_gzip))
        location = _first_location(payload.get("samples"))
        if location is not None:
            weather = {**weather, "context": [_as_utc(run.started_at).isoformat(), round(location[0], 5), round(location[1], 5)]}
            stream.analysis = {**(stream.analysis or {}), "weather": weather}
            db.flush()
    return weather


def register_weather_routes(app: FastAPI) -> None:
    """Register the optional owner-scoped weather endpoint on ``app``."""

    if getattr(app.state, "_runwise_weather_registered", False):
        return
    app.state._runwise_weather_registered = True
    app.state.weather_cache = OrderedDict()
    app.state.weather_cache_lock = Lock()

    @app.get("/api/runs/{run_id}/weather")
    def get_run_weather(
        run_id: int,
        owner_id: str = Depends(get_current_owner),
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        # This query intentionally precedes the cache lookup.  Weather cache
        # keys do not carry an owner, so authorization must be established from
        # the private run and stream rows first.
        run = db.scalar(select(Run).where(Run.id == run_id, Run.owner_id == owner_id))
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        stream = db.scalar(
            select(RunStream).where(
                RunStream.run_id == run_id,
                RunStream.owner_id == owner_id,
            )
        )
        weather = get_and_store_weather(app, db, run, stream)
        db.commit()
        return {key: value for key, value in weather.items() if key != "context"}

    @app.get("/api/runs/{run_id}/fitness")
    def get_run_fitness(run_id: int, owner_id: str = Depends(get_current_owner), db: Session = Depends(get_db)) -> dict[str, Any]:
        from .fitness import compute_fitness

        run = db.scalar(select(Run).where(Run.id == run_id, Run.owner_id == owner_id))
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        stream = db.scalar(select(RunStream).where(RunStream.run_id == run_id, RunStream.owner_id == owner_id))
        weather = get_and_store_weather(app, db, run, stream)
        analysis = dict(stream.analysis or {}) if stream is not None else {}
        result = compute_fitness(run, weather=weather, analysis=analysis)
        if stream is not None:
            stream.analysis = {**analysis, "fitness": result}
        db.commit()
        return result
