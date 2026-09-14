# Personal deployment

The current target is a single-owner app. Render Free serves both the built React frontend and the FastAPI backend at the same HTTPS origin. Supabase Free supplies PostgreSQL only. Separate registration, account management, and Supabase Auth are deferred.

## Owner access

Connecting Google Health also verifies the owner's Google identity. Only the server-configured Google subject or verified email allowlist may establish the app's HttpOnly session cookie. Health-data access still requires Google's read-only OAuth consent. Anonymous visitors cannot read private runs; explicitly created share links expose only their limited snapshots.

Use the same origin for the app and API. This avoids cross-site cookie restrictions. Local development remains on loopback and uses its existing local identity; never expose development mode to the internet.

## Database

Create a Supabase Free project and apply the SQL migration in `supabase/migrations/` after reviewing `supabase/README.md`. The migration creates application-owned owner records independently of Supabase Auth and enables row-level security. Browser database roles have no application-table privileges.

Use a dedicated runtime login with only the `runwise_api` role's privileges. The backend and sync job set the verified owner claim inside every database transaction. Do not give the runtime role superuser or BYPASSRLS. Use the actual Supabase connection/pooler host and TLS. Keep credentials only in private environment settings.

## Render

The checked-in `render.yaml` selects `plan: free`, Python 3.12 and Node 24. The native Python runtime includes Node/npm, so one build installs Python dependencies and compiles the React app. No paid worker, Render database, disk, or cron service is created.

| Environment variable | Value |
|---|---|
| `RUNWISE_MODE` | `personal` |
| `DATABASE_URL` | Dedicated Supabase runtime PostgreSQL URL with TLS |
| `RUNWISE_OWNER_GOOGLE_EMAILS` | Owner's allowed Google email; or use `RUNWISE_OWNER_GOOGLE_SUB` |
| `RUNWISE_FRONTEND_URL` | Actual Render HTTPS origin |
| `RUNWISE_PUBLIC_BASE_URL` | Same Render HTTPS origin |
| `RUNWISE_CORS_ORIGINS` | Same exact origin |
| `RUNWISE_TOKEN_ENCRYPTION_KEY` | Persistent Fernet key |
| `GOOGLE_CLIENT_ID` | Web OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Web OAuth client secret |
| `RUNWISE_GOOGLE_REDIRECT_URI` | Exact registered callback URI |

Render supplies `RENDER_EXTERNAL_HOSTNAME` for the host allowlist. For a custom domain or the external sync job, configure `RUNWISE_ALLOWED_HOSTS` explicitly. Never use wildcard CORS/hosts. Leave `VITE_API_BASE` unset for same-origin requests.

Render's filesystem is ephemeral. Do not use SQLite for cloud persistence. Keep an encrypted database backup separately, together with a securely stored encryption key. Losing the key requires Google reconnection.

## Google configuration

Enable Google Health API in the chosen Cloud project. Configure the test-mode consent screen and add only the owner's account as a test user. Create a Web OAuth client with the exact callback:

```text
https://YOUR-SERVICE.onrender.com/api/integrations/google-health/callback
```

For local testing, also register `http://localhost:8000/api/integrations/google-health/callback`. Store local configuration in ignored `backend/.env`; the Windows start script loads it. Google secrets must never enter the frontend bundle or Git.

See [Google API notes](google-health-api.md) for verified scopes, detailed-data formats, and history limitations. Testing-mode refresh grants can expire; reconnect when required. The app should distinguish provider errors from a successful empty history scan.

## Daily incremental sync

`.github/workflows/sync.yml` supports manual dispatch and a daily 10:23 UTC schedule. Create a GitHub `production` environment with the named secrets and variables. Use the same database credentials and token encryption key as Render. Set repository variable `RUNWISE_SYNC_ENABLED=true` only after a successful manual real-account sync. Leaving it unset disables the job.

This performs real provider work, has a 25-minute limit, and uploads no data artifacts. It is not an uptime guarantee. A run with no connected accounts does not prove provider integration works. GitHub schedules can be delayed or disabled by platform rules; monitor failed runs and data freshness.

[Supabase Free may pause low-activity projects](https://supabase.com/docs/guides/platform/free-project-pausing). [Render Free sleeps after idle periods](https://render.com/docs/free). Neither free tier guarantees permanent availability. A useful sync schedule can maintain fresh data, but must not be presented as a promise of always-on service.

## Verification

Record actual results in `docs/verification.md`: live app/API, anonymous denial, wrong-owner denial, successful Google callback, one real run and its detailed samples, oldest observed history and scan completeness, manual shoe preservation, share revocation/expiry/privacy, and persistence across a redeploy. Configuration files alone are not a completed deployment.
