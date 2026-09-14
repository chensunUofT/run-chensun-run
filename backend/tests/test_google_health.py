from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.google_health import (
    DEFAULT_GOOGLE_HEALTH_SCOPES,
    GOOGLE_AUTHORIZATION_ENDPOINT,
    GOOGLE_HEALTH_BASE_URL,
    GOOGLE_TOKEN_ENDPOINT,
    GoogleHealthClient,
    GoogleHealthRateLimitError,
    GoogleHealthTimeoutError,
    build_authorization_url,
    exchange_code,
    refresh_token,
)


SESSION_POINT = {
    "name": "users/me/dataTypes/exercise/dataPoints/run-123",
    "dataSource": {"recordingMethod": "ACTIVELY_MEASURED"},
    "exercise": {
        "interval": {
            "startTime": "2026-04-20T08:00:00Z",
            "startUtcOffset": "0s",
            "endTime": "2026-04-20T08:35:00Z",
            "endUtcOffset": "0s",
        },
        "exerciseType": "RUNNING",
        "displayName": "Morning Trail Run",
        "activeDuration": "1800s",
        "metricsSummary": {
            "distanceMillimeters": 5_000_000.0,
            "averageSpeedMillimetersPerSecond": 2777.78,
            "averagePaceSecondsPerMeter": 360.0,
            "averageHeartRateBeatsPerMinute": "148",
        },
        "exerciseMetadata": {"hasGps": True},
        "exerciseEvents": [
            {
                "eventTime": "2026-04-20T08:15:00Z",
                "eventUtcOffset": "0s",
                "exerciseEventType": "PAUSE",
            }
        ],
        "splitSummaries": [
            {
                "startTime": "2026-04-20T08:00:00Z",
                "startUtcOffset": "0s",
                "endTime": "2026-04-20T08:15:00Z",
                "endUtcOffset": "0s",
                "activeDuration": "900s",
                "splitType": "DISTANCE",
                "metricsSummary": {"distanceMillimeters": 2_500_000.0},
            }
        ],
    },
}


HEART_RATE_POINT = {
    "name": "users/me/dataTypes/heart-rate/dataPoints/hr-1",
    "dataSource": {"recordingMethod": "ACTIVELY_MEASURED"},
    "heartRate": {
        "sampleTime": {"physicalTime": "2026-04-20T08:00:00Z", "utcOffset": "0s"},
        "beatsPerMinute": "148",
        "metadata": {"motionContext": "ACTIVE", "sensorLocation": "WRIST"},
    },
}


DISTANCE_POINT = {
    "name": "users/me/dataTypes/distance/dataPoints/dist-1",
    "distance": {
        "interval": {
            "startTime": "2026-04-20T08:00:00Z",
            "startUtcOffset": "0s",
            "endTime": "2026-04-20T08:01:00Z",
            "endUtcOffset": "0s",
        },
        "millimeters": "100000",
    },
}


def _client(handler, **kwargs) -> tuple[GoogleHealthClient, httpx.Client]:
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return GoogleHealthClient("access-token", http_client=http_client, backoff_base=0, **kwargs), http_client


def test_authorization_url_requests_verified_scopes_and_pkce() -> None:
    url = build_authorization_url("client-id", "https://runwise.example/oauth/callback", "state-1", "challenge")
    parsed = parse_qs(urlparse(url).query)

    assert url.startswith(f"{GOOGLE_AUTHORIZATION_ENDPOINT}?")
    assert parsed["response_type"] == ["code"]
    assert parsed["access_type"] == ["offline"]
    assert parsed["prompt"] == ["consent"]
    assert parsed["state"] == ["state-1"]
    assert parsed["code_challenge"] == ["challenge"]
    assert parsed["code_challenge_method"] == ["S256"]
    assert set(parsed["scope"][0].split()) == set(DEFAULT_GOOGLE_HEALTH_SCOPES)


def test_oauth_code_exchange_and_refresh_use_google_token_endpoint() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = parse_qs(request.content.decode())
        if body.get("grant_type") == ["authorization_code"]:
            assert body["code_verifier"] == ["verifier"]
            return httpx.Response(200, json={"access_token": "new", "refresh_token": "long", "expires_in": 3600})
        assert body["refresh_token"] == ["long"]
        return httpx.Response(200, json={"access_token": "newer", "expires_in": 3600})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    exchanged = exchange_code(
        "code",
        "client-id",
        "https://runwise.example/callback",
        "secret",
        "verifier",
        http_client=http_client,
    )
    refreshed = refresh_token("long", "client-id", "secret", http_client=http_client)

    assert exchanged["refresh_token"] == "long"
    assert refreshed["access_token"] == "newer"
    assert all(request.url == httpx.URL(GOOGLE_TOKEN_ENDPOINT) for request in requests)
    assert requests[0].headers["content-type"].startswith("application/x-www-form-urlencoded")


def test_activity_session_page_normalizes_schema_and_keeps_raw_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(f"{GOOGLE_HEALTH_BASE_URL}/users/me/dataTypes/exercise/dataPoints").copy_add_param(
            "pageSize", "25"
        ).copy_add_param(
            "filter", 'exercise.interval.civil_start_time >= "2026-04-20"'
        )
        assert request.headers["authorization"] == "Bearer access-token"
        return httpx.Response(200, json={"dataPoints": [SESSION_POINT]})

    client, http_client = _client(handler)
    page = client.fetch_activity_sessions("2026-04-20", None)
    http_client.close()

    session = page["sessions"][0]
    assert page["complete"] is True
    assert page["next_page_token"] is None
    assert session["source_id"].endswith("run-123")
    assert session["activity_type"] == "RUNNING"
    assert session["duration_seconds"] == 2100
    assert session["active_duration_seconds"] == 1800
    assert session["distance_m"] == 5000
    assert session["average_hr_bpm"] == 148
    assert session["has_gps"] is True
    assert session["splits"][0]["distance_m"] == 2500
    assert session["raw"] == SESSION_POINT


