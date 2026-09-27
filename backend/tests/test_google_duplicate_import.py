from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db import GoogleConnection, Run, RunStream
from app.google_health import _sessions_are_passive_active_duplicate
from app.google_routes import _persist_sync_result
from app.owner import DEV_OWNER_ID
from app.run_visibility import EXCLUDED_GOOGLE_SOURCE


ACTIVE_SOURCE = {
    "platform": "FITBIT",
    "recordingMethod": "ACTIVELY_MEASURED",
    "device": {"displayName": "Pixel Watch"},
}
PASSIVE_SOURCE = {
    "platform": "HEALTH_CONNECT",
    "recordingMethod": "PASSIVELY_MEASURED",
    "application": {"packageName": "com.google.android.apps.fitness"},
}


def _session(source_id: str, started_at: datetime, *, source: dict, duration: int = 1_800) -> dict:
    return {
        "provider_id": source_id,
        "source_id": source_id,
        "id": source_id,
        "title": "Run",
        "activity_type": "RUNNING",
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "duration_seconds": duration,
        "active_duration_seconds": duration,
        "distance_m": 5_000,
        "avg_hr": 145,
        "data_source": source,
        "metrics_summary": {"distanceMillimeters": 5_000_000},
        "exercise_events": [],
    }


def _route(source_id: str, started_at: datetime, *, duration: int = 1_800) -> dict:
    start = started_at.isoformat().replace("+00:00", "Z")
    end = (started_at + timedelta(seconds=duration)).isoformat().replace("+00:00", "Z")
    return {
        "exercise_id": source_id,
        "points": [
            {"timestamp": start, "lat": 43.0, "lon": -79.0, "distance_m": 0},
            {"timestamp": end, "lat": 43.01, "lon": -79.01, "distance_m": 5_000},
        ],
        "laps": [],
    }


def _result(*sessions: dict, routes: list[dict] | None = None) -> dict:
    return {
        "sessions": list(sessions),
        "routes": routes if routes is not None else [],
        "samples": {},
        "coverage": {"complete": True, "record_counts": {"samples": {}}},
        "errors": [],
    }


def _connection() -> GoogleConnection:
    return GoogleConnection(
        owner_id=DEV_OWNER_ID,
        provider="google-health",
        refresh_token_encrypted="test-token",
        connected_at=datetime.now(timezone.utc),
    )


def test_passive_active_interval_guard_preserves_active_pairs_and_separate_warmups() -> None:
    start = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    active = _session("active", start, source=ACTIVE_SOURCE)
    passive = _session("passive", start + timedelta(seconds=30), source=PASSIVE_SOURCE, duration=1_740)
    assert _sessions_are_passive_active_duplicate(active, passive)

    active_again = _session("active-2", start, source=ACTIVE_SOURCE)
    assert not _sessions_are_passive_active_duplicate(active, active_again)

    passive_child = _session("passive-child", start + timedelta(seconds=90), source=PASSIVE_SOURCE, duration=180)
    assert _sessions_are_passive_active_duplicate(active, passive_child)

    broad_passive = _session("passive-parent", start - timedelta(hours=1), source=PASSIVE_SOURCE, duration=7_200)
    assert not _sessions_are_passive_active_duplicate(active, broad_passive)

    later = _session("passive-later", start + timedelta(hours=2), source=PASSIVE_SOURCE)
    assert not _sessions_are_passive_active_duplicate(active, later)


def test_summary_only_phone_duplicate_is_suppressed_without_a_gps_stream(client):
    start = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    result = _result(
        _session('phone-summary', start + timedelta(seconds=20), source=PASSIVE_SOURCE, duration=1700),
        _session('watch-summary', start, source=ACTIVE_SOURCE),
    )
    with client.app.state.session_factory() as db:
        assert _persist_sync_result(db, DEV_OWNER_ID, result, _connection()) == (1, 1, 0)
        db.commit()
        assert db.query(Run).count() == 1
        assert db.query(Run).one().source_id == 'watch-summary'


def test_import_prefers_active_watch_and_is_repeat_safe(client) -> None:
    start = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    active = _session("watch-1", start, source=ACTIVE_SOURCE)
    passive = _session("phone-1", start + timedelta(seconds=35), source=PASSIVE_SOURCE, duration=1_740)
    result = _result(active, passive, routes=[_route("watch-1", start), _route("phone-1", start + timedelta(seconds=35), duration=1_740)])

    with client.app.state.session_factory() as db:
        imported, skipped, errors = _persist_sync_result(db, DEV_OWNER_ID, result, _connection())
        db.commit()
    assert (imported, skipped, errors) == (1, 1, 0)

    with client.app.state.session_factory() as db:
        runs = db.query(Run).filter(Run.owner_id == DEV_OWNER_ID).all()
        assert len(runs) == 1
        assert runs[0].source_id == "watch-1"
        stream = db.query(RunStream).filter(RunStream.run_id == runs[0].id).one()
        assert stream.analysis["provider_data_source"]["recording_method"] == "ACTIVELY_MEASURED"

        imported, skipped, errors = _persist_sync_result(db, DEV_OWNER_ID, result, _connection())
        db.commit()
    assert (imported, skipped, errors) == (0, 2, 0)

    with client.app.state.session_factory() as db:
        assert db.query(Run).filter(Run.owner_id == DEV_OWNER_ID).count() == 1


