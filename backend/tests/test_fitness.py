from __future__ import annotations

import pytest

from app.fitness import (
    MAX_ASCENT_TIME_PENALTY,
    MAX_HEAT_TIME_PENALTY,
    _raw_vdot,
    _vdot,
    compute_fitness,
    equivalent_time_for_vdot,
)


def _run(**overrides):
    run = {
        "distance_km": 5.0,
        "duration_seconds": 1_200,
        "run_type": "race",
        "title": "5K",
    }
    run.update(overrides)
    return run


def _weather(temperature_c=15.0, humidity_percent=50.0):
    return {
        "status": "available",
        "temperature_c": temperature_c,
        "humidity_percent": humidity_percent,
    }


def test_race_uses_daniels_vdot_and_easy_run_is_conservative_proxy():
    race = compute_fitness(_run(), _weather(), {})
    easy = compute_fitness(
        _run(run_type="easy", title="Easy run", duration_seconds=1_500),
        _weather(),
        {},
    )

    assert 20 <= race["score"] <= 90
    assert race["method"] == "vdot_daniels_gilbert"
    assert race["confidence"] == "high"
    assert race["factors"]["prediction_eligible"] is True
    assert easy["score"] < race["score"]
    assert easy["confidence"] == "low"
    assert easy["factors"]["prediction_eligible"] is False
    assert easy["factors"]["training_run_not_all_out"] is True


def test_cold_weather_and_missing_weather_have_explicit_neutral_fallbacks():
    cold = compute_fitness(_run(), _weather(-5.0, 90.0), {})
    missing = compute_fitness(_run(), None, {})

    assert cold["factors"]["weather_time_penalty"] == 0.0
    assert cold["factors"]["weather_adjustment_method"] == "cold_or_neutral_no_heat_penalty"
    assert missing["factors"]["weather_time_penalty"] == 0.0
    assert "weather_missing" in missing["factors"]["weather_missing_reasons"]
    assert "temperature_missing" in missing["factors"]["weather_missing_reasons"]
    assert "humidity_missing" in missing["factors"]["weather_missing_reasons"]


def test_heat_penalty_is_bounded_and_humidity_is_not_charged_twice():
    neutral = compute_fitness(_run(), _weather(15.0, 50.0), {})
    hot_dry = compute_fitness(_run(), _weather(32.0, 20.0), {})
    hot_humid = compute_fitness(_run(), _weather(32.0, 90.0), {})

    assert hot_dry["factors"]["weather_time_penalty"] <= MAX_HEAT_TIME_PENALTY
    assert hot_humid["factors"]["weather_time_penalty"] <= MAX_HEAT_TIME_PENALTY
    assert hot_humid["factors"]["weather_time_penalty"] > hot_dry["factors"]["weather_time_penalty"]
    assert hot_humid["factors"]["weather_adjustment_method"] == "combined_temperature_humidity_proxy"
    # Humidity changes one combined stress term; it is not an additional full
    # temperature penalty.
    difference = (
        hot_humid["factors"]["weather_time_penalty"]
        - hot_dry["factors"]["weather_time_penalty"]
    )
    assert difference < hot_dry["factors"]["weather_time_penalty"]
    assert hot_humid["score"] > neutral["score"]


def test_ascent_is_conservative_and_altitude_samples_are_counted_once():
    direct = compute_fitness(
        _run(),
        _weather(),
        {"total_ascent_m": 10_000},
    )
    samples = compute_fitness(
        _run(),
        _weather(),
        {"samples": [{"altitude_m": 0}, {"altitude_m": 50}, {"altitude_m": 25}, {"altitude_m": 100}]},
    )

    assert direct["factors"]["elevation_time_penalty"] <= MAX_ASCENT_TIME_PENALTY
    assert direct["factors"]["ascent_capped"] is True
    assert samples["factors"]["ascent_source"] == "samples"
    assert samples["factors"]["total_ascent_m"] == 125.0
    assert samples["factors"]["elevation_time_penalty"] > 0


def test_interval_average_is_reported_without_becoming_prediction_evidence():
    result = compute_fitness(
        _run(run_type="interval", title="6 x 800m", duration_seconds=1_800),
        _weather(),
        {},
    )

    assert result["score"] is not None
    assert "interval" in result["method"]
    assert result["factors"]["interval_average_not_performance"] is True
    assert result["factors"]["score_kind"] == "interval_average_only"
    assert result["factors"]["equivalent_vdot"] is None
    assert result["factors"]["equivalent_race_duration_seconds"] is None
    assert result["factors"]["prediction_eligible"] is False


