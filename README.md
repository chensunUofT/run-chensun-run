# Runwise

A personal running analytics web app: Python/FastAPI for data and analysis, React/TypeScript for the interface, and Supabase PostgreSQL for cloud persistence. The interface supports English and Simplified Chinese. Code and documentation are English; translations live in locale resources.

The repository can run locally without cloud accounts. A configured cloud deployment requires Google Cloud OAuth, Supabase and Render. Deployment templates are not proof that a live deployment or personal-data sync has succeeded; see [verification](docs/verification.md) for tested status.

## Windows quick start

In PowerShell, enter this project directory and run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup.ps1
powershell -ExecutionPolicy Bypass -File scripts/start.ps1
```

Open http://localhost:5173. Press Ctrl+C in the server window to stop. ExecutionPolicy Bypass applies only to that process. Standard installations need Python 3.12+ and Node.js 24 LTS with npm; the scripts also discover bundled Codex runtimes on this machine. Dependency installation needs network access.

Backend startup errors appear in `work/backend-error.log`. Stop a previous Runwise instance before starting another on ports 8000 and 5173.

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check.ps1
```

This runs backend tests and the frontend production build.

## Data and features

- Four main views: runs, weekly/monthly statistics, coach/training plan, and shoe management.
- The run library supports filtered multi-selection and atomic bulk updates of run type or shoes. Explicit shoe choices, including no shoe, remain manual assignments.
- Body status is read from health data or derived from available measurements; no manual daily check-ins. Missing health data remains unavailable.
- Data-source settings and import/export tools are available in a settings dialog.
- Google Health integration for exercise records and available detailed samples. App sign-in and permission to read health data are separate authorizations.
- Data inspection distinguishes the earliest observed record from a completed history scan. Available history depends on the account and provider response.
- Detailed samples and laps support split analysis and estimated moving time. Missing or sparse samples limit accuracy; summary-only activities cannot reveal individual traffic-light stops.
- Shoe purchase dates and configurable rules support reviewable inferred assignments. Manual run-level choices take precedence.
- Share snapshots for a run, week, or month, with expiry and revocation. Sharing is an explicit action; snapshots omit precise routes and private notes.
- English/Chinese interface and a Google-authorized private owner session. Separate registration is deferred.

Run lists and statistics exclude demo/sample records. Run details combine smoothed pace, heart rate, and elevation on a distance-based chart when samples are available. Opening a run queries nearby historical temperature, humidity, and conditions through Open-Meteo using a rounded location. Explicit custom intervals are highlighted only when supplied by the provider; distance laps are not treated as workout intervals.

Coaching uses a saved race goal, historical performance projection, and a configurable weekly schedule to generate an editable plan through race day. The default schedule is Tuesday/Saturday easy, Thursday quality, and Sunday long. Monthly calendars encode distance and type; weekly blocks combine recorded and planned sessions. See [training algorithm](docs/training-algorithm.md) for assumptions and limitations. No LLM service is connected. Other activity platforms and original-file import adapters remain future work.

CSV examples are in [examples/runs.csv](examples/runs.csv). Personal exports, credentials, local databases, dependencies, and scratch files must not be committed. Keep personal files under the ignored `work/` directory or outside the repository.

## Architecture and deployment

| Layer | Local | Cloud target |
|---|---|---|
| Interface | Vite on loopback | Render, same origin as the API |
| Python API | FastAPI on loopback | Render Free web service |
| Database | SQLite | Supabase Free PostgreSQL |
| Owner access | Fixed local identity | Session established by Google Health authorization |
| Sync | Explicit action | Explicit action plus scheduled incremental sync |

Render's filesystem is ephemeral: never deploy the local SQLite database there. Production requires explicit authentication and database configuration. Google tokens belong only on the backend and are encrypted at rest.

Free tiers have quotas and no always-on guarantee. Render can sleep when idle; Supabase may pause low-activity projects. Real scheduled sync keeps data current but cannot guarantee uptime. Separate registration, email signup, and account management are outside the personal-app scope.

See [deployment](docs/deployment.md), [architecture](docs/architecture.md), [shoe catalog](docs/shoe-catalog.md), and [agent orchestration](docs/orchestration.md).

## Repository

| Directory | Purpose |
|---|---|
| `backend/` | FastAPI, persistence, provider clients, analysis, tests |
| `frontend/` | React interface and translations |
| `supabase/` | PostgreSQL migrations and access policies |
| `data/shoe-catalog.json` | Public, source-attributed starter shoe catalog |
| `scripts/` | Windows development tools |
| `.github/workflows/` | Continuous integration and sync automation |
| `docs/` | Architecture, deployment, verification |
| `.codex/`, `.agents/` | Project-local Astra/Luna orchestrator configuration |
