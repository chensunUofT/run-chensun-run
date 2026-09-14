"""Additive database migration helpers.

The project deliberately has no destructive migration step. New local
databases are created from SQLAlchemy metadata; an existing SQLite database
gets missing columns and tables added in place, with legacy rows assigned to
the fixed local owner. Production databases should run the checked-in
Supabase SQL migration before the API starts.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from .db import Base
from .owner import DEV_OWNER_ID


_MISSING_COLUMNS: dict[str, dict[str, str]] = {
    "shoes": {
        "owner_id": "VARCHAR(36) NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001'",
        "purchase_date": "DATE",
        "image_url": "VARCHAR(2048)",
        "rules": "JSON NOT NULL DEFAULT '{}'",
    },
    "runs": {
        "owner_id": "VARCHAR(36) NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001'",
        "shoe_assignment": "VARCHAR(20) NOT NULL DEFAULT 'unassigned'",
        "shoe_confidence": "FLOAT",
        "shoe_reason": "VARCHAR(500)",
        "moving_seconds": "INTEGER",
        "stream_available": "BOOLEAN NOT NULL DEFAULT 0",
    },
    "checkins": {
        # The old schema used date as its primary key. This nullable id is an
        # additive compatibility column; fresh databases use the integer id
        # declared by the current model.
        "id": "INTEGER",
        "owner_id": "VARCHAR(36) NOT NULL DEFAULT '00000000-0000-0000-0000-000000000001'",
    },
    "oauth_states": {
        "code_verifier_encrypted": "TEXT",
    },
}


def _quote_identifier(identifier: str) -> str:
    # All callers use constants above; retaining this helper documents that
    # identifiers are never interpolated from a request.
    return '"' + identifier.replace('"', '""') + '"'


def migrate_database(engine: Engine, *, owner_id: str = DEV_OWNER_ID, production: bool = False) -> None:
    """Apply safe additive migrations and create newly introduced tables.

    A production database with an old table lacking ``owner_id`` is rejected
    because assigning those rows to a guessed account would be unsafe. Local
    and test databases can safely backfill their historical rows to the fixed
    development owner.
    """

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    if production:
        # Production schema creation belongs to the reviewed Supabase SQL
        # migration, which also installs RLS and grants.  Refuse to silently
        # create an unprotected schema through SQLAlchemy if that migration has
        # not been applied yet.
        required_tables = {
            "runwise_owners",
            "shoes",
            "runs",
            "checkins",
            "run_streams",
            "shares",
            "google_connections",
            "oauth_states",
        }
        missing_tables = sorted(required_tables - existing_tables)
        if missing_tables:
            raise RuntimeError(
                "production database must be initialized with the Supabase migration; "
                f"missing tables: {', '.join(missing_tables)}"
            )
        missing_owner_columns = [
            table
            for table in sorted(required_tables)
            if "owner_id" not in {item["name"] for item in inspector.get_columns(table)}
        ]
        if missing_owner_columns:
            raise RuntimeError(
                "production migration requires owner-scoped tables; missing owner_id on "
                + ", ".join(missing_owner_columns)
            )
        # Existing cloud tables are intentionally left untouched here.  Apply
        # reviewed schema changes through Supabase migrations so RLS and grants
        # change atomically with any column/index additions.
        return

    with engine.begin() as connection:
        for table, columns in _MISSING_COLUMNS.items():
            if table not in existing_tables:
                continue
            present = {item["name"] for item in inspect(connection).get_columns(table)}
            for name, sql_type in columns.items():
                if name in present:
                    continue
                if production and name == "owner_id":
                    raise RuntimeError(f"production migration cannot infer owner_id for {table}")
                # SQLite and PostgreSQL both support this form for the simple
                # scalar columns above. The default is only used for legacy
                # local rows; API writes always set owner_id explicitly.
                connection.execute(
                    text(
                        f"ALTER TABLE {_quote_identifier(table)} ADD COLUMN "
                        f"{_quote_identifier(name)} {sql_type}"
                    )
                )
    # Local SQLite needs metadata to create newly introduced tables.  Cloud
    # schemas are created by the reviewed Supabase migration; avoid silently
    # creating tables without its RLS, grants, and owner policies.
    if not production:
        Base.metadata.create_all(engine)

    # Older SQLite databases have a nullable/default owner column after ALTER;
    # explicitly backfill any nulls without touching user data. PostgreSQL is
    # expected to arrive with complete owner values from its migration.
    if not production:
        with engine.begin() as connection:
            for table in ("shoes", "runs", "checkins", "run_streams", "shares", "google_connections", "oauth_states"):
                if table not in set(inspect(connection).get_table_names()):
                    continue
                columns = {item["name"] for item in inspect(connection).get_columns(table)}
                if "owner_id" in columns:
                    connection.execute(
                        text(
                            f"UPDATE {_quote_identifier(table)} SET \"owner_id\" = :owner "
                            "WHERE \"owner_id\" IS NULL"
                        ),
                        {"owner": owner_id},
                    )
            # Before owner scoping, SQLite used ``date`` as the check-in
            # primary key.  The current model uses an integer id so that the
            # owner/date uniqueness can be represented without changing the
            # user's historical date values.  SQLite exposes the stable rowid
            # for that legacy table; populate the additive id column once so
            # ORM refreshes and future updates remain addressable.
            if "checkins" in set(inspect(connection).get_table_names()):
                checkin_columns = {item["name"] for item in inspect(connection).get_columns("checkins")}
                if "id" in checkin_columns:
                    connection.execute(text('UPDATE "checkins" SET "id" = rowid WHERE "id" IS NULL'))
