# Runwise Supabase database

Apply `migrations/0001_owner_scoped_schema.sql` in the Supabase SQL editor or
through the Supabase migration workflow before pointing the API at PostgreSQL.
The migration is additive: it creates the owner registry, private tables,
owner-scoped unique keys, immutable owner triggers, and RLS policies without
dropping rows.

Runwise uses `runwise_api`, a server-only `NOLOGIN` role. The role receives
table and sequence grants only after RLS is enabled. Keep the PostgreSQL
connection string and any service credential in Render's server environment;
never put them in frontend variables or expose them to `anon` or
`authenticated`. Browser roles have no privileges on these tables, including
`google_connections` and `oauth_states`, whose columns contain encrypted OAuth
secrets and short-lived state.

The Render API must set the transaction-local `request.jwt.claim.sub` to the
verified owner UUID when using the RLS-bound `runwise_api` role, or use a
trusted server connection that applies the same owner policy. Personal mode
provisions its configured fixed owner UUID in `runwise_owners`; Supabase JWT
mode provisions one row for each verified subject. Do not insert a guessed
`auth.users` row to satisfy the foreign key: Runwise owns this identity table
because personal Google OAuth is separate from Supabase Auth.

The private Supabase Storage bucket for future large stream payloads should
follow the same owner prefix (`<owner UUID>/runs/<run UUID>/...`) and deny
browser access by default. The current API stores a bounded gzip payload in
`run_streams` and can migrate that payload to Storage later without changing
the owner or public-share policy.

## Runtime login provisioning

Provision the login separately from application schema migrations, keeping its generated password out of migration history:

```sql
create role runwise_personal login inherit nosuperuser nocreatedb nocreaterole nobypassrls;
grant runwise_api to runwise_personal with inherit true;
```

Set the password through a private administrative channel. PostgreSQL 16+ stores inheritance on each membership grant; changing the role-level attribute alone does not update an existing grant. The verified runtime login uses the shared session pooler and TLS. Transaction-local owner claims are reapplied after every commit. Live verification confirmed owner writes, post-commit refresh, cross-owner denial, and cleared claims on pooled connections.
