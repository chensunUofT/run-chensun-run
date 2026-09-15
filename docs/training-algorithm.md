# Runwise training algorithm

Runwise coaching is a persisted, deterministic training helper. It uses the
athlete's local run history and the race goal supplied through the API. It does
not call an LLM, request data from another service, estimate VO2max, or turn an
absence of measurements into a readiness score. Every generated workout can be
recreated from the goal, the schedule, and the run rows that existed when the
plan was generated.

## API and stored state

`GET /api/coaching` returns one owner-scoped object:

```json
{
  "goal": {
    "race_date": "2026-11-08",
    "distance_km": 21.0975,
    "target_seconds": 7200,
    "weekly_schedule": [
      {"weekday": 1, "run_type": "easy"},
      {"weekday": 3, "run_type": "quality"},
      {"weekday": 5, "run_type": "easy"},
      {"weekday": 6, "run_type": "long"}
    ]
  },
  "prediction": null,
  "sessions": [],
  "paces": {"easy": null, "tempo": null, "interval": null, "long": null}
}
```

The normal goal input may be sent directly or inside a `goal` object. A race
date must be after the current date in `RUNWISE_TIMEZONE` and at most one year
away. Distance and target time must be positive. The schedule contains two to
six unique weekdays, uses only `easy`, `quality`, and `long`, and has at most
one quality day. If omitted, it is Tuesday easy, Thursday quality, Saturday
easy, and Sunday long. A session edit accepts `easy`, `quality`, `long`, or
`race`; sending `run_type: "rest"` removes that training day from the planned
workload. The API retains a manually edited rest marker with zero distance and
zero pace so it remains visible for editing and a later regeneration does not
silently recreate the removed day. Race day itself is always generated, even
when its weekday is absent from the recurring schedule, and cannot be removed.

The feature uses these owner-scoped SQLAlchemy tables on the existing `Base`:

| Table | Columns | Constraints and indexes |
| --- | --- | --- |
| `coaching_goals` | `id` integer primary key, `owner_id` owner UUID, `race_date` date, `distance_km` float, `target_seconds` integer, `created_at` and `updated_at` UTC timestamps | Unique `owner_id`; owner index |
| `coaching_schedules` | `id` integer primary key, `owner_id`, `goal_id` foreign key, `weekday` integer, `run_type` string | Unique `(owner_id, goal_id, weekday)`; owner/goal index; goal delete cascade |
| `coaching_sessions` | `id` integer primary key, `owner_id`, `goal_id` foreign key, `date`, `run_type`, `distance_km`, `target_pace_seconds`, `description`, nullable `completed_run_id` foreign key to `runs`, `manually_edited`, `created_at`, `updated_at` | Unique `(owner_id, goal_id, date)`; owner/goal/date index; owner/completed-run index; goal delete cascade |

The deployment migration should preserve the same table and column names,
install owner policies for all three tables, and enforce that private queries
are filtered by the verified owner claim. The API never accepts an owner ID in
the request body or query string.

## Evidence and prediction

Only runs whose local date is today or earlier, whose timestamp is not in the
future, and whose source is not `demo`, `sample`, `example`, `fixture`, or
`seed` are evidence. The history window is the most recent 180 days.

The prediction uses the Riegel-style relationship

`T2 = T1 * (D2 / D1)^1.06`.

A recent race run is preferred and uses its elapsed finish time. With no race,
the reference is the fastest plausible sustained run of at least 3 km in the
window, using moving time when the provider supplied a positive moving-time
value. A plausible pace is between 2:00 and 20:00 per kilometre; interval-like
workouts and sub-3 km snippets are excluded from this fallback. If no sustained
run meets that filter, prediction is `null` rather than a fabricated estimate.
The result reports
the projected seconds, a broad uncertainty interval, the reference run ID,
and the method string. Race evidence is labelled `high`; an ordinary run is
always labelled `low`. The labels describe evidence quality, not a medical or
physiological assessment.

