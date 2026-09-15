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

# A low-confidence training projection, when there is no harder evidence, is
# explicitly slower than a literal all-out interpretation of the run.
_TRAINING_PROJECTION_MULTIPLIER = {
    "race": 1.0,
    "time_trial": 1.0,
    "tempo": 1.03,
    "quality": 1.08,
    "interval": None,
    "easy": 1.15,
    "long": 1.12,
    "general": 1.10,
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


def _vdot(distance_km: float, duration_seconds: float) -> float | None:
    """Calculate Daniels--Gilbert VDOT from distance and a hard-effort time."""

    if distance_km <= 0 or duration_seconds <= 0:
        return None
    pace = duration_seconds / distance_km
    if not MIN_PACE_SECONDS_PER_KM <= pace <= MAX_PACE_SECONDS_PER_KM:
        return None
    minutes = duration_seconds / 60.0
    velocity_m_per_minute = distance_km * 1_000.0 / minutes
    oxygen_cost = -4.60 + 0.182258 * velocity_m_per_minute + 0.000104 * velocity_m_per_minute**2
    fraction = (
        0.8
        + 0.1894393 * math.exp(-0.012778 * minutes)
        + 0.2989558 * math.exp(-0.1932605 * minutes)
    )
    if fraction <= 0 or oxygen_cost <= 0:
        return None
    return _bounded(oxygen_cost / fraction, MIN_SCORE, MAX_SCORE)


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


def compute_fitness(
    run: Any,
    weather: Any | None = None,
    analysis: Any | None = None,
) -> dict[str, Any]:
    """Compute a bounded per-run fitness result.

    ``score`` is VDOT for race/time-trial evidence.  For training runs it is a
    lower-confidence pace-derived lower-bound effort index; the factors
    explain that distinction.  The method is pure and safe for missing or
    malformed optional inputs.
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
        "score_kind": "vdot" if evidence_class in {"race", "time_trial"} else "lower_bound_effort_index",
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

    # A pace-derived value from a training run is only a lower-bound effort
    # index.  It is deliberately not reduced by an invented run-type constant:
    # the evidence class, confidence, and prediction eligibility carry the
    # uncertainty without double-penalising an already non-maximal effort.
    score = _bounded(adjusted_vdot, MIN_SCORE, MAX_SCORE)

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
            "run_type_score_deduction": 0.0,
            "interval_average_not_performance": evidence_class == "interval",
            "training_run_not_all_out": evidence_class not in {"race", "time_trial"},
            # Coaching may use this only as a deliberately conservative final
            # fallback.  Intervals have no projection duration at all.
            "prediction_duration_seconds": _round(
                neutral_duration * _TRAINING_PROJECTION_MULTIPLIER[evidence_class]
                if _TRAINING_PROJECTION_MULTIPLIER[evidence_class] is not None
                else None,
                2,
            ),
        }
    )

    if evidence_class in {"race", "time_trial"}:
        confidence = "high" if distance >= MIN_ANCHOR_DISTANCE_KM else "medium"
        factors["prediction_eligible"] = bool(distance >= MIN_ANCHOR_DISTANCE_KM)
        factors["used_in_coaching_race_predictions"] = factors["prediction_eligible"]
        method = "vdot_daniels_gilbert"
    elif evidence_class == "tempo":
        confidence = "medium" if distance >= MIN_ANCHOR_DISTANCE_KM else "low"
        factors["prediction_eligible"] = bool(distance >= MIN_ANCHOR_DISTANCE_KM)
        factors["used_in_coaching_race_predictions"] = factors["prediction_eligible"]
        method = "tempo_training_proxy"
    elif evidence_class == "interval":
        confidence = "low"
        method = "interval_training_proxy_whole_run_not_performance"
    else:
        confidence = "low"
        method = f"{evidence_class}_training_proxy"
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
]
