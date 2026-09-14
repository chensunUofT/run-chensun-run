# Runwise backend

Run the local API from this directory after installing `requirements.txt`:

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The default is `RUNWISE_MODE=dev` with SQLite at `data/runwise.db`. Set
`DATABASE_URL` to a SQLAlchemy PostgreSQL URL when a PostgreSQL deployment is
ready. `RUNWISE_MODE=production` intentionally fails during startup until
authentication and authorization are implemented. Set `RUNWISE_TIMEZONE` to an
IANA timezone name (the default is `America/New_York`) for local-date stats;
naive input datetimes are interpreted in that zone and stored as UTC.

The default browser origins are `http://localhost:5173` and
`http://127.0.0.1:5173`; override them with a comma-separated
`RUNWISE_CORS_ORIGINS`. Requests that mutate data and carry an unconfigured
`Origin` are rejected, while command-line requests without `Origin` remain
supported. Local data uses the fixed owner UUID
`00000000-0000-0000-0000-000000000001`; production always derives the owner
from a verified Supabase Auth JWT.

Supabase multiuser mode requires `RUNWISE_MODE=production`, a PostgreSQL
`DATABASE_URL`, HTTPS `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, HTTPS
`SUPABASE_JWKS_URL` and `SUPABASE_JWT_ISSUER` (derived from `SUPABASE_URL`
when omitted), `SUPABASE_JWT_AUDIENCE`, HTTPS `RUNWISE_FRONTEND_URL`, HTTPS
`RUNWISE_PUBLIC_BASE_URL`, HTTPS `RUNWISE_GOOGLE_REDIRECT_URI`, explicit
`RUNWISE_CORS_ORIGINS`, explicit `RUNWISE_ALLOWED_HOSTS` (or Render's
`RENDER_EXTERNAL_HOSTNAME`), and a Fernet `RUNWISE_TOKEN_ENCRYPTION_KEY`.

The personal deployment uses `RUNWISE_MODE=personal` and the same PostgreSQL,
HTTPS, CORS, host, and encryption requirements. Set `GOOGLE_CLIENT_ID`,
`GOOGLE_CLIENT_SECRET`, and either `RUNWISE_OWNER_GOOGLE_SUB` or
`RUNWISE_OWNER_GOOGLE_EMAILS` (comma-separated; verified email matches only).
Google's ID-token JWKS defaults to
`https://www.googleapis.com/oauth2/v3/certs`; override it with
`GOOGLE_JWKS_URL` only when needed. Personal OAuth creates a fixed-owner
HttpOnly Fernet session; private APIs return 401 until that session exists.
The publishable Supabase key is safe for the browser; database credentials and
the encryption key belong only in the API environment.

## CSV import

`POST /api/import/csv` accepts UTF-8 CSV with this exact header vocabulary:

```text
source_id,title,started_at,distance_km,duration_seconds,run_type,avg_hr,shoe_id,notes,rpe,source
```

The required columns are `started_at`, `distance_km`, and `duration_seconds`.
All other columns are optional. A missing title becomes `Imported run
YYYY-MM-DD`; a missing source becomes `csv`. Datetimes may include an offset
or `Z`. Imports are limited to 5 MiB and 10,000 rows by default and are
validated before one database transaction. Repeated `source + source_id` rows
are skipped; rows without a provider ID use a normalized content fingerprint.

## API notes

`GET /api/stats?period=week` reports the current local Monday-to-Sunday week;
`period=month` reports the current local calendar month grouped into
Monday-based buckets. `average_pace_seconds` is weighted as total duration
divided by total distance. Add `date=YYYY-MM-DD` to select an explicit local
week or month. `POST /api/demo/seed` is available only in local/test mode and
demo runs are marked with `source: "demo"`.

`GET /api/runs/{id}/analysis` is a deterministic rules summary. It does not
call an LLM and does not make medical claims. `GET /api/integrations` reports
the persisted Google Health connection state; it never pretends OAuth or sync
succeeded. Google OAuth state is short-lived, single-use, PKCE-bound, and
owner-bound. Refresh/access tokens are encrypted with the server Fernet key
and never returned to the browser. Runwise's background command is `python -m
app.sync_job`; it performs bounded paginated provider syncs and exits non-zero
for account failures.

`POST /api/shoes/infer` previews deterministic, rule-based candidate shoes and
accepts `{ "apply": true }` to persist inferred assignments. It skips manual
locks, including an explicit null shoe. Active shoes are eligible for all
historical dates; retired shoes are eligible for historical dates only, and a
purchase date after a run's local date excludes a shoe. `PUT
/api/runs/{id}/streams` stores a gzip-compressed, owner-scoped JSON payload
bounded to 50,000 samples and returns the activity analysis result. New
deployments may move that blob to a private Supabase Storage bucket without
changing the API contract.

`DELETE /api/shoes/{id}` intentionally detaches a run's `shoe_id` while
preserving that run. Existing SQLite databases are migrated additively on
startup; no rows are dropped.
