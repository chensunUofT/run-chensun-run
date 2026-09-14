"""Run the additive local/production database migration explicitly."""

from __future__ import annotations

from .config import Settings
from .db import create_engine_and_session
from .migrations import migrate_database


def main() -> None:
    settings = Settings.from_env()
    settings.validate_runtime()
    engine, _ = create_engine_and_session(settings.database_url)
    try:
        cloud_schema = settings.mode == "production" or (
            settings.mode == "personal" and engine.dialect.name == "postgresql"
        )
        migrate_database(engine, owner_id=settings.personal_owner_id, production=cloud_schema)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
