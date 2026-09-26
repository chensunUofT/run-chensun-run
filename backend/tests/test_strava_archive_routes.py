from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import zipfile

from app import strava_archive_routes
from app.db import Run


def test_export_duration_overrides_old_estimate_confidence_but_not_race_elapsed():
    from app.fitness import _duration_inputs
    run = {"duration_seconds": 1800, "moving_seconds": 1700}
    analysis = {"moving_time_source": "strava_export", "provider_active_seconds": 1700,
                "moving_time_confidence": 0, "moving_time_confidence_label": "insufficient"}
    assert _duration_inputs(run, analysis, "training")[0] == 1700
    assert _duration_inputs(run, analysis, "race")[0] == 1800


def _zip_bytes() -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("activities.csv", "id,name\n1,Run\n")
    return output.getvalue()


def _activity(*, source_id: str = "strava-1", run_type: str = "Run", start: str = "2026-09-14T12:00:00+00:00") -> dict:
    return {
        "source_id": source_id,
        "title": "Exported run",
        "started_at": datetime.fromisoformat(start),
        "distance_km": 5.0,
        "duration_seconds": 600,
        "moving_seconds": 570,
        "avg_hr": 145,
        "source_utc_offset_seconds": 0,
        "run_type": run_type,
        "samples": [
            {"elapsed_seconds": 0, "distance_m": 0, "latitude": 43.0, "longitude": -79.0},
            {"elapsed_seconds": 600, "distance_m": 5000, "latitude": 43.01, "longitude": -79.01},
        ],
        "laps": [],
        "metadata": {"source_file": "activities.csv"},
    }


def test_strava_import_is_idempotent_and_preserves_supplied_moving_time(client, monkeypatch):
    monkeypatch.setattr(
        strava_archive_routes,
        "_parse_archive",
        lambda contents: {"activities": [_activity()], "skipped": 0, "warnings": []},
    )

    first = client.post("/api/import/strava", files={"file": ("export.zip", _zip_bytes(), "application/zip")})
    assert first.status_code == 200, first.text
    assert first.json()["imported"] == 1
    assert first.json()["matched"] == 0

    second = client.post("/api/import/strava", files={"file": ("export.zip", _zip_bytes(), "application/zip")})
    assert second.status_code == 200, second.text
    assert second.json()["imported"] == 0
    assert second.json()["matched"] == 1

    rows = client.get("/api/runs").json()
    assert len(rows) == 1
    assert rows[0]["source"] == "strava-export"
    assert rows[0]["moving_seconds"] == 570


def test_strava_walk_is_filtered_without_creating_a_run(client, monkeypatch):
    monkeypatch.setattr(
        strava_archive_routes,
        "_parse_archive",
        lambda contents: {"activities": [_activity(source_id="walk-1", run_type="Walk")], "skipped": 0, "warnings": []},
    )
    response = client.post("/api/import/strava", files={"file": ("export.zip", _zip_bytes(), "application/zip")})
    assert response.status_code == 200, response.text
    assert response.json()["imported"] == 0
    assert response.json()["skipped"] == 1
    assert client.get("/api/runs").json() == []


def test_invalid_zip_is_atomic(client):
    response = client.post("/api/import/strava", files={"file": ("broken.zip", b"not a zip", "application/zip")})
    assert response.status_code == 422
    assert client.get("/api/runs").json() == []


def test_direct_strava_csv_is_supported(client):
    csv = (
        "Activity ID,Activity Date,Activity Type,Elapsed Time,Moving Time,Distance,Distance,Activity Name,Average Heart Rate\n"
        "csv-1,2026-09-14T12:00:00Z,Run,600,570,5.0,5000,CSV run,145\n"
    ).encode()
    response = client.post("/api/import/strava", files={"file": ("activities.csv", csv, "text/csv")})
    assert response.status_code == 200, response.text
    assert response.json()["imported"] == 1


