"""Owner-scoped import of a user's Strava data export archive.

The archive parser intentionally lives in :mod:`strava_archive`.  This module
only handles the API boundary and the merge into the existing Runwise data
model.  In particular, the importer does not add a provider column or a new
table: a Strava identity is linked from ``RunStream.analysis["strava_export"]``.
That lets a Google Health run keep its existing source, route, weather and
race metadata while still being associated with the corresponding Strava
activity.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import math
import re
from typing import Any
import zipfile

from fastapi import Depends, File, FastAPI, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import get_current_owner
from .db import Run, RunStream, get_db


STRAVA_SOURCE = "strava-export"
MAX_STRAVA_IMPORT_BYTES = 100 * 1024 * 1024
MAX_STREAM_BYTES = 12 * 1024 * 1024

# The parser is intentionally permissive about names because its normalized
# contract has evolved while supporting both Strava's CSV and JSON/ZIP exports.
_ID_KEYS = ("source_id", "activity_id", "strava_id", "id", "activityId")
_TITLE_KEYS = ("title", "name", "activity_name", "activityName")
_START_KEYS = ("started_at", "start_date", "start_date_local", "start_time", "timestamp", "date")
_DISTANCE_KEYS = ("distance_km", "distanceKm", "distance")
_ELAPSED_KEYS = (
    "duration_seconds",
    "elapsed_seconds",
    "elapsed_time_seconds",
    "elapsed_time",
    "duration",
    "elapsed",
)
_MOVING_KEYS = (
    "moving_seconds",
    "moving_time_seconds",
    "moving_time",
    "active_duration_seconds",
    "active_seconds",
)
_TYPE_KEYS = ("activity_type", "sport_type", "sport", "type", "kind", "workout_type", "run_type")
_OFFSET_KEYS = (
    "source_utc_offset_seconds",
    "utc_offset_seconds",
    "timezone_offset_seconds",
    "offset_seconds",
)
_STREAM_KEYS = ("samples", "stream_samples", "streams", "stream", "points", "route")
_LAP_KEYS = ("laps", "splits")


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    for method_name in ("model_dump", "to_dict", "as_dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                result = method(mode="json") if method_name == "model_dump" else method()
            except TypeError:
                result = method()
            if isinstance(result, Mapping):
                return result
    return None


def _field(record: Any, *names: str, default: Any = None) -> Any:
    mapping = _as_mapping(record)
    if mapping is not None:
        for name in names:
            if name in mapping:
                return mapping[name]
        return default
    for name in names:
        if hasattr(record, name):
            return getattr(record, name)
    return default


def _field_present(record: Any, *names: str) -> tuple[bool, Any]:
    mapping = _as_mapping(record)
    if mapping is not None:
        for name in names:
            if name in mapping:
                return True, mapping[name]
        return False, None
    for name in names:
        if hasattr(record, name):
            return True, getattr(record, name)
    return False, None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        # CSV exports occasionally carry a unit suffix (for example ``5.2
        # km``). The parser normally strips it; retaining this tiny fallback
        # keeps the route safe without guessing a unit.
        match = re.fullmatch(r"[-+]?\d+(?:\.\d+)?", text)
        if not match:
            return None
        value = match.group(0)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _integer(value: Any) -> int | None:
    parsed = _number(value)
    if parsed is None:
        return None
    return int(round(parsed))


def _parse_offset(value: Any) -> int | None:
    parsed = _integer(value)
    if parsed is None:
        return None
    # A UTC offset is necessarily less than one day. Treat an invalid value as
    # unknown rather than silently applying a local timezone.
    return parsed if -86_400 < parsed < 86_400 else None


def _parse_datetime(value: Any, *, offset_seconds: int | None) -> tuple[datetime | None, bool]:
    """Return UTC datetime and whether a naive timestamp stayed ambiguous."""

    if isinstance(value, datetime):
        current = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            current = datetime.fromisoformat(raw)
        except ValueError:
            return None, False
    else:
        return None, False
    if current.tzinfo is None:
        if offset_seconds is None:
            return None, True
        current = current.replace(tzinfo=timezone(timedelta(seconds=offset_seconds)))
    return current.astimezone(timezone.utc), False


def _canonical_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").strip().lower()).strip()


def _is_run(record: Any, activity_type: Any) -> bool:
    explicit, flag = _field_present(record, "is_run", "is_running", "run_eligible", "accepted")
    if explicit and flag is False:
        return False
    if explicit and flag is True:
        return True
    value = _canonical_text(activity_type)
    if not value:
        # The parser's normalized activity list contains only run candidates
        # when no type is supplied. Do not reject such records here.
        return True
    rejected_words = {
        "walk",
        "walking",
        "hike",
        "hiking",
        "ride",
        "cycling",
        "bike",
        "virtual ride",
        "swim",
        "rowing",
        "ski",
        "snowboard",
        "yoga",
        "weight training",
        "workout",
    }
    if value in rejected_words or any(value.startswith(item + " ") for item in rejected_words):
        return False
    return any(token in value for token in ("run", "running", "treadmill", "jog"))


def _distance_km(record: Any) -> float | None:
    explicit, value = _field_present(record, "distance_km", "distanceKm")
    if explicit:
        return _number(value)
    # ``distance_m`` is an unambiguous fallback used by route exports.
    explicit, value = _field_present(record, "distance_m", "distanceMeters", "distance_metres")
    if explicit:
        parsed = _number(value)
        return parsed / 1000 if parsed is not None else None
    value = _field(record, "distance")
    parsed = _number(value)
    if parsed is None:
        return None
    unit = _canonical_text(_field(record, "distance_unit", "distance_units", "unit"))
    if unit in {"m", "meter", "meters", "metre", "metres"}:
        return parsed / 1000
    # The parser normalizes Strava CSV/API distance to km. A bare value is
    # therefore deliberately treated as km; guessing from its magnitude would
    # corrupt short activities.
    return parsed


def _duration_seconds(record: Any) -> int | None:
    value = _field(record, *_ELAPSED_KEYS)
    parsed = _number(value)
    if parsed is None:
        return None
    unit = _canonical_text(_field(record, "duration_unit", "elapsed_unit", "time_unit"))
    if unit in {"min", "minute", "minutes"}:
        parsed *= 60
    elif unit in {"h", "hr", "hour", "hours"}:
        parsed *= 3600
    return int(round(parsed))


def _moving_seconds(record: Any) -> int | None:
    value = _field(record, *_MOVING_KEYS)
    parsed = _number(value)
    return int(round(parsed)) if parsed is not None else None


def _source_id(record: Any) -> str | None:
    value = _field(record, *_ID_KEYS)
    if value is None:
        return None
    text = str(value).strip()
    return text[:200] if text else None


def _activity_list(parsed: Any) -> list[Any]:
    if isinstance(parsed, Mapping):
        for key in ("activities", "runs", "records", "items"):
            value = parsed.get(key)
            if isinstance(value, list):
                return value
        return []
    for key in ("activities", "runs", "records", "items"):
        value = getattr(parsed, key, None)
        if isinstance(value, (list, tuple)):
            return list(value)
    if isinstance(parsed, (list, tuple)):
        return list(parsed)
    return []


def _archive_warnings(parsed: Any) -> list[str]:
    value = parsed.get("warnings", []) if isinstance(parsed, Mapping) else getattr(parsed, "warnings", [])
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, Mapping)):
        return [str(value)] if value else []
    return [str(item) for item in value if item]


def _load_archive_parser() -> Any:
    """Load the parser lazily so importing the API remains cheap and testable."""

    from . import strava_archive

    for name in ("parse_strava_archive", "parse_strava_export", "parse_archive"):
        parser = getattr(strava_archive, name, None)
        if callable(parser):
            return parser
    raise RuntimeError("Strava archive parser is not available")


def _parse_archive(contents: bytes) -> Any:
    # The parser accepts both Strava's ZIP export and a directly uploaded
    # activities.csv. It validates the container before returning, so keeping
    # this call before any database work preserves archive/CSV atomicity.
    parser = _load_archive_parser()
    return parser(contents)


def _stable_fallback_id(record: Any, started_at: datetime, distance_km: float, duration_seconds: int) -> str:
    mapping = _as_mapping(record)
    if mapping is not None:
        stable = {key: mapping[key] for key in sorted(mapping) if key not in {"raw", "metadata", "samples", "streams", "points"}}
    else:
        stable = {"started_at": started_at.isoformat(), "distance_km": distance_km, "duration_seconds": duration_seconds}
    encoded = json.dumps(stable, default=str, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "row-" + hashlib.sha256(encoded).hexdigest()[:48]


def _normalise_sample(sample: Any, *, index: int, stream_arrays: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Normalize one parser sample for the existing GET /streams contract."""

    if stream_arrays is not None:
        def array_value(*names: str) -> Any:
            for name in names:
                values = stream_arrays.get(name)
                if isinstance(values, (list, tuple)) and index < len(values):
                    return values[index]
            return None

        sample_mapping: Mapping[str, Any] = {
            "elapsed_seconds": array_value("time", "elapsed_seconds", "elapsed", "timestamp"),
            "distance_m": array_value("distance", "distance_m", "distanceMeters"),
            "latitude": None,
            "longitude": None,
            "altitude_m": array_value("altitude", "altitude_m", "elevation"),
            "heart_rate": array_value("heartrate", "heart_rate", "heartRate", "bpm"),
            "cadence": array_value("cadence"),
            "velocity_m_s": array_value("velocity_smooth", "velocity", "speed"),
        }
        latlng = array_value("latlng", "lat_lng", "coordinates")
        if isinstance(latlng, (list, tuple)) and len(latlng) >= 2:
            sample_mapping = {**sample_mapping, "latitude": latlng[0], "longitude": latlng[1]}
    else:
        mapping = _as_mapping(sample)
        sample_mapping = mapping if mapping is not None else {}

    output: dict[str, Any] = {}
    aliases = {
        "elapsed_seconds": ("elapsed_seconds", "elapsed", "time", "seconds"),
        "distance_m": ("distance_m", "distanceMeters", "distance"),
        "latitude": ("latitude", "lat"),
        "longitude": ("longitude", "lon", "lng"),
        "altitude_m": ("altitude_m", "altitude", "elevation_m", "elevation"),
        "heart_rate": ("heart_rate", "heartrate", "heartRate", "bpm"),
        "cadence": ("cadence",),
        "velocity_m_s": ("velocity_m_s", "velocity_smooth", "velocity", "speed", "speed_mps"),
        "timestamp": ("timestamp", "time_iso", "datetime"),
    }
    for target, names in aliases.items():
        value = None
        for name in names:
            if name in sample_mapping:
                value = sample_mapping[name]
                break
        if value is None:
            continue
        if target == "distance_m":
            # Normalized stream contracts use metres. Preserve an explicit
            # distance_km field when supplied by an exporter.
            if "distance_km" in sample_mapping and "distance_m" not in sample_mapping:
                value = _number(value)
                value = value * 1000 if value is not None else None
            else:
                value = _number(value)
        elif target in {"elapsed_seconds", "altitude_m", "heart_rate", "cadence", "velocity_m_s", "latitude", "longitude"}:
            value = _number(value) if target != "timestamp" else value
            if target == "heart_rate" and value is not None:
                value = int(round(value))
        if value is not None:
            output[target] = value
    # Preserve explicit metadata flags used by activity_analysis, while
    # keeping the payload JSON serializable and frontend-friendly.
    for key in ("unknown_before", "moving", "state", "provider"):
        if key in sample_mapping and isinstance(sample_mapping[key], (bool, int, float, str)):
            output[key] = sample_mapping[key]
    return output


