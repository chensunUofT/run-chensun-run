from __future__ import annotations

from app.google_telemetry import build_telemetry_route


def _source(platform: str = "FITBIT", *, display_name: str = "Pixel Watch 3", package: str | None = None) -> dict:
    value = {"platform": platform, "device": {"displayName": display_name}}
    if package is not None:
        value["application"] = {"packageName": package}
    return value


def _session(
    *,
    source: dict | None = None,
    start: str = "2025-09-02T23:17:10Z",
    end: str = "2025-09-02T23:20:10Z",
) -> dict:
    source = source or _source()
    return {
        "provider_id": "run-123",
        "started_at": start,
        "ended_at": end,
        "data_source": source,
        "raw": {"dataSource": source},
    }


def _distance(start: str, end: str, value: float, source: dict | None = None) -> dict:
    source = source or _source()
    return {
        "start_time": start,
        "end_time": end,
        "distance_m": value,
        "raw": {"dataSource": source},
    }


def _heart_rate(timestamp: str, bpm: int, source: dict | None = None) -> dict:
    source = source or _source()
    return {"timestamp": timestamp, "heart_rate_bpm": bpm, "raw": {"dataSource": source}}


def test_selects_one_matching_source_and_never_sums_overlapping_providers() -> None:
    session_source = _source()
    other_source = _source("HEALTH_CONNECT", display_name="Fitness Band", package="com.google.android.apps.fitness")
    result = build_telemetry_route(
        _session(source=session_source),
        {
            "distance": [
                _distance("2025-09-02T23:17:10Z", "2025-09-02T23:18:10Z", 100, other_source),
                _distance("2025-09-02T23:18:10Z", "2025-09-02T23:19:10Z", 100, other_source),
                _distance("2025-09-02T23:17:10Z", "2025-09-02T23:18:10Z", 60, session_source),
                _distance("2025-09-02T23:18:10Z", "2025-09-02T23:19:10Z", 70, session_source),
            ]
        },
    )

    assert result is not None
    assert [point["distance_m"] for point in result["points"]] == [0, 60, 130]
    assert result["points"][0]["interval_boundary"] == "start"
    assert result["points"][0]["unknown_before"] is False
    assert result["distance_source"] == "google_health_distance_intervals"
    assert result["provenance"]["source_label"].startswith("FITBIT:Pixel Watch 3")


def test_deduplicates_identical_intervals_and_rejects_ambiguous_overlap() -> None:
    source = _source()
    result = build_telemetry_route(
        _session(),
        {
            "distance": [
                _distance("2025-09-02T23:17:10Z", "2025-09-02T23:18:10Z", 60, source),
                _distance("2025-09-02T23:17:10Z", "2025-09-02T23:18:10Z", 60, source),
                # Both records in this overlap cluster are discarded.  The
                # normalizer leaves the time unknown rather than choosing a
                # value and counting it twice.
                _distance("2025-09-02T23:18:00Z", "2025-09-02T23:19:00Z", 90, source),
                _distance("2025-09-02T23:19:00Z", "2025-09-02T23:20:00Z", 80, source),
            ]
        },
    )

    assert result is not None
    assert [point["distance_m"] for point in result["points"]] == [0, 80]
    assert result["quality"]["deduplicated_intervals"] == 1
    assert result["quality"]["rejected_overlapping_intervals"] == 2


def test_clips_distance_proportionally_and_marks_coarse() -> None:
    source = _source()
    result = build_telemetry_route(
        _session(start="2025-09-02T23:17:30Z", end="2025-09-02T23:19:30Z"),
        {
            "distance": [
                _distance("2025-09-02T23:17:00Z", "2025-09-02T23:18:00Z", 120, source),
                _distance("2025-09-02T23:18:00Z", "2025-09-02T23:19:00Z", 60, source),
                _distance("2025-09-02T23:19:00Z", "2025-09-02T23:20:00Z", 60, source),
            ]
        },
    )

    assert result is not None
    assert [point["timestamp"] for point in result["points"]] == [
        "2025-09-02T23:17:30Z",
        "2025-09-02T23:18:00Z",
        "2025-09-02T23:19:00Z",
        "2025-09-02T23:19:30Z",
    ]
    assert [point["distance_m"] for point in result["points"]] == [0, 60, 120, 150]
    assert result["coarse"] is True
    assert result["quality"]["clipped_intervals"] == 2
    assert result["sample_interval_seconds"] == 60


def test_keeps_gaps_sparse_and_reports_bounded_analysis_gap() -> None:
    source = _source()
    result = build_telemetry_route(
        _session(start="2025-09-02T23:17:10Z", end="2025-09-02T23:22:10Z"),
        {
            "distance": [
                _distance("2025-09-02T23:17:10Z", "2025-09-02T23:18:10Z", 60, source),
                _distance("2025-09-02T23:19:10Z", "2025-09-02T23:20:10Z", 60, source),
            ]
        },
    )

    assert result is not None
    assert len(result["points"]) == 4
    assert [point["distance_m"] for point in result["points"]] == [0, 60, 60, 120]
    assert result["points"][2]["unknown_before"] is True
    assert result["points"][2]["interval_boundary"] == "gap_start"
    assert result["quality"]["gap_count"] == 1
    assert result["quality"]["max_gap_seconds"] == 120
    assert result["quality"]["emitted_point_max_gap_seconds"] == 60
    assert result["quality"]["analysis_max_gap_seconds"] == 65


def test_attaches_nearest_heart_rate_only_within_thirty_seconds_and_same_source() -> None:
    source = _source()
    other_source = _source("HEALTH_CONNECT", display_name="Other Watch")
    result = build_telemetry_route(
        _session(),
        {
            "distance": [_distance("2025-09-02T23:17:10Z", "2025-09-02T23:18:10Z", 60, source)],
            "heart-rate": [
                _heart_rate("2025-09-02T23:17:45Z", 145, source),
                _heart_rate("2025-09-02T23:18:00Z", 200, other_source),
                _heart_rate("2025-09-02T23:19:00Z", 190, source),
            ],
        },
    )

    assert result is not None
    assert result["points"][1]["heart_rate"] == 145


def test_returns_none_without_an_eligible_matching_distance_source() -> None:
    result = build_telemetry_route(
        _session(source=_source("FITBIT", display_name="Pixel Watch 3")),
        {
            "distance": [
                _distance(
                    "2025-09-02T23:17:10Z",
                    "2025-09-02T23:18:10Z",
                    60,
                    _source("HEALTH_CONNECT", display_name="Fitness Band", package="com.google.android.apps.fitness"),
                )
            ]
        },
    )

    assert result is None
