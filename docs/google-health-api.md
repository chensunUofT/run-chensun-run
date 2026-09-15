# Google Health API integration contract

Runwise reads workouts and telemetry from the current Google Health API v4
surface. Google Health is the modern Fitbit and Pixel device data platform;
the integration does not call legacy provider URLs and does not assume that a
previous Fitbit OAuth token can be reused. Google’s migration guidance says
that users must authorize the Google OAuth integration again.

The implementation in `backend/app/google_health.py` is transport-only. It
accepts an access token, returns provider JSON alongside normalized records,
and owns neither token storage nor application routes. The cloud backend is
responsible for obtaining and refreshing tokens, encrypting them, and deciding
which user may use a client.

## Verified setup and OAuth

Create or select a Google Cloud project, enable **Google Health API**, create a
Web Server OAuth client, and register the exact redirect URI used by Runwise.
During testing, add each test account to the OAuth consent screen’s test-user
list. Google Health scopes are Restricted and production use may require the
Google verification and security-review process.

The authorization URL built by `build_authorization_url` is the documented
Google endpoint:

```text
https://accounts.google.com/o/oauth2/v2/auth
```

It carries `response_type=code`, `access_type=offline`, `prompt=consent`, the
Runwise `state`, and a PKCE `code_challenge` with
`code_challenge_method=S256`. The default read scopes are:

```text
https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly
https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly
https://www.googleapis.com/auth/googlehealth.location.readonly
```

The activity scope covers the `exercise` and `distance` data types. The health
metrics scope covers `heart-rate`. The location scope is needed for GPS route
export. The callback exchanges the code at:

```text
https://oauth2.googleapis.com/token
```

`exchange_code` accepts the PKCE verifier and optional confidential-client
secret. `refresh_token` uses the same token endpoint and returns the provider’s
JSON response. Refresh tokens must be kept in the cloud backend’s encrypted
credential store; this module never persists them.

## REST data contract

The fixed service endpoint is:

```text
https://health.googleapis.com/v4
```

Runwise calls the documented list method:

```http
GET /users/me/dataTypes/{dataType}/dataPoints
Authorization: Bearer <access-token>
Accept: application/json
```

Supported query parameters are `pageSize`, `pageToken`, and the documented AIP
160 `filter` expression. The client does not accept an arbitrary URL. Google
orders list results by interval or sample start time in descending order.

The exercise session page uses:

```text
filter=exercise.interval.civil_start_time >= "2026-04-20"
  AND exercise.interval.civil_start_time < "2026-04-21"
```

The continuous telemetry pages use physical timestamps:

```text
filter=heart_rate.sample_time.physical_time >= "2026-04-20T08:00:00Z"
  AND heart_rate.sample_time.physical_time < "2026-04-20T09:00:00Z"
```

```text
filter=distance.interval.start_time >= "2026-04-20T08:00:00Z"
  AND distance.interval.start_time < "2026-04-20T09:00:00Z"
```

The API’s `startTime`/`endTime` examples on some data-type guide pages do not
replace the v4 list reference’s `filter` contract. Runwise emits the v4 list
reference’s filter fields so pagination and date restrictions are explicit.

An exercise data point contains the fields relevant to Runwise:

```json
{
  "name": "users/me/dataTypes/exercise/dataPoints/run-123",
  "dataSource": {"recordingMethod": "ACTIVELY_MEASURED"},
  "exercise": {
    "interval": {
      "startTime": "2026-04-20T08:00:00Z",
      "endTime": "2026-04-20T08:35:00Z",
      "startUtcOffset": "0s",
      "endUtcOffset": "0s"
    },
    "exerciseType": "RUNNING",
    "activeDuration": "1800s",
    "metricsSummary": {
      "distanceMillimeters": 5000000.0,
      "averageHeartRateBeatsPerMinute": "148",
      "averagePaceSecondsPerMeter": 0.36
    },
    "exerciseMetadata": {"hasGps": true},
    "exerciseEvents": [],
    "splitSummaries": []
  }
}
```

The API serializes 64-bit integer fields such as heart rate and steps as JSON
strings. A heart-rate point has
`heartRate.sampleTime.physicalTime`, `heartRate.beatsPerMinute`, and optional
`heartRate.metadata` such as `motionContext` and `sensorLocation`. A distance
point has `distance.interval.startTime`, `distance.interval.endTime`, and
`distance.millimeters`.

For an exercise with GPS, the client calls the documented custom method:

```http
GET /users/me/dataTypes/exercise/dataPoints/{exercise-id}:exportExerciseTcx?alt=media
```

Google returns raw TCX XML when `alt=media` is present. The client preserves
the raw bytes and parses `Trackpoint` elements into timestamp, latitude,
longitude, altitude, and cumulative distance fields. The method also accepts
the JSON `tcxData` envelope documented for clients that omit media mode, but
Runwise always requests media mode. When TCX contains no points, matching-source
distance intervals and heart-rate samples can supply coarse telemetry. This
fallback never fabricates GPS coordinates or altitude.

