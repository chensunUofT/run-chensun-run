# Verification — 2026-09-14

## Local application

- Backend suite: 38 tests passed, including cross-browser OAuth rejection, replay rejection, and allowlist revocation; two dependency deprecation warnings remain.
- Final frontend typecheck and production build passed. The optional Supabase SDK is dynamically loaded and pinned in the lockfile.
- FastAPI starts on loopback with the existing SQLite database and private local environment file.
- The built frontend and `/api/health` return HTTP 200 from the same origin at `http://localhost:8000`.
- Browser checks confirm the settings page, Google connection entry, and English/Chinese language switching.
- Provider and activity-analysis tests cover pagination, physical-time filters, real resource names, TCX parsing, elapsed/active duration separation, stops, gaps, and splits.
- Feature regression tests cover shoe purchase-date inference, manual assignment locks, share privacy/expiry/revocation, stream coordinates and laps, and personal owner-session isolation.
- Source and documentation scanning found no Chinese text outside UI translation resources.
- The Render Blueprint passed validation against Render's official JSON schema.

Synthetic tests use isolated temporary databases. The normal local database was backed up before additive migration. No real Google Health data has been imported yet.

## External configuration

- Google Cloud project, Google Health API, testing consent screen, Web OAuth client, and owner test user exist.
- The local callback URI was verified in Google Cloud. Client credentials are saved only in ignored local files.
- Google read-only scope settings were saved after approval. Actual health-data consent and first sync remain pending.
- Supabase Free project and application schema are initialized. Security advisors returned no findings. The dedicated runtime role passed live owner writes, post-commit refresh, cross-owner RLS isolation, and pooled-connection claim reset checks.
- GitHub repository exists. Windows git-remote-https crashed during upload; publication proceeds through the GitHub connector. Remote CI, Render deployment, and scheduled sync activation remain unverified.

## Release gate

Before declaring cloud deployment complete, verify the application with its restricted PostgreSQL runtime role, including owner claims after commit, anonymous access denial, a real Google callback, historical coverage, detailed streams, and persistence after redeployment. Local tests and the frontend build passed after the review fixes; they do not replace these live checks.

Free infrastructure does not guarantee permanent uptime. A scheduled real sync is not evidence of successful provider access until a connected account has completed a sync.