def _normalise_samples(record: Any) -> list[dict[str, Any]]:
    value = _field(record, *_STREAM_KEYS)
    if value is None:
        return []
    if isinstance(value, Mapping):
        # A Strava stream export is commonly represented as parallel arrays.
        lengths = [len(item) for item in value.values() if isinstance(item, (list, tuple))]
        if not lengths:
            return []
        return [_normalise_sample({}, index=index, stream_arrays=value) for index in range(max(lengths))]
    if not isinstance(value, (list, tuple)):
        return []
    return [_normalise_sample(sample, index=index) for index, sample in enumerate(value) if sample is not None]


def _normalise_laps(record: Any) -> list[dict[str, Any]]:
    value = _field(record, *_LAP_KEYS)
    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    for lap in value:
        mapping = _as_mapping(lap)
        if mapping is None:
            continue
        clean: dict[str, Any] = {}
        aliases = {
            "start_seconds": ("start_seconds", "start_time", "start"),
            "end_seconds": ("end_seconds", "end_time", "end"),
            "distance_m": ("distance_m", "distanceMeters", "distance"),
            "elapsed_seconds": ("elapsed_seconds", "elapsed_time", "elapsed"),
            "moving_seconds": ("moving_seconds", "moving_time", "active_duration_seconds"),
            "pace_seconds": ("pace_seconds", "pace"),
            "label": ("label", "name", "split_type"),
        }
        for target, names in aliases.items():
            for name in names:
                if name in mapping and mapping[name] is not None:
                    value_item = mapping[name]
                    if target != "label":
                        value_item = _number(value_item)
                    if value_item is not None:
                        clean[target] = value_item
                    break
        if clean:
            result.append(clean)
    return result


