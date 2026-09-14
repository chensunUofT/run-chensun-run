from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import activity_analysis
from app.auth import (
    PERSONAL_SESSION_COOKIE,
    CurrentUser,
    issue_personal_session,
)
from app.config import Settings
from app.db import Share
from app.main import create_app


def _run_payload(
    *,
    title: str = "Regression run",
    started_at: str = "2026-09-10T12:00:00Z",
    distance_km: float = 8.0,
    duration_seconds: int = 2_400,
    run_type: str = "easy",
    shoe_id: int | None = None,
    include_shoe_id: bool = False,
    notes: str = "",
    rpe: int | None = 4,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": title,
        "started_at": started_at,
        "distance_km": distance_km,
        "duration_seconds": duration_seconds,
        "run_type": run_type,
        "avg_hr": 145,
        "notes": notes,
        "rpe": rpe,
        "source": "manual",
    }
    if include_shoe_id:
        # Sending null is a deliberate manual lock, which is distinct from
        # omitting the field and leaving a run available for inference.
        payload["shoe_id"] = shoe_id
    return payload


def _create_run(client: TestClient, **kwargs: Any) -> dict[str, Any]:
    response = client.post("/api/runs", json=_run_payload(**kwargs))
    assert response.status_code == 201, response.text
    return response.json()


def test_shoe_purchase_date_and_rules_drive_preview_apply_and_manual_lock(client: TestClient) -> None:
    matching_rules = {
        "min_distance_km": 5,
        "max_distance_km": 10,
        "min_pace_seconds": 250,
        "max_pace_seconds": 360,
        "run_types": ["easy"],
        "priority": 1,
    }
    eligible_response = client.post(
        "/api/shoes",
        json={
            "name": "Rotation easy",
            "brand": "Runwise",
            "purchase_date": "2026-09-01",
            "initial_distance_km": 12,
            "rules": matching_rules,
        },
    )
    assert eligible_response.status_code == 201, eligible_response.text
    eligible = eligible_response.json()
    assert eligible["purchase_date"] == "2026-09-01"
    assert eligible["initial_distance_km"] == 12
    assert eligible["total_distance_km"] == 12
    assert eligible["rules"]["run_types"] == ["easy"]

    future_response = client.post(
        "/api/shoes",
        json={
            "name": "Not yet purchased",
            "brand": "Runwise",
            "purchase_date": "2026-09-20",
            "rules": matching_rules,
        },
    )
    assert future_response.status_code == 201, future_response.text
    future = future_response.json()

    candidate_run = _create_run(client)
    locked_run = _create_run(client, title="Explicitly unassigned", include_shoe_id=True)
    assert locked_run["shoe_id"] is None
    assert locked_run["shoe_assignment"] == "manual"
    assert locked_run["shoe_reason"] == "Manually marked without a shoe"

    preview_response = client.post("/api/shoes/infer", json={"apply": False})
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["apply"] is False
    assert preview["updated"] == 0
    assert preview["skipped_manual"] == 1
    assert len(preview["assignments"]) == 1
    preview_assignment = preview["assignments"][0]
    assert preview_assignment["run_id"] == candidate_run["id"]
    assert preview_assignment["shoe_id"] == eligible["id"]
    assert preview_assignment["applied"] is False
    assert preview_assignment["confidence"] > 0
    assert future["id"] not in {item["shoe_id"] for item in preview_assignment["candidates"]}
    assert client.get(f"/api/runs/{candidate_run['id']}").json()["shoe_assignment"] == "unassigned"

    apply_response = client.post("/api/shoes/infer", json={"apply": True})
    assert apply_response.status_code == 200, apply_response.text
    applied = apply_response.json()
    assert applied["updated"] == 1
    assert applied["assignments"][0]["applied"] is True
    assigned = client.get(f"/api/runs/{candidate_run['id']}").json()
    assert assigned["shoe_id"] == eligible["id"]
    assert assigned["shoe_assignment"] == "inferred"
    assert assigned["shoe_confidence"] == applied["assignments"][0]["confidence"]
    assert "rule" in assigned["shoe_reason"]
    assert client.get(f"/api/runs/{locked_run['id']}").json()["shoe_assignment"] == "manual"

    # A user can lock an inferred assignment to no shoe. Future inference
    # passes must preserve that explicit choice.
    lock_response = client.patch(f"/api/runs/{candidate_run['id']}", json={"shoe_id": None})
    assert lock_response.status_code == 200, lock_response.text
    locked = lock_response.json()
    assert locked["shoe_id"] is None
    assert locked["shoe_assignment"] == "manual"
    assert locked["shoe_reason"] == "Manually marked without a shoe"
    repeat_response = client.post("/api/shoes/infer", json={"apply": True})
    assert repeat_response.status_code == 200, repeat_response.text
    assert repeat_response.json()["assignments"] == []
    assert repeat_response.json()["updated"] == 0
    assert repeat_response.json()["skipped_manual"] == 2