def test_invalid_input_returns_insufficient_without_fabrication():
    result = compute_fitness(_run(distance_km=0, duration_seconds=0), None, None)

    assert result == {
        "score": None,
        "method": "insufficient_data",
        "confidence": "insufficient",
        "factors": {
            "evidence_class": "race",
            "run_type": "race",
            "distance_km": 0.0,
            "score_kind": "vdot",
            "prediction_eligible": False,
            "used_in_coaching_race_predictions": False,
            "prediction_duration_multiplier": 1.0,
            "environment_correction_is_heuristic": True,
            "reason": "distance_missing_or_too_short",
        },
    }


def test_training_vdot_uses_type_intensity_and_is_length_invariant_for_same_pace():
    easy_5k = compute_fitness(
        _run(distance_km=5.0, duration_seconds=1_500, run_type="easy", title="Easy"),
        _weather(),
        {},
    )
    easy_10k = compute_fitness(
        _run(distance_km=10.0, duration_seconds=3_000, run_type="easy", title="Easy"),
        _weather(),
        {},
    )
    race = compute_fitness(_run(), _weather(), {})

    assert easy_5k["factors"]["training_intensity_assumption"] == "easy"
    assert easy_5k["factors"]["training_intensity_basis"] == "fraction_of_pace_oxygen_cost"
    assert easy_5k["factors"]["training_intensity_fraction_low"] == 0.65
    assert easy_5k["factors"]["training_intensity_fraction_high"] == 0.79
    assert easy_5k["score"] == pytest.approx(easy_10k["score"], abs=0.02)
    # The easy pace is not silently displayed as an all-out race result.
    assert easy_5k["score"] < race["score"]
    assert easy_5k["factors"]["equivalent_race_duration_seconds"] < easy_5k["factors"]["observed_duration_seconds"]
    assert easy_5k["factors"]["equivalent_race_duration_low_seconds"] < easy_5k["factors"]["equivalent_race_duration_high_seconds"]


def test_equivalent_time_is_the_inverse_of_vdot():
    for distance_km, score in ((5.0, 20.0), (5.0, 40.0), (10.0, 45.0), (21.0975, 37.5), (5.0, 90.0)):
        seconds = equivalent_time_for_vdot(score, distance_km)
        assert seconds is not None
        assert _raw_vdot(distance_km, seconds) == pytest.approx(score, abs=1e-8)
        assert _vdot(distance_km, seconds) == pytest.approx(score, abs=1e-8)


def test_interval_vdot_uses_sustained_work_segments_and_excludes_recovery():
    result = compute_fitness(
        _run(run_type="interval", title="4 x 1 km", distance_km=6.0, duration_seconds=2_400),
        _weather(),
        {
            "intervals": [
                {"distance_m": 1_000, "moving_seconds": 270, "label": "moving"},
                {"distance_m": 500, "moving_seconds": 180, "label": "recovery"},
                {"distance_m": 1_000, "moving_seconds": 268, "label": "moving"},
                {"distance_m": 500, "moving_seconds": 180, "label": "walking recovery"},
                {"distance_m": 1_000, "moving_seconds": 272, "label": "moving"},
                {"distance_m": 500, "moving_seconds": 180, "label": "rest"},
                {"distance_m": 1_000, "moving_seconds": 269, "label": "moving"},
            ]
        },
    )

    factors = result["factors"]
    assert result["method"].startswith("vdot_interval_sustained_segments")
    assert factors["interval_work_segments_available"] is True
    assert factors["interval_work_distance_km"] == 4.0
    assert factors["interval_work_duration_seconds"] == 1_079.0
    assert factors["effort_source"] == "intervals"
    assert factors["prediction_duration_seconds"] is not None
    assert factors["prediction_duration_seconds"] < factors["observed_duration_seconds"]


def test_race_keeps_elapsed_time_when_provider_moving_time_is_present():
    result = compute_fitness(
        _run(moving_seconds=1_080, duration_seconds=1_200),
        _weather(),
        {"moving_time_source": "provider_reported", "provider_active_seconds": 1_080},
    )

    assert result["factors"]["duration_source"] == "elapsed"
    assert result["factors"]["observed_duration_seconds"] == 1_200.0
    assert result["factors"]["duration_provenance"]["provider_active_seconds"] == 1_080.0