def _stream_richness(payload: Mapping[str, Any]) -> tuple[int, int, int, int]:
    samples = payload.get("samples", [])
    if not isinstance(samples, list):
        samples = []
    return (
        len(samples),
        sum(1 for sample in samples if isinstance(sample, Mapping) and sample.get("latitude") is not None),
        sum(1 for sample in samples if isinstance(sample, Mapping) and sample.get("heart_rate") is not None),
        sum(1 for sample in samples if isinstance(sample, Mapping) and sample.get("altitude_m") is not None),
    )


def _decode_payload(stream: RunStream | None) -> dict[str, Any]:
    if stream is None or not stream.payload_gzip:
        return {"samples": [], "laps": []}
    try:
        value = json.loads(gzip.decompress(stream.payload_gzip).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"samples": [], "laps": []}
    return value if isinstance(value, dict) else {"samples": [], "laps": []}


def _safe_analysis(stream: RunStream | None) -> dict[str, Any]:
    value = stream.analysis if stream is not None else {}
    return dict(value) if isinstance(value, Mapping) else {}


def _load_strava_links(db: Session, owner_id: str) -> dict[int, set[str]]:
    """Load the owner-scoped one-to-one Strava identity links.

    Native Strava rows are included because an earlier import may have created
    the run before its stream metadata was available. The stream metadata is
    then authoritative for cross-provider Google rows.
    """

    links: dict[int, set[str]] = {}
    rows = db.scalars(select(Run).where(Run.owner_id == owner_id)).all()
    for run in rows:
        if run.source == STRAVA_SOURCE and run.source_id:
            links.setdefault(run.id, set()).add(str(run.source_id))
    streams = db.scalars(select(RunStream).where(RunStream.owner_id == owner_id)).all()
    for stream in streams:
        analysis = _safe_analysis(stream)
        metadata = analysis.get("strava_export")
        if not isinstance(metadata, Mapping):
            continue
        values = {metadata.get("activity_id"), metadata.get("source_id")}
        values = {str(value).strip() for value in values if value is not None and str(value).strip()}
        if values:
            links.setdefault(stream.run_id, set()).update(values)
    return links


