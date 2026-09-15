"""Bounded telemetry fallback for Google Health exercise sessions.

Google Health's distance stream is an interval aggregate, rather than a GPS
track.  This module turns one matching distance source into sparse cumulative
points when an exercise's TCX export is empty or unavailable.  It is kept
independent from the transport and route modules so callers can choose this
fallback without changing the provider client.

The source selection is deliberately conservative.  A session's platform,
application, and device identify the source; recording method is ignored for
identity because an actively measured exercise can have passively measured
supporting samples.  One source is selected and overlapping intervals in that
source are discarded as ambiguous.  Records from another provider are never
added to the selected stream.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
import math
import statistics
from typing import Any


__all__ = ["build_telemetry_route"]


_DISTANCE_SOURCE = "google_health_distance_intervals"
_HR_MAX_NEAREST_SECONDS = 30.0
_COARSE_INTERVAL_SECONDS = 5.0
_ANALYSIS_GAP_CAP_SECONDS = 65.0
_DISTANCE_EPSILON_M = 1e-6


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _timestamp(value: Any) -> datetime | None:
    """Parse an RFC3339 value, treating an absent offset as UTC."""

    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            result = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="auto").replace("+00:00", "Z")


def _last_component(value: Any) -> str | None:
    raw = _text(value)
    if not raw:
        return None
    return raw.rstrip("/").split("/")[-1] or None


def _nested_source(value: Any) -> Mapping[str, Any] | None:
    source = _as_mapping(value)
    if source is None:
        return None
    # A few callers pass a raw point directly and a few pass its normalized
    # ``data_source`` projection.  Accept both spellings without exposing raw
    # provider records to the rest of the normalizer.
    nested = source.get("dataSource")
    if not isinstance(nested, Mapping):
        nested = source.get("data_source")
    if isinstance(nested, Mapping):
        return nested
    return source


def _source_descriptor(value: Any) -> dict[str, Any] | None:
    source = _nested_source(value)
    if source is None:
        return None

    device = _as_mapping(source.get("device")) or {}
    application = _as_mapping(source.get("application")) or {}

    platform = _text(source.get("platform") or source.get("platformName"))
    package_name = _text(
        application.get("packageName")
        or application.get("package_name")
        or source.get("packageName")
        or source.get("package_name")
    )
    display_name = _text(
        device.get("displayName")
        or device.get("display_name")
        or source.get("deviceDisplayName")
        or source.get("device_display_name")
    )
    form_factor = _text(
        device.get("formFactor")
        or device.get("form_factor")
        or source.get("deviceFormFactor")
        or source.get("device_form_factor")
    )
    manufacturer = _text(device.get("manufacturer") or device.get("manufacturerName"))
    model = _text(device.get("model") or device.get("modelName"))
    recording_method = _text(source.get("recordingMethod") or source.get("recording_method"))

    # A data source with none of these stable identity fields is not eligible
    # for source matching.  It is still retained in the raw provenance when a
    # richer source record is available elsewhere.
    if not any((platform, package_name, display_name, form_factor, manufacturer, model)):
        return None
    return {
        "platform": platform.upper() if platform else None,
        "application_package": package_name,
        "device_display_name": display_name,
        "device_form_factor": form_factor.upper() if form_factor else None,
        "device_manufacturer": manufacturer,
        "device_model": model,
        "recording_method": recording_method.upper() if recording_method else None,
        "raw": deepcopy(dict(source)),
    }


def _source_key(source: Mapping[str, Any]) -> tuple[str | None, ...]:
    """Return source identity while intentionally omitting recording method."""

    return (
        _text(source.get("platform")),
        _text(source.get("application_package")),
        _text(source.get("device_display_name")),
        _text(source.get("device_form_factor")),
        _text(source.get("device_manufacturer")),
        _text(source.get("device_model")),
    )


def _source_label(source: Mapping[str, Any]) -> str:
    parts = [
        _text(source.get("platform")),
        _text(source.get("application_package")),
        _text(source.get("device_display_name")),
        _text(source.get("device_form_factor")),
    ]
    return ":".join(part for part in parts if part) or "unknown"


def _source_candidates(value: Any) -> list[dict[str, Any]]:
    """Extract all usable data-source projections from a session or sample."""

    values: list[Any] = []
    mapping = _as_mapping(value)
    if mapping is None:
        return []
    if "data_source" in mapping:
        values.append(mapping.get("data_source"))
    if "dataSource" in mapping:
        values.append(mapping.get("dataSource"))
    raw = _as_mapping(mapping.get("raw"))
    if raw is not None:
        if "data_source" in raw:
            values.append(raw.get("data_source"))
        if "dataSource" in raw:
            values.append(raw.get("dataSource"))
    # This also accepts a source mapping supplied directly to the helper.
    if not values and ("platform" in mapping or "device" in mapping or "application" in mapping):
        values.append(mapping)

    result: list[dict[str, Any]] = []
    seen: set[tuple[str | None, ...]] = set()
    for item in values:
        descriptor = _source_descriptor(item)
        if descriptor is None:
            continue
        key = _source_key(descriptor)
        if key in seen:
            continue
        seen.add(key)
        result.append(descriptor)
    return result


def _source_matches(expected: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    """Check stable provider/device fields, with recording method ignored."""

    expected_platform = _text(expected.get("platform"))
    candidate_platform = _text(candidate.get("platform"))
    if expected_platform and candidate_platform != expected_platform:
        return False
    if expected_platform and not candidate_platform:
        return False

    for field in ("application_package", "device_display_name"):
        expected_value = _text(expected.get(field))
        candidate_value = _text(candidate.get(field))
        if expected_value and candidate_value != expected_value:
            return False

    # Form factor helps distinguish sources when it is the only device detail,
    # but a Pixel Watch source often has a display name and no form factor in
    # the distance stream.  A known display name or application is sufficient
    # in that case.
    expected_form = _text(expected.get("device_form_factor"))
    candidate_form = _text(candidate.get("device_form_factor"))
    if expected_form and candidate_form and expected_form != candidate_form:
        return False
    if expected_form and not candidate_form and not (
        _text(expected.get("device_display_name")) or _text(expected.get("application_package"))
    ):
        return False

    for field in ("device_manufacturer", "device_model"):
        expected_value = _text(expected.get(field))
        candidate_value = _text(candidate.get(field))
        if expected_value and candidate_value != expected_value:
            return False
    return True


def _source_match_score(
    session_sources: Sequence[Mapping[str, Any]],
    candidate: Mapping[str, Any],
) -> tuple[int, int, int, str]:
    """Rank exact matches before preferring a source with more records."""

    best: tuple[int, int, int] = (-1, -1, -1)
    for expected in session_sources:
        if not _source_matches(expected, candidate):
            continue
        fields = (
            "platform",
            "application_package",
            "device_display_name",
            "device_form_factor",
            "device_manufacturer",
            "device_model",
        )
        populated = sum(bool(_text(expected.get(field))) for field in fields)
        exact = sum(
            bool(_text(expected.get(field))) and _text(expected.get(field)) == _text(candidate.get(field))
            for field in fields
        )
        # A source with a concrete application/device is a safer choice than a
        # platform-only source when both match.  The actual record count is
        # added by the caller after overlap validation.
        best = max(best, (exact, populated, int(_text(expected.get("recording_method")) == _text(candidate.get("recording_method")))))
    return (*best, _source_label(candidate))


def _session_source_descriptors(session: Mapping[str, Any]) -> list[dict[str, Any]]:
    return _source_candidates(session)


def _stream_values(samples: Mapping[str, Any], names: Sequence[str]) -> list[Mapping[str, Any]]:
    """Get one normalized stream while avoiding ``samples``/``records`` duplicates."""

    current: Any = samples
    # Support passing the complete fetch result's nested ``samples`` object.
    if isinstance(current, Mapping) and not any(name in current for name in names):
        nested = current.get("samples")
        if isinstance(nested, Mapping):
            current = nested

    value: Any = None
    for name in names:
        if isinstance(current, Mapping) and name in current:
            value = current.get(name)
            break
    if isinstance(value, Mapping):
        for key in ("samples", "records", "data"):
            nested = value.get(key)
            if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes, bytearray)):
                value = nested
                break
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _interval_value(sample: Mapping[str, Any], key: str) -> Any:
    for name in (key, key.replace("_", ""), key[0] + "".join(part.title() for part in key.split("_")[1:])):
        if name in sample:
            return sample.get(name)
    for container_name in ("distance", "payload"):
        container = _as_mapping(sample.get(container_name))
        if container is None:
            continue
        interval = _as_mapping(container.get("interval"))
        if interval is not None:
            for name in (key, key.replace("_", ""), key[0] + "".join(part.title() for part in key.split("_")[1:])):
                if name in interval:
                    return interval.get(name)
    raw = _as_mapping(sample.get("raw"))
    if raw is not None:
        payload = _as_mapping(raw.get("distance"))
        interval = _as_mapping(payload.get("interval")) if payload is not None else None
        if interval is not None:
            for name in (key, key.replace("_", ""), key[0] + "".join(part.title() for part in key.split("_")[1:])):
                if name in interval:
                    return interval.get(name)
    return None


def _distance_value(sample: Mapping[str, Any]) -> float | None:
    for key in ("distance_m", "distanceMeters", "distance_meters", "distanceM", "value"):
        if key in sample:
            value = _number(sample.get(key))
            if value is not None and value >= 0:
                return value

    for container_name in ("distance", "payload"):
        payload = _as_mapping(sample.get(container_name))
        if payload is None:
            continue
        for key, divisor in (("millimeters", 1000.0), ("distanceMillimeters", 1000.0), ("meters", 1.0), ("distanceMeters", 1.0), ("distance_m", 1.0)):
            value = _number(payload.get(key))
            if value is not None and value >= 0:
                return value / divisor

    raw = _as_mapping(sample.get("raw"))
    if raw is not None:
        payload = _as_mapping(raw.get("distance"))
        if payload is not None:
            value = _number(payload.get("millimeters"))
            if value is not None and value >= 0:
                return value / 1000.0
    return None


def _distance_intervals(values: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    intervals: list[dict[str, Any]] = []
    for position, sample in enumerate(values):
        start = _timestamp(_interval_value(sample, "start_time"))
        end = _timestamp(_interval_value(sample, "end_time"))
        distance = _distance_value(sample)
        source_values = _source_candidates(sample)
        if start is None or end is None or end <= start or distance is None:
            continue
        intervals.append(
            {
                "start": start,
                "end": end,
                "distance": distance,
                "duration": (end - start).total_seconds(),
                "sources": source_values,
                "position": position,
            }
        )
    return intervals


def _heart_rate_value(sample: Mapping[str, Any]) -> float | None:
    for key in ("heart_rate_bpm", "bpm", "heart_rate", "value"):
        if key in sample:
            value = _number(sample.get(key))
            if value is not None and 20 <= value <= 260:
                return value
    raw = _as_mapping(sample.get("raw"))
    payload = _as_mapping(raw.get("heartRate")) if raw is not None else None
    if payload is not None:
        value = _number(payload.get("beatsPerMinute"))
        if value is not None and 20 <= value <= 260:
            return value
    return None


def _heart_rate_samples(values: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for position, sample in enumerate(values):
        value = _timestamp(sample.get("timestamp") or sample.get("sample_time") or sample.get("sampleTime"))
        if value is None:
            raw = _as_mapping(sample.get("raw"))
            payload = _as_mapping(raw.get("heartRate")) if raw is not None else None
            sample_time = _as_mapping(payload.get("sampleTime")) if payload is not None else None
            value = _timestamp(sample_time.get("physicalTime")) if sample_time is not None else None
        bpm = _heart_rate_value(sample)
        sources = _source_candidates(sample)
        if value is None or bpm is None:
            continue
        result.append({"timestamp": value, "bpm": bpm, "sources": sources, "position": position})
    return result


def _source_groups(
    intervals: Sequence[Mapping[str, Any]],
    session_sources: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[tuple[str | None, ...], dict[str, Any]] = {}
    for interval in intervals:
        values = interval.get("sources")
        if not isinstance(values, Sequence):
            continue
        # A normalized sample can expose both ``data_source`` and
        # ``raw.dataSource``.  If they disagree, consider the interval under
        # each identity and let the session match select the eligible one.
        for source_value in values:
            if not isinstance(source_value, Mapping):
                continue
            source = dict(source_value)
            key = _source_key(source)
            group = groups.setdefault(
                key,
                {
                    "key": key,
                    "source": source,
                    "intervals": [],
                    "records_seen": 0,
                },
            )
            # Keep the richest source object as provenance when recording
            # method or optional device metadata differs between records.
            if len(source) > len(group["source"]):
                group["source"] = source
            group["intervals"].append(interval)
            group["records_seen"] += 1

    candidates: list[dict[str, Any]] = []
    for group in groups.values():
        source = group["source"]
        if not any(_source_matches(expected, source) for expected in session_sources):
            continue
        candidates.append(group)
    return candidates


def _safe_intervals(intervals: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
    """Dedupe exact records and drop every interval in an overlap cluster."""

    ordered = sorted(intervals, key=lambda item: (item["start"], item["end"], item["position"]))
    unique: list[dict[str, Any]] = []
    deduplicated = 0
    for item in ordered:
        duplicate = None
        for existing in unique:
            if item["start"] != existing["start"] or item["end"] != existing["end"]:
                continue
            if abs(float(item["distance"]) - float(existing["distance"])) <= _DISTANCE_EPSILON_M:
                duplicate = existing
                break
        if duplicate is not None:
            deduplicated += 1
            continue
        unique.append(dict(item))

    # A sweep marks the active interval set whenever an overlap is found.  All
    # members of that cluster are discarded, leaving an explicit unknown gap
    # instead of making an arbitrary choice and double counting distance.
    ambiguous: set[int] = set()
    active: list[int] = []
    for index, item in enumerate(unique):
        active = [active_index for active_index in active if unique[active_index]["end"] > item["start"]]
        if active:
            ambiguous.add(index)
            ambiguous.update(active)
        active.append(index)

    safe = [item for index, item in enumerate(unique) if index not in ambiguous]
    return safe, deduplicated, len(ambiguous)


def _select_source(
    groups: Sequence[dict[str, Any]],
    session_sources: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], int, int] | None:
    ranked: list[tuple[tuple[int, int, int, int, float, str], dict[str, Any], list[dict[str, Any]], int, int]] = []
    for group in groups:
        safe, deduplicated, ambiguous = _safe_intervals(group["intervals"])
        if not safe:
            continue
        score = _source_match_score(session_sources, group["source"])
        total_distance = sum(float(item["distance"]) for item in safe)
        rank = (*score[:3], len(safe), total_distance, score[3])
        ranked.append((rank, group, safe, deduplicated, ambiguous))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    _, group, safe, deduplicated, ambiguous = ranked[0]
    return group, safe, deduplicated, ambiguous


def _matching_heart_rate(
    point_time: datetime,
    heart_rate: Sequence[Mapping[str, Any]],
) -> tuple[int | float | None, float | None]:
    if not heart_rate:
        return None, None
    # The caller supplies records already filtered to the selected source and
    # sorted by timestamp.  Looking at the two neighboring records avoids a
    # scan of a complete heart-rate stream for every sparse distance point.
    timestamps = [sample["timestamp"] for sample in heart_rate]
    index = bisect_left(timestamps, point_time)
    nearest: tuple[float, int, Mapping[str, Any]] | None = None
    for position in (index - 1, index):
        if position < 0 or position >= len(heart_rate):
            continue
        sample = heart_rate[position]
        timestamp = sample.get("timestamp")
        bpm = _number(sample.get("bpm"))
        if not isinstance(timestamp, datetime) or bpm is None:
            continue
        delta = abs((timestamp - point_time).total_seconds())
        if delta > _HR_MAX_NEAREST_SECONDS:
            continue
        candidate = (delta, position, sample)
        if nearest is None or candidate[:2] < nearest[:2]:
            nearest = candidate
    if nearest is None:
        return None, None
    bpm = nearest[2]["bpm"]
    if isinstance(bpm, float) and bpm.is_integer():
        bpm = int(bpm)
    return bpm, nearest[0]


def _session_bounds(session: Mapping[str, Any]) -> tuple[datetime | None, datetime | None]:
    start = _timestamp(session.get("started_at") or session.get("start_time") or session.get("startTime"))
    end = _timestamp(session.get("ended_at") or session.get("end_time") or session.get("endTime"))
    return start, end


def _route_id(session: Mapping[str, Any]) -> str | None:
    for key in ("exercise_id", "provider_id", "source_id", "id", "resource_name"):
        value = _text(session.get(key))
        if value:
            return value if key in {"exercise_id", "provider_id"} else (_last_component(value) or value)
    return None


def build_telemetry_route(session: Mapping[str, Any], samples: Mapping[str, Any]) -> dict[str, Any] | None:
    """Build sparse cumulative distance points for one Google exercise.

    ``samples`` accepts the aggregate fetch shape (``{"distance": [...],
    "heart-rate": [...]}``) as well as stream objects containing ``samples``
    or ``records``.  The result is ``None`` unless a distance record carries a
    source that matches the session's ``data_source`` or ``raw.dataSource``.

    Distance intervals are clipped to known session bounds in proportion to
    their duration.  Missing intervals remain missing; no one-hertz samples,
    GPS coordinates, altitude, or summary-distance extrapolation is created.
    """

    if not isinstance(session, Mapping) or not isinstance(samples, Mapping):
        return None

    session_sources = _session_source_descriptors(session)
    if not session_sources:
        return None
    session_start, session_end = _session_bounds(session)
    if session_start is not None and session_end is not None and session_end <= session_start:
        return None

    distance_records = _stream_values(samples, ("distance", "distance_samples", "distanceSamples"))
    intervals = _distance_intervals(distance_records)
    if not intervals:
        return None
    groups = _source_groups(intervals, session_sources)
    selected = _select_source(groups, session_sources)
    if selected is None:
        return None
    group, safe_intervals, deduplicated_count, ambiguous_count = selected
    selected_source = group["source"]

    hr_records = _stream_values(samples, ("heart-rate", "heart_rate", "heartRate"))
    heart_rate = _heart_rate_samples(hr_records)
    matching_heart_rate = sorted(
        (
            sample
            for sample in heart_rate
            if any(
                isinstance(source, Mapping) and _source_key(source) == _source_key(selected_source)
                for source in sample.get("sources", [])
            )
        ),
        key=lambda sample: (sample["timestamp"], sample["position"]),
    )

    # Clip before emitting points so gap detection is based on the actual
    # interval boundaries presented to the caller.  The original durations
    # remain the cadence signal for ``sample_interval_seconds``.
    used_intervals: list[dict[str, Any]] = []
    original_durations: list[float] = []
    clipped_count = 0
    clipped_bounds: list[dict[str, str]] = []
    for interval in safe_intervals:
        start = interval["start"]
        end = interval["end"]
        clipped_start = max(start, session_start) if session_start is not None else start
        clipped_end = min(end, session_end) if session_end is not None else end
        if clipped_end <= clipped_start:
            continue
        original_duration = float(interval["duration"])
        clipped_duration = (clipped_end - clipped_start).total_seconds()
        if clipped_duration <= 0 or original_duration <= 0:
            continue
        original_durations.append(original_duration)
        ratio = min(1.0, max(0.0, clipped_duration / original_duration))
        distance = float(interval["distance"]) * ratio
        if clipped_start != start or clipped_end != end:
            clipped_count += 1
            clipped_bounds.append(
                {
                    "original_start": _format_timestamp(start),
                    "original_end": _format_timestamp(end),
                    "start": _format_timestamp(clipped_start),
                    "end": _format_timestamp(clipped_end),
                }
            )
        used_intervals.append(
            {
                "start": clipped_start,
                "end": clipped_end,
                "distance": distance,
                "original_start": start,
                "original_end": end,
            }
        )

    if not used_intervals:
        return None

    points: list[dict[str, Any]] = []

    def append_boundary(
        timestamp: datetime,
        distance: float,
        *,
        interval: Mapping[str, Any],
        boundary: str,
        unknown_before: bool,
    ) -> None:
        point: dict[str, Any] = {
            "timestamp": _format_timestamp(timestamp),
            "distance_m": distance,
            "unknown_before": unknown_before,
            "interval_boundary": boundary,
            "interval_start": _format_timestamp(interval["start"]),
            "interval_end": _format_timestamp(interval["end"]),
            "interval_distance_m": interval["distance"],
        }
        bpm, _ = _matching_heart_rate(timestamp, matching_heart_rate)
        if bpm is not None:
            point["heart_rate"] = bpm
        points.append(point)

    # Distance interval values belong to their end time.  The first start
    # anchor prevents the analyzer from treating the first interval's distance
    # and elapsed time as an unobserved prehistory.
    cumulative_distance = 0.0
    first_interval = used_intervals[0]
    append_boundary(
        first_interval["start"],
        cumulative_distance,
        interval=first_interval,
        boundary="start",
        unknown_before=False,
    )
    interval_gaps: list[float] = []
    endpoint_times: list[datetime] = []
    for index, interval in enumerate(used_intervals):
        if index:
            previous = used_intervals[index - 1]
            gap_seconds = (interval["start"] - previous["end"]).total_seconds()
            if gap_seconds > 0:
                interval_gaps.append(gap_seconds)
                # The point carries the current interval metadata, while
                # ``unknown_before`` marks the preceding segment as a gap.
                append_boundary(
                    interval["start"],
                    cumulative_distance,
                    interval=interval,
                    boundary="gap_start",
                    unknown_before=True,
                )
        cumulative_distance += float(interval["distance"])
        append_boundary(
            interval["end"],
            cumulative_distance,
            interval=interval,
            boundary="end",
            unknown_before=False,
        )
        endpoint_times.append(interval["end"])

    sample_interval_seconds = statistics.median(original_durations) if original_durations else None
    timestamps = [_timestamp(point["timestamp"]) for point in points]
    parsed_timestamps = [value for value in timestamps if value is not None]
    gaps = [
        (second - first).total_seconds()
        for first, second in zip(parsed_timestamps, parsed_timestamps[1:])
        if second >= first
    ]
    # Keep the cadence metric comparable with the former endpoint-only
    # projection: a missing interval appears as a larger endpoint-to-endpoint
    # gap even though an explicit boundary point now preserves its unknown
    # status for downstream analysis.
    endpoint_gaps = [
        (second - first).total_seconds()
        for first, second in zip(endpoint_times, endpoint_times[1:])
        if second >= first
    ]
    observed_max_gap = max(endpoint_gaps, default=sample_interval_seconds or 0.0)
    emitted_max_gap = max(gaps, default=sample_interval_seconds or 0.0)
    if sample_interval_seconds is not None:
        analysis_max_gap = min(_ANALYSIS_GAP_CAP_SECONDS, max(1.0, sample_interval_seconds + 5.0))
    else:
        analysis_max_gap = _ANALYSIS_GAP_CAP_SECONDS
    gap_count = len(interval_gaps)

    first_interval_start = used_intervals[0]["start"]
    last_interval_end = used_intervals[-1]["end"]
    leading_gap = (
        max(0.0, (first_interval_start - session_start).total_seconds())
        if session_start is not None
        else 0.0
    )
    trailing_gap = (
        max(0.0, (session_end - last_interval_end).total_seconds())
        if session_end is not None
        else 0.0
    )
    internal_unknown = sum(interval_gaps)
    unknown_gap_seconds = leading_gap + trailing_gap + internal_unknown
    coarse = True  # interval aggregates are coarse telemetry even at short cadence

    distance_provenance = {
        "provider": "google_health",
        "source": deepcopy(selected_source.get("raw") or {}),
        "source_key": list(_source_key(selected_source)),
        "source_label": _source_label(selected_source),
        "records_seen": group["records_seen"],
        "intervals_used": len(used_intervals),
        "deduplicated_intervals": deduplicated_count,
        "rejected_overlapping_intervals": ambiguous_count,
        "clipped_intervals": clipped_count,
        "clipped_bounds": clipped_bounds,
    }
    heart_rate_provenance = {
        "provider": "google_health",
        "same_source_records": len(matching_heart_rate),
        "nearest_bound_seconds": _HR_MAX_NEAREST_SECONDS,
        "source_consistent": bool(matching_heart_rate) or not heart_rate,
    }
    quality = {
        "coarse": coarse,
        "sample_count": len(points),
        "interval_count": len(used_intervals),
        "sample_interval_seconds": sample_interval_seconds,
        "max_gap_seconds": observed_max_gap,
        "emitted_point_max_gap_seconds": emitted_max_gap,
        "analysis_max_gap_seconds": analysis_max_gap,
        "gap_count": gap_count,
        "unknown_gap_count": gap_count,
        "leading_gap_seconds": leading_gap,
        "trailing_gap_seconds": trailing_gap,
        "unknown_gap_seconds": unknown_gap_seconds,
        "distance_complete": unknown_gap_seconds <= 0.0 and ambiguous_count == 0,
        "source_consistent": True,
        "clipped_intervals": clipped_count,
        "deduplicated_intervals": deduplicated_count,
        "rejected_overlapping_intervals": ambiguous_count,
    }

    return {
        "exercise_id": _route_id(session),
        "points": points,
        "sample_interval_seconds": sample_interval_seconds,
        "distance_source": _DISTANCE_SOURCE,
        "provenance": {
            "provider": "google_health",
            "distance_source": distance_provenance,
            "heart_rate_source": heart_rate_provenance,
            "data_source": deepcopy(selected_source.get("raw") or {}),
            "source_key": list(_source_key(selected_source)),
            "source_label": _source_label(selected_source),
            "source_consistent": True,
        },
        "quality": quality,
        # Flat aliases make the result convenient for route owners while the
        # nested quality object remains the canonical metadata projection.
        "coarse": coarse,
        "max_gap_seconds": observed_max_gap,
        "analysis_max_gap_seconds": analysis_max_gap,
        "unknown_gap_count": gap_count,
    }
