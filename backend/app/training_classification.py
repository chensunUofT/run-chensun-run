"""Conservative deterministic training-type classification.

The classifier is deliberately small and reviewable.  It recognizes only two
workout signatures from telemetry:

* tempo: one continuous moving block of at least 2 km at 6:00/km or faster;
* interval: at least two sustained fast bouts (faster than 5:00/km) separated
  by walking recoveries.

Everything else falls back to ``easy`` unless a race, long-run, or other known
label is explicitly confirmed by the caller.  Distance alone never creates a
race label, and a single fast GPS point never creates an interval label.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
import math
from typing import Any, Mapping, Sequence


DEFAULT_SPEED_THRESHOLD_MPS = 0.5
DEFAULT_MAX_GAP_SECONDS = 30.0
DEFAULT_GPS_SPIKE_SPEED_MPS = 12.0
TEMPO_MAX_PACE_SECONDS_PER_KM = 360.0
INTERVAL_MAX_PACE_SECONDS_PER_KM = 300.0
MIN_CONTINUOUS_TEMPO_DISTANCE_M = 2_000.0
MIN_FAST_BOUT_SECONDS = 8.0
MIN_FAST_BOUT_DISTANCE_M = 30.0
MIN_WALKING_RECOVERY_SECONDS = 5.0
# Keep the recovery test conservative.  Faster than 2.0 m/s is roughly
# 8:20/km and may be an easy jog rather than a walking recovery.
WALKING_MAX_SPEED_MPS = 2.0

_KNOWN_LABELS = frozenset({"easy", "long", "tempo", "interval", "race", "quality", "recovery"})
_PROTECTED_LABELS = frozenset({"race", "long"})


@dataclass(frozen=True, slots=True)
class _Motion:
    start_seconds: float
    end_seconds: float
    distance_m: float
    speed_mps: float | None
    state: str
    source: str
    segment_count: int = 1

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, self.end_seconds - self.start_seconds)

    @property
    def pace_seconds_per_km(self) -> float | None:
        if self.speed_mps is None or self.speed_mps <= 0:
            return None
        return 1_000.0 / self.speed_mps


def _value(source: Any, *names: str, default: Any = None) -> Any:
    if source is None:
        return default
    if isinstance(source, Mapping):
        for name in names:
            if name in source:
                return source[name]
        return default
    for name in names:
        if hasattr(source, name):
            return getattr(source, name)
    return default


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _normalise_label(value: Any) -> str | None:
    if value is None:
        return None
    label = str(value).strip().lower().replace("-", "_")
    if not label:
        return None
    aliases = {
        "threshold": "tempo",
        "tempo_run": "tempo",
        "repetition": "interval",
        "repetitions": "interval",
        "repeats": "interval",
        "intervals": "interval",
        "long_run": "long",
        "race_run": "race",
        "recovery_run": "recovery",
    }
    return aliases.get(label, label)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _haversine_meters(first: Mapping[str, Any], second: Mapping[str, Any]) -> float | None:
    def coordinate(item: Mapping[str, Any], *names: str) -> float | None:
        for name in names:
            number = _finite(item.get(name))
            if number is not None:
                return number
        return None

    lat1 = coordinate(first, "lat", "latitude")
    lon1 = coordinate(first, "lon", "longitude")
    lat2 = coordinate(second, "lat", "latitude")
    lon2 = coordinate(second, "lon", "longitude")
    if None in {lat1, lon1, lat2, lon2}:
        return None
    if not (-90 <= lat1 <= 90 and -90 <= lat2 <= 90 and -180 <= lon1 <= 180 and -180 <= lon2 <= 180):
        return None
    lat1_r = math.radians(lat1)
    lat2_r = math.radians(lat2)
    d_lat = lat2_r - lat1_r
    d_lon = math.radians(lon2 - lon1)
    a = math.sin(d_lat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(d_lon / 2) ** 2
    return 2 * 6_371_000.0 * math.asin(min(1.0, math.sqrt(max(0.0, a))))


def _motion_segments(
    samples: Sequence[Mapping[str, Any]],
    *,
    speed_threshold_mps: float = DEFAULT_SPEED_THRESHOLD_MPS,
    max_gap_seconds: float = DEFAULT_MAX_GAP_SECONDS,
    gps_spike_speed_mps: float = DEFAULT_GPS_SPIKE_SPEED_MPS,
) -> tuple[list[_Motion], set[str]]:
    """Build tolerant motion segments for classification.

    ``analyze_activity`` remains the source of persisted activity metrics. The
    classifier repeats only the small amount of segment arithmetic it needs so
    it can be used during import before an analysis object has been stored.
    Invalid points are skipped here; validation errors belong to the API
    boundary and should not turn an optional type suggestion into an import
    failure.
    """

    if not isinstance(samples, (list, tuple)):
        return [], {"invalid_telemetry"}
    segments: list[_Motion] = []
    flags: set[str] = set()
    previous: Mapping[str, Any] | None = None
    previous_time: float | None = None
    previous_distance: float | None = None
    for sample in samples:
        if not isinstance(sample, Mapping):
            flags.add("invalid_telemetry")
            continue
        elapsed = _finite(sample.get("elapsed_seconds"))
        if elapsed is None or elapsed < 0:
            flags.add("invalid_telemetry")
            continue
        if previous is None:
            previous = sample
            previous_time = elapsed
            distance_value = _finite(sample.get("distance_m", sample.get("distance")))
            if distance_value is not None:
                previous_distance = distance_value
            continue
        assert previous_time is not None
        dt = elapsed - previous_time
        if dt <= 0:
            flags.add("duplicate_or_reversed_time")
            previous = sample
            previous_time = elapsed
            continue

        distance_value = _finite(sample.get("distance_m", sample.get("distance")))
        distance_delta: float | None = None
        source = "none"
        if distance_value is not None and previous_distance is not None:
            candidate = distance_value - previous_distance
            if candidate >= 0:
                distance_delta = candidate
                source = "cumulative"
            else:
                flags.add("non_monotonic_distance")
        route_delta = _haversine_meters(previous, sample)
        raw_speed = distance_delta / dt if distance_delta is not None else None
        route_speed = route_delta / dt if route_delta is not None else None
        if distance_delta is None and route_delta is not None:
            distance_delta = route_delta
            source = "gps"
        elif raw_speed is not None and raw_speed > gps_spike_speed_mps and route_speed is not None and route_speed <= gps_spike_speed_mps:
            # A cumulative jump is ignored when the coordinate path is sane.
            distance_delta = route_delta or 0.0
            source = "gps_recovered"
            flags.add("gps_distance_recovered")
        elif route_speed is not None and raw_speed is not None and route_speed > gps_spike_speed_mps and raw_speed <= gps_spike_speed_mps:
            flags.add("gps_route_spike_ignored")
        elif route_speed is not None and raw_speed is not None and route_speed < speed_threshold_mps and raw_speed >= speed_threshold_mps:
            # Coordinate drift wins over an implausibly active cumulative
            # counter when the route itself remains below walking speed.
            distance_delta = 0.0
            source = "gps_jitter_filtered"
            flags.add("gps_jitter_smoothed")
        elif route_speed is not None and raw_speed is not None and route_speed < max(1.0, speed_threshold_mps * 2.0) and distance_delta > (route_delta or 0.0) * 1.35 and (route_delta or 0.0) <= 12.0:
            distance_delta = 0.0
            source = "gps_jitter_filtered"
            flags.add("gps_jitter_smoothed")

        distance_delta = max(0.0, distance_delta or 0.0)
        speed = distance_delta / dt
        is_gap = dt > max_gap_seconds or sample.get("unknown_before") is True
        is_spike = speed > gps_spike_speed_mps
        if is_gap:
            flags.add("missing_data_gap")
        if is_spike:
            flags.add("gps_distance_spike_filtered")
            distance_delta = 0.0
            speed = 0.0
        if is_gap or is_spike:
            state = "unknown"
        elif source == "gps_jitter_filtered":
            state = "stopped"
        elif speed >= speed_threshold_mps:
            state = "moving"
        else:
            state = "stopped"
        segments.append(_Motion(previous_time, elapsed, distance_delta, speed, state, source))
        previous = sample
        previous_time = elapsed
        if distance_value is not None and distance_value >= (previous_distance or 0.0):
            previous_distance = distance_value
    return segments, flags


def _metadata_label(metadata: Any, explicit_label: Any) -> tuple[str | None, bool, bool, str | None]:
    label = _normalise_label(explicit_label)
    label_source = "argument" if label else None
    if label is None:
        for key in (
            "confirmed_type",
            "manual_type",
            "training_type",
            "run_type",
            "type",
            "classification",
        ):
            candidate = _normalise_label(_value(metadata, key))
            if candidate:
                label = candidate
                label_source = key
                break
    confirmed = _as_bool(
        _value(
            metadata,
            "confirmed",
            "confirmed_type",
            "is_confirmed",
            "user_confirmed",
            "confirmed_race",
            "is_race_confirmed",
            default=False,
        )
    )
    manual = _as_bool(
        _value(
            metadata,
            "manual",
            "manual_type",
            "manually_labeled",
            "user_labeled",
            "manual_label",
            default=False,
        )
    )
    manual = manual or str(_value(metadata, "run_type_assignment", default="")).strip().lower() == "manual"
    # A confirmed/manual type field is itself evidence of an explicit label,
    # even when the caller did not provide a separate boolean marker.
    if label_source in {"confirmed_type", "manual_type"}:
        confirmed = confirmed or label_source == "confirmed_type"
        manual = manual or label_source == "manual_type"
    return label, confirmed, manual, label_source


def _result(
    label: str,
    *,
    reason: str,
    confidence: float,
    evidence: Mapping[str, Any],
    preserved: bool = False,
) -> dict[str, Any]:
    confidence_value = round(max(0.0, min(1.0, float(confidence))), 3)
    payload = {
        "run_type": label,
        "training_type": label,
        "classification": label,
        "type": label,
        "decision": label,
        "reason": reason,
        "decision_reason": reason,
        "confidence": confidence_value,
        "preserved": preserved,
        "evidence": dict(evidence),
    }
    return payload


def _tempo_windows(motion: Sequence[_Motion]) -> list[tuple[float, float, float, float, float]]:
    """Find qualifying two-kilometre windows inside moving blocks.

    Looking only at a whole moving block would hide a tempo segment inside an
    easy-run warm-up or cool-down.  This helper evaluates each segment boundary
    as a possible start and interpolates the end of a 2 km window through the
    segment that crosses the boundary.  Stopped/unknown segments split the
    blocks, so a traffic stop cannot make an otherwise easy run look like a
    continuous tempo effort.
    """

    windows: list[tuple[float, float, float, float, float]] = []
    index = 0
    while index < len(motion):
        if motion[index].state != "moving":
            index += 1
            continue
        block: list[_Motion] = [motion[index]]
        index += 1
        while index < len(motion) and motion[index].state == "moving" and math.isclose(
            motion[index - 1].end_seconds,
            motion[index].start_seconds,
            abs_tol=1e-6,
        ):
            block.append(motion[index])
            index += 1

        cumulative = [0.0]
        for segment in block:
            cumulative.append(cumulative[-1] + max(0.0, segment.distance_m))
        if cumulative[-1] < MIN_CONTINUOUS_TEMPO_DISTANCE_M - 1e-3:
            continue
        for start_index, segment in enumerate(block):
            start_distance = cumulative[start_index]
            target_distance = start_distance + MIN_CONTINUOUS_TEMPO_DISTANCE_M
            if target_distance > cumulative[-1] + 1e-3:
                break
            boundary_index = bisect_left(cumulative, target_distance, lo=start_index + 1)
            if boundary_index >= len(cumulative):
                continue
            end_segment = block[boundary_index - 1]
            covered_distance = max(0.0, target_distance - start_distance)
            segment_start_distance = cumulative[boundary_index - 1]
            segment_distance = max(0.0, end_segment.distance_m)
            if segment_distance <= 0:
                end_seconds = end_segment.end_seconds
            else:
                fraction = max(0.0, min(1.0, (target_distance - segment_start_distance) / segment_distance))
                end_seconds = end_segment.start_seconds + fraction * end_segment.elapsed_seconds
            moving_seconds = max(0.0, end_seconds - segment.start_seconds)
            pace = moving_seconds / (covered_distance / 1_000.0) if covered_distance > 0 else math.inf
            if pace <= TEMPO_MAX_PACE_SECONDS_PER_KM + 1e-6:
                windows.append((covered_distance, moving_seconds, pace, segment.start_seconds, end_seconds))
    return windows


def _qualifies_fast_bout(bout: _Motion) -> bool:
    """Reject one-point spikes while allowing a genuinely long sparse rep."""

    if bout.elapsed_seconds < MIN_FAST_BOUT_SECONDS or bout.distance_m < MIN_FAST_BOUT_DISTANCE_M:
        return False
    # At normal 1 Hz/5 Hz recording rates a real rep spans multiple samples.
    # A sparse provider may emit one 30-second summary segment, which is still
    # long enough to retain as evidence.  Short single-sample jumps remain
    # ineligible even if their speed happens to be below the GPS spike cap.
    return bout.segment_count >= 2 or bout.elapsed_seconds >= 30.0


def classify_training(
    samples: Sequence[Mapping[str, Any]] | None = None,
    laps: Sequence[Mapping[str, Any]] | None = None,
    analysis: Mapping[str, Any] | None = None,
    *,
    metadata: Any = None,
    existing_label: str | None = None,
    existing_type: str | None = None,
    current_type: str | None = None,
    run_type: str | None = None,
    title: str | None = None,
    confirmed: bool | None = None,
    manual: bool | None = None,
    preserve_existing: bool = False,
    speed_threshold_mps: float = DEFAULT_SPEED_THRESHOLD_MPS,
    max_gap_seconds: float = DEFAULT_MAX_GAP_SECONDS,
    gps_spike_speed_mps: float = DEFAULT_GPS_SPIKE_SPEED_MPS,
) -> dict[str, Any]:
    """Return a conservative training label and its decision evidence.

    ``metadata`` may be a mapping or a run-like object.  The explicit label
    keyword aliases make the function easy to call from import/enrichment code
    while keeping the positional contract ``(samples, laps, analysis)`` small.
    """

    if not isinstance(speed_threshold_mps, (int, float)) or not math.isfinite(float(speed_threshold_mps)) or speed_threshold_mps <= 0:
        raise ValueError("speed_threshold_mps must be finite and greater than zero")
    if not isinstance(max_gap_seconds, (int, float)) or not math.isfinite(float(max_gap_seconds)) or max_gap_seconds <= 0:
        raise ValueError("max_gap_seconds must be finite and greater than zero")
    if not isinstance(gps_spike_speed_mps, (int, float)) or not math.isfinite(float(gps_spike_speed_mps)) or gps_spike_speed_mps <= 0:
        raise ValueError("gps_spike_speed_mps must be finite and greater than zero")

    explicit = next((item for item in (existing_label, existing_type, current_type, run_type) if item is not None), None)
    label, metadata_confirmed, metadata_manual, label_source = _metadata_label(metadata, explicit)
    confirmed_flag = metadata_confirmed if confirmed is None else bool(confirmed)
    manual_flag = metadata_manual if manual is None else bool(manual)
    if label in _KNOWN_LABELS and (
        preserve_existing
        or manual_flag
        or confirmed_flag
        or (label in _PROTECTED_LABELS and label_source is not None)
    ):
        reason = (
            f"Preserved the confirmed {label} label."
            if confirmed_flag or label in _PROTECTED_LABELS
            else f"Preserved the manually assigned {label} label."
        )
        return _result(
            label,
            reason=reason,
            confidence=1.0,
            preserved=True,
            evidence={"label_source": label_source or "caller", "confirmed": confirmed_flag, "manual": manual_flag},
        )

    analysis_value: Mapping[str, Any] = analysis if isinstance(analysis, Mapping) else {}
    segment_flags: set[str] = set()
    motion: list[_Motion] = []
    if isinstance(samples, (list, tuple)) and len(samples) >= 2:
        motion, segment_flags = _motion_segments(
            samples,
            speed_threshold_mps=float(speed_threshold_mps),
            max_gap_seconds=float(max_gap_seconds),
            gps_spike_speed_mps=float(gps_spike_speed_mps),
        )
    elif samples is not None and not isinstance(samples, (list, tuple)):
        segment_flags.add("invalid_telemetry")

    # When callers have not already analyzed a stream, use the shared activity
    # analyzer for quality/provenance metadata.  Classification still works
    # when this optional analysis is unavailable or invalid.
    if not analysis_value and motion and isinstance(samples, (list, tuple)):
        try:
            from .activity_analysis import analyze_activity

            candidate = analyze_activity(list(samples), list(laps) if isinstance(laps, (list, tuple)) else None)
            if isinstance(candidate, Mapping):
                analysis_value = candidate
        except Exception:
            segment_flags.add("analysis_unavailable")

    distance_m = _finite(_value(metadata, "distance_m"))
    if distance_m is None:
        distance_km = _finite(_value(metadata, "distance_km"))
        distance_m = distance_km * 1_000.0 if distance_km is not None and distance_km >= 0 else None
    if distance_m is None and motion:
        distance_m = sum(segment.distance_m for segment in motion if segment.state != "unknown")
    if distance_m is None:
        distance_m = _finite(analysis_value.get("distance_m"))
    distance_m = max(0.0, distance_m or 0.0)

    moving_seconds = _finite(_value(metadata, "moving_seconds", "moving_time_seconds"))
    if moving_seconds is None:
        moving_seconds = _finite(analysis_value.get("moving_seconds"))
    moving_pace = _finite(analysis_value.get("moving_pace_seconds_per_km"))
    if moving_pace is None:
        old_pace = _finite(analysis_value.get("moving_pace_seconds"))
        moving_pace = old_pace * 1_000.0 if old_pace is not None else None
    if moving_pace is None and moving_seconds is not None and distance_m > 0:
        moving_pace = moving_seconds / (distance_m / 1_000.0)

    unknown_seconds = _finite(analysis_value.get("unclassified_seconds")) or 0.0
    elapsed_seconds = _finite(analysis_value.get("elapsed_seconds"))
    if elapsed_seconds is None:
        elapsed_seconds = _finite(_value(metadata, "duration_seconds", "elapsed_seconds"))
    quality_flags = analysis_value.get("quality_flags")
    if isinstance(quality_flags, (list, tuple)):
        segment_flags.update(str(flag) for flag in quality_flags)
    confidence_factor = 1.0
    if "missing_data_gap" in segment_flags or "uncertain_coverage" in segment_flags:
        confidence_factor *= 0.72
    if "gps_distance_spike_filtered" in segment_flags or "gps_jitter_smoothed" in segment_flags:
        confidence_factor *= 0.80
    analysis_confidence = _finite(analysis_value.get("moving_time_confidence"))
    if analysis_confidence is not None:
        confidence_factor *= max(0.25, min(1.0, analysis_confidence))

    fast_bouts: list[_Motion] = []
    current_bout: _Motion | None = None
    for segment in motion:
        # The interval rule is intentionally strict: exactly 5:00/km is
        # ordinary threshold pace, while an interval bout must be faster.
        is_fast = segment.state == "moving" and segment.speed_mps is not None and segment.speed_mps > 1_000.0 / INTERVAL_MAX_PACE_SECONDS_PER_KM
        if is_fast:
            if current_bout is None:
                current_bout = segment
            elif math.isclose(current_bout.end_seconds, segment.start_seconds, abs_tol=1e-6):
                current_bout = _Motion(
                    current_bout.start_seconds,
                    segment.end_seconds,
                    current_bout.distance_m + segment.distance_m,
                    segment.speed_mps,
                    "moving",
                    current_bout.source,
                    current_bout.segment_count + segment.segment_count,
                )
            else:
                if _qualifies_fast_bout(current_bout):
                    fast_bouts.append(current_bout)
                current_bout = segment
        elif current_bout is not None:
            if _qualifies_fast_bout(current_bout):
                fast_bouts.append(current_bout)
            current_bout = None
    if current_bout is not None and _qualifies_fast_bout(current_bout):
        fast_bouts.append(current_bout)

    walking_recoveries = 0
    recovery_seconds = 0.0
    for first, second in zip(fast_bouts, fast_bouts[1:]):
        between = [segment for segment in motion if segment.start_seconds >= first.end_seconds - 1e-6 and segment.end_seconds <= second.start_seconds + 1e-6]
        duration = sum(
            segment.elapsed_seconds
            for segment in between
            if segment.state == "moving" and segment.speed_mps is not None and DEFAULT_SPEED_THRESHOLD_MPS <= segment.speed_mps <= WALKING_MAX_SPEED_MPS
        )
        if duration >= MIN_WALKING_RECOVERY_SECONDS:
            walking_recoveries += 1
            recovery_seconds += duration

    tempo_windows = _tempo_windows(motion)

    evidence = {
        "distance_m": round(distance_m, 3),
        "distance_km": round(distance_m / 1_000.0, 3),
        "moving_seconds": round(moving_seconds, 3) if moving_seconds is not None else None,
        "moving_pace_seconds_per_km": round(moving_pace, 3) if moving_pace is not None else None,
        "tempo_blocks": [
            {
                "distance_m": round(window_distance, 3),
                "moving_seconds": round(window_time, 3),
                "pace_seconds_per_km": round(window_pace, 3),
                "start_seconds": round(window_start, 3),
                "end_seconds": round(window_end, 3),
            }
            for window_distance, window_time, window_pace, window_start, window_end in tempo_windows
        ],
        "fast_bout_count": len(fast_bouts),
        "walking_recovery_count": walking_recoveries,
        "walking_recovery_seconds": round(recovery_seconds, 3),
        "quality_flags": sorted(segment_flags),
        "telemetry_available": bool(motion),
        "elapsed_seconds": round(elapsed_seconds, 3) if elapsed_seconds is not None else None,
    }

    if len(fast_bouts) >= 2 and walking_recoveries >= 1:
        reason = f"Detected {len(fast_bouts)} sustained fast bouts with walking recoveries."
        return _result(
            "interval",
            reason=reason,
            confidence=0.90 * confidence_factor,
            evidence=evidence,
        )

    if tempo_windows:
        longest_distance, longest_time, pace, window_start, window_end = max(tempo_windows, key=lambda item: item[0])
        reason = f"Detected a continuous {longest_distance / 1_000.0:.2f} km block at {pace:.0f} sec/km."
        return _result(
            "tempo",
            reason=reason,
            confidence=0.84 * confidence_factor,
            evidence=evidence,
        )

    if not motion and (distance_m <= 0 or moving_pace is None):
        reason = "Telemetry is unavailable or too sparse to identify tempo or intervals; defaulted to easy."
        confidence = 0.20 * confidence_factor
    else:
        reason = "No sustained tempo block or repeated fast bouts with walking recoveries were found; defaulted to easy."
        confidence = 0.45 * confidence_factor
    return _result("easy", reason=reason, confidence=confidence, evidence=evidence)


def infer_training_type(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Compatibility alias for importers that call the operation inference."""

    return classify_training(*args, **kwargs)


__all__ = [
    "classify_training",
    "infer_training_type",
    "TEMPO_MAX_PACE_SECONDS_PER_KM",
    "INTERVAL_MAX_PACE_SECONDS_PER_KM",
]