def _linked_runs_for_source(
    db: Session,
    owner_id: str,
    source_id: str,
    links: Mapping[int, set[str]],
) -> list[Run]:
    run_ids = [run_id for run_id, values in links.items() if source_id in values]
    if not run_ids:
        return []
    return db.scalars(select(Run).where(Run.owner_id == owner_id, Run.id.in_(run_ids))).all()


def _restore_provider_moving(run: Run, stream: RunStream | None, moving_seconds: int | None) -> None:
    """Make stored analysis agree with an export's authoritative moving time."""

    if moving_seconds is None or moving_seconds < 0 or moving_seconds > int(run.duration_seconds):
        return
    run.moving_seconds = moving_seconds
    if stream is None:
        return
    analysis = _safe_analysis(stream)
    distance = float(run.distance_km) if _number(run.distance_km) else 0.0
    analysis.update(
        {
            "moving_seconds": moving_seconds,
            "elapsed_seconds": int(run.duration_seconds),
            "stopped_seconds": max(0, int(run.duration_seconds) - moving_seconds),
            "moving_pace_seconds": moving_seconds / (distance * 1000) if distance > 0 else None,
            "moving_pace_seconds_per_km": moving_seconds / distance if distance > 0 else None,
            "provider_active_seconds": moving_seconds,
            "moving_time_source": "strava_export",
            "moving_time_available": bool(distance > 0),
        }
    )
    # Recompute fitness after replacing the enrichment estimate, otherwise its
    # duration inputs and the visible movement fields can disagree.
    try:
        from .fitness import compute_fitness

        analysis["fitness"] = compute_fitness(run, analysis.get("weather"), analysis)
    except Exception:
        # Fitness is an optional derived field; preserve the import if an old
        # database contains malformed weather or analysis metadata.
        pass
    stream.analysis = analysis


