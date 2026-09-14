"""Bounded Google Health synchronization job for a real scheduler.

Run this module from Render Cron or another scheduler. It performs actual
provider work for connected accounts and exits with a non-zero status when an
account fails; it is not a keepalive endpoint or synthetic activity generator.
"""

from __future__ import annotations

import argparse
from datetime import date
import json
import sys

from sqlalchemy import select

from .config import Settings
from .db import GoogleConnection, create_engine_and_session
from .google_routes import _sync_owner
from .migrations import migrate_database


def sync_connected_accounts(settings: Settings, *, start_date: date | None = None, full_history: bool = False) -> list[dict[str, object]]:
    """Synchronize all eligible connected Google accounts once."""

    settings.validate_runtime()
    engine, session_factory = create_engine_and_session(settings.database_url)
    cloud_schema = settings.mode == "production" or (
        settings.mode == "personal" and engine.dialect.name == "postgresql"
    )
    migrate_database(engine, owner_id=settings.personal_owner_id, production=cloud_schema)
    results: list[dict[str, object]] = []
    try:
        with session_factory() as db:
            if settings.mode == "personal":
                # The personal scheduler has one configured owner.  Set the
                # transaction claim before the first RLS-protected query;
                # ``after_begin`` reapplies it after each commit.
                db.info["runwise_owner_id"] = settings.personal_owner_id
            rows = db.scalars(select(GoogleConnection).where(GoogleConnection.provider == "google-health")).all()
            for connection in rows:
                # A disabled/error account is retried explicitly by the next
                # scheduler invocation; no fake status is written when a call
                # cannot reach Google.
                try:
                    result = _sync_owner(settings, connection.owner_id, db, start_date=start_date, full_history=full_history)
                    coverage = result.get("coverage") if isinstance(result.get("coverage"), dict) else {}
                    errors = result.get("errors") if isinstance(result.get("errors"), list) else []
                    # Scheduler output is intentionally aggregate and contains
                    # no owner identifiers, raw provider records, or dates.
                    results.append({"ok": True, "imported": int(result.get("imported", 0)), "skipped": int(result.get("skipped", 0)), "complete": bool(coverage.get("complete")), "error_count": len(errors)})
                except Exception as exc:  # one user's failure must not skip others
                    db.rollback()
                    results.append({"ok": False, "error": exc.__class__.__name__})
    finally:
        engine.dispose()
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synchronize connected Runwise Google Health accounts")
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--full-history", action="store_true")
    args = parser.parse_args(argv)
    results = sync_connected_accounts(Settings.from_env(), start_date=args.start_date, full_history=args.full_history)
    print(json.dumps(results, default=str, separators=(",", ":")))
    return 0 if all(bool(result.get("ok")) for result in results) else 1


if __name__ == "__main__":  # pragma: no cover - exercised by the scheduler.
    sys.exit(main())
