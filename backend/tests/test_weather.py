from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app import weather
from app.auth import get_current_owner


def _create_run(client, started_at: datetime) -> int:
    response = client.post(
        "/api/runs",
        json={
            "title": "Weather test run",
            "started_at": started_at.isoformat(),
            "distance_km": 5,
            "duration_seconds": 1_800,
        },
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _put_stream(client, run_id: int, latitude: float | None, longitude: float | None) -> None:
    response = client.put(
        f"/api/runs/{run_id}/streams",
        json={
            "samples": [
                {
                    "elapsed_seconds": 0,
                    "distance_m": 0,
                    "latitude": latitude,
                    "longitude": longitude,
                }
            ],
            "laps": [],
        },
    )
    assert response.status_code == 200, response.text


def _hourly_payload(times: list[str], temperatures: list[Any], humidities: list[Any], codes: list[Any]) -> dict[str, Any]:
    return {
        "hourly": {
            "time": times,
            "temperature_2m": temperatures,
            "relative_humidity_2m": humidities,
            "weather_code": codes,
        }
    }


def test_missing_location_and_zero_coordinates(client, monkeypatch):
    calls: list[tuple[str, dict[str, Any], float]] = []

    def fake_get(url: str, *, params: dict[str, Any], timeout: float):
        calls.append((url, params, timeout))
        return httpx.Response(
            200,
            json=_hourly_payload(
                ["2020-01-01T00:00"],
                [4.5],
                [72],
                [0],
            ),
        )

    monkeypatch.setattr(weather.httpx, "get", fake_get)
    run_id = _create_run(client, datetime(2020, 1, 1, 0, 10, tzinfo=timezone.utc))
    _put_stream(client, run_id, 0.0, 0.0)

    response = client.get(f"/api/runs/{run_id}/weather")
    assert response.status_code == 200
    assert response.json() == {
        "status": "available",
        "reason": None,
        "temperature_c": 4.5,
        "humidity_percent": 72.0,
        "weather_code": 0,
        "provider": "Open-Meteo",
        "estimated": True,
    }
    assert set(response.json()) == {
        "status",
        "reason",
        "temperature_c",
        "humidity_percent",
        "weather_code",
        "provider",
        "estimated",
    }
    assert calls[0][0] == weather.ARCHIVE_URL
    assert calls[0][1]["latitude"] == 0.0
    assert calls[0][1]["longitude"] == 0.0
    assert calls[0][1]["timezone"] == "GMT"
    assert calls[0][2] <= 12

    no_location_id = _create_run(client, datetime(2020, 1, 2, 0, 10, tzinfo=timezone.utc))
    _put_stream(client, no_location_id, None, None)
    no_location = client.get(f"/api/runs/{no_location_id}/weather")
    assert no_location.status_code == 200
    assert no_location.json() == {
        "status": "unavailable",
        "reason": "missing_location",
        "temperature_c": None,
        "humidity_percent": None,
        "weather_code": None,
        "provider": "Open-Meteo",
        "estimated": True,
    }
    assert len(calls) == 1


def test_run_owner_is_checked_before_weather_lookup(client, monkeypatch):
    calls: list[str] = []

    def fake_get(url: str, *, params: dict[str, Any], timeout: float):
        calls.append(url)
        return httpx.Response(200, json=_hourly_payload(["2020-01-01T00:00"], [1], [2], [3]))

    monkeypatch.setattr(weather.httpx, "get", fake_get)
    run_id = _create_run(client, datetime(2020, 1, 1, 0, tzinfo=timezone.utc))
    _put_stream(client, run_id, 41.0, -72.0)

    client.app.dependency_overrides[get_current_owner] = lambda: "00000000-0000-0000-0000-000000000099"
    try:
        response = client.get(f"/api/runs/{run_id}/weather")
    finally:
        client.app.dependency_overrides.pop(get_current_owner, None)

    assert response.status_code == 404
    assert response.json() == {"detail": "Run not found"}
    assert calls == []


def test_utc_offset_uses_nearest_hour_and_preserves_partial_nulls(client, monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []
    run_started = datetime(2020, 1, 2, 15, 31, tzinfo=timezone(timedelta(hours=-5)))
    target_utc = run_started.astimezone(timezone.utc)

    def fake_get(url: str, *, params: dict[str, Any], timeout: float):
        calls.append((url, params))
        return httpx.Response(
            200,
            json=_hourly_payload(
                ["2020-01-02T20:00", "2020-01-02T21:00"],
                [10.0, None],
                [40, 60],
                [1, None],
            ),
        )

    monkeypatch.setattr(weather.httpx, "get", fake_get)
    run_id = _create_run(client, run_started)
    _put_stream(client, run_id, 40.125, -73.996)

    response = client.get(f"/api/runs/{run_id}/weather")
    assert response.status_code == 200
    assert response.json() == {
        "status": "available",
        "reason": None,
        "temperature_c": None,
        "humidity_percent": 60.0,
        "weather_code": None,
        "provider": "Open-Meteo",
        "estimated": True,
    }
    assert calls[0][0] == weather.ARCHIVE_URL
    assert calls[0][1]["start_date"] == target_utc.date().isoformat()
    assert calls[0][1]["end_date"] == target_utc.date().isoformat()
    assert calls[0][1]["latitude"] == 40.12
    assert calls[0][1]["longitude"] == -74.0


def test_recent_runs_use_forecast_past_days(client, monkeypatch):
    now = datetime.now(timezone.utc)
    run_started = now.replace(hour=10, minute=20, second=0, microsecond=0) - timedelta(days=2)
    target = run_started.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_get(url: str, *, params: dict[str, Any], timeout: float):
        calls.append((url, params))
        return httpx.Response(
            200,
            json=_hourly_payload([target.strftime("%Y-%m-%dT%H:%M")], [18], [55], [2]),
        )

    monkeypatch.setattr(weather.httpx, "get", fake_get)
    run_id = _create_run(client, run_started)
    _put_stream(client, run_id, 40.125, -73.996)

    response = client.get(f"/api/runs/{run_id}/weather")
    assert response.status_code == 200
    assert response.json()["status"] == "available"
    assert calls[0][0] == weather.FORECAST_URL
    assert calls[0][1]["past_days"] == 5
    assert calls[0][1]["timezone"] == "GMT"
    assert "start_date" not in calls[0][1]


def test_same_day_cache_keeps_hourly_payload_for_each_run(client, monkeypatch):
    now = datetime.now(timezone.utc)
    first_started = (now - timedelta(days=30)).replace(hour=9, minute=10, second=0, microsecond=0)
    second_started = first_started.replace(hour=15, minute=45)
    calls: list[str] = []

    def fake_get(url: str, *, params: dict[str, Any], timeout: float):
        calls.append(url)
        day = first_started.date().isoformat()
        return httpx.Response(
            200,
            json=_hourly_payload(
                [f"{day}T09:00", f"{day}T10:00", f"{day}T15:00", f"{day}T16:00"],
                [9, 10, 15, 16],
                [50, 51, 55, 56],
                [1, 2, 3, 4],
            ),
        )

    monkeypatch.setattr(weather.httpx, "get", fake_get)
    first_id = _create_run(client, first_started)
    second_id = _create_run(client, second_started)
    _put_stream(client, first_id, 40.125, -73.996)
    _put_stream(client, second_id, 40.124, -73.995)

    first = client.get(f"/api/runs/{first_id}/weather")
    second = client.get(f"/api/runs/{second_id}/weather")
    assert first.status_code == second.status_code == 200
    assert first.json()["temperature_c"] == 9.0
    assert second.json()["temperature_c"] == 16.0
    assert len(calls) == 1
    assert len(client.app.state.weather_cache) == 1


def test_provider_failure_is_safe_and_short_cached(client, monkeypatch):
    now = datetime.now(timezone.utc) - timedelta(days=30)
    calls: list[str] = []

    def fake_get(url: str, *, params: dict[str, Any], timeout: float):
        calls.append(url)
        return httpx.Response(503, json={"error": "unavailable"})

    monkeypatch.setattr(weather.httpx, "get", fake_get)
    run_id = _create_run(client, now)
    _put_stream(client, run_id, 40.125, -73.996)
    first = client.get(f"/api/runs/{run_id}/weather")
    second = client.get(f"/api/runs/{run_id}/weather")

    assert first.status_code == second.status_code == 200
    assert first.json()["reason"] == second.json()["reason"] == "provider_unavailable"
    assert len(calls) == 1
    key, entry = next(iter(client.app.state.weather_cache.items()))
    assert entry.success is False
    assert weather.FAILURE_CACHE_TTL_SECONDS == 15 * 60

