from __future__ import annotations

import pytest

from app.training_classification import classify_training


def _stream(speeds: list[tuple[float, int]]) -> list[dict[str, float]]:
    """Build ten-second cumulative samples from metres-per-second blocks."""

    elapsed = 0.0
    distance = 0.0
    samples = [{"elapsed_seconds": elapsed, "distance_m": distance}]
    for speed, seconds in speeds:
        for _ in range(seconds // 10):
            elapsed += 10
            distance += speed * 10
            samples.append({"elapsed_seconds": elapsed, "distance_m": distance})
    return samples


def test_tempo_requires_a_continuous_two_kilometre_block_at_or_below_six_minutes_per_km() -> None:
    result = classify_training(_stream([(1_000 / 360, 720)]))

    assert result["run_type"] == "tempo"
    assert result["evidence"]["tempo_blocks"][0]["distance_m"] == pytest.approx(2_000)
    assert result["evidence"]["tempo_blocks"][0]["pace_seconds_per_km"] == pytest.approx(360)

    slower = classify_training(_stream([(1_000 / 361, 720)]))
    assert slower["run_type"] == "easy"


def test_tempo_window_can_sit_inside_easy_warmup_and_cooldown() -> None:
    result = classify_training(
        _stream(
            [
                (1_000 / 480, 600),
                (1_000 / 340, 680),
                (1_000 / 480, 600),
            ]
        )
    )

    assert result["run_type"] == "tempo"
    assert any(item["pace_seconds_per_km"] == pytest.approx(340) for item in result["evidence"]["tempo_blocks"])


def test_intervals_require_repeated_fast_bouts_and_walking_recoveries() -> None:
    result = classify_training(
        _stream(
            [
                (4.0, 30),
                (1.0, 20),
                (4.0, 30),
                (1.0, 20),
                (4.0, 30),
            ]
        )
    )

    assert result["run_type"] == "interval"
    assert result["evidence"]["fast_bout_count"] == 3
    assert result["evidence"]["walking_recovery_count"] == 2


def test_an_isolated_fast_gps_spike_is_not_an_interval() -> None:
    result = classify_training(
        _stream(
            [
                (2.0, 40),
                (20.0, 10),
                (2.0, 40),
            ]
        )
    )

    assert result["run_type"] == "easy"
    assert result["evidence"]["fast_bout_count"] == 0


def test_confirmed_race_long_and_manual_labels_are_preserved() -> None:
    assert classify_training([], metadata={"run_type": "race"})["run_type"] == "race"
    assert classify_training([], metadata={"run_type": "long"})["run_type"] == "long"
    manual = classify_training([], metadata={"run_type": "tempo", "manual": True})
    assert manual["run_type"] == "tempo"
    assert manual["preserved"] is True


def test_missing_telemetry_falls_back_without_claiming_a_workout_signature() -> None:
    result = classify_training(
        [],
        analysis={
            "distance_m": 5_000,
            "moving_seconds": 1_800,
            "moving_pace_seconds_per_km": 360,
            "moving_time_confidence": 0,
        },
    )

    assert result["run_type"] == "easy"
    assert result["confidence"] < 0.5
    assert result["evidence"]["telemetry_available"] is False