def _strava_metadata(record: Any, source_id: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "activity_id": source_id,
        "source_id": source_id,
        "source": STRAVA_SOURCE,
    }
    activity_type = _field(record, *_TYPE_KEYS)
    if activity_type:
        metadata["activity_type"] = str(activity_type)
    raw_metadata = _field(record, "provenance", "metadata", "source_metadata")
    if isinstance(raw_metadata, Mapping):
        # Keep this bounded: provider raw records may contain large route data.
        cleaned = {
            str(key): value
            for key, value in raw_metadata.items()
            if key not in {"samples", "streams", "points", "route"}
        }
        # Preserve the parser's source metadata under the provider link. This
        # is intentionally namespaced so legacy ``analysis["source"]`` and
        # other Google provenance fields remain untouched.
        metadata["metadata"] = cleaned
        metadata["provenance"] = cleaned
    return metadata


def _set_stream(
    db: Session,
    owner_id: str,
    run: Run,
    *,
    samples: list[dict[str, Any]],
    laps: list[dict[str, Any]],
    metadata: dict[str, Any],
    replace_payload: bool,
) -> RunStream:
    stream = db.scalar(select(RunStream).where(RunStream.owner_id == owner_id, RunStream.run_id == run.id))
    if stream is None:
        stream = RunStream(owner_id=owner_id, run_id=run.id, created_at=datetime.now(timezone.utc))
        db.add(stream)
        replace_payload = True

    analysis = _safe_analysis(stream)
    existing_export = analysis.get("strava_export")
    if isinstance(existing_export, Mapping):
        merged_export = dict(existing_export)
        merged_export.update(metadata)
        metadata = merged_export
    analysis["strava_export"] = metadata

    if replace_payload:
        payload = {"samples": samples, "laps": laps}
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(raw) > MAX_STREAM_BYTES:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Strava activity stream is too large")
        compressed = gzip.compress(raw, compresslevel=6)
        stream.payload_gzip = compressed
        stream.sample_count = len(samples)
        stream.raw_bytes = len(raw)
        stream.compressed_bytes = len(compressed)
        stream.laps = laps
        run.stream_available = bool(samples)
    # ``analysis`` is always assigned even when a richer Google payload is
    # retained. This is the only cross-provider link the importer adds.
    stream.analysis = analysis
    return stream


def _candidate_matches(
    db: Session,
    owner_id: str,
    started_at: datetime,
    distance_km: float,
    duration_seconds: int,
    *,
    source_id: str,
    strava_links: Mapping[int, set[str]],
) -> list[Run]:
    candidates = db.scalars(select(Run).where(Run.owner_id == owner_id, Run.source != STRAVA_SOURCE)).all()
    matched: list[Run] = []
    for run in candidates:
        # A provider identity already linked to another activity is a hard
        # one-to-one boundary. It must never be overwritten by a nearby CSV
        # row whose timestamp and distance happen to look similar.
        linked_ids = strava_links.get(run.id, set())
        if linked_ids and source_id not in linked_ids:
            continue
        run_started = run.started_at
        if run_started.tzinfo is None:
            run_started = run_started.replace(tzinfo=timezone.utc)
        start_delta = abs((run_started.astimezone(timezone.utc) - started_at).total_seconds())
        if start_delta > 60:
            continue
        existing_distance = _number(run.distance_km)
        existing_duration = _integer(run.duration_seconds)
        if existing_distance is None or existing_duration is None or existing_distance <= 0 or existing_duration <= 0:
            continue
        distance_limit = max(0.3, 0.03 * max(existing_distance, distance_km))
        duration_limit = max(120, 0.05 * max(existing_duration, duration_seconds))
        if abs(existing_distance - distance_km) > distance_limit:
            continue
        if abs(existing_duration - duration_seconds) > duration_limit:
            continue
        matched.append(run)
    return matched


def _official_distance(stream: RunStream | None) -> float | None:
    value = _safe_analysis(stream).get("official_race_distance_km")
    parsed = _number(value)
    return parsed if parsed is not None and parsed > 0 else None


