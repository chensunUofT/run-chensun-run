from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.coaching import CoachingGoal, CoachingSchedule, CoachingSession
from app.owner import DEV_OWNER_ID


def _today() -> date:
    return datetime.now(ZoneInfo("America/New_York")).date()


def _goal_payload(*, days: int = 56, distance_km: float = 10.0) -> dict[str, Any]:
    return {
        "race_date": (_today() + timedelta(days=days)).isoformat(),
        "distance_km": distance_km,
        "target_seconds": 3_600,
    }


def _run_payload(
    *,
    started_at: datetime,
    distance_km: float = 5.0,
    duration_seconds: int = 1_800,
    source: str = "manual",
    run_type: str = "easy",
) -> dict[str, Any]:
    return {
        "title": "Training evidence",
        "started_at": started_at.isoformat(),
        "distance_km": distance_km,
        "duration_seconds": duration_seconds,
        "run_type": run_type,
        "avg_hr": 145,
        "notes": "",
        "rpe": 4,
        "source": source,
    }


def test_goal_default_schedule_race_and_pace_contract(client: TestClient) -> None:
    response = client.put("/api/coaching/goal", json=_goal_payload())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["goal"]["weekly_schedule"] == [
        {"weekday": 1, "run_type": "easy"},
        {"weekday": 3, "run_type": "quality"},
        {"weekday": 5, "run_type": "easy"},
        {"weekday": 6, "run_type": "long"},
    ]
    assert set(body["paces"]) == {"easy", "tempo", "interval", "long"}
    assert all(isinstance(value, int) for value in body["paces"].values())
    sessions = body["sessions"]
    assert sessions
    race_date = body["goal"]["race_date"]
    race = [session for session in sessions if session["date"] == race_date]
    assert len(race) == 1
    assert race[0]["run_type"] == "race"
    assert race[0]["distance_km"] == 10.0
    assert all(session["date"] >= _today().isoformat() for session in sessions)
    quality_by_week: dict[date, int] = defaultdict(int)
    for session in sessions:
        if session["run_type"] == "quality":
            session_date = date.fromisoformat(session["date"])
            quality_by_week[session_date - timedelta(days=session_date.weekday())] += 1
    assert max(quality_by_week.values(), default=0) <= 1


