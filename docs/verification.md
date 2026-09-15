# Verification — 2026-09-15

## Application checks

Backend tests cover owner isolation, Google OAuth and pagination, TCX and interval telemetry mapping, moving-time gaps and stops, training classification, manual type/shoe preservation, share privacy, weather failures, and coaching. The suite passed before final explicit-gap integration; remaining new regression and related enrichment tests passed separately. Frontend TypeScript and the Vite production build passed.

The explicit-gap regression verifies that a 60-second record with 20 seconds of observed stops and 30 seconds of missing data retains 40 seconds as moving time. Coarse telemetry retains source provenance and never fabricates GPS or altitude.

Numeric calendar dates prefer the provider's civil date. Run detail and share pace use moving seconds when available. Manual training labels and shoe locks survive repeated synchronization. Synthetic tests use isolated databases, never the personal database.

## Real-account integration

Monthly exercise queries recovered richer running types and metrics than broad queries for the same provider IDs. The importer queries monthly windows and deduplicates across them. Pure walking is excluded. Sessions without valid distance or duration are reported as incomplete. Private per-record audits remain in ignored local files.

TCX, standalone distance intervals, and heart-rate pages were independently read and reconciled. Moving time is a Runwise estimate, not Strava's proprietary result. Phone-visible custom segments were not present in checked exports; provider distance splits remain distinct from inferred intervals.

## Cloud status

The dedicated Supabase runtime role authenticated successfully. The personal dataset, compressed streams, shoes, encrypted Google connection, and coaching records were copied in one transaction. Counts were independently verified and identity sequences reset administratively. Security advisors returned no findings.

Render's baseline build succeeded but startup failed because DATABASE_URL was absent. Server environment configuration is pending explicit approval for the credential destination. The Google OAuth client currently has only the local callback. Cloud readiness, cloud callback, and scheduled sync are not complete.

Historical weather bulk lookup is pending explicit approval for Open-Meteo. Fitness computation supports missing weather and labels incomplete evidence; it does not invent readings.

## Release gate

Before declaring cloud deployment complete, verify HTTPS app/API readiness, anonymous private-data denial, owner Google sign-in, database-backed run detail, and persistence across redeployment. Keep credentials and private health audits out of Git. Free Render and Supabase plans do not guarantee continuous availability.
