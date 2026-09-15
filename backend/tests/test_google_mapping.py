from __future__ import annotations

import pytest

from app.google_routes import _build_google_stream, _session_to_run


def _session(activity_type: object) -> dict[str, object]:
    return {
        "activity_type": activity_type,
        "started_at": "2026-04-20T08:00:00Z",
        "distance_km": 5.0,
        "duration_seconds": 1_800,
        "provider_id": "run-123",
        "title": "Google Health run",
    }


@pytest.mark.parametrize("activity_type", ["RUNNING", "TREADMILL", " running ", "treadmill"])
def test_google_running_activity_types_map_to_runs(activity_type: str) -> None:
    mapped = _session_to_run(_session(activity_type))

    assert mapped is not None
    assert mapped["run_type"] == "run"


@pytest.mark.parametrize(
    "activity_type",
    ["WALKING", "BIKING", "WORKOUT", "UNKNOWN", "", None, 1],
)
def test_google_non_running_or_missing_activity_types_are_rejected(activity_type: object) -> None:
    assert _session_to_run(_session(activity_type)) is None


def test_active_duration_is_checked_before_rounding_elapsed() -> None:
    session = {**_session("RUNNING"), "duration_seconds": 1800.4, "active_duration_seconds": 1800.3}
    assert _session_to_run(session)["moving_seconds"] == 1800


def test_google_route_keeps_embedded_telemetry() -> None:
    session = {**_session("RUNNING"), "id": "run-123"}
    result = {"routes": [{"exercise_id": "run-123", "points": [
        {"timestamp": "2026-04-20T08:00:00Z", "distance_m": 0, "altitude_m": 15.5, "heart_rate": 142},
    ]}]}
    points, _ = _build_google_stream(session, result)
    assert points[0]["heart_rate"] == 142
    assert points[0]["altitude_m"] == 15.5