def test_public_run_share_is_privacy_safe_snapshot_and_expires_or_revokes(client: TestClient) -> None:
    shoe_response = client.post(
        "/api/shoes",
        json={"name": "Public shoe", "brand": "Runwise", "purchase_date": "2026-01-01"},
    )
    assert shoe_response.status_code == 201, shoe_response.text
    run = _create_run(
        client,
        title="Before sharing",
        shoe_id=shoe_response.json()["id"],
        include_shoe_id=True,
        notes="Private training note",
        rpe=8,
    )

    share_response = client.post(
        "/api/shares",
        json={"kind": "run", "run_id": run["id"], "expires_days": 7},
    )
    assert share_response.status_code == 200, share_response.text
    share = share_response.json()
    assert share["url"].endswith(f"/api/public/shares/{share['token']}")
    public_response = client.get(f"/api/public/shares/{share['token']}")
    assert public_response.status_code == 200, public_response.text
    public = public_response.json()
    assert public["kind"] == "run"
    snapshot = public["run"]
    assert snapshot["title"] == "Before sharing"
    assert snapshot["shoe"] == {"name": "Public shoe", "brand": "Runwise"}
    assert "notes" not in snapshot
    assert "rpe" not in snapshot
    assert "source" not in snapshot
    assert "source_id" not in snapshot
    assert "expires_at" in public

    # The public URL serves the immutable snapshot, even after the private
    # run is edited.
    updated = client.patch(f"/api/runs/{run['id']}", json={"title": "After sharing", "notes": "Changed privately"})
    assert updated.status_code == 200, updated.text
    unchanged = client.get(f"/api/public/shares/{share['token']}")
    assert unchanged.status_code == 200
    assert unchanged.json()["run"]["title"] == "Before sharing"

    listed = client.get("/api/shares")
    assert listed.status_code == 200, listed.text
    listed_share = listed.json()[0]
    assert listed_share["active"] is True
    assert listed_share["run_id"] == run["id"]

    revoke_response = client.delete(f"/api/shares/{listed_share['id']}")
    assert revoke_response.status_code == 204, revoke_response.text
    assert client.get(f"/api/public/shares/{share['token']}").status_code == 404
    revoked = client.get("/api/shares").json()[0]
    assert revoked["active"] is False
    assert revoked["revoked_at"] is not None

    expiring_response = client.post(
        "/api/shares",
        json={"kind": "run", "run_id": run["id"], "expires_days": 1},
    )
    assert expiring_response.status_code == 200, expiring_response.text
    expiring = expiring_response.json()
    expiring_id = max(row["id"] for row in client.get("/api/shares").json())
    with client.app.state.session_factory() as db:
        row = db.get(Share, expiring_id)
        assert row is not None
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    assert client.get(f"/api/public/shares/{expiring['token']}").status_code == 404
    expired = next(row for row in client.get("/api/shares").json() if row["id"] == expiring_id)
    assert expired["active"] is False


