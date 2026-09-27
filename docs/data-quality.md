# Activity identity and fitness estimates

Google Health can return both an actively recorded Pixel Watch workout and
passive phone detections from Google Fit / Health Connect. These have different
provider IDs and can report different distances for overlapping time ranges.
Calendar date and distance alone are not identity keys.

The importer prefers an active recording over substantially contained passive
detections. Separate active workouts and non-overlapping runs remain separate.
Source metadata and interval overlap are required; records with uncertain
provenance must not be silently merged.

Historical duplicate rows use the reversible source marker
`google_health_excluded`. Their IDs, measurements, manual labels, shoes, and
streams remain in the database and backup export. Lists, statistics, shoe
mileage, and coaching exclude the marker. Re-importing its original provider
ID must not reactivate it. A private local audit records the canonical run and
the reason for each quarantine. To restore an audited row, restore its original
source value; do not generate a new activity or dedupe key.

## Coaching

Race/time-trial estimates use elapsed time and the Daniels--Gilbert performance
equation. Training estimates invert the pace oxygen-cost equation using explicit
intensity assumptions by run type. Holding the same training pace for 5 km or
10 km therefore produces the same pace-derived score. Weather and ascent remain
bounded heuristic corrections, not a validated extension of VDOT.

The broad intensity assumptions follow the distinctions described by
[Jack Daniels' training definitions](https://vdoto2.com/learn-more/training-definitions).
They do not measure an athlete's actual oxygen uptake. Interval averages cannot
stand in for work bouts; missing usable work segments produce no equivalent
VDOT or race-time estimate. Inferred segment estimates carry uncertainty.

Coaching prioritizes race evidence, clusters races within 90 days of the latest
eligible race, and keeps up to a year of race history with reduced confidence
for older evidence. Training fallback uses recent history. The displayed VDOT,
equivalent race times, and training pace guide share the same mathematical basis.
The recent-run table exposes individual estimates even when they are not selected
as the race-prediction anchor. Existing manually edited plan sessions are retained.

The September 2026 refresh used renewed Google Health read-only authorization.
It imported seven new running activities with seven route streams, through
September 26. Standalone walking, cycling, and stair-climbing records were not
imported. Strava remains a user-supplied ZIP/CSV import, not an automatic API
connection.

Sites migration is deferred. The current Python/FastAPI application uses a
server process and PostgreSQL connection. Sites requires compatible Worker
output and HTTP-based database access, so moving the complete application
requires a backend port rather than publishing the current Python directory.
