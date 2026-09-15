from __future__ import annotations

import math

import pytest

from app.activity_analysis import analyze_activity


def _samples(times: list[float], distances: list[float]) -> list[dict[str, float]]:
    return [
        {"elapsed_seconds": time, "distance_m": distance}
        for time, distance in zip(times, distances)
    ]


def test_traffic_stop_is_reported_without_losing_moving_time() -> None:
    result = analyze_activity(
        _samples([0, 10, 20, 30, 40, 50], [0, 50, 100, 100, 150, 200])
    )

    assert result["elapsed_seconds"] == 50
    assert result["moving_seconds"] == 40
    assert result["stopped_seconds"] == 10
    assert result["distance_m"] == 200
    assert result["moving_pace_seconds"] == pytest.approx(0.2)
    assert [interval["label"] for interval in result["intervals"]] == [
        "moving",
        "stopped",
        "moving",
    ]


def test_continuous_run_has_one_inferred_moving_interval() -> None:
    result = analyze_activity(
        _samples([0, 10, 20, 30, 40], [0, 50, 100, 150, 200])
    )

    assert result["moving_seconds"] == 40
    assert result["stopped_seconds"] == 0
    assert result["distance_m"] == 200
    assert result["intervals"][0]["label"] == "moving"
    assert result["intervals"][0]["inferred"] is True


def test_short_stop_is_smoothed_by_configurable_hysteresis() -> None:
    result = analyze_activity(
        _samples([0, 10, 12, 20], [0, 50, 50, 100]),
        min_stop_seconds=5,
    )

    assert result["moving_seconds"] == 20
    assert result["stopped_seconds"] == 0
    assert "short_stop_smoothed" in result["quality_flags"]


def test_gap_is_excluded_from_confident_inference_and_flagged() -> None:
    result = analyze_activity(
        _samples([0, 20, 80, 90], [0, 100, 300, 350])
    )

    assert result["elapsed_seconds"] == 90
    assert result["distance_m"] == 350
    assert result["moving_seconds"] == 30
    assert result["stopped_seconds"] == 0
    assert result["unclassified_seconds"] == 60
    assert "missing_data_gap" in result["quality_flags"]
    assert "uncertain_coverage" in result["quality_flags"]
    assert result["intervals"][1]["label"] == "unknown"


def test_implausible_gps_distance_is_filtered_and_retained_as_uncertain() -> None:
    result = analyze_activity(
        _samples([0, 10, 20], [0, 10_000, 10_100]),
        gps_spike_speed_mps=12,
    )

    assert result["raw_distance_m"] == 10_100
    assert result["distance_m"] == 100
    assert result["moving_seconds"] == 10
    assert "gps_distance_spike_filtered" in result["quality_flags"]
    assert "distance_filtered" in result["quality_flags"]


def test_one_kilometer_splits_interpolate_boundaries_and_keep_final_partial() -> None:
    # 100 m every 15 seconds means the 1 km boundary falls between samples.
    result = analyze_activity(
        _samples([0, 15, 30, 45, 60, 75, 90, 105, 120, 135, 150, 165, 180], [
            0,
            100,
            200,
            300,
            400,
            500,
            600,
            700,
            800,
            900,
            1000,
            1100,
            1200,
        ])
    )

    assert len(result["splits"]) == 2
    assert result["splits"][0]["distance_m"] == 1000
    assert result["splits"][0]["start_seconds"] == pytest.approx(0)
    assert result["splits"][0]["end_seconds"] == pytest.approx(150)
    assert result["splits"][1]["distance_m"] == 200
    assert result["splits"][1]["elapsed_seconds"] == pytest.approx(30)


def test_explicit_laps_are_preserved_and_validated() -> None:
    laps = [
        {
            "start_seconds": 0,
            "end_seconds": 20,
            "distance_m": 100,
            "elapsed_seconds": 20,
            "moving_seconds": 18,
            "pace_seconds": 0.18,
            "label": "provided lap",
        }
    ]
    result = analyze_activity(_samples([0, 10, 20], [0, 50, 100]), laps=laps)

    assert result["splits"] == [{**laps[0], "explicit": True}]
    assert "explicit_laps_used" in result["quality_flags"]
    with pytest.raises(ValueError, match="non-overlapping"):
        analyze_activity(
            _samples([0, 10, 20], [0, 50, 100]),
            laps=[
                {"start_seconds": 0, "end_seconds": 15, "distance_m": 75},
                {"start_seconds": 10, "end_seconds": 20, "distance_m": 50},
            ],
        )