## Query-window compatibility and workout hierarchy

The 2026-09-15 real-account audit returned the same exercise IDs in a broad
history scan and in monthly queries. The broad response
classified many records as generic `WORKOUT`, whereas monthly responses supplied
their `RUNNING` type and richer metrics. This is observed query-range-dependent
provider behavior, not proof of a particular API version migration. Runwise now
queries calendar-month windows, paginates each window, and deduplicates by the
provider ID. Pure `WALKING` sessions are excluded. Generic workouts require
affirmative running evidence, such as a Running TCX activity.

The REST resource is an `exercise` session. `exerciseType` is its activity
classification; Runwise's `run` is the application's filtered representation.
TCX `Activity` and `Lap` are XML containers, not guaranteed custom-workout
Run/Walk intervals. The official REST schema provides `splitSummaries` with
distance, duration, or manual boundaries and `exerciseEvents` for pauses.
It does not promise the phone app's entire custom-workout hierarchy.

The checked real TCX exports contained either one Activity and one Lap,
or one Activity without laps or points. Thus these exports did not
provide the phone-visible custom Run/Walk sequence. Retained provider splits are
kept distinct from algorithmically inferred training segments.

Distance telemetry records are interval increments, not cumulative odometer
readings. Fitbit and Health Connect can overlap for the same period. The
fallback selects one matching platform/device source, removes duplicates,
rejects ambiguous overlaps, and records provenance and missing intervals.
It cannot resolve short traffic-light stops from minute-level measurements.
The app conservatively retains unknown time and labels coarse estimates.

## Pagination, history, and failures

For `exercise` (and `sleep`) Google documents a default and maximum page size
of 25. Other list data types have a maximum page size of 10,000. Every page
may contain `nextPageToken`; callers must pass that token to retrieve the next
page. `fetch_activity` follows pages up to its explicit `max_pages` bound and
records a `page_limit_exceeded` error if that bound is reached.

Google documents that historical data may be queried as far back as it has
been recorded and does not promise a universal retention start date. A
no-bound query therefore reports the oldest record actually returned in
`coverage.oldest_returned`; it never claims that no older record exists.
Historical retrieval is still subject to shared rate limits. The client uses a
bounded exponential retry for 429 and transient 5xx responses, honors a
numeric `Retry-After` value when present, retries timeouts, and surfaces a
typed error after the retry budget. `fetch_activity` keeps successful pages and
raw provider payloads when another stream fails. Its
`coverage.complete` flag is false whenever a stream, page, or requested route
could not be completed.

`fetch_activity_sessions` and `fetch_supporting_samples` each return one page:

```text
{
  data_type,
  sessions|samples,        # normalized records; each has raw provider JSON
  next_page_token,
  complete,
  raw                      # complete provider page, including token
}
```

`fetch_activity` combines session pages, heart-rate and distance pages, and
optional TCX routes into `{sessions, samples, routes, coverage, errors, raw}`.
The normalized session includes `source_id`, `started_at`, `ended_at`,
`duration_seconds`, `active_duration_seconds`, `distance_m`, `avg_hr`,
`activity_type`, `has_gps`, normalized provider splits/events, and `raw`. No
Strava integration, write API, webhook subscriber, identity mapping, or
provider-side reconciliation is guessed or implemented in this module; those
are separate integration decisions and only documented endpoints are used.

## Pace analysis boundary

`backend/app/activity_analysis.py` consumes normalized samples with
`elapsed_seconds` and cumulative `distance_m`. It reports raw elapsed pace
(`raw_pace_seconds`), inferred moving pace (`moving_pace_seconds`), and
per-split moving pace (`pace_seconds`) separately. Moving time is a
configurable speed-threshold estimate with short-stop hysteresis. Gaps over 30
seconds and implausible GPS jumps are marked uncertain and excluded from
moving/stopped inference; the original provider distance remains visible in
`raw_distance_m`. One-kilometer split boundaries are linearly interpolated and
the final partial split is included. Supplied provider laps are preserved after
validation. These are deterministic Runwise estimates, not an exact Strava
moving-time result.

## Official sources

- [About the Google Health API](https://developers.google.com/health/about)
- [Set up Google Cloud and OAuth](https://developers.google.com/health/setup)
- [Scopes](https://developers.google.com/health/scopes)
- [REST API reference](https://developers.google.com/health/reference/rest)
- [Data points list method](https://developers.google.com/health/reference/rest/v4/users.dataTypes.dataPoints/list)
- [Data point schemas](https://developers.google.com/health/reference/rest/v4/users.dataTypes.dataPoints)
- [Workout sessions and TCX routes](https://developers.google.com/health/data-types/workouts)
- [TCX export method](https://developers.google.com/health/reference/rest/v4/users.dataTypes.dataPoints/exportExerciseTcx)
- [Historical data and endpoint conventions](https://developers.google.com/health/endpoints)
- [Fitbit migration overview](https://developers.google.com/health/migration)
