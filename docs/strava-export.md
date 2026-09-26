# Personal Strava exports

Runwise imports files that the owner downloads using Strava's personal export tool. It does not register an API client, request an API token, scrape activities, or automatically synchronize Strava.

## Request the archive

1. Sign in to Strava on the web.
2. Open Settings, then My Account.
3. Under Download your account, choose Get Started, then Request download.
4. Follow the download link sent to the account email. Preparation can take several hours.
5. Keep the ZIP private. Upload it through Runwise's data settings without extracting it. An activities.csv file can also provide activity summaries, but cannot provide the original activity curves by itself.

See [Strava's export instructions](https://support.strava.com/en-us/articles/15401919-how-do-i-export-my-strava-data). Only the owner should access the private Runwise app. Export archives can contain location and health information; do not commit them to Git.

## Why this does not use the API

The [Strava API Policy effective June 1, 2026](https://www.strava.com/legal/api_policy) restricts analytics, persistent storage, and AI use of API data, and limits caching to seven days. Those restrictions conflict with the intended long-term history and coaching architecture. Section 6.6 separately preserves the user's free bulk-export right. This integration consumes the owner's downloaded files and makes no claim that API-derived data can bypass API restrictions.

## Operational limits

An export is a snapshot. New runs require a new export and upload; there is no background Strava sync. Original files and CSV layouts can differ by recording device and export version. The importer reports skipped or unsupported records rather than claiming a complete history. A real personal archive is required to verify the earliest available date and reconcile missing runs.

The importer supports embedded FIT, TCX and GPX activity files, including gzip-compressed originals. It reads Strava's raw metric distance column and exported moving time, excluding walking activities. CSV formats without unambiguous distance units are reported rather than guessed. ZIP uploads are limited to 100 MiB; detailed samples have additional decompression and memory limits. Activities exceeding detail limits still retain valid summaries and produce warnings. FIT decoding has contract tests; compatibility with the owner's actual device files still needs real-export validation.

Existing Google records are retained. Matching must be conservative, and manual shoe and training-type assignments must survive a matching import. Ambiguous matches require review rather than creating an obvious duplicate.

The Supabase database can still pause on the free plan when inactive. File import does not create an always-on guarantee, and database restoration is independent of Render deployment readiness.
