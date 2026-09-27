"""Shared visibility rule for reversible provider duplicate quarantine."""

from sqlalchemy import func

from .db import Run

EXCLUDED_GOOGLE_SOURCE = "google_health_excluded"


def is_visible_run_source(source: object) -> bool:
    value = str(source or "").lower()
    return value != EXCLUDED_GOOGLE_SOURCE and "demo" not in value and "sample" not in value


def visible_run_clause():
    return (
        (Run.source != EXCLUDED_GOOGLE_SOURCE)
        & ~func.lower(Run.source).contains("demo")
        & ~func.lower(Run.source).contains("sample")
    )
