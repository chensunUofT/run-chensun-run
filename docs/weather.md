# Run weather

`GET /api/runs/{id}/weather` adds an estimated hourly weather reading to an
owner's run detail. The endpoint first loads the run and its private
`RunStream` for the authenticated owner, then decompresses the stored samples
and uses the first complete GPS latitude/longitude pair. Coordinates are
rounded to two decimal places before they are sent to Open-Meteo, and are
never returned by this endpoint.

The response always has this shape:

```json
{
  "status": "available",
  "reason": null,
  "temperature_c": 17.4,
  "humidity_percent": 64.0,
  "weather_code": 2,
  "provider": "Open-Meteo",
  "estimated": true
}
```

`status` is either `available` or `unavailable`. An unavailable response uses
`reason` `missing_location` when no valid GPS pair is present,
`provider_unavailable` for a timeout or provider error, and `no_data` when the
stored stream or hourly response has no usable observation. Individual values
may be `null` when Open-Meteo omitted only that field; all three values being
missing produces `no_data`. Weather errors do not change or prevent a run
response.

Open-Meteo's archive endpoint is used for dates older than the recent lookback:

`https://archive-api.open-meteo.com/v1/archive`

The request asks for `temperature_2m`, `relative_humidity_2m`, and
`weather_code` with `timezone=GMT` and the run's UTC date as both
`start_date` and `end_date`. Runs on the current date or within the previous
five UTC calendar days use the forecast endpoint with `past_days=5`:

`https://api.open-meteo.com/v1/forecast`

The returned hourly arrays are matched to the run's UTC start timestamp by the
nearest hour. The service keeps a bounded in-process cache of at most 512
coordinate/date entries. Successful hourly payloads live for 24 hours and
failed provider lookups for 15 minutes. Authorization is checked against the
run before the cache is read, so a cache entry cannot grant access to another
owner's run.

The endpoint uses Open-Meteo's public weather API and does not use Google
Health credentials or health-data tokens. See the
[Open-Meteo historical weather API documentation](https://open-meteo.com/en/docs/historical-weather-api)
for the provider's hourly fields and archive behavior.
