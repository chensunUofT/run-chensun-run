from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.main import create_app


def run_payload(*, distance: float, duration: int, source_id: str | None = None, shoe_id: int | None = None):
    payload = {
        "title": "Test run",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "distance_km": distance,
        "duration_seconds": duration,
        "run_type": "easy",
        "avg_hr": 145,
        "shoe_id": shoe_id,
        "notes": "",
        "rpe": 4,
        "source": "manual",
    }
    if source_id is not None:
        payload["source_id"] = source_id
    return payload


def test_csv_import_deduplicates_by_source_id_and_fingerprint(client):
    csv = (
        "source_id,title,started_at,distance_km,duration_seconds,run_type,avg_hr,shoe_id,notes,rpe,source\n"
        "provider-1,Imported,2026-09-10T12:00:00+00:00,5,1500,easy,140,,,4,strava\n"
        ",Imported without id,2026-09-11T12:00:00+00:00,10,3000,long,150,,,6,strava\n"
    )
    first = client.post(
        "/api/import/csv",
        files={"file": ("runs.csv", csv.encode(), "text/csv")},
    )
    assert first.status_code == 200, first.text
    assert first.json() == {"imported": 2, "skipped": 0}

    second = client.post(
        "/api/import/csv",
        files={"file": ("runs.csv", csv.encode(), "text/csv")},
    )
    assert second.status_code == 200, second.text
    assert second.json() == {"imported": 0, "skipped": 2}
    assert len(client.get("/api/runs").json()) == 2


def test_stats_uses_weighted_pace_and_monday_week_buckets(client):
    # Both datetimes are in the current local week, so the result is stable
    # even when the test runs around a UTC/local midnight boundary.
    now = datetime.now(timezone.utc).isoformat()
    first = run_payload(distance=5, duration=1500)
    second = run_payload(distance=10, duration=3600)
    first["started_at"] = now
    second["started_at"] = now
    assert client.post("/api/runs", json=first).status_code == 201
    assert client.post("/api/runs", json=second).status_code == 201

    result = client.get("/api/stats?period=week")
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["total_distance_km"] == 15
    assert body["total_duration_seconds"] == 5100
    assert body["run_count"] == 2
    assert body["average_pace_seconds"] == 340
    assert len(body["buckets"]) == 7
    assert sum(bucket["run_count"] for bucket in body["buckets"]) == 2


def test_shoe_edit_computes_total_and_delete_unlinks_runs(client):
    shoe = client.post(
        "/api/shoes",
        json={"name": "Daily trainer", "brand": "Runwise", "initial_distance_km": 20, "status": "active"},
    )
    assert shoe.status_code == 201, shoe.text
    shoe_id = shoe.json()["id"]
    created = client.post("/api/runs", json=run_payload(distance=8, duration=2400, shoe_id=shoe_id))
    assert created.status_code == 201, created.text

    updated = client.patch(f"/api/shoes/{shoe_id}", json={"initial_distance_km": 25})
    assert updated.status_code == 200, updated.text
    assert updated.json()["total_distance_km"] == 33

    deleted = client.delete(f"/api/shoes/{shoe_id}")
    assert deleted.status_code == 204
    assert all(shoe["id"] != shoe_id for shoe in client.get("/api/shoes").json())
    assert client.get("/api/runs").json()[0]["shoe_id"] is None


def test_validation_rejects_invalid_values_and_csv_headers(client):
    invalid_run = run_payload(distance=-1, duration=100)
    assert client.post("/api/runs", json=invalid_run).status_code == 422
    invalid_checkin = client.put(
        "/api/checkins/2026-09-14",
        json={"sleep_hours": 8, "energy": 6, "soreness": 2, "notes": ""},
    )
    assert invalid_checkin.status_code == 422
    unknown_header = "started_at,distance_km,duration_seconds,unexpected\n2026-09-14T12:00:00Z,5,1000,x\n"
    imported = client.post(
        "/api/import/csv",
        files={"file": ("bad.csv", unknown_header.encode(), "text/csv")},
    )
    assert imported.status_code == 422


def test_demo_seed_is_opt_in_and_idempotent(client):
    assert client.get("/api/runs").json() == []
    first = client.post("/api/demo/seed")
    second = client.post("/api/demo/seed")
    assert first.status_code == second.status_code == 200
    assert first.json()["seeded"] == 3
    assert second.json() == {"seeded": 0, "skipped": 3, "run_ids": []}
    assert all(run["source"] == "demo" for run in client.get("/api/runs").json())


def test_external_browser_origins_cannot_write_local_data(client):
    blocked_seed = client.post("/api/demo/seed", headers={"Origin": "https://evil.example"})
    assert blocked_seed.status_code == 403
    assert client.get("/api/runs").json() == []

    csv = "started_at,distance_km,duration_seconds\n2026-09-14T12:00:00Z,5,1200\n"
    blocked_import = client.post(
        "/api/import/csv",
        files={"file": ("runs.csv", csv.encode(), "text/csv")},
        headers={"Origin": "https://evil.example"},
    )
    assert blocked_import.status_code == 403
    assert client.get("/api/runs").json() == []


def test_production_startup_is_blocked_without_authentication(tmp_path):
    app = create_app(
        Settings(
            mode="production",
            database_url=f"sqlite:///{(tmp_path / 'production.db').as_posix()}",
            cors_origins=("http://localhost:5173",),
            max_import_bytes=1024,
            max_import_rows=10,
        )
    )
    with pytest.raises(RuntimeError, match="authentication"):
        from fastapi.testclient import TestClient

        with TestClient(app):
            pass
    app.state.engine.dispose()


def test_render_origin_defaults(monkeypatch):
    from app.config import Settings
    for name in ('RUNWISE_FRONTEND_URL', 'FRONTEND_URL', 'RUNWISE_PUBLIC_BASE_URL', 'PUBLIC_BASE_URL', 'RUNWISE_CORS_ORIGINS', 'CORS_ORIGINS', 'RUNWISE_GOOGLE_REDIRECT_URI', 'GOOGLE_REDIRECT_URI'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('RENDER_EXTERNAL_URL', 'https://runwise.example')
    monkeypatch.setenv('RENDER_EXTERNAL_HOSTNAME', 'runwise.example')
    settings = Settings.from_env()
    assert settings.frontend_url == 'https://runwise.example'
    assert settings.public_base_url == 'https://runwise.example'
    assert settings.cors_origins == ('https://runwise.example',)
    assert settings.allowed_hosts == ('runwise.example',)
    assert settings.google_redirect_uri == 'https://runwise.example/api/integrations/google-health/callback'