def test_schedule_validation_rest_edit_and_manual_regeneration_persistence(client: TestClient) -> None:
    payload = _goal_payload(days=42)
    payload["weekly_schedule"] = [
        {"weekday": 1, "run_type": "easy"},
        {"weekday": 3, "run_type": "quality"},
    ]
    assert client.put("/api/coaching/goal", json=payload).status_code == 200
    body = client.get("/api/coaching").json()
    future = next(session for session in body["sessions"] if session["run_type"] != "race")
    edited = client.patch(
        f"/api/coaching/sessions/{future['id']}",
        json={"description": "My fixed workout", "target_pace_seconds": 333},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["manually_edited"] is True
    regenerated = client.post("/api/coaching/generate")
    assert regenerated.status_code == 200, regenerated.text
    retained = next(session for session in regenerated.json()["sessions"] if session["id"] == future["id"])
    assert retained["description"] == "My fixed workout"
    assert retained["target_pace_seconds"] == 333
    assert retained["manually_edited"] is True

    removable = next(session for session in regenerated.json()["sessions"] if session["run_type"] != "race" and session["id"] != future["id"])
    removed = client.patch(
        f"/api/coaching/sessions/{removable['id']}",
        json={"run_type": "rest"},
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["id"] == removable["id"]
    assert removed.json()["run_type"] == "rest"
    rest = next(session for session in client.get("/api/coaching").json()["sessions"] if session["id"] == removable["id"])
    assert rest["distance_km"] == 0.0
    assert rest["target_pace_seconds"] == 0
    assert rest["manually_edited"] is True
    regenerated_after_rest = client.post("/api/coaching/generate")
    assert regenerated_after_rest.status_code == 200, regenerated_after_rest.text
    retained_rest = next(session for session in regenerated_after_rest.json()["sessions"] if session["id"] == removable["id"])
    assert retained_rest["run_type"] == "rest"

    invalid = dict(payload)
    invalid["weekly_schedule"] = [
        {"weekday": 1, "run_type": "quality"},
        {"weekday": 3, "run_type": "quality"},
    ]
    assert client.put("/api/coaching/goal", json=invalid).status_code == 422


def test_goal_update_replaces_schedule_without_stale_relationship_rows(client: TestClient) -> None:
    payload = _goal_payload(days=35)
    assert client.put("/api/coaching/goal", json=payload).status_code == 200
    payload["weekly_schedule"] = [
        {"weekday": 0, "run_type": "easy"},
        {"weekday": 4, "run_type": "long"},
    ]
    updated = client.put("/api/coaching/goal", json=payload)
    assert updated.status_code == 200, updated.text
    assert updated.json()["goal"]["weekly_schedule"] == payload["weekly_schedule"]


def test_prediction_excludes_demo_sample_and_future_and_marks_ordinary_low_confidence(client: TestClient) -> None:
    assert client.put("/api/coaching/goal", json=_goal_payload(days=70)).status_code == 200
    assert client.post("/api/demo/seed").status_code == 200
    assert client.get("/api/coaching").json()["prediction"] is None

    local_zone = ZoneInfo("America/New_York")
    yesterday = datetime.combine(_today() - timedelta(days=1), time(8), tzinfo=local_zone)
    real = client.post("/api/runs", json=_run_payload(started_at=yesterday, source="manual"))
    assert real.status_code == 201, real.text
    future = client.post(
        "/api/runs",
        json=_run_payload(
            started_at=datetime.now(timezone.utc) + timedelta(days=1),
            source="manual",
            distance_km=20,
            duration_seconds=3_000,
        ),
    )
    assert future.status_code == 201, future.text
    body = client.get("/api/coaching").json()
    prediction = body["prediction"]
    assert prediction is not None
    assert prediction["reference_run_id"] == real.json()["id"]
    assert prediction["confidence"] == "low"
    assert prediction["low_seconds"] <= prediction["seconds"] <= prediction["high_seconds"]


def test_prediction_stays_null_for_only_short_or_interval_like_evidence(client: TestClient) -> None:
    assert client.put("/api/coaching/goal", json=_goal_payload(days=30)).status_code == 200
    local_zone = ZoneInfo("America/New_York")
    started = datetime.combine(_today() - timedelta(days=1), time(8), tzinfo=local_zone)
    short = client.post("/api/runs", json=_run_payload(started_at=started, distance_km=1.0, duration_seconds=240))
    assert short.status_code == 201, short.text
    interval = client.post(
        "/api/runs",
        json=_run_payload(
            started_at=started - timedelta(days=1),
            distance_km=5,
            duration_seconds=1_500,
            run_type="interval",
        ),
    )
    assert interval.status_code == 201, interval.text
    body = client.get("/api/coaching").json()
    assert body["prediction"] is None


def test_same_local_date_completion_is_one_to_one_and_distance_aware(client: TestClient) -> None:
    today = _today()
    payload = _goal_payload(days=14)
    payload["weekly_schedule"] = [
        {"weekday": today.weekday(), "run_type": "easy"},
        {"weekday": (today.weekday() + 2) % 7, "run_type": "long"},
    ]
    assert client.put("/api/coaching/goal", json=payload).status_code == 200
    planned = next(session for session in client.get("/api/coaching").json()["sessions"] if session["date"] == today.isoformat())
    local_zone = ZoneInfo("America/New_York")
    # A same-day fixture must not be in the future when tests run before 7 AM.
    started = datetime.combine(today, time(0), tzinfo=local_zone)
    real = client.post("/api/runs", json=_run_payload(started_at=started, distance_km=planned["distance_km"], source="manual"))
    assert real.status_code == 201, real.text
    assert client.post("/api/runs", json=_run_payload(started_at=started + timedelta(minutes=1), source="demo")).status_code == 201
    assert client.post("/api/runs", json=_run_payload(started_at=started + timedelta(minutes=2), source="sample")).status_code == 201
    completed = next(session for session in client.get("/api/coaching").json()["sessions"] if session["id"] == planned["id"])
    assert completed["completed_run_id"] == real.json()["id"]


def test_coaching_rows_and_routes_are_owner_scoped(client: TestClient) -> None:
    # A row for another owner must never become the active goal or an editable
    # session for the fixed local owner.
    other_owner = "00000000-0000-0000-0000-000000000002"
    today = _today()
    with client.app.state.session_factory() as db:
        goal = CoachingGoal(
            owner_id=other_owner,
            race_date=today + timedelta(days=30),
            distance_km=10,
            target_seconds=3_600,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(goal)
        db.flush()
        db.add(CoachingSchedule(owner_id=other_owner, goal_id=goal.id, weekday=1, run_type="easy"))
        session = CoachingSession(
            owner_id=other_owner,
            goal_id=goal.id,
            date=today + timedelta(days=1),
            run_type="easy",
            distance_km=3,
            target_pace_seconds=400,
            description="Other owner",
            manually_edited=False,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(session)
        db.commit()
        other_session_id = session.id
    assert client.get("/api/coaching").json()["goal"] is None
    assert client.patch(f"/api/coaching/sessions/{other_session_id}", json={"description": "leak"}).status_code == 404
    assert DEV_OWNER_ID != other_owner


def test_build_deload_and_taper_lower_non_race_volume(client: TestClient) -> None:
    assert client.put("/api/coaching/goal", json=_goal_payload(days=70)).status_code == 200
    sessions = client.get("/api/coaching").json()["sessions"]
    volumes: dict[date, float] = defaultdict(float)
    for session in sessions:
        if session["run_type"] == "race":
            continue
        session_date = date.fromisoformat(session["date"])
        volumes[session_date - timedelta(days=session_date.weekday())] += session["distance_km"]
    ordered = [volumes[key] for key in sorted(volumes)]
    assert len(ordered) >= 5
    assert ordered[-1] < ordered[-2]
    assert any(ordered[index] < ordered[index - 1] for index in range(1, len(ordered)))