def test_run_streams_preserve_coordinates_and_infer_or_respect_laps(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _create_run(client, title="Stream run", distance_km=1.2, duration_seconds=150)
    samples = [
        {"elapsed_seconds": 0, "distance_m": 0, "heart_rate": 140, "latitude": 40.0000, "longitude": -73.0000},
        {"elapsed_seconds": 30, "distance_m": 250, "heart_rate": 142, "latitude": 40.0005, "longitude": -73.0005},
        {"elapsed_seconds": 60, "distance_m": 500, "heart_rate": 144, "latitude": 40.0010, "longitude": -73.0010},
        {"elapsed_seconds": 90, "distance_m": 750, "heart_rate": 146, "latitude": 40.0015, "longitude": -73.0015},
        {"elapsed_seconds": 120, "distance_m": 1000, "heart_rate": 148, "latitude": 40.0020, "longitude": -73.0020},
        {"elapsed_seconds": 150, "distance_m": 1200, "heart_rate": 150, "latitude": 40.0025, "longitude": -73.0025},
    ]
    captured: dict[str, Any] = {}
    original_analyzer = activity_analysis.analyze_activity

    def capture_coordinates(analyzer_samples: list[dict[str, Any]], laps: list[dict[str, Any]] | None = None, **kwargs: Any) -> dict[str, Any]:
        captured["samples"] = analyzer_samples
        captured["laps"] = laps
        return original_analyzer(analyzer_samples, laps, **kwargs)

    monkeypatch.setattr(activity_analysis, "analyze_activity", capture_coordinates)
    inferred_response = client.put(
        f"/api/runs/{run['id']}/streams",
        json={"samples": samples, "laps": []},
    )
    assert inferred_response.status_code == 200, inferred_response.text
    inferred = inferred_response.json()
    assert inferred["run_id"] == run["id"]
    assert inferred["distance_m"] == pytest.approx(1_200)
    assert len(inferred["splits"]) == 2
    assert inferred["splits"][0]["distance_m"] == pytest.approx(1_000)
    assert inferred["splits"][1]["distance_m"] == pytest.approx(200)
    assert "one_kilometer_splits_interpolated" in inferred["quality_flags"]
    assert captured["laps"] is None
    assert captured["samples"][0]["lat"] == samples[0]["latitude"]
    assert captured["samples"][0]["lon"] == samples[0]["longitude"]
    assert captured["samples"][0]["latitude"] == samples[0]["latitude"]
    assert captured["samples"][0]["longitude"] == samples[0]["longitude"]

    run_after_stream = client.get(f"/api/runs/{run['id']}").json()
    assert run_after_stream["stream_available"] is True
    assert run_after_stream["moving_seconds"] == 150
    stored = client.get(f"/api/runs/{run['id']}/streams")
    assert stored.status_code == 200, stored.text
    assert stored.json()["samples"] == samples
    assert stored.json()["laps"] == []

    explicit_laps = [
        {
            "start_seconds": 0,
            "end_seconds": 120,
            "distance_m": 1_000,
            "elapsed_seconds": 120,
            "moving_seconds": 120,
            "label": "first kilometer",
        }
    ]
    explicit_response = client.put(
        f"/api/runs/{run['id']}/streams",
        json={"samples": samples, "laps": explicit_laps},
    )
    assert explicit_response.status_code == 200, explicit_response.text
    explicit = explicit_response.json()
    assert explicit["splits"][0]["explicit"] is True
    assert explicit["splits"][0]["label"] == "first kilometer"
    assert "explicit_laps_used" in explicit["quality_flags"]


@pytest.fixture()
def personal_client(tmp_path: Any) -> TestClient:
    settings = Settings(
        mode="personal",
        database_url=f"sqlite:///{(tmp_path / 'personal-test.db').as_posix()}",
        cors_origins=("https://runwise.example",),
        max_import_bytes=1024 * 1024,
        max_import_rows=100,
        frontend_url="https://runwise.example",
        public_base_url="https://runwise.example",
        allowed_hosts=("testserver",),
        token_encryption_key=Fernet.generate_key().decode("ascii"),
        google_client_id="test-google-client",
        google_client_secret="test-google-secret",
        google_redirect_uri="https://runwise.example/api/integrations/google-health/callback",
        google_jwks_url="https://www.googleapis.com/oauth2/v3/certs",
        google_id_token_issuer="https://accounts.google.com",
        owner_google_emails=("owner@example.com",),
    )
    with TestClient(create_app(settings), base_url="https://testserver") as test_client:
        yield test_client


def test_personal_mode_uses_owner_bound_cookie_and_origin_guard(personal_client: TestClient) -> None:
    assert personal_client.get("/api/me").status_code == 401
    assert personal_client.get("/api/runs").status_code == 401

    owner = UUID(personal_client.app.state.settings.personal_owner_id)
    forged_owner = CurrentUser(subject=UUID("00000000-0000-0000-0000-000000000002"), role="personal")
    personal_client.cookies.set(
        PERSONAL_SESSION_COOKIE,
        issue_personal_session(forged_owner, personal_client.app.state.settings),
    )
    assert personal_client.get("/api/me").status_code == 401

    user = CurrentUser(
        subject=owner,
        email="owner@example.com",
        role="personal",
        claims={"sub": "allowed-google-sub", "email_verified": True},
    )
    personal_client.cookies.set(
        PERSONAL_SESSION_COOKIE,
        issue_personal_session(user, personal_client.app.state.settings),
    )
    me = personal_client.get("/api/me")
    assert me.status_code == 200, me.text
    assert me.json() == {
        "id": str(owner),
        "user_id": str(owner),
        "email": "owner@example.com",
        "role": "personal",
    }

    payload = _run_payload(title="Allowed origin")
    allowed = personal_client.post("/api/runs", json=payload, headers={"Origin": "https://runwise.example"})
    assert allowed.status_code == 201, allowed.text
    blocked = personal_client.post("/api/runs", json=_run_payload(title="Blocked origin"), headers={"Origin": "https://evil.example"})
    assert blocked.status_code == 403


def test_oauth_callback_requires_initiating_browser_and_rejects_replay(personal_client: TestClient) -> None:
    from urllib.parse import parse_qs, urlsplit
    from app.google_routes import GOOGLE_CALLBACK_PATH, OAUTH_STATE_COOKIE

    started = personal_client.post(
        '/api/integrations/google-health/connect',
        headers={'Origin': 'https://runwise.example'},
    )
    assert started.status_code == 200
    state = parse_qs(urlsplit(started.json()['url']).query)['state'][0]
    binding = personal_client.cookies.get(OAUTH_STATE_COOKIE)
    assert binding == state
    personal_client.cookies.clear()
    callback = f'{GOOGLE_CALLBACK_PATH}?state={state}&error=access_denied'
    unbound = personal_client.get(callback, follow_redirects=False)
    assert 'invalid_state' in unbound.headers['location']
    personal_client.cookies.set(OAUTH_STATE_COOKIE, binding, path=GOOGLE_CALLBACK_PATH)
    bound = personal_client.get(callback, follow_redirects=False)
    assert 'authorization_denied' in bound.headers['location']
    personal_client.cookies.set(OAUTH_STATE_COOKIE, binding, path=GOOGLE_CALLBACK_PATH)
    replay = personal_client.get(callback, follow_redirects=False)
    assert 'invalid_state' in replay.headers['location']


def test_personal_cookie_obeys_updated_allowlist(personal_client: TestClient) -> None:
    from dataclasses import replace

    settings = personal_client.app.state.settings
    user = CurrentUser(
        subject=UUID(settings.personal_owner_id), email='owner@example.com',
        role='personal', claims={'sub': 'allowed-google-sub', 'email_verified': True},
    )
    personal_client.cookies.set(PERSONAL_SESSION_COOKIE, issue_personal_session(user, settings))
    assert personal_client.get('/api/me').status_code == 200
    personal_client.app.state.settings = replace(settings, owner_google_emails=('replacement@example.com',))
    assert personal_client.get('/api/me').status_code == 401