def _maybe_update_matched_summary(run: Run, stream: RunStream | None, distance_km: float, duration_seconds: int) -> None:
    """Fill only impossible missing summaries; preserve race/user values."""

    if _official_distance(stream) is not None:
        return
    # Current Run rows are non-null, but this guards legacy records. It also
    # avoids replacing a Google summary merely because Strava has a few more
    # decimals in its CSV.
    if not _number(run.distance_km) or float(run.distance_km) <= 0:
        run.distance_km = distance_km
    if not _integer(run.duration_seconds) or int(run.duration_seconds) <= 0:
        run.duration_seconds = duration_seconds


def _should_replace_stream(existing_payload: Mapping[str, Any], incoming_samples: list[dict[str, Any]], stream: RunStream | None) -> bool:
    if not incoming_samples:
        return False
    existing_samples = existing_payload.get("samples", [])
    if not isinstance(existing_samples, list) or not existing_samples:
        return True
    incoming_payload = {"samples": incoming_samples}
    # A Google route with richer curves remains authoritative. A detailed
    # Strava FIT/GPX route can still replace a sparse Google summary; compare
    # the actual curves rather than treating provider names as a hard lock.
    return _stream_richness(incoming_payload) > _stream_richness(existing_payload)


def _normalise_record(record: Any) -> dict[str, Any] | None:
    activity_type = _field(record, *_TYPE_KEYS)
    if not _is_run(record, activity_type):
        return {"skip": "activity is not a run"}
    offset_present, raw_offset = _field_present(record, *_OFFSET_KEYS)
    offset_seconds = _parse_offset(raw_offset) if offset_present else None
    started_at, ambiguous = _parse_datetime(_field(record, *_START_KEYS), offset_seconds=offset_seconds)
    if started_at is None:
        return {"skip": "activity timestamp is invalid or has an ambiguous timezone" if ambiguous else "activity timestamp is invalid"}
    distance_km = _distance_km(record)
    duration_seconds = _duration_seconds(record)
    if distance_km is None or distance_km <= 0 or duration_seconds is None or duration_seconds <= 0:
        return {"skip": "activity has no valid positive distance and elapsed duration"}
    source_id = _source_id(record) or _stable_fallback_id(record, started_at, distance_km, duration_seconds)
    moving_seconds = _moving_seconds(record)
    if moving_seconds is not None and (moving_seconds < 0 or moving_seconds > duration_seconds):
        return {"skip": "activity moving duration is outside elapsed duration"}
    return {
        "record": record,
        "source_id": source_id,
        "title": str(_field(record, *_TITLE_KEYS) or "Strava run")[:120],
        "started_at": started_at,
        "source_utc_offset_seconds": offset_seconds,
        "distance_km": float(distance_km),
        "duration_seconds": int(duration_seconds),
        "moving_seconds": moving_seconds,
        "samples": _normalise_samples(record),
        "laps": _normalise_laps(record),
    }