When there is no eligible evidence, `prediction` is `null`. Paces still have
all four keys and are derived from the goal's target pace as a minimal,
conservative starting baseline. Generated descriptions say that history is
limited so an athlete can adjust the starter plan rather than treating the
target as demonstrated ability.

## Training paces

For each eligible recent run, Runwise converts its time to an equivalent pace
at the goal distance using the same exponent. The median of those equivalent
paces is the current evidence pace. The target race pace is the requested
target time divided by goal distance. To avoid forcing an implausibly quick
goal, the effective race pace is the slower of the target and a 5%-faster
bridge from current evidence (paces are seconds per kilometre, so the slower
bound is the larger number).

The returned ranges are simple, transparent offsets from that evidence and
effective race pace:

| Pace | Calculation before rounding |
| --- | --- |
| `easy` | slower of evidence × 1.18 and effective race × 1.14 |
| `tempo` | slower of evidence × 1.05 and effective race × 1.04 |
| `interval` | slower of evidence × 0.94 and effective race × 0.96 |
| `long` | slower of evidence × 1.15 and effective race × 1.12 |

These are planning guides. Terrain, weather, fatigue, and health can make a
slower pace appropriate.

## Plan volume and workout text

The recent eight weeks are grouped by local Monday. Their median weekly
distance is the starting baseline, bounded to at least 8 km and at most
160 km. With no history, the plan uses a minimal baseline of
`min(25 km, max(8 km, 0.55 × goal distance))`, which keeps a marathon goal from
implicitly requiring marathon-level training on its first week.

The build rises gradually toward 1.30 times a historical baseline or 1.18
times a no-history baseline. Every fourth build week is a 22% deload. The last
two weeks taper when the plan is at least six weeks long (one week for shorter
plans), with approximately 70% and 45% of peak non-race volume. A one-week plan
is treated as taper only. The race distance is kept as the race-day session and
is excluded when evaluating the taper's ordinary training volume.

Weekly volume is allocated around the selected schedule. A long run is capped
at a race-distance class cap (about 12 km for a 5 km goal, 16 km for 10 km,
22 km for a half marathon, and 32 km for a marathon) and at 45% of the current
baseline, with a small, bounded build over time. Quality is capped at 45% of
race distance and uses no more than one quality slot per week. Remaining
distance is spread over easy slots, each with a 2 km floor. The schedule type
remains `quality`; its pace alternates between the returned tempo and interval
paces. Prescriptions scale to the allocated distance: a short quality day uses
brief repeats, while a sufficiently long day may say “Warm up 10 min; 3 x 5
min at tempo pace with 2 min easy recovery; cool down 10 min.” Every
description tells the athlete to stay within the planned distance. Taper
quality is shorter and has full recovery.

`POST /api/coaching/generate` deletes only future, unedited sessions and
rebuilds them. Past sessions and all manually edited sessions retain their ID,
date, type, distance, pace, and description. `PUT /api/coaching/goal` applies
the same preservation rule while updating the goal and recurring schedule.

## Completion matching

Completion is derived from real runs on the same local calendar date. A run is
used at most once. When more than one run is available, the nearest distance
to the planned session wins, with a conservative 35% difference (or 1.5 km)
limit so a short unrelated jog does not complete a long workout. Ties use
duration proximity and then run ID. Missing matches remain `null`; the API
does not fabricate completion from a nearby date, a demo row, a sample row, or
a future timestamp.

## Design references

The pace categories follow the general idea that current or estimated race
performance can anchor training pace guidance, as described by [V.O2's pace
calculator](https://news.vdoto2.com/2023/04/pace-calculator/). The plan's easy,
quality, long-run, recovery, and taper structure is a custom heuristic
informed by the [Boston Athletic Association half marathon training
guidance](https://www.baa.org/races/boston-half/info-for-athletes/boston-half-training/).
Runwise does not reproduce either source's proprietary plan or claim that
these heuristics are a substitute for individualized coaching or medical
advice.
