"""Owner identity constants shared by local adapters and migrations."""

from __future__ import annotations

from uuid import UUID


# A fixed local identity keeps SQLite development deterministic while making
# it impossible to accidentally treat a browser supplied identifier as an
# owner. Production always uses the UUID from a verified Supabase JWT.
DEV_OWNER_ID = "00000000-0000-0000-0000-000000000001"
DEV_OWNER_UUID = UUID(DEV_OWNER_ID)