@pytest.mark.parametrize(
    "samples, message",
    [
        ([{"elapsed_seconds": 0, "distance_m": 0}, {"elapsed_seconds": -1, "distance_m": 2}], "elapsed_seconds"),
        ([{"elapsed_seconds": 2, "distance_m": 0}, {"elapsed_seconds": 1, "distance_m": 2}], "sorted"),
        ([{"elapsed_seconds": 0, "distance_m": 5}, {"elapsed_seconds": 1, "distance_m": 4}], "cumulative"),
        ([{"elapsed_seconds": 0, "distance_m": math.nan}], "finite"),
        ([{"elapsed_seconds": 0}], "missing cumulative distance_m"),
    ],
)
def test_invalid_samples_are_rejected(samples: list[dict[str, float]], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        analyze_activity(samples)


def test_lap_nanosecond_duration_rounding():
    samples = _samples([0, 446.642682], [0, 1000])
    lap = {'start_seconds': 0, 'end_seconds': 446.642682, 'elapsed_seconds': 446.642682, 'distance_m': 1000, 'moving_seconds': 446.642682432}
    result = analyze_activity(samples, laps=[lap])
    assert result['splits'][0]['moving_seconds'] == pytest.approx(446.642682)
    with pytest.raises(ValueError, match='cannot exceed'):
        analyze_activity(samples, laps=[{**lap, 'moving_seconds': 447}])


def test_walking_speed_counts_as_moving() -> None:
    # 1.0 m/s is a brisk walk, but it is still movement for net moving time.
    result = analyze_activity(
        [
            {"elapsed_seconds": 0, "distance_m": 0},
            {"elapsed_seconds": 20, "distance_m": 20},
            {"elapsed_seconds": 40, "distance_m": 40},
        ]
    )

    assert result["moving_seconds"] == 40
    assert result["stopped_seconds"] == 0
    assert result["moving_pace_seconds_per_km"] == pytest.approx(1_000)


def test_coordinate_jitter_does_not_create_distance_or_moving_time() -> None:
    result = analyze_activity(
        [
            {"elapsed_seconds": 0, "distance_m": 0, "latitude": 40.0, "longitude": -74.0},
            {"elapsed_seconds": 5, "distance_m": 5, "latitude": 40.00002, "longitude": -74.00001},
            {"elapsed_seconds": 10, "distance_m": 10, "latitude": 39.99999, "longitude": -74.00002},
            {"elapsed_seconds": 15, "distance_m": 15, "latitude": 40.00001, "longitude": -74.00001},
            {"elapsed_seconds": 20, "distance_m": 20, "latitude": 40.0, "longitude": -74.0},
        ]
    )

    assert result["moving_seconds"] == 0
    assert result["distance_m"] == 0
    assert "gps_jitter_smoothed" in result["quality_flags"]
    assert result["moving_time_confidence_label"] == "unknown"


def test_gps_distance_delta_is_used_when_cumulative_distance_is_missing() -> None:
    result = analyze_activity(
        [
            {"elapsed_seconds": 0, "latitude": 40.0, "longitude": -74.0},
            {"elapsed_seconds": 10, "latitude": 40.0001, "longitude": -74.0},
            {"elapsed_seconds": 20, "latitude": 40.0002, "longitude": -74.0},
        ]
    )

    assert result["distance_source"] == "gps"
    assert result["distance_m"] == pytest.approx(22.24, rel=0.01)
    assert result["moving_seconds"] == 20


def test_reported_active_duration_is_retained_without_overriding_net_time() -> None:
    result = analyze_activity(
        _samples([0, 10, 20], [0, 100, 200]),
        reported_moving_seconds=12,
    )

    assert result["moving_seconds"] == 20
    assert result["stopped_seconds"] == 0
    assert result["provider_moving_seconds"] == 12
    assert result["moving_time_source"] == "distance_speed_threshold"
    assert result["moving_time_estimated"] is True
    assert "provider_moving_time_available" in result["quality_flags"]
