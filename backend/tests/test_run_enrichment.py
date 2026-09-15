"""Regression coverage for full-run pace and manual enrichment locks."""
from datetime import datetime, timezone


def create_run(client, **extra):
    payload = {"title": "Recorded run", "started_at": datetime.now(timezone.utc).isoformat(), "distance_km": 3, "duration_seconds": 930}
    payload.update(extra)
    response = client.post("/api/runs", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_coarse_interval_gaps_are_unknown_not_stops(client):
    from app.db import Run
    from app.owner import DEV_OWNER_ID
    from app.google_routes import _persist_google_stream
    from app.run_enrichment import enrich_run
    identifier = create_run(client, distance_km=0.02, duration_seconds=60)
    session = {"provider_id": "gap-run", "started_at": "2025-01-01T00:00:00Z"}
    route = {"exercise_id": "gap-run", "coarse": True, "quality": {"coarse": True, "analysis_max_gap_seconds": 15}, "points": [
        {"timestamp": f"2025-01-01T00:00:{t:02d}Z", "distance_m": d, "unknown_before": gap}
        for t, d, gap in [(0, 0, False), (10, 20, False), (30, 20, True), (40, 20, False), (50, 20, True)]
    ] + [{"timestamp": "2025-01-01T00:01:00Z", "distance_m": 20}]}
    with client.app.state.session_factory() as db:
        run = db.get(Run, identifier)
        assert _persist_google_stream(db, DEV_OWNER_ID, run, session, {"routes": [route]})
        enrich_run(db, run)
        assert run.moving_seconds == 40
        assert run.stream.analysis["unclassified_seconds"] == 30
        assert run.stream.analysis["distance_m"] == 20


def test_stops_reduce_full_run_pace_and_reprocess_preserves_manual_choices(client):
    shoe = client.post("/api/shoes", json={"name": "Daily trainer", "purchase_date": "2020-01-01"}).json()
    identifier = create_run(client)
    # Three kilometers at 5:00/km with a 30-second traffic light halfway.
    samples = [{"elapsed_seconds": t, "distance_m": (t if t <= 450 else max(450, t - 30)) * 10 / 3} for t in range(0, 931, 5)]
    response = client.put(f"/api/runs/{identifier}/streams", json={"samples": samples})
    assert response.status_code == 200, response.text
    run = client.get(f"/api/runs/{identifier}").json()
    assert run["moving_seconds"] == 900
    assert run["shoe_id"] == shoe["id"]
    stats = client.get("/api/stats").json()
    assert stats["average_pace_seconds"] == 300
    assert stats["total_duration_seconds"] == 930
    client.patch(f"/api/runs/{identifier}", json={"run_type": "easy", "shoe_id": None})
    assert client.post("/api/runs/reprocess").status_code == 200
    run = client.get(f"/api/runs/{identifier}").json()
    assert run["run_type"] == "easy"
    assert run["shoe_id"] is None
    assert run["shoe_assignment"] == "manual"


def test_short_stream_cannot_turn_long_run_into_fast_run(client):
    identifier = create_run(client, distance_km=10, duration_seconds=3600)
    samples = [{"elapsed_seconds": t, "distance_m": t * 3} for t in range(0, 61, 5)]
    response = client.put(f"/api/runs/{identifier}/streams", json={"samples": samples})
    assert response.status_code == 200, response.text
    assert client.get(f"/api/runs/{identifier}").json()["moving_seconds"] == 3600
    assert response.json()["moving_time_confidence_label"] == "low"
    assert response.json()["moving_pace_seconds_per_km"] == 360


def test_unavailable_distance_does_not_produce_zero_pace(client):
    identifier = create_run(client)
    samples = [{"elapsed_seconds": t, "distance_m": 0, "heart_rate": 145} for t in (0, 10, 20)]
    response = client.put(f"/api/runs/{identifier}/streams", json={"samples": samples})
    assert response.status_code == 200, response.text
    run = client.get(f"/api/runs/{identifier}").json()
    assert run["moving_seconds"] is None
    assert client.get("/api/stats").json()["average_pace_seconds"] > 0


def test_empty_route_does_not_starve_following_batch(client):
    from app.db import GoogleConnection
    from app.google_routes import _attach_bounded_google_routes
    from app.owner import DEV_OWNER_ID
    calls = []

    class Provider:
        def export_exercise_tcx(self, identifier):
            calls.append(identifier)
            return {"exercise_id": identifier, "points": []}

    sessions = [{"id": str(n), "provider_id": str(n), "resource_name": str(n), "activity_type": "WORKOUT", "started_at": f"2026-03-{n:02d}T08:00:00Z"} for n in (1, 2)]
    with client.app.state.session_factory() as db:
        db.add(GoogleConnection(owner_id=DEV_OWNER_ID, provider="google-health", connected_at=datetime.now(timezone.utc), refresh_token_encrypted="test", scopes=[]))
        db.flush()
        for _ in range(2):
            _attach_bounded_google_routes(Provider(), {"sessions": sessions, "coverage": {"complete": True}}, db, DEV_OWNER_ID, max_routes=1)
            db.flush()
    assert len(calls) == 2
    assert len(set(calls)) == 2