def test_supporting_heart_rate_page_uses_sample_filter_and_normalizes_int64() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = dict(request.url.params)
        assert query["pageSize"] == "1000"
        assert query["filter"] == (
            'heart_rate.sample_time.physical_time >= "2026-04-20T08:00:00Z" '
            'AND heart_rate.sample_time.physical_time < "2026-04-20T08:35:00Z"'
        )
        return httpx.Response(200, json={"dataPoints": [HEART_RATE_POINT], "nextPageToken": "next"})

    client, http_client = _client(handler)
    page = client.fetch_supporting_samples(
        "heart-rate",
        datetime(2026, 4, 20, 8, tzinfo=timezone.utc),
        datetime(2026, 4, 20, 8, 35, tzinfo=timezone.utc),
    )
    http_client.close()

    assert page["complete"] is False
    assert page["next_page_token"] == "next"
    assert page["samples"][0]["heart_rate_bpm"] == 148
    assert page["samples"][0]["metadata"]["sensorLocation"] == "WRIST"
    assert page["samples"][0]["raw"] == HEART_RATE_POINT


def test_distance_page_uses_verified_interval_schema() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["filter"].startswith("distance.interval.start_time >=")
        return httpx.Response(200, json={"dataPoints": [DISTANCE_POINT]})

    client, http_client = _client(handler)
    page = client.fetch_supporting_samples(
        "distance",
        "2026-04-20T08:00:00Z",
        "2026-04-20T08:35:00Z",
    )
    http_client.close()

    assert page["samples"][0]["distance_m"] == 100
    assert page["samples"][0]["elapsed_seconds"] == 60


@pytest.mark.parametrize("resource_user", ["me", "abcd1234"])
def test_route_export_appends_alt_media_and_parses_tcx_trackpoints(resource_user: str) -> None:
    tcx = b'''<?xml version="1.0" encoding="UTF-8"?>
    <TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">
      <Activities><Activity Sport="Running"><Lap StartTime="2026-04-20T08:00:00Z">
        <Track><Trackpoint><Time>2026-04-20T08:00:00Z</Time><Position>
          <LatitudeDegrees>37.7749</LatitudeDegrees><LongitudeDegrees>-122.4194</LongitudeDegrees>
        </Position><AltitudeMeters>15.0</AltitudeMeters><DistanceMeters>0.0</DistanceMeters></Trackpoint></Track>
      </Lap></Activity></Activities>
    </TrainingCenterDatabase>'''

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(
            f"{GOOGLE_HEALTH_BASE_URL}/users/me/dataTypes/exercise/dataPoints/run-123:exportExerciseTcx?alt=media"
        )
        return httpx.Response(200, content=tcx, headers={"content-type": "application/tcx+xml"})

    client, http_client = _client(handler)
    route = client.export_exercise_tcx(f"users/{resource_user}/dataTypes/exercise/dataPoints/run-123")
    http_client.close()

    assert route["content_type"] == "application/tcx+xml"
    assert route["points"][0]["lat"] == pytest.approx(37.7749)
    assert route["points"][0]["lon"] == pytest.approx(-122.4194)
    assert route["raw"] == tcx


@pytest.mark.parametrize("data_type", ["heart-rate", "distance"])
def test_physical_sample_filters_use_rfc3339_for_date_bounds(data_type: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        expression = request.url.params["filter"]
        assert '"2026-04-20T00:00:00Z"' in expression
        assert '"2026-04-21T00:00:00Z"' in expression
        return httpx.Response(200, json={"dataPoints": []})

    client, http_client = _client(handler)
    client.fetch_supporting_samples(data_type, start_time=date(2026, 4, 20), end_time="2026-04-21")
    http_client.close()


def test_429_is_retried_then_exposed_with_status_and_retry_after() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={"error": "quota"}, headers={"Retry-After": "0"})

    client, http_client = _client(handler, max_retries=1, sleep=lambda _: None)
    with pytest.raises(GoogleHealthRateLimitError) as error:
        client.fetch_activity_sessions()
    http_client.close()

    assert calls == 2
    assert error.value.status_code == 429
    assert error.value.retry_after == 0


def test_timeout_retries_then_raises_typed_error() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("slow", request=request)

    client, http_client = _client(handler, max_retries=1, sleep=lambda _: None)
    with pytest.raises(GoogleHealthTimeoutError):
        client.fetch_activity_sessions()
    http_client.close()
    assert calls == 2


def test_aggregate_keeps_partial_sessions_and_marks_coverage_on_stream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/exercise/" in str(request.url):
            return httpx.Response(200, json={"dataPoints": [SESSION_POINT]})
        return httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED"}})

    client, http_client = _client(handler, max_retries=0)
    result = client.fetch_activity(include_samples=True)
    http_client.close()

    assert len(result["sessions"]) == 1
    assert result["coverage"]["sessions_complete"] is True
    assert result["coverage"]["samples_complete"] is False
    assert result["coverage"]["complete"] is False
    assert result["errors"]
    assert result["raw"]["sessions"]

def test_missing_active_duration_remains_unknown() -> None:
    record = deepcopy(SESSION_POINT)
    record['exercise'].pop('activeDuration')
    client, http_client = _client(lambda _: httpx.Response(200, json={'dataPoints': [record]}))
    session = client.fetch_activity_sessions()['sessions'][0]
    http_client.close()
    assert session['active_duration_seconds'] is None
    assert session['duration_seconds'] == session['elapsed_seconds'] == 2100