def register_strava_archive_routes(app: FastAPI) -> None:
    """Register the private Strava export importer on ``app``."""

    @app.post("/api/import/strava")
    def import_strava_archive(
        file: UploadFile = File(...),
        owner_id: str = Depends(get_current_owner),
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        contents = file.file.read(MAX_STRAVA_IMPORT_BYTES + 1)
        if len(contents) > MAX_STRAVA_IMPORT_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Strava export exceeds the {MAX_STRAVA_IMPORT_BYTES} byte limit",
            )
        if not contents:
            raise HTTPException(status_code=422, detail="Strava export is empty")
        try:
            parsed = _parse_archive(contents)
        except HTTPException:
            raise
        except (ValueError, OSError, zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError) as exc:
            db.rollback()
            raise HTTPException(status_code=422, detail=f"Invalid Strava export: {exc}") from exc
        except Exception as exc:
            # Parser errors are client input errors. No write has happened yet,
            # so rollback is enough to guarantee invalid archives are atomic.
            db.rollback()
            raise HTTPException(status_code=422, detail=f"Could not parse Strava export: {exc}") from exc

        warnings = _archive_warnings(parsed)
        records = _activity_list(parsed)
        parser_skipped = parsed.get("skipped", 0) if isinstance(parsed, Mapping) else getattr(parsed, "skipped", 0)
        parser_skipped = _integer(parser_skipped) or 0
        if not records:
            warnings.append("The Strava export contained no activities.")

        imported = 0
        matched_count = 0
        skipped = parser_skipped
        seen_ids: set[str] = set()
        strava_links = _load_strava_links(db, owner_id)

        try:
            for record in records:
                normalized = _normalise_record(record)
                if normalized is None:
                    skipped += 1
                    warnings.append("Skipped an unreadable activity.")
                    continue
                if normalized.get("skip"):
                    skipped += 1
                    warnings.append(str(normalized["skip"]))
                    continue
                source_id = normalized["source_id"]
                if source_id in seen_ids:
                    skipped += 1
                    warnings.append(f"Skipped duplicate Strava activity {source_id} in this archive.")
                    continue
                seen_ids.add(source_id)

                linked = _linked_runs_for_source(db, owner_id, source_id, strava_links)
                if len(linked) > 1:
                    skipped += 1
                    warnings.append(f"Skipped Strava activity {source_id}: its provider identity is linked to multiple runs.")
                    continue
                existing = linked[0] if linked else None
                if existing is None:
                    candidates = _candidate_matches(
                        db,
                        owner_id,
                        normalized["started_at"],
                        normalized["distance_km"],
                        normalized["duration_seconds"],
                        source_id=source_id,
                        strava_links=strava_links,
                    )
                    if len(candidates) > 1:
                        skipped += 1
                        warnings.append(f"Skipped Strava activity {source_id}: multiple runs matched conservatively.")
                        continue
                    if candidates:
                        existing = candidates[0]

                metadata = _strava_metadata(record, source_id)
                samples = normalized["samples"]
                laps = normalized["laps"]

                if existing is not None:
                    stream = db.scalar(select(RunStream).where(RunStream.owner_id == owner_id, RunStream.run_id == existing.id))
                    old_payload = _decode_payload(stream)
                    replace_payload = _should_replace_stream(old_payload, samples, stream)
                    stream = _set_stream(
                        db,
                        owner_id,
                        existing,
                        samples=samples,
                        laps=laps,
                        metadata=metadata,
                        replace_payload=replace_payload,
                    )
                    _maybe_update_matched_summary(existing, stream, normalized["distance_km"], normalized["duration_seconds"])
                    supplied_moving = normalized["moving_seconds"]
                    if replace_payload and samples:
                        from .run_enrichment import enrich_run

                        enrich_run(db, existing, provider_active_seconds=supplied_moving)
                    # If an existing Strava row was first imported without a
                    # route, a later archive can attach it. Enrichment above
                    # respects manual training/shoe choices on matched rows.
                    _restore_provider_moving(existing, stream, supplied_moving)
                    strava_links.setdefault(existing.id, set()).add(source_id)
                    matched_count += 1
                    continue

                run = Run(
                    owner_id=owner_id,
                    title=normalized["title"],
                    started_at=normalized["started_at"],
                    source_utc_offset_seconds=normalized["source_utc_offset_seconds"],
                    distance_km=normalized["distance_km"],
                    duration_seconds=normalized["duration_seconds"],
                    run_type="run",
                    run_type_assignment="unassigned",
                    avg_hr=_integer(_field(record, "average_heartrate", "avg_hr", "average_hr")),
                    shoe_id=None,
                    shoe_assignment="unassigned",
                    notes="",
                    rpe=None,
                    source=STRAVA_SOURCE,
                    source_id=source_id,
                    content_fingerprint=None,
                    dedupe_key=f"owner:{owner_id}|source:{STRAVA_SOURCE}|id:{source_id}",
                    moving_seconds=normalized["moving_seconds"],
                    stream_available=False,
                )
                db.add(run)
                db.flush()
                stream = _set_stream(
                    db,
                    owner_id,
                    run,
                    samples=samples,
                    laps=laps,
                    metadata=metadata,
                    replace_payload=True,
                )
                from .run_enrichment import enrich_run

                enrich_run(db, run, provider_active_seconds=normalized["moving_seconds"])
                # Enrichment intentionally estimates movement from samples; a
                # Strava export's supplied moving duration is authoritative for
                # this imported row and must survive that estimate.
                _restore_provider_moving(run, stream, normalized["moving_seconds"])
                strava_links.setdefault(run.id, set()).add(source_id)
                imported += 1

            db.commit()
        except HTTPException:
            db.rollback()
            raise
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail="Strava import conflicted with an existing run") from exc
        except Exception as exc:
            db.rollback()
            raise HTTPException(status_code=422, detail=f"Strava import could not be saved: {exc}") from exc

        return {"imported": imported, "matched": matched_count, "skipped": skipped, "warnings": warnings}
