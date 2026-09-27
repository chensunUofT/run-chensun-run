"""Bounded, deterministic fitness estimates for individual runs.

The module has two deliberately separate ideas:

* Daniels--Gilbert VDOT is used as a validated race/time-trial conversion
  when the input represents a sustained, hard effort.
* Weather and ascent adjustments are small, transparent heuristics.  They
  make a hot or hilly result less likely to understate the athlete's neutral
  condition, but they are not a validated VDOT extension.

``compute_fitness`` accepts either SQLAlchemy-like objects or dictionaries so
the API layer can choose when to load optional weather and stream analysis.
It never performs I/O and never persists a result.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
import re
from typing import Any


# Daniels' VDOT formula is useful over ordinary distance-running efforts, but
# extrapolating a noisy GPS snippet is not.  These bounds are intentionally
# conservative and are also used by coaching before a run can be an anchor.
MIN_DISTANCE_KM = 1.0
MIN_ANCHOR_DISTANCE_KM = 3.0
MIN_PACE_SECONDS_PER_KM = 120.0
MAX_PACE_SECONDS_PER_KM = 1_200.0
MIN_SCORE = 20.0
MAX_SCORE = 90.0

# Conditions around 10--17.5 C air temperature tend to be favourable in the
# race literature.  We do not claim to compute WBGT because the weather
# endpoint has neither wind nor radiant temperature.  Instead one combined
# heat-stress proxy prevents temperature and humidity from being charged as
# two independent full penalties.
NEUTRAL_TEMPERATURE_C = 15.0
HUMIDITY_REFERENCE_PERCENT = 50.0
HUMIDITY_HEAT_WEIGHT = 0.08
HEAT_TIME_RATE = 0.004
MAX_HEAT_TIME_PENALTY = 0.15

# Total ascent is less informative than a grade profile.  This small rate is
# therefore a bounded proxy, not a grade-adjusted pace model.
ASCENT_PENALTY_PER_M_PER_KM = 0.00025
MAX_ASCENT_PER_KM = 320.0
MAX_ASCENT_TIME_PENALTY = 0.08

# VDOT is a race-performance equivalence, so a training run cannot be fed to
# the race equation unchanged.  These are deliberately broad fractions of
# oxygen cost/VO2 demand, used only as transparent training assumptions.  The
# broad ranges echo Daniels-style zones (easy roughly 65--79%, threshold
# 83--88%, intervals 95--100%) without claiming to measure VO2max from a
# recreational GPS run.  A lower fraction produces the faster edge of the
# equivalent-race range.
_TRAINING_INTENSITY = {
    "easy": {"low": 0.65, "high": 0.79, "central": 0.75, "label": "easy"},
    "long": {"low": 0.65, "high": 0.77, "central": 0.73, "label": "long"},
    "general": {"low": 0.68, "high": 0.80, "central": 0.75, "label": "general"},
    "quality": {"low": 0.82, "high": 0.90, "central": 0.86, "label": "quality"},
    "tempo": {"low": 0.83, "high": 0.88, "central": 0.855, "label": "threshold"},
    "interval": {"low": 0.95, "high": 1.00, "central": 0.975, "label": "interval"},
}

# Retained as a compatibility map for callers that read the old factor name.
# Values now hold the derived equivalent-duration ratio for compatibility;
# training scores are derived from pace and the intensity assumptions above.
_TRAINING_PROJECTION_MULTIPLIER = {
    "race": 1.0,
    "time_trial": 1.0,
    "tempo": _TRAINING_INTENSITY["tempo"]["central"],
    "quality": _TRAINING_INTENSITY["quality"]["central"],
    "interval": None,
    "easy": _TRAINING_INTENSITY["easy"]["central"],
    "long": _TRAINING_INTENSITY["long"]["central"],
    "general": _TRAINING_INTENSITY["general"]["central"],
}

_INTERVAL_WORDS = (
    "interval",
    "repetition",
    "repeats",
    "repeat",
    "track",
    "fartlek",
    "400m",
    "800m",
    "1km rep",
)
_TEMPO_WORDS = ("tempo", "threshold", "steady state", "cruise")
_EASY_WORDS = ("easy", "recovery", "base", "aerobic")
_LONG_WORDS = ("long", "endurance")


def _get(source: Any, *names: str) -> Any:
    """Read the first present field from a mapping or object."""

    if source is None:
        return None
    if isinstance(source, Mapping):
        for name in names:
            if name in source:
                value = source[name]
                if value is not None:
                    return value
        return None
    for name in names:
        try:
            value = getattr(source, name)
        except (AttributeError, TypeError):
            continue
        if value is not None:
            return value
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _bounded(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _round(value: float | None, digits: int = 3) -> float | None:
    return round(float(value), digits) if value is not None else None


def _text(value: Any) -> str:
    return str(value or "").strip().lower()


def _classification_text(run: Any, analysis: Any) -> str:
    values = [
        _text(_get(run, "run_type")),
        _text(_get(run, "title")),
    ]
    classification = _get(analysis, "training_classification", "classification")
    if isinstance(classification, Mapping):
        values.extend(
            _text(classification.get(field))
            for field in ("run_type", "type", "label", "kind", "workout_type")
        )
    elif classification is not None:
        values.append(_text(classification))
    return " ".join(value for value in values if value)


def _has_word(text: str, words: Sequence[str]) -> bool:
    return any(word in text for word in words)


def _evidence_class(run: Any, analysis: Any) -> str:
    """Classify evidence without treating every fast run as a race."""

    run_type = _text(_get(run, "run_type"))
    title = _text(_get(run, "title"))
    text = _classification_text(run, analysis)

    # An explicit race label wins over descriptive title text.  Time trials
    # are equivalent hard efforts when the athlete recorded them as a run.
    normalized_run_type = re.sub(r"[-\s]+", "_", run_type)
    if normalized_run_type in {"race", "competition", "event", "raced"} or re.search(r"\brace\b", run_type):
        return "race"
    if re.search(r"\btime[\s_-]*trial\b|\btt\b", title) or normalized_run_type in {"time_trial", "tt"}:
        return "time_trial"

    # Preserve explicit training labels before inspecting generic title text;
    # an easy run titled "race pace practice" is still easy evidence.
    if normalized_run_type in {"interval", "intervals", "repetition", "repetitions", "track", "fartlek"}:
        return "interval"
    if normalized_run_type in {"tempo", "threshold", "steady", "steady_state"}:
        return "tempo"
    if normalized_run_type in {"easy", "recovery", "base"}:
        return "easy"
    if normalized_run_type in {"long", "long_run", "endurance"}:
        return "long"
    if normalized_run_type == "quality":
        if _has_word(text, _INTERVAL_WORDS):
            return "interval"
        if _has_word(text, _TEMPO_WORDS):
            return "tempo"
        return "quality"

    # Generic provider labels may leave the workout type in the title.  Keep
    # workout descriptors ahead of a generic "race pace" phrase.
    if _has_word(text, _INTERVAL_WORDS):
        return "interval"
    if _has_word(text, _TEMPO_WORDS):
        return "tempo"
    if _has_word(text, _EASY_WORDS):
        return "easy"
    if _has_word(text, _LONG_WORDS):
        return "long"
    if re.search(r"\brace\b", title):
        return "race"
    return "general"


def _weather_inputs(weather: Any) -> tuple[float | None, float | None, str, list[str]]:
    """Normalize weather and preserve explicit missing/invalid reasons."""

    reasons: list[str] = []
    if weather is None:
        return None, None, "missing", ["weather_missing", "temperature_missing", "humidity_missing"]

    status = _text(_get(weather, "status"))
    if status and status not in {"available", "ok"}:
        return None, None, status, [f"weather_{status}"]

    temperature = _number(_get(weather, "temperature_c", "temperature", "temp_c"))
    humidity = _number(_get(weather, "humidity_percent", "relative_humidity_percent", "humidity"))
    if temperature is not None and not -60.0 <= temperature <= 70.0:
        temperature = None
        reasons.append("temperature_invalid")
    if humidity is not None and not 0.0 <= humidity <= 100.0:
        humidity = None
        reasons.append("humidity_invalid")
    if temperature is None:
        reasons.append("temperature_missing")
    if humidity is None:
        reasons.append("humidity_missing")
    return temperature, humidity, status or "available", reasons


def _ascent_inputs(run: Any, analysis: Any, distance_km: float) -> tuple[float | None, str, bool]:
    """Return positive ascent once, preferring an explicit aggregate."""

    direct_names = (
        "total_ascent_m",
        "total_elevation_gain_m",
        "elevation_gain_m",
        "positive_elevation_gain_m",
        "ascent_m",
        "elevation_gain",
        "total_ascent",
    )
    # The analysis value is the canonical location.  A run-level value is
    # accepted for imported/provider records that expose it there.
    for source, source_name in ((analysis, "analysis"), (run, "run")):
        for name in direct_names:
            value = _number(_get(source, name))
            if value is not None:
                bounded = _bounded(max(0.0, value), 0.0, distance_km * MAX_ASCENT_PER_KM)
                return bounded, source_name, bounded != value

    samples = _get(analysis, "samples", "elevation_samples")
    if samples is None:
        samples = _get(run, "samples", "elevation_samples")
    if not isinstance(samples, (list, tuple)):
        return None, "missing", False

    ascent = 0.0
    previous: float | None = None
    saw_altitude = False
    for sample in samples:
        value = _number(_get(sample, "altitude_m", "elevation_m", "altitude", "elevation"))
        if value is None:
            continue
        saw_altitude = True
        if previous is not None and value > previous:
            ascent += value - previous
        previous = value
    if not saw_altitude:
        return None, "missing", False
    bounded = _bounded(ascent, 0.0, distance_km * MAX_ASCENT_PER_KM)
    return bounded, "samples", bounded != ascent


def _duration_inputs(run: Any, analysis: Any, evidence_class: str) -> tuple[float | None, str, list[str]]:
    """Choose a duration without letting poor moving-time metadata win."""

    reasons: list[str] = []
    elapsed = _number(_get(run, "duration_seconds", "elapsed_seconds"))
    moving = _number(_get(run, "moving_seconds"))
    if moving is None:
        moving = _number(_get(analysis, "moving_seconds"))
    if elapsed is None:
        elapsed = _number(_get(analysis, "elapsed_seconds"))

    if evidence_class in {"race", "time_trial"}:
        if elapsed is not None and elapsed > 0:
            return elapsed, "elapsed", reasons
        if moving is not None and moving > 0:
            reasons.append("elapsed_missing_used_moving")
            return moving, "moving_fallback", reasons
        return None, "missing", ["duration_missing"]

    # A race finish includes pauses by definition.  A training run can use a
    # positive moving time, but only when telemetry did not explicitly mark it
    # unavailable and does not carry a low-confidence estimate.
    # A supplied export duration is independent of the quality of a retained
    # Google/GPS movement estimate. Keep that telemetry confidence unchanged.
    if (_get(analysis, "moving_time_source") == "strava_export"
            and moving is not None and moving > 0
            and _number(_get(analysis, "provider_active_seconds")) == moving
            and elapsed is not None and moving <= elapsed):
        return moving, "moving", reasons
    available = _get(analysis, "moving_time_available")
    confidence = _number(_get(analysis, "moving_time_confidence"))
    confidence_label = _text(_get(analysis, "moving_time_confidence_label"))
    confidence_ok = confidence is None or confidence >= 0.45
    if confidence_label in {"low", "unknown", "insufficient"}:
        confidence_ok = False
    if moving is not None and moving > 0 and (available is not False) and confidence_ok:
        if elapsed is None or moving <= elapsed * 1.02:
            return moving, "moving", reasons
        reasons.append("moving_exceeds_elapsed_ignored")
    if elapsed is not None and elapsed > 0:
        return elapsed, "elapsed", reasons
    if moving is not None and moving > 0:
        reasons.append("elapsed_missing_used_moving")
        return moving, "moving_fallback", reasons
    return None, "missing", ["duration_missing"]


def _oxygen_cost(distance_km: float, duration_seconds: float) -> float | None:
    """Return the Daniels running oxygen-cost term for a pace."""

    if distance_km <= 0 or duration_seconds <= 0:
        return None
    pace = duration_seconds / distance_km
    # Allow the tiny floating-point error produced by ``distance * pace``
    # when the inverse uses the exact supported boundary.
    if not MIN_PACE_SECONDS_PER_KM - 1e-9 <= pace <= MAX_PACE_SECONDS_PER_KM + 1e-9:
        return None
    minutes = duration_seconds / 60.0
    velocity_m_per_minute = distance_km * 1_000.0 / minutes
    return -4.60 + 0.182258 * velocity_m_per_minute + 0.000104 * velocity_m_per_minute**2


def _raw_vdot(distance_km: float, duration_seconds: float) -> float | None:
    """Calculate the unclamped Daniels--Gilbert VDOT value."""

    oxygen_cost = _oxygen_cost(distance_km, duration_seconds)
    if oxygen_cost is None:
        return None
    minutes = duration_seconds / 60.0
    fraction = (
        0.8
        + 0.1894393 * math.exp(-0.012778 * minutes)
        + 0.2989558 * math.exp(-0.1932605 * minutes)
    )
    if fraction <= 0 or oxygen_cost <= 0:
        return None
    return oxygen_cost / fraction


def _vdot(distance_km: float, duration_seconds: float) -> float | None:
    """Calculate bounded Daniels--Gilbert VDOT from a hard-effort time."""

    raw = _raw_vdot(distance_km, duration_seconds)
    return _bounded(raw, MIN_SCORE, MAX_SCORE) if raw is not None else None


def equivalent_time_for_vdot(score: float, distance_km: float) -> float | None:
    """Return the Daniels--Gilbert equivalent time for ``score``.

    This is the inverse of :func:`_vdot`, solved over the same supported pace
    range.  Keeping the inverse beside the forward equation prevents coaching
    from mixing a VDOT score with a separate, opaque duration multiplier.
    ``None`` is returned for malformed scores or distances.
    """

    score_value = _number(score)
    distance_value = _number(distance_km)
    if score_value is None or distance_value is None:
        return None
    if not MIN_SCORE <= score_value <= MAX_SCORE or distance_value < MIN_DISTANCE_KM:
        return None

    low = distance_value * MIN_PACE_SECONDS_PER_KM
    high = distance_value * MAX_PACE_SECONDS_PER_KM
    # Use unclamped values here.  Otherwise every score at the safe lower or
    # upper output bound would be mapped to the arbitrary pace boundary rather
    # than to the duration that actually solves the Daniels equation.
    low_score = _raw_vdot(distance_value, low)
    high_score = _raw_vdot(distance_value, high)
    if low_score is None or high_score is None:
        return None
    if score_value >= low_score:
        return low
    if score_value <= high_score:
        return high

    # VDOT decreases monotonically with duration across the supported running
    # range.  The fixed iteration count is deterministic and more than enough
    # for sub-second agreement with the forward equation.
    for _ in range(72):
        midpoint = (low + high) / 2.0
        midpoint_score = _raw_vdot(distance_value, midpoint)
        if midpoint_score is None:
            return None
        if midpoint_score > score_value:
            low = midpoint
        else:
            high = midpoint
    return (low + high) / 2.0


def vdot_for_time(distance_km: float, duration_seconds: float) -> float | None:
    """Public bounded wrapper for the Daniels performance equation."""

    return _vdot(float(distance_km), float(duration_seconds))


def training_pace_for_vdot(score: float, run_type: str) -> float | None:
    """Return a seconds/km training pace for a type-aware VDOT estimate.

    The pace is derived from the same oxygen-cost term used by
    :func:`compute_fitness`; it does not apply a second arbitrary multiplier.
    """

    score_value = _number(score)
    if score_value is None or not MIN_SCORE <= score_value <= MAX_SCORE:
        return None
    intensity = _intensity_assumption(_text(run_type)) or _TRAINING_INTENSITY["general"]
    oxygen_target = score_value * float(intensity["central"])
    coefficient = 0.000104
    linear = 0.182258
    constant = -4.60 - oxygen_target
    discriminant = linear * linear - 4.0 * coefficient * constant
    if discriminant <= 0:
        return None
    velocity = (-linear + math.sqrt(discriminant)) / (2.0 * coefficient)
    if velocity <= 0:
        return None
    pace = 60_000.0 / velocity
    return pace if MIN_PACE_SECONDS_PER_KM <= pace <= MAX_PACE_SECONDS_PER_KM else None


def _intensity_assumption(evidence_class: str) -> dict[str, float | str] | None:
    value = _TRAINING_INTENSITY.get(evidence_class)
    if value is None:
        return None
    return value


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _interval_distance_km(item: Any) -> float | None:
    direct = _number(_get(item, "distance_km"))
    if direct is not None:
        return direct
    metres = _number(_get(item, "distance_m", "distance_metres", "meters", "metres"))
    if metres is not None:
        return metres / 1_000.0
    value = _number(_get(item, "distance"))
    # Provider analysis uses metres for ``distance_m`` and kilometres for a
    # run-level ``distance``.  A bare segment distance is normally metres when
    # it is larger than a plausible one-kilometre split.
    if value is not None:
        return value / 1_000.0 if value > 20.0 else value
    return None


def _interval_duration_seconds(item: Any, distance_km: float | None) -> float | None:
    duration = _number(
        _get(
            item,
            "moving_seconds",
            "active_duration_seconds",
            "duration_seconds",
            "elapsed_seconds",
            "time_seconds",
        )
    )
    if duration is not None and duration > 0:
        return duration
    pace = _number(_get(item, "pace_seconds_per_km", "pace_seconds", "pace"))
    if pace is not None and distance_km is not None and pace > 0:
        # ``activity_analysis`` stores pace_seconds in seconds/metre for the
        # legacy field and exposes the unambiguous per-km field as well.
        if _get(item, "pace_seconds_per_km") is None and pace < 20.0:
            pace *= 1_000.0
        return pace * distance_km
    return None


def _interval_marker(item: Any) -> str:
    value = _get(item, "phase", "interval_type", "workout_type", "kind", "type", "label", "state", "name")
    return _text(value)


def _explicit_work_marker(item: Any) -> bool | None:
    for name in ("is_work", "work", "sustained", "is_sustained", "quality", "is_interval"):
        value = _get(item, name)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return bool(value)
        if isinstance(value, str):
            normalized = _text(value)
            if normalized in {"work", "true", "yes", "quality", "sustained", "interval"}:
                return True
            if normalized in {"recovery", "rest", "false", "no", "easy", "warmup", "cooldown"}:
                return False
    return None


def _sustained_work_metrics(
    analysis: Mapping[str, Any],
    *,
    evidence_class: str,
    full_distance_km: float,
    full_duration_seconds: float,
) -> tuple[float, float, str] | None:
    """Extract a sustained work block without using an interval average.

    Provider streams use several names for the same idea.  Explicit work
    blocks win; inferred activity intervals are accepted only when they carry
    moving work and are never allowed to collapse to the complete run.  A
    plain split list falls back to its faster half so warm-up, recoveries, and
    cool-down do not define an interval score.
    """

    if evidence_class not in {"interval", "tempo", "quality"}:
        return None

    names = (
        "sustained_intervals",
        "work_intervals",
        "quality_intervals",
        "tempo_intervals",
        "threshold_intervals",
        "interval_segments",
        "interval_reps",
        "intervals",
        "laps",
        "splits",
    )
    selected_name = ""
    entries: list[Any] = []
    for name in names:
        candidate = _sequence(_get(analysis, name))
        if candidate:
            selected_name = name
            entries = candidate
            break
    if not entries:
        return None

    candidates: list[tuple[float, float, bool | None, str]] = []
    saw_explicit_nonwork_label = False
    recovery_words = ("recovery", "recover", "rest", "stopped", "stop", "unknown", "warmup", "warm-up", "cooldown", "cool-down", "easy", "walk", "walking", "jog")
    for item in entries:
        if not isinstance(item, Mapping) and not hasattr(item, "__dict__"):
            continue
        distance = _interval_distance_km(item)
        duration = _interval_duration_seconds(item, distance)
        if distance is None or duration is None:
            continue
        if distance <= 0 or duration <= 0:
            continue
        marker = _interval_marker(item)
        explicit_work = _explicit_work_marker(item)
        if explicit_work is False or any(word in marker for word in recovery_words):
            if marker:
                saw_explicit_nonwork_label = True
            continue
        pace = duration / distance
        if not MIN_PACE_SECONDS_PER_KM <= pace <= MAX_PACE_SECONDS_PER_KM:
            continue
        candidates.append((distance, duration, explicit_work, marker))

    if not candidates:
        return None

    # The analyzer's inferred ``intervals`` has moving/stopped states.  It may
    # contain a single moving interval equal to the whole run; that is still a
    # whole-run average and must not be promoted.
    total_distance = sum(item[0] for item in candidates)
    total_duration = sum(item[1] for item in candidates)
    if (
        len(candidates) == 1
        and total_distance >= full_distance_km * 0.92
        and total_duration >= full_duration_seconds * 0.92
        and selected_name in {"intervals", "laps", "splits"}
    ):
        return None

    # Without work/recovery labels, kilometer splits are the least informative
    # shape.  Use the faster half, retaining at least one split, rather than
    # silently using the whole average.  Explicit work labels retain all work
    # entries, including varied repetition paces.
    generic_markers = {"", "moving", "running", "run", "distance", "split", "lap"}
    has_explicit_markers = saw_explicit_nonwork_label or any(item[2] is not None or item[3] not in generic_markers for item in candidates)
    if selected_name in {"splits", "laps", "intervals"} and not has_explicit_markers and len(candidates) > 1:
        candidates.sort(key=lambda item: item[1] / item[0])
        keep = max(1, (len(candidates) + 1) // 2)
        candidates = candidates[:keep]
        total_distance = sum(item[0] for item in candidates)
        total_duration = sum(item[1] for item in candidates)

    if total_distance < MIN_DISTANCE_KM:
        return None
    return total_distance, total_duration, selected_name


def _environment_adjustment(
    temperature: float | None,
    humidity: float | None,
    ascent_m: float | None,
    distance_km: float,
) -> tuple[float, dict[str, Any]]:
    """Return a multiplicative neutral-condition duration adjustment."""

    temp_component = 0.0
    humidity_component = 0.0
    heat_penalty = 0.0
    heat_reason = "temperature_or_humidity_missing"
    if temperature is not None:
        # Cold conditions are retained as neutral rather than rewarded: there
        # is not enough context about clothing, wind, or acclimation for a
        # reliable cold adjustment.
        temp_component = max(0.0, temperature - NEUTRAL_TEMPERATURE_C)
        if temperature <= NEUTRAL_TEMPERATURE_C:
            heat_reason = "cold_or_neutral_no_heat_penalty"
        elif humidity is not None:
            humidity_component = max(0.0, humidity - HUMIDITY_REFERENCE_PERCENT) * HUMIDITY_HEAT_WEIGHT
            heat_reason = "combined_temperature_humidity_proxy"
        else:
            heat_reason = "temperature_only_proxy"
        stress = temp_component + humidity_component
        heat_penalty = _bounded(stress * HEAT_TIME_RATE, 0.0, MAX_HEAT_TIME_PENALTY)
    elif humidity is not None:
        heat_reason = "humidity_without_temperature_not_applied"

    ascent_penalty = 0.0
    ascent_per_km: float | None = None
    if ascent_m is not None and distance_km > 0:
        ascent_per_km = _bounded(ascent_m / distance_km, 0.0, MAX_ASCENT_PER_KM)
        ascent_penalty = _bounded(ascent_per_km * ASCENT_PENALTY_PER_M_PER_KM, 0.0, MAX_ASCENT_TIME_PENALTY)

    # Multiplication represents two independent sources of extra elapsed time
    # while avoiding an unbounded additive correction.
    combined_multiplier = (1.0 + heat_penalty) * (1.0 + ascent_penalty)
    return combined_multiplier, {
        "temperature_c": _round(temperature, 2),
        "humidity_percent": _round(humidity, 1),
        "heat_stress_temperature_component_c": _round(temp_component, 3),
        "heat_stress_humidity_component_c": _round(humidity_component, 3),
        "weather_time_penalty": _round(heat_penalty, 5),
        "weather_time_penalty_percent": _round(heat_penalty * 100.0, 3),
        "weather_adjustment_method": heat_reason,
        "total_ascent_m": _round(ascent_m, 1),
        "ascent_m_per_km": _round(ascent_per_km, 2),
        "elevation_time_penalty": _round(ascent_penalty, 5),
        "elevation_time_penalty_percent": _round(ascent_penalty * 100.0, 3),
        "environment_time_multiplier": _round(combined_multiplier, 5),
    }


def _duration_provenance(run: Any, analysis: Mapping[str, Any]) -> dict[str, Any]:
    """Expose moving/elapsed provenance without changing the chosen duration."""

    values: dict[str, Any] = {}
    for name in (
        "moving_time_source",
        "moving_time_confidence",
        "moving_time_confidence_label",
        "moving_time_available",
        "moving_time_estimated",
        "moving_time_reason",
        "provider_moving_seconds",
        "provider_active_seconds",
    ):
        value = _get(analysis, name)
        if value is None:
            value = _get(run, name)
        if value is not None:
            if name in {"moving_time_confidence", "provider_moving_seconds", "provider_active_seconds"}:
                value = _round(_number(value), 3)
            values[name] = value
    moving = _number(_get(run, "moving_seconds"))
    if moving is None:
        moving = _number(_get(analysis, "moving_seconds"))
    elapsed = _number(_get(run, "duration_seconds", "elapsed_seconds"))
    if elapsed is None:
        elapsed = _number(_get(analysis, "elapsed_seconds"))
    values.update(
        {
            "elapsed_seconds": _round(elapsed, 2),
            "moving_seconds": _round(moving, 2),
            "race_time_basis": "elapsed",
        }
    )
    return values


def compute_fitness(
    run: Any,
    weather: Any | None = None,
    analysis: Any | None = None,
) -> dict[str, Any]:
    """Compute a bounded per-run fitness result.

    ``score`` is VDOT for race/time-trial evidence.  For training runs it is a
    type-aware equivalent VDOT inferred from the observed pace and a broad
    training-intensity range.  The estimate remains lower confidence and is
    never promoted to race evidence merely because the pace was fast.  The
    method is pure and safe for missing or malformed optional inputs.
    """

    analysis = analysis if isinstance(analysis, Mapping) else (analysis or {})
    evidence_class = _evidence_class(run, analysis)
    distance = _number(_get(run, "distance_km", "distance"))
    official_distance = _number(_get(analysis, "official_race_distance_km"))
    if evidence_class in {"race", "time_trial"} and official_distance is not None and distance is not None and 0.9 * distance <= official_distance <= 1.1 * distance:
        distance = official_distance
    if distance is None:
        distance = _number(_get(analysis, "distance_km"))

    factors: dict[str, Any] = {
        "evidence_class": evidence_class,
        "run_type": _text(_get(run, "run_type")) or "run",
        "distance_km": _round(distance, 3),
        "score_kind": "vdot" if evidence_class in {"race", "time_trial"} else "training_vdot_estimate",
        "prediction_eligible": False,
        "used_in_coaching_race_predictions": False,
        "prediction_duration_multiplier": _TRAINING_PROJECTION_MULTIPLIER[evidence_class],
        "environment_correction_is_heuristic": True,
    }

    if distance is None or distance < MIN_DISTANCE_KM:
        factors["reason"] = "distance_missing_or_too_short"
        return {
            "score": None,
            "method": "insufficient_data",
            "confidence": "insufficient",
            "factors": factors,
        }

    duration, duration_source, duration_reasons = _duration_inputs(run, analysis, evidence_class)
    factors["observed_duration_seconds"] = _round(duration, 2)
    factors["duration_source"] = duration_source
    factors["data_quality_reasons"] = duration_reasons
    if duration is None or duration <= 0:
        factors["reason"] = "duration_missing_or_invalid"
        return {
            "score": None,
            "method": "insufficient_data",
            "confidence": "insufficient",
            "factors": factors,
        }

    raw_pace = duration / distance
    factors["observed_pace_seconds_per_km"] = _round(raw_pace, 2)
    if not MIN_PACE_SECONDS_PER_KM <= raw_pace <= MAX_PACE_SECONDS_PER_KM:
        factors["reason"] = "pace_outside_supported_bounds"
        return {
            "score": None,
            "method": "insufficient_data",
            "confidence": "insufficient",
            "factors": factors,
        }

    temperature, humidity, weather_status, weather_reasons = _weather_inputs(weather)
    ascent_m, ascent_source, ascent_capped = _ascent_inputs(run, analysis, distance)
    environment_multiplier, environment_factors = _environment_adjustment(
        temperature,
        humidity,
        ascent_m,
        distance,
    )
    neutral_duration = duration / environment_multiplier
    raw_vdot = _vdot(distance, duration)
    adjusted_vdot = _vdot(distance, neutral_duration)
    if adjusted_vdot is None or raw_vdot is None:
        factors["reason"] = "vdot_outside_supported_bounds"
        return {
            "score": None,
            "method": "insufficient_data",
            "confidence": "insufficient",
            "factors": factors,
        }

    # Races use the complete elapsed result.  A training score uses a
    # sustained block when one is available, then maps the observed pace into
    # a broad type-specific equivalent race effort.  In particular, the whole
    # interval average is never passed through the interval equation.
    effort_distance = distance
    effort_duration = neutral_duration
    effort_source = "whole_run"
    interval_metrics = None
    if evidence_class in {"interval", "tempo", "quality"}:
        interval_metrics = _sustained_work_metrics(
            analysis,
            evidence_class=evidence_class,
            full_distance_km=distance,
            full_duration_seconds=duration,
        )
        if interval_metrics is not None:
            effort_distance, observed_effort_duration, effort_source = interval_metrics
            effort_duration = observed_effort_duration / environment_multiplier

    intensity = None if evidence_class in {"race", "time_trial"} else _intensity_assumption(evidence_class)
    equivalent_vdot = adjusted_vdot
    equivalent_duration = neutral_duration
    equivalent_low_duration = neutral_duration
    equivalent_high_duration = neutral_duration
    equivalent_low_vdot = adjusted_vdot
    equivalent_high_vdot = adjusted_vdot
    effort_oxygen_cost = _oxygen_cost(effort_distance, effort_duration)
    if effort_oxygen_cost is None:
        factors["reason"] = "effort_pace_outside_supported_bounds"
        return {
            "score": None,
            "method": "insufficient_data",
            "confidence": "insufficient",
            "factors": factors,
        }
    if intensity is not None and not (evidence_class == "interval" and interval_metrics is None):
        low_fraction = float(intensity["low"])
        high_fraction = float(intensity["high"])
        central_fraction = float(intensity["central"])
        # Estimate the athlete's neutral VDOT from pace oxygen cost and the
        # assumed training intensity.  This is pace/type-aware and does not
        # vary simply because the same pace was held for a different distance.
        equivalent_vdot = _bounded(effort_oxygen_cost / central_fraction, MIN_SCORE, MAX_SCORE)
        # Low VDOT is the slower edge of the time range; high VDOT is the
        # faster edge.  The corresponding durations therefore have the
        # opposite ordering.
        equivalent_low_vdot = _bounded(effort_oxygen_cost / high_fraction, MIN_SCORE, MAX_SCORE)
        equivalent_high_vdot = _bounded(effort_oxygen_cost / low_fraction, MIN_SCORE, MAX_SCORE)
        equivalent_duration = equivalent_time_for_vdot(equivalent_vdot, effort_distance)
        equivalent_low_duration = equivalent_time_for_vdot(equivalent_high_vdot, effort_distance)
        equivalent_high_duration = equivalent_time_for_vdot(equivalent_low_vdot, effort_distance)
    if evidence_class == "interval" and interval_metrics is None:
        # A whole-run interval average is useful as a low-confidence display
        # value only.  Do not expose it as an equivalent race time or let
        # coaching accidentally consume it.
        equivalent_duration = None
        equivalent_low_duration = None
        equivalent_high_duration = None
    display_score = equivalent_vdot
    if evidence_class == "interval" and interval_metrics is None:
        # Preserve the bounded whole-run diagnostic for the existing run
        # detail contract, but do not call it an equivalent VDOT.  Coaching
        # and the recent-evidence table consume the explicit null below.
        display_score = adjusted_vdot
        equivalent_vdot = None
        equivalent_low_vdot = None
        equivalent_high_vdot = None
    if display_score is None:
        factors["reason"] = "equivalent_vdot_outside_supported_bounds"
        return {
            "score": None,
            "method": "insufficient_data",
            "confidence": "insufficient",
            "factors": factors,
        }

    score = _bounded(display_score, MIN_SCORE, MAX_SCORE)

    factors.update(environment_factors)
    factors.update(
        {
            "weather_status": weather_status,
            "weather_missing_reasons": weather_reasons,
            "weather_available": bool(temperature is not None or humidity is not None),
            "ascent_source": ascent_source,
            "ascent_capped": ascent_capped,
            "neutral_duration_seconds": _round(neutral_duration, 2),
            "neutral_pace_seconds_per_km": _round(neutral_duration / distance, 2),
            "raw_vdot": _round(raw_vdot, 2),
            "environment_adjusted_vdot": _round(adjusted_vdot, 2),
            "equivalent_vdot": _round(equivalent_vdot, 2),
            "equivalent_vdot_low": _round(equivalent_low_vdot, 2),
            "equivalent_vdot_high": _round(equivalent_high_vdot, 2),
            "equivalent_race_duration_seconds": _round(equivalent_duration, 2),
            "equivalent_race_duration_low_seconds": _round(equivalent_low_duration, 2),
            "equivalent_race_duration_high_seconds": _round(equivalent_high_duration, 2),
            "equivalent_race_pace_seconds_per_km": _round(
                equivalent_duration / effort_distance
                if equivalent_duration is not None and effort_distance > 0
                else None,
                2,
            ),
            "effort_distance_km": _round(effort_distance, 3),
            "effort_duration_seconds": _round(effort_duration, 2),
            "effort_pace_seconds_per_km": _round(effort_duration / effort_distance, 2) if effort_distance > 0 else None,
            "effort_source": effort_source,
            "run_type_score_adjustment": _round(equivalent_vdot - adjusted_vdot, 2) if equivalent_vdot is not None else None,
            # Keep the old field as an alias for clients that used it to
            # explain why a training score was not a race result.
            "run_type_score_deduction": _round(equivalent_vdot - adjusted_vdot, 2) if equivalent_vdot is not None else None,
            "training_intensity_assumption": (
                str(intensity["label"]) if intensity is not None else None
            ),
            "training_intensity_basis": (
                "fraction_of_pace_oxygen_cost" if intensity is not None else None
            ),
            "training_intensity_fraction": (
                _round(float(intensity["central"]), 4) if intensity is not None else None
            ),
            "training_intensity_fraction_low": (
                _round(float(intensity["low"]), 4) if intensity is not None else None
            ),
            "training_intensity_fraction_high": (
                _round(float(intensity["high"]), 4) if intensity is not None else None
            ),
            # Compatibility aliases for early clients that called these
            # factors speed fractions.  The values are oxygen-cost fractions,
            # and the explicit basis above prevents that name being opaque.
            "training_speed_fraction": (
                _round(float(intensity["central"]), 4) if intensity is not None else None
            ),
            "training_speed_fraction_low": (
                _round(float(intensity["low"]), 4) if intensity is not None else None
            ),
            "training_speed_fraction_high": (
                _round(float(intensity["high"]), 4) if intensity is not None else None
            ),
            "interval_average_not_performance": evidence_class == "interval",
            "interval_work_segments_available": bool(interval_metrics is not None) if evidence_class == "interval" else None,
            "interval_work_distance_km": _round(interval_metrics[0], 3) if interval_metrics is not None and evidence_class == "interval" else None,
            "interval_work_duration_seconds": _round(interval_metrics[1], 2) if interval_metrics is not None and evidence_class == "interval" else None,
            "training_run_not_all_out": evidence_class not in {"race", "time_trial"},
            # Coaching receives the same type-aware equivalent duration used
            # to derive the score.  Interval rows without sustained segments
            # deliberately have no projection duration.
            "prediction_duration_seconds": _round(
                equivalent_duration if not (evidence_class == "interval" and interval_metrics is None) else None,
                2,
            ),
            "duration_provenance": _duration_provenance(run, analysis),
        }
    )
    factors["prediction_duration_multiplier"] = _round(
        equivalent_duration / neutral_duration
        if equivalent_duration is not None and neutral_duration > 0
        else None,
        4,
    )
    if evidence_class == "interval" and interval_metrics is None:
        factors["score_kind"] = "interval_average_only"

    if evidence_class in {"race", "time_trial"}:
        confidence = "high" if distance >= MIN_ANCHOR_DISTANCE_KM else "medium"
        factors["prediction_eligible"] = bool(distance >= MIN_ANCHOR_DISTANCE_KM)
        factors["used_in_coaching_race_predictions"] = factors["prediction_eligible"]
        method = "vdot_daniels_gilbert"
    elif evidence_class == "tempo":
        confidence = "medium" if distance >= MIN_ANCHOR_DISTANCE_KM else "low"
        factors["prediction_eligible"] = bool(distance >= MIN_ANCHOR_DISTANCE_KM)
        factors["used_in_coaching_race_predictions"] = factors["prediction_eligible"]
        method = "vdot_tempo_threshold_proxy"
    elif evidence_class == "interval":
        confidence = "low"
        method = (
            "vdot_interval_sustained_segments_proxy"
            if interval_metrics is not None
            else "interval_training_proxy_no_sustained_segments"
        )
    else:
        confidence = "low"
        method = f"vdot_{evidence_class}_pace_proxy"
    if environment_multiplier > 1.000001:
        method += "_with_heuristic_environment_adjustment"

    return {
        "score": _round(score, 2),
        "method": method,
        "confidence": confidence,
        "factors": factors,
    }


def computefitness(run: Any, weather: Any | None = None, analysis: Any | None = None) -> dict[str, Any]:
    """Compatibility alias for callers using the compact contract spelling."""

    return compute_fitness(run, weather, analysis)


__all__ = [
    "MAX_ASCENT_TIME_PENALTY",
    "MAX_HEAT_TIME_PENALTY",
    "compute_fitness",
    "computefitness",
    "equivalent_time_for_vdot",
    "training_pace_for_vdot",
    "vdot_for_time",
]
