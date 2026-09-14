"""Deterministic activity analysis for Google Health samples.

The Google Health API supplies raw samples and, for an exercise session, may
also supply an authoritative active duration and provider splits.  This
module is intentionally independent of the provider and does not claim to
reproduce Strava's proprietary moving-time algorithm.  When only samples are
available, moving and stopped time are estimates derived from segment speed.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
from typing import Any, Mapping, Sequence


DEFAULT_SPEED_THRESHOLD_MPS = 0.5
DEFAULT_MIN_STOP_SECONDS = 5.0
DEFAULT_MAX_GAP_SECONDS = 30.0
DEFAULT_GPS_SPIKE_SPEED_MPS = 12.0
DEFAULT_SPLIT_DISTANCE_M = 1000.0


@dataclass(slots=True)
class _Point:
    elapsed_seconds: float
    distance_m: float
    lat: float | None = None
    lon: float | None = None


@dataclass(slots=True)
class _Segment:
    index: int
    start_seconds: float
    end_seconds: float
    elapsed_seconds: float
    raw_distance_m: float
    accepted_distance_m: float
    speed_mps: float | None
    state: str
    is_gap: bool = False
    is_spike: bool = False


def _finite_number(value: Any, *, field: str, index: int, minimum: float = 0.0) -> float:
    if isinstance(value, bool):
        raise ValueError(f"sample {index} {field} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"sample {index} {field} must be a finite number") from exc
    if not math.isfinite(number) or number < minimum:
        raise ValueError(f"sample {index} {field} must be finite and >= {minimum:g}")
    return number


def _optional_coordinate(value: Any, *, field: str, index: int, low: float, high: float) -> float | None:
    if value is None:
        return None
    number = _finite_number(value, field=field, index=index, minimum=-math.inf)
    if number < low or number > high:
        raise ValueError(f"sample {index} {field} must be between {low:g} and {high:g}")
    return number


def _validate_samples(samples: Sequence[Mapping[str, Any]]) -> list[_Point]:
    if not isinstance(samples, (list, tuple)):
        raise ValueError("samples must be a list of mappings")
    points: list[_Point] = []
    previous_time: float | None = None
    previous_distance: float | None = None
    for index, sample in enumerate(samples):
        if not isinstance(sample, Mapping):
            raise ValueError(f"sample {index} must be a mapping")
        if "elapsed_seconds" not in sample:
            raise ValueError(f"sample {index} is missing elapsed_seconds")
        if "distance_m" not in sample:
            raise ValueError(f"sample {index} is missing cumulative distance_m")
        elapsed = _finite_number(sample["elapsed_seconds"], field="elapsed_seconds", index=index)
        distance = _finite_number(sample["distance_m"], field="distance_m", index=index)
        if previous_time is not None and elapsed < previous_time:
            raise ValueError("samples must be sorted by nondecreasing elapsed_seconds")
        if previous_distance is not None and distance < previous_distance:
            raise ValueError("sample distance_m must be cumulative and nondecreasing")
        lat = _optional_coordinate(sample.get("lat"), field="lat", index=index, low=-90.0, high=90.0)
        lon = _optional_coordinate(sample.get("lon"), field="lon", index=index, low=-180.0, high=180.0)
        points.append(_Point(elapsed, distance, lat, lon))
        previous_time = elapsed
        previous_distance = distance
    return points


def _haversine_meters(first: _Point, second: _Point) -> float | None:
    if first.lat is None or first.lon is None or second.lat is None or second.lon is None:
        return None
    radius = 6_371_000.0
    lat1 = math.radians(first.lat)
    lat2 = math.radians(second.lat)
    d_lat = lat2 - lat1
    d_lon = math.radians(second.lon - first.lon)
    a = math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


def _validate_thresholds(
    *,
    speed_threshold_mps: float,
    min_stop_seconds: float,
    max_gap_seconds: float,
    gps_spike_speed_mps: float,
    split_distance_m: float,
) -> None:
    for name, value in (
        ("speed_threshold_mps", speed_threshold_mps),
        ("min_stop_seconds", min_stop_seconds),
        ("max_gap_seconds", max_gap_seconds),
        ("gps_spike_speed_mps", gps_spike_speed_mps),
        ("split_distance_m", split_distance_m),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(f"{name} must be finite")
        if float(value) <= 0:
            raise ValueError(f"{name} must be greater than zero")


def _build_segments(
    points: list[_Point],
    *,
    speed_threshold_mps: float,
    max_gap_seconds: float,
    gps_spike_speed_mps: float,
) -> tuple[list[_Segment], list[float], list[str]]:
    segments: list[_Segment] = []
    corrected_distances = [0.0]
    flags: list[str] = []
    for index in range(1, len(points)):
        first = points[index - 1]
        second = points[index]
        dt = second.elapsed_seconds - first.elapsed_seconds
        raw_delta = second.distance_m - first.distance_m
        if dt <= 0:
            # Duplicate timestamps cannot contribute duration.  A positive
            # distance at the same timestamp is retained as an unknown spike.
            is_spike = raw_delta > 0
            if is_spike:
                flags.append("gps_distance_spike_filtered")
            accepted_delta = 0.0 if is_spike else raw_delta
            segments.append(
                _Segment(
                    index=index - 1,
                    start_seconds=first.elapsed_seconds,
                    end_seconds=second.elapsed_seconds,
                    elapsed_seconds=0.0,
                    raw_distance_m=raw_delta,
                    accepted_distance_m=accepted_delta,
                    speed_mps=None,
                    state="unknown",
                    is_spike=is_spike,
                )
            )
            corrected_distances.append(corrected_distances[-1] + accepted_delta)
            continue

        speed = raw_delta / dt
        route_delta = _haversine_meters(first, second)
        route_speed = route_delta / dt if route_delta is not None else None
        is_gap = dt > max_gap_seconds
        is_spike = (
            raw_delta > 0 and speed > gps_spike_speed_mps
        ) or (route_speed is not None and route_speed > gps_spike_speed_mps)
        if is_gap:
            flags.append("missing_data_gap")
        if is_spike:
            flags.append("gps_distance_spike_filtered")
        accepted_delta = 0.0 if is_spike else raw_delta
        if is_gap or is_spike:
            state = "unknown"
        elif speed >= speed_threshold_mps:
            state = "moving"
        else:
            state = "stopped"
        segments.append(
            _Segment(
                index=index - 1,
                start_seconds=first.elapsed_seconds,
                end_seconds=second.elapsed_seconds,
                elapsed_seconds=dt,
                raw_distance_m=raw_delta,
                accepted_distance_m=accepted_delta,
                speed_mps=speed,
                state=state,
                is_gap=is_gap,
                is_spike=is_spike,
            )
        )
        corrected_distances.append(corrected_distances[-1] + accepted_delta)
    return segments, corrected_distances, flags


def _apply_stop_hysteresis(
    segments: list[_Segment],
    *,
    min_stop_seconds: float,
) -> bool:
    changed = False
    index = 0
    while index < len(segments):
        if segments[index].state != "stopped":
            index += 1
            continue
        end = index
        duration = 0.0
        while end < len(segments) and segments[end].state == "stopped":
            duration += segments[end].elapsed_seconds
            end += 1
        if duration < min_stop_seconds:
            for candidate in segments[index:end]:
                candidate.state = "moving"
            changed = True
        index = end
    return changed


def _pace(moving_seconds: float, distance_m: float) -> float | None:
    if moving_seconds <= 0 or distance_m <= 0:
        return None
    return moving_seconds / distance_m


def _time_at_distance(
    target_distance_m: float,
    points: list[_Point],
    corrected_distances: list[float],
) -> float:
    if target_distance_m <= 0 or not points:
        return points[0].elapsed_seconds if points else 0.0
    for index in range(1, len(points)):
        start_distance = corrected_distances[index - 1]
        end_distance = corrected_distances[index]
        if end_distance < target_distance_m:
            continue
        if end_distance <= start_distance:
            if math.isclose(end_distance, target_distance_m, abs_tol=1e-9):
                return points[index].elapsed_seconds
            continue
        fraction = (target_distance_m - start_distance) / (end_distance - start_distance)
        fraction = max(0.0, min(1.0, fraction))
        return points[index - 1].elapsed_seconds + fraction * (
            points[index].elapsed_seconds - points[index - 1].elapsed_seconds
        )
    return points[-1].elapsed_seconds


def _split_metrics(
    start_seconds: float,
    end_seconds: float,
    distance_m: float,
    segments: list[_Segment],
) -> dict[str, Any]:
    elapsed = max(0.0, end_seconds - start_seconds)
    moving_seconds = 0.0
    moving_distance = 0.0
    unknown_seconds = 0.0
    for segment in segments:
        overlap = max(0.0, min(end_seconds, segment.end_seconds) - max(start_seconds, segment.start_seconds))
        if overlap <= 0 or segment.elapsed_seconds <= 0:
            continue
        ratio = overlap / segment.elapsed_seconds
        if segment.state == "moving":
            moving_seconds += overlap
            moving_distance += segment.accepted_distance_m * ratio
        elif segment.state == "unknown":
            unknown_seconds += overlap
    return {
        "start_seconds": start_seconds,
        "end_seconds": end_seconds,
        "distance_m": max(0.0, distance_m),
        "elapsed_seconds": elapsed,
        "moving_seconds": moving_seconds,
        "pace_seconds": _pace(moving_seconds, moving_distance),
        "elapsed_pace_seconds": elapsed / distance_m if distance_m > 0 else None,
        "moving_distance_m": moving_distance,
        "unknown_seconds": unknown_seconds,
    }


def _build_detected_intervals(segments: list[_Segment]) -> list[dict[str, Any]]:
    intervals: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for segment in segments:
        if segment.elapsed_seconds <= 0:
            continue
        label = segment.state
        if current is None or current["label"] != label:
            if current is not None:
                intervals.append(current)
            current = {
                "start_seconds": segment.start_seconds,
                "end_seconds": segment.end_seconds,
                "label": label,
                "inferred": True,
                "elapsed_seconds": segment.elapsed_seconds,
                "moving_seconds": segment.elapsed_seconds if label == "moving" else 0.0,
                "distance_m": segment.accepted_distance_m,
                "unknown_seconds": segment.elapsed_seconds if label == "unknown" else 0.0,
            }
            continue
        current["end_seconds"] = segment.end_seconds
        current["elapsed_seconds"] += segment.elapsed_seconds
        current["moving_seconds"] += segment.elapsed_seconds if label == "moving" else 0.0
        current["distance_m"] += segment.accepted_distance_m
        current["unknown_seconds"] += segment.elapsed_seconds if label == "unknown" else 0.0
    if current is not None:
        intervals.append(current)
    for interval in intervals:
        interval["pace_seconds"] = _pace(interval["moving_seconds"], interval["distance_m"])
    return intervals


def _lap_number(lap: Mapping[str, Any], names: tuple[str, ...], *, field: str, index: int) -> Any:
    for name in names:
        if name in lap:
            return lap[name]
    raise ValueError(f"lap {index} is missing {field}")


def _validate_explicit_laps(laps: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(laps, (list, tuple)):
        raise ValueError("laps must be a list of mappings")
    validated: list[dict[str, Any]] = []
    previous_end = -math.inf
    for index, original in enumerate(laps):
        if not isinstance(original, Mapping):
            raise ValueError(f"lap {index} must be a mapping")
        start = _finite_number(
            _lap_number(original, ("start_seconds", "start"), field="start_seconds", index=index),
            field="start_seconds",
            index=index,
        )
        end = _finite_number(
            _lap_number(original, ("end_seconds", "end"), field="end_seconds", index=index),
            field="end_seconds",
            index=index,
        )
        if end <= start:
            raise ValueError(f"lap {index} end_seconds must be greater than start_seconds")
        if start < previous_end:
            raise ValueError("explicit laps must be sorted and non-overlapping")
        distance_value = original.get("distance_m")
        if distance_value is None:
            distance_value = original.get("distance")
        distance: float | None = None
        if distance_value is not None:
            distance = _finite_number(distance_value, field="distance_m", index=index)
        elapsed_value = original.get("elapsed_seconds", end - start)
        elapsed = _finite_number(elapsed_value, field="elapsed_seconds", index=index)
        if elapsed <= 0:
            raise ValueError(f"lap {index} elapsed_seconds must be greater than zero")
        moving_value = original.get("moving_seconds", original.get("active_duration_seconds", elapsed))
        moving = _finite_number(moving_value, field="moving_seconds", index=index)
        if moving > elapsed + 1e-9:
            raise ValueError(f"lap {index} moving_seconds cannot exceed elapsed_seconds")
        pace_value = original.get("pace_seconds")
        pace = (
            _finite_number(pace_value, field="pace_seconds", index=index)
            if pace_value is not None
            else _pace(moving, distance or 0.0)
        )
        normalized = deepcopy(dict(original))
        normalized.update(
            {
                "start_seconds": start,
                "end_seconds": end,
                "distance_m": distance,
                "elapsed_seconds": elapsed,
                "moving_seconds": moving,
                "pace_seconds": pace,
                "explicit": True,
            }
        )
        validated.append(normalized)
        previous_end = end
    return validated


def analyze_activity(
    samples: list[dict[str, Any]],
    laps: list[dict[str, Any]] | None = None,
    *,
    speed_threshold_mps: float = DEFAULT_SPEED_THRESHOLD_MPS,
    min_stop_seconds: float = DEFAULT_MIN_STOP_SECONDS,
    max_gap_seconds: float = DEFAULT_MAX_GAP_SECONDS,
    gps_spike_speed_mps: float = DEFAULT_GPS_SPIKE_SPEED_MPS,
    split_distance_m: float = DEFAULT_SPLIT_DISTANCE_M,
) -> dict[str, Any]:
    """Analyze elapsed time and cumulative distance samples.

    A segment is considered moving when its observed speed is at least
    ``speed_threshold_mps``.  Consecutive stopped segments shorter than
    ``min_stop_seconds`` are folded into movement to avoid flicker around the
    threshold.  Segments separated by more than ``max_gap_seconds`` and
    implausible GPS jumps are excluded from moving/stopped inference and are
    reported as uncertain.  The defaults are intentionally configurable and
    should not be described as an exact Strava-equivalent result.
    """

    _validate_thresholds(
        speed_threshold_mps=speed_threshold_mps,
        min_stop_seconds=min_stop_seconds,
        max_gap_seconds=max_gap_seconds,
        gps_spike_speed_mps=gps_spike_speed_mps,
        split_distance_m=split_distance_m,
    )
    points = _validate_samples(samples)
    explicit_splits = _validate_explicit_laps(laps) if laps is not None else None
    if not points:
        flags = ["no_samples", "moving_time_estimated"]
        if explicit_splits:
            flags.append("explicit_laps_used")
        return {
            "elapsed_seconds": 0.0,
            "moving_seconds": 0.0,
            "stopped_seconds": 0.0,
            "distance_m": 0.0,
            "moving_pace_seconds": None,
            "quality_flags": flags,
            "splits": explicit_splits or [],
            "intervals": [],
            "method": "heuristic_speed_threshold_with_gap_and_gps_filter",
            "raw_distance_m": 0.0,
            "confident_seconds": 0.0,
            "unclassified_seconds": 0.0,
            "moving_distance_m": 0.0,
            "unclassified_distance_m": 0.0,
        }

    # Normalize the first point to zero while retaining cumulative deltas.
    origin_time = points[0].elapsed_seconds
    origin_distance = points[0].distance_m
    points = [
        _Point(point.elapsed_seconds - origin_time, point.distance_m, point.lat, point.lon)
        for point in points
    ]
    raw_distance = max(0.0, points[-1].distance_m - origin_distance)
    segments, corrected_distances, segment_flags = _build_segments(
        points,
        speed_threshold_mps=float(speed_threshold_mps),
        max_gap_seconds=float(max_gap_seconds),
        gps_spike_speed_mps=float(gps_spike_speed_mps),
    )
    hysteresis_changed = _apply_stop_hysteresis(
        segments,
        min_stop_seconds=float(min_stop_seconds),
    )
    accepted_distance = corrected_distances[-1] if corrected_distances else 0.0
    moving_seconds = sum(segment.elapsed_seconds for segment in segments if segment.state == "moving")
    stopped_seconds = sum(segment.elapsed_seconds for segment in segments if segment.state == "stopped")
    confident_seconds = moving_seconds + stopped_seconds
    unclassified_seconds = sum(segment.elapsed_seconds for segment in segments if segment.state == "unknown")
    moving_distance = sum(
        segment.accepted_distance_m for segment in segments if segment.state == "moving"
    )
    unclassified_distance = sum(
        segment.accepted_distance_m for segment in segments if segment.state == "unknown"
    )
    elapsed_seconds = points[-1].elapsed_seconds
    quality_flags: list[str] = ["moving_time_estimated"]
    for flag in segment_flags:
        if flag not in quality_flags:
            quality_flags.append(flag)
    if hysteresis_changed:
        quality_flags.append("short_stop_smoothed")
    if not segments:
        quality_flags.append("insufficient_samples")
    if unclassified_seconds > 0 and "uncertain_coverage" not in quality_flags:
        quality_flags.append("uncertain_coverage")
    if accepted_distance < raw_distance - 1e-9 and "distance_filtered" not in quality_flags:
        quality_flags.append("distance_filtered")
    if explicit_splits is not None:
        quality_flags.append("explicit_laps_used")
    else:
        quality_flags.append("one_kilometer_splits_interpolated")
    if segments:
        quality_flags.append("inferred_intervals")

    if explicit_splits is not None:
        splits = explicit_splits
    else:
        splits: list[dict[str, Any]] = []
        split_start_distance = 0.0
        while split_start_distance < accepted_distance - 1e-9:
            split_end_distance = min(split_start_distance + float(split_distance_m), accepted_distance)
            start_seconds = _time_at_distance(split_start_distance, points, corrected_distances)
            end_seconds = _time_at_distance(split_end_distance, points, corrected_distances)
            splits.append(
                _split_metrics(
                    start_seconds,
                    end_seconds,
                    split_end_distance - split_start_distance,
                    segments,
                )
            )
            split_start_distance = split_end_distance

    intervals = _build_detected_intervals(segments)
    return {
        "elapsed_seconds": elapsed_seconds,
        "moving_seconds": moving_seconds,
        "stopped_seconds": stopped_seconds,
        "distance_m": accepted_distance,
        "moving_pace_seconds": _pace(moving_seconds, moving_distance),
        "quality_flags": quality_flags,
        "splits": splits,
        "intervals": intervals,
        "method": "heuristic_speed_threshold_with_gap_and_gps_filter",
        # These additional fields make the raw-vs-inferred distinction
        # explicit for callers displaying a confidence explanation.
        "raw_distance_m": raw_distance,
        "raw_pace_seconds": elapsed_seconds / raw_distance if raw_distance > 0 else None,
        "confident_seconds": confident_seconds,
        "unclassified_seconds": unclassified_seconds,
        "moving_distance_m": moving_distance,
        "unclassified_distance_m": unclassified_distance,
        "thresholds": {
            "speed_threshold_mps": float(speed_threshold_mps),
            "min_stop_seconds": float(min_stop_seconds),
            "max_gap_seconds": float(max_gap_seconds),
            "gps_spike_speed_mps": float(gps_spike_speed_mps),
            "split_distance_m": float(split_distance_m),
        },
    }


__all__ = [
    "DEFAULT_GPS_SPIKE_SPEED_MPS",
    "DEFAULT_MAX_GAP_SECONDS",
    "DEFAULT_MIN_STOP_SECONDS",
    "DEFAULT_SPEED_THRESHOLD_MPS",
    "DEFAULT_SPLIT_DISTANCE_M",
    "analyze_activity",
]