def test_same_interval_active_runs_and_nonoverlap_passive_runs_are_kept(client) -> None:
    start = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    active_one = _session("watch-a", start, source=ACTIVE_SOURCE)
    active_two = _session("watch-b", start + timedelta(seconds=20), source=ACTIVE_SOURCE)
    passive_later = _session("phone-later", start + timedelta(hours=2), source=PASSIVE_SOURCE)
    result = _result(
        active_one,
        active_two,
        passive_later,
        routes=[_route("watch-a", start), _route("watch-b", start + timedelta(seconds=20)), _route("phone-later", start + timedelta(hours=2))],
    )
    with client.app.state.session_factory() as db:
        imported, skipped, errors = _persist_sync_result(db, DEV_OWNER_ID, result, _connection())
        db.commit()
    assert (imported, skipped, errors) == (3, 0, 0)
    with client.app.state.session_factory() as db:
        assert db.query(Run).filter(Run.owner_id == DEV_OWNER_ID).count() == 3


def test_passive_midworkout_child_matches_active_parent_across_start_offset(client) -> None:
    start = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    active_parent = _session("watch-parent", start, source=ACTIVE_SOURCE, duration=7_200)
    passive_child = _session(
        "phone-child",
        start + timedelta(hours=1),
        source=PASSIVE_SOURCE,
        duration=1_200,
    )
    result = _result(
        active_parent,
        passive_child,
        routes=[
            _route("watch-parent", start, duration=7_200),
            _route("phone-child", start + timedelta(hours=1), duration=1_200),
        ],
    )
    with client.app.state.session_factory() as db:
        imported, skipped, errors = _persist_sync_result(db, DEV_OWNER_ID, result, _connection())
        db.commit()
    assert (imported, skipped, errors) == (1, 1, 0)
    with client.app.state.session_factory() as db:
        assert db.query(Run).filter(Run.owner_id == DEV_OWNER_ID).count() == 1


def test_quarantined_google_row_is_not_refreshed_or_relinked(client) -> None:
    start = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    session = _session("quarantined", start, source=ACTIVE_SOURCE)
    with client.app.state.session_factory() as db:
        db.add(
            Run(
                owner_id=DEV_OWNER_ID,
                title="Manual title",
                started_at=start,
                distance_km=1.0,
                duration_seconds=60,
                run_type="easy",
                run_type_assignment="manual",
                shoe_assignment="unassigned",
                notes="keep",
                source=EXCLUDED_GOOGLE_SOURCE,
                source_id="quarantined",
                dedupe_key=f"owner:{DEV_OWNER_ID}|source:google_health|id:quarantined",
            )
        )
        db.commit()

        imported, skipped, errors = _persist_sync_result(db, DEV_OWNER_ID, _result(session), _connection())
        db.commit()
    assert (imported, skipped, errors) == (0, 1, 0)
    with client.app.state.session_factory() as db:
        row = db.query(Run).filter(Run.source_id == "quarantined").one()
        assert row.source == EXCLUDED_GOOGLE_SOURCE
        assert row.distance_km == 1.0
        assert row.duration_seconds == 60
        assert row.notes == "keep"


def test_passive_active_guard_is_owner_scoped(client) -> None:
    start = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    with client.app.state.session_factory() as db:
        other_owner = "00000000-0000-0000-0000-000000000002"
        other = Run(
            owner_id=other_owner,
            title="Other owner",
            started_at=start,
            distance_km=5.0,
            duration_seconds=1_800,
            run_type="run",
            run_type_assignment="unassigned",
            shoe_assignment="unassigned",
            notes="",
            source="google_health",
            source_id="other-watch",
            dedupe_key=f"owner:{other_owner}|source:google_health|id:other-watch",
            stream_available=True,
        )
        db.add(other)
        db.flush()
        db.add(
            RunStream(
                owner_id=other_owner,
                run_id=other.id,
                payload_gzip=b"x",
                sample_count=2,
                raw_bytes=1,
                compressed_bytes=1,
                laps=[],
                analysis={"provider_data_source": {"recording_method": "ACTIVELY_MEASURED"}},
                created_at=start,
            )
        )
        db.commit()

    passive = _session("owner-phone", start, source=PASSIVE_SOURCE)
    with client.app.state.session_factory() as db:
        imported, skipped, errors = _persist_sync_result(db, DEV_OWNER_ID, _result(passive), _connection())
        db.commit()
    assert (imported, skipped, errors) == (1, 0, 0)


def test_walking_source_is_not_an_active_run_duplicate(client) -> None:
    start = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    with client.app.state.session_factory() as db:
        other = Run(
            owner_id=DEV_OWNER_ID,
            title="Walking source",
            started_at=start,
            distance_km=5.0,
            duration_seconds=1_800,
            run_type="run",
            run_type_assignment="unassigned",
            shoe_assignment="unassigned",
            notes="",
            source="google_health",
            source_id="walking-source",
            dedupe_key=f"owner:{DEV_OWNER_ID}|source:google_health|id:walking-source",
            stream_available=True,
        )
        db.add(other)
        db.flush()
        db.add(
            RunStream(
                owner_id=DEV_OWNER_ID,
                run_id=other.id,
                payload_gzip=b"x",
                sample_count=2,
                raw_bytes=1,
                compressed_bytes=1,
                laps=[],
                analysis={
                    "provider_data_source": {"recording_method": "ACTIVELY_MEASURED"},
                    "provider_activity_type": "WALKING",
                },
                created_at=start,
            )
        )
        db.commit()

    passive = _session("phone-run", start + timedelta(seconds=10), source=PASSIVE_SOURCE)
    with client.app.state.session_factory() as db:
        imported, skipped, errors = _persist_sync_result(db, DEV_OWNER_ID, _result(passive), _connection())
        db.commit()
    assert (imported, skipped, errors) == (1, 0, 0)