def test_cross_provider_match_is_owner_scoped_and_links_metadata(client, monkeypatch):
    start = "2026-09-14T12:00:00+00:00"
    existing = client.post(
        "/api/runs",
        json={
            "source": "google_health",
            "source_id": "google-1",
            "title": "Google run",
            "started_at": start,
            "distance_km": 5.0,
            "duration_seconds": 600,
            "run_type": "race",
            "avg_hr": 140,
            "notes": "",
        },
    )
    assert existing.status_code == 201, existing.text
    run_id = existing.json()["id"]

    # Add the metadata that the existing Google importer uses for a measured
    # race. The Strava distance differs slightly but must not replace it.
    session_factory = client.app.state.session_factory
    with session_factory() as db:
        run = db.get(Run, run_id)
        run.run_type_assignment = "manual"
        stream = run.stream
        from app.db import RunStream

        stream = RunStream(
            owner_id=run.owner_id,
            run_id=run.id,
            payload_gzip=__import__("gzip").compress(b'{"samples": [], "laps": []}'),
            sample_count=0,
            raw_bytes=26,
            compressed_bytes=0,
            laps=[],
            analysis={"official_race_distance_km": 5.0, "weather": {"temperature_c": 12}},
            created_at=datetime.now(timezone.utc),
        )
        db.add(stream)
        db.commit()

    activity = _activity(source_id="strava-1", start=start)
    activity["distance_km"] = 5.1
    monkeypatch.setattr(
        strava_archive_routes,
        "_parse_archive",
        lambda contents: {"activities": [activity], "skipped": 0, "warnings": []},
    )
    response = client.post("/api/import/strava", files={"file": ("export.zip", _zip_bytes(), "application/zip")})
    assert response.status_code == 200, response.text
    assert response.json()["matched"] == 1

    rows = client.get("/api/runs").json()
    assert len(rows) == 1
    assert rows[0]["id"] == run_id
    assert rows[0]["source"] == "google_health"
    assert rows[0]["run_type"] == "race"
    assert rows[0]["distance_km"] == 5.0
    assert rows[0]["moving_seconds"] == 570
    stream = client.get(f"/api/runs/{run_id}/streams")
    assert stream.status_code == 200, stream.text
    assert stream.json()["analysis"]["strava_export"]["activity_id"] == "strava-1"
    assert stream.json()["analysis"]["moving_seconds"] == 570


def test_linked_id_stays_matched_after_existing_date_edit(client, monkeypatch):
    start = "2026-09-14T12:00:00+00:00"
    existing = client.post(
        "/api/runs",
        json={
            "source": "google_health",
            "source_id": "google-linked",
            "title": "Google run",
            "started_at": start,
            "distance_km": 5.0,
            "duration_seconds": 600,
            "run_type": "easy",
            "notes": "",
        },
    )
    run_id = existing.json()["id"]
    monkeypatch.setattr(
        strava_archive_routes,
        "_parse_archive",
        lambda contents: {"activities": [_activity(source_id="linked-id", start=start)], "skipped": 0, "warnings": []},
    )
    first = client.post("/api/import/strava", files={"file": ("export.zip", _zip_bytes(), "application/zip")})
    assert first.json()["matched"] == 1

    with client.app.state.session_factory() as db:
        run = db.get(Run, run_id)
        run.started_at = datetime.fromisoformat("2026-09-15T12:00:00+00:00")
        db.commit()
    second = client.post("/api/import/strava", files={"file": ("export.zip", _zip_bytes(), "application/zip")})
    assert second.status_code == 200, second.text
    assert second.json()["matched"] == 1
    assert second.json()["imported"] == 0
    assert len(client.get("/api/runs").json()) == 1


def test_different_strava_id_cannot_overwrite_existing_link(client, monkeypatch):
    start = "2026-09-14T12:00:00+00:00"
    existing = client.post(
        "/api/runs",
        json={
            "source": "google_health",
            "source_id": "google-one-to-one",
            "title": "Google run",
            "started_at": start,
            "distance_km": 5.0,
            "duration_seconds": 600,
            "run_type": "easy",
            "notes": "",
        },
    )
    run_id = existing.json()["id"]
    monkeypatch.setattr(
        strava_archive_routes,
        "_parse_archive",
        lambda contents: {"activities": [_activity(source_id="first-id", start=start)], "skipped": 0, "warnings": []},
    )
    assert client.post("/api/import/strava", files={"file": ("export.zip", _zip_bytes(), "application/zip")}).json()["matched"] == 1
    monkeypatch.setattr(
        strava_archive_routes,
        "_parse_archive",
        lambda contents: {"activities": [_activity(source_id="second-id", start=start)], "skipped": 0, "warnings": []},
    )
    response = client.post("/api/import/strava", files={"file": ("export.zip", _zip_bytes(), "application/zip")})
    assert response.status_code == 200, response.text
    assert response.json()["imported"] == 1
    assert client.get(f"/api/runs/{run_id}/streams").json()["analysis"]["strava_export"]["activity_id"] == "first-id"


def test_cross_provider_matching_does_not_read_another_owner(client, monkeypatch):
    with client.app.state.session_factory() as db:
        db.add(
            Run(
                owner_id="00000000-0000-0000-0000-000000000002",
                title="Other owner",
                started_at=datetime.fromisoformat("2026-09-14T12:00:00+00:00"),
                distance_km=5.0,
                duration_seconds=600,
                run_type="easy",
                run_type_assignment="manual",
                shoe_assignment="unassigned",
                notes="",
                source="google_health",
                source_id="other-owner-google",
            )
        )
        db.commit()
    monkeypatch.setattr(
        strava_archive_routes,
        "_parse_archive",
        lambda contents: {"activities": [_activity(source_id="owner-safe", start="2026-09-14T12:00:00+00:00")], "skipped": 0, "warnings": []},
    )
    response = client.post("/api/import/strava", files={"file": ("export.zip", _zip_bytes(), "application/zip")})
    assert response.status_code == 200, response.text
    assert response.json()["imported"] == 1
    assert response.json()["matched"] == 0
