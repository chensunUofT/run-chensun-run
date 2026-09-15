"""Runwise FastAPI application with local and owner-scoped cloud adapters."""

from __future__ import annotations

import csv
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import CurrentUser, get_current_owner, get_current_user
from .config import Settings
from .coaching import register_coaching_routes
from .db import (
    Base,
    GoogleConnection,
    OAuthState,
    Run,
    RunStream,
    Share,
    Shoe,
    create_engine_and_session,
    get_db,
)
from .migrations import migrate_database
from .owner import DEV_OWNER_ID
from .schemas import (
    GoogleDataInspection,
    HealthRead,
    IntegrationRead,
    MeRead,
    RunCreate,
    RunRead,
    RunUpdate,
    ShareCreate,
    ShareRead,
    ShoeCreate,
    ShoeInferInput,
    ShoeRead,
    ShoeRules,
    ShoeUpdate,
    StatsBucket,
    StatsRead,
    StreamsInput,
)
from .shoe_inference import suggest_for_run


CSV_REQUIRED_HEADERS = frozenset({"started_at", "distance_km", "duration_seconds"})
CSV_ALLOWED_HEADERS = frozenset(
    {
        "source_id",
        "title",
        "started_at",
        "distance_km",
        "duration_seconds",
        "run_type",
        "avg_hr",
        "shoe_id",
        "notes",
        "rpe",
        "source",
    }
)
MAX_STREAM_SAMPLES = 50_000


def _local_timezone() -> ZoneInfo | timezone:
    name = os.getenv("RUNWISE_TIMEZONE", "America/New_York").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(f"RUNWISE_TIMEZONE={name!r} is not a valid IANA timezone") from exc


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _local_date(value: datetime) -> date:
    return _ensure_utc(value).astimezone(_local_timezone()).date()


def _display_iso(value: datetime) -> str:
    return _ensure_utc(value).isoformat()


def _run_local_date(run: Run) -> date:
    offset = run.source_utc_offset_seconds
    if offset is not None and -86400 < offset < 86400:
        return _ensure_utc(run.started_at).astimezone(timezone(timedelta(seconds=offset))).date()
    return _local_date(run.started_at)


def _run_read(run: Run) -> RunRead:
    return RunRead(
        id=run.id,
        local_date=_run_local_date(run),
        title=run.title,
        started_at=_ensure_utc(run.started_at),
        distance_km=run.distance_km,
        duration_seconds=run.duration_seconds,
        run_type=run.run_type,
        avg_hr=run.avg_hr,
        shoe_id=run.shoe_id,
        notes=run.notes,
        rpe=run.rpe,
        source=run.source,
        shoe_assignment=run.shoe_assignment or ("manual" if run.shoe_id is not None else "unassigned"),
        shoe_confidence=run.shoe_confidence,
        shoe_reason=run.shoe_reason,
        moving_seconds=run.moving_seconds,
        stream_available=bool(run.stream_available),
    )


def _real_run_clause():
    return ~func.lower(Run.source).contains("demo") & ~func.lower(Run.source).contains("sample")


def _shoe_read(shoe: Shoe, total_distance: float | int | None = None) -> ShoeRead:
    total = float(shoe.initial_distance_km) + float(total_distance or 0)
    rules = shoe.rules if isinstance(shoe.rules, dict) else {}
    return ShoeRead(
        id=shoe.id,
        name=shoe.name,
        brand=shoe.brand,
        initial_distance_km=float(shoe.initial_distance_km),
        status=shoe.status,
        purchase_date=shoe.purchase_date,
        image_url=shoe.image_url,
        rules=rules,
        total_distance_km=round(total, 3),
    )


def _dedupe_key(
    source: str,
    source_id: str | None,
    content_fingerprint: str | None = None,
    owner_id: str | None = None,
) -> str | None:
    if source_id:
        key = f"source:{source}|id:{source_id}"
    elif content_fingerprint:
        key = f"fingerprint:{content_fingerprint}"
    else:
        return None
    # Prefixing is needed for an old SQLite database whose original schema had
    # a global unique dedupe_key. New databases also enforce owner_id+key.
    return f"owner:{owner_id}|{key}" if owner_id else key


def _fingerprint(payload: RunCreate) -> str:
    values = payload.model_dump(mode="json", exclude={"source_id"})
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _commit(db: Session, *, conflict_message: str = "A record with these values already exists") -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=conflict_message) from exc


def _find_shoe(db: Session, owner_id: str, shoe_id: int | None) -> Shoe | None:
    if shoe_id is None:
        return None
    return db.scalar(select(Shoe).where(Shoe.id == shoe_id, Shoe.owner_id == owner_id))


def _require_shoe(db: Session, owner_id: str, shoe_id: int | None) -> None:
    if shoe_id is not None and _find_shoe(db, owner_id, shoe_id) is None:
        raise HTTPException(status_code=422, detail=f"shoe_id {shoe_id} does not exist")


def _require_run(db: Session, owner_id: str, run_id: int) -> Run:
    run = db.scalar(select(Run).where(Run.id == run_id, Run.owner_id == owner_id))
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


def _reject_null_non_nullable(data: dict[str, Any], fields: set[str]) -> None:
    invalid = sorted(field for field in fields if field in data and data[field] is None)
    if invalid:
        raise HTTPException(status_code=422, detail=f"These fields cannot be null: {', '.join(invalid)}")


def _format_bucket_label(bucket_date: date) -> str:
    return f"{bucket_date.strftime('%b')} {bucket_date.day}"


def _stats_range(period: Literal["week", "month"], anchor_date: date | None = None) -> tuple[date, date, list[date]]:
    today = anchor_date or datetime.now(_local_timezone()).date()
    if period == "week":
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
        bucket_dates = [start + timedelta(days=offset) for offset in range(7)]
    else:
        start = today.replace(day=1)
        next_month = date(start.year + 1, 1, 1) if start.month == 12 else date(start.year, start.month + 1, 1)
        end = next_month - timedelta(days=1)
        first_bucket = start - timedelta(days=start.weekday())
        bucket_dates = []
        cursor = first_bucket
        while cursor <= end:
            bucket_dates.append(cursor)
            cursor += timedelta(days=7)
    return start, end, bucket_dates


def _build_stats(
    db: Session,
    period: Literal["week", "month"],
    owner_id: str = DEV_OWNER_ID,
    anchor_date: date | None = None,
) -> StatsRead:
    start, end, bucket_dates = _stats_range(period, anchor_date)
    bucket_values: dict[date, dict[str, float | int]] = {
        bucket: {"distance_km": 0.0, "run_count": 0} for bucket in bucket_dates
    }
    total_distance = 0.0
    total_duration = 0
    total_moving = 0
    run_count = 0
    runs = db.scalars(select(Run).where(Run.owner_id == owner_id)).all()
    for run in runs:
        if "demo" in run.source.lower() or "sample" in run.source.lower():
            continue
        run_date = _run_local_date(run)
        if run_date < start or run_date > end:
            continue
        distance = float(run.distance_km)
        total_distance += distance
        total_duration += int(run.duration_seconds)
        total_moving += int(run.moving_seconds if run.moving_seconds is not None else run.duration_seconds)
        run_count += 1
        bucket = run_date if period == "week" else run_date - timedelta(days=run_date.weekday())
        if bucket in bucket_values:
            bucket_values[bucket]["distance_km"] += distance
            bucket_values[bucket]["run_count"] += 1
    average_pace = total_moving / total_distance if total_distance > 0 else None
    buckets = [
        StatsBucket(
            label=bucket.strftime("%a") if period == "week" else _format_bucket_label(bucket),
            distance_km=round(float(bucket_values[bucket]["distance_km"]), 3),
            run_count=int(bucket_values[bucket]["run_count"]),
        )
        for bucket in bucket_dates
    ]
    return StatsRead(
        total_distance_km=round(total_distance, 3),
        total_duration_seconds=total_duration,
        run_count=run_count,
        average_pace_seconds=round(average_pace, 3) if average_pace is not None else None,
        buckets=buckets,
        start_date=start,
        end_date=end,
    )


def _demo_payloads() -> list[RunCreate]:
    local_now = datetime.now(_local_timezone())
    examples = (
        ("demo-1", "Morning easy run", 5.2, 1_872, "easy", 142, 4),
        ("demo-2", "Tempo progression", 7.4, 2_516, "tempo", 158, 7),
        ("demo-3", "Long run", 10.0, 3_540, "long", 149, 5),
    )
    return [
        RunCreate(
            source_id=source_id,
            source="demo",
            title=title,
            started_at=local_now - timedelta(days=(2 - offset)),
            distance_km=distance,
            duration_seconds=duration,
            run_type=run_type,
            avg_hr=avg_hr,
            notes="Opt-in Runwise demo data",
            rpe=rpe,
        )
        for offset, (source_id, title, distance, duration, run_type, avg_hr, rpe) in enumerate(examples)
    ]


def _csv_row_payload(raw: dict[str, str]) -> RunCreate:
    started_at = raw.get("started_at", "").strip()
    title = raw.get("title", "").strip() or (f"Imported run {started_at[:10]}" if started_at else "Imported run")

    def optional(raw_value: str | None) -> str | None:
        value = (raw_value or "").strip()
        return value or None

    return RunCreate(
        source_id=optional(raw.get("source_id")),
        source=optional(raw.get("source")) or "csv",
        title=title,
        started_at=started_at,
        distance_km=raw.get("distance_km", ""),
        duration_seconds=raw.get("duration_seconds", ""),
        run_type=optional(raw.get("run_type")) or "run",
        avg_hr=optional(raw.get("avg_hr")),
        shoe_id=optional(raw.get("shoe_id")),
        notes=raw.get("notes", "").strip(),
        rpe=optional(raw.get("rpe")),
    )


def _catalog_path() -> Path:
    root = Path(__file__).resolve().parents[2]
    candidates = (root / "data" / "shoe-catalog.json", root / "backend" / "data" / "shoe-catalog.json")
    return next((path for path in candidates if path.exists()), candidates[0])


def _safe_catalog() -> list[dict[str, Any]]:
    path = _catalog_path()
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(value, dict):
        value = value.get("shoes", [])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _privacy_safe_run_snapshot(run: Run, shoe: Shoe | None = None) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "id": run.id,
        "title": run.title,
        "started_at": _display_iso(run.started_at),
        "date": _run_local_date(run).isoformat(),
        "local_date": _run_local_date(run).isoformat(),
        "distance_km": float(run.distance_km),
        "duration_seconds": int(run.duration_seconds),
        "moving_seconds": run.moving_seconds,
        "run_type": run.run_type,
        "avg_hr": run.avg_hr,
        "shoe_id": run.shoe_id,
        "shoe_assignment": run.shoe_assignment,
    }
    if shoe is not None:
        snapshot["shoe"] = {"name": shoe.name, "brand": shoe.brand}
    return snapshot


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _stream_analysis(samples: list[dict[str, Any]], laps: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        from .activity_analysis import analyze_activity
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="Activity analysis is not configured") from exc
    # The public API uses descriptive latitude/longitude keys. The analyzer's
    # internal contract uses lat/lon to match provider-normalized samples.
    analyzer_samples = [
        {
            **sample,
            "lat": sample.get("latitude"),
            "lon": sample.get("longitude"),
        }
        for sample in samples
    ]
    try:
        result = analyze_activity(analyzer_samples, laps)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Activity stream analysis failed: {exc}") from exc
    if hasattr(result, "model_dump"):
        result = result.model_dump(mode="json")
    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail="Activity analysis returned an invalid result")
    required = {"elapsed_seconds", "moving_seconds", "stopped_seconds", "distance_m", "moving_pace_seconds", "quality_flags", "splits", "intervals", "method"}
    missing = required - set(result)
    if missing:
        raise HTTPException(status_code=500, detail=f"Activity analysis omitted fields: {', '.join(sorted(missing))}")
    return result


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = None
    session_factory = None
    if settings.database_url:
        engine, session_factory = create_engine_and_session(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.validate_runtime()
        _local_timezone()
        if app.state.engine is None or app.state.session_factory is None:
            raise RuntimeError("A DATABASE_URL is required before starting Runwise")
        cloud_schema = settings.mode == "production" or (
            settings.mode == "personal" and app.state.engine.dialect.name == "postgresql"
        )
        migrate_database(app.state.engine, owner_id=settings.personal_owner_id, production=cloud_schema)
        yield
        app.state.engine.dispose()

    app = FastAPI(
        title="Runwise API",
        version="0.2.0",
        description="An owner-scoped running log with local SQLite and Supabase production adapters.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept"],
    )
    allowed_hosts = list(settings.allowed_hosts) or ["localhost", "127.0.0.1", "[::1]", "testserver"]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @app.middleware("http")
    async def reject_external_write_origins(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") not in settings.cors_origins:
                return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={"detail": "Request origin is not allowed"})
        return await call_next(request)

    @app.get("/api/config")
    def public_config() -> dict[str, Any]:
        return {
            "auth_required": settings.auth_required,
            "supabase_url": settings.supabase_url,
            "supabase_publishable_key": settings.supabase_publishable_key,
        }

    @app.get("/api/health", response_model=HealthRead)
    def health() -> HealthRead:
        return HealthRead(status="ok", mode=settings.mode)

    @app.get("/api/me", response_model=MeRead)
    def me(user: CurrentUser = Depends(get_current_user)) -> MeRead:
        identity = str(user.subject)
        return MeRead(id=identity, user_id=identity, email=user.email, role=user.role)

    @app.get("/api/runs", response_model=list[RunRead])
    def list_runs(owner_id: str = Depends(get_current_owner), db: Session = Depends(get_db)) -> list[RunRead]:
        runs = db.scalars(select(Run).where(Run.owner_id == owner_id).order_by(Run.started_at.desc(), Run.id.desc())).all()
        return [_run_read(run) for run in runs if "demo" not in run.source.lower() and "sample" not in run.source.lower()]

    @app.get("/api/runs/{run_id}", response_model=RunRead)
    def get_run(run_id: int, owner_id: str = Depends(get_current_owner), db: Session = Depends(get_db)) -> RunRead:
        return _run_read(_require_run(db, owner_id, run_id))

    @app.post("/api/runs/reprocess")
    def reprocess_runs(owner_id: str = Depends(get_current_owner), db: Session = Depends(get_db)) -> dict[str, Any]:
        from .run_enrichment import enrich_run
        runs = db.scalars(select(Run).where(Run.owner_id == owner_id, _real_run_clause()).order_by(Run.started_at, Run.id)).all()
        results = [enrich_run(db, run) for run in runs]
        _commit(db)
        return {"processed": len(results), "types_changed": sum(r["type_changed"] for r in results), "shoes_changed": sum(r["shoe_changed"] for r in results), "with_streams": sum(r["has_stream"] for r in results)}

    @app.post("/api/runs", response_model=RunRead, status_code=status.HTTP_201_CREATED)
    def create_run(payload: RunCreate, owner_id: str = Depends(get_current_owner), db: Session = Depends(get_db)) -> RunRead:
        _require_shoe(db, owner_id, payload.shoe_id)
        dedupe_key = _dedupe_key(payload.source, payload.source_id, owner_id=owner_id)
        if dedupe_key and db.scalar(select(Run.id).where(Run.owner_id == owner_id, Run.dedupe_key == dedupe_key)) is not None:
            raise HTTPException(status_code=409, detail="A run with this source_id already exists")
        explicit_shoe = "shoe_id" in payload.model_fields_set
        run = Run(
            owner_id=owner_id,
            title=payload.title,
            started_at=payload.started_at,
            distance_km=payload.distance_km,
            duration_seconds=payload.duration_seconds,
            run_type=payload.run_type,
            run_type_assignment="manual" if "run_type" in payload.model_fields_set else "unassigned",
            avg_hr=payload.avg_hr,
            shoe_id=payload.shoe_id,
            shoe_assignment="manual" if explicit_shoe else "unassigned",
            shoe_confidence=None,
            shoe_reason=("Manually selected" if payload.shoe_id is not None else "Manually marked without a shoe") if explicit_shoe else None,
            notes=payload.notes,
            rpe=payload.rpe,
            source=payload.source,
            source_id=payload.source_id,
            dedupe_key=dedupe_key,
        )
        db.add(run)
        _commit(db)
        db.refresh(run)
        return _run_read(run)

    @app.patch("/api/runs/{run_id}", response_model=RunRead)
    def update_run(run_id: int, payload: RunUpdate, owner_id: str = Depends(get_current_owner), db: Session = Depends(get_db)) -> RunRead:
        run = _require_run(db, owner_id, run_id)
        data = payload.model_dump(exclude_unset=True)
        _reject_null_non_nullable(data, {"title", "started_at", "distance_km", "duration_seconds", "run_type", "notes", "source"})
        if "run_type" in data:
            run.run_type_assignment = "manual"
        if "shoe_id" in data:
            _require_shoe(db, owner_id, data["shoe_id"])
            run.shoe_assignment = "manual"
            run.shoe_confidence = None
            run.shoe_reason = "Manually selected" if data["shoe_id"] is not None else "Manually marked without a shoe"
        if "rules" in data:
            data["rules"] = data["rules"].model_dump()
        new_source = data.get("source", run.source)
        new_source_id = data.get("source_id", run.source_id)
        new_key = _dedupe_key(new_source, new_source_id, run.content_fingerprint, owner_id)
        if new_key and new_key != run.dedupe_key:
            existing = db.scalar(select(Run.id).where(Run.owner_id == owner_id, Run.dedupe_key == new_key, Run.id != run_id))
            if existing is not None:
                raise HTTPException(status_code=409, detail="A run with this source identity already exists")
        for field, value in data.items():
            setattr(run, field, value)
        run.dedupe_key = new_key
        _commit(db)
        db.refresh(run)
        return _run_read(run)

    @app.delete("/api/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_run(run_id: int, owner_id: str = Depends(get_current_owner), db: Session = Depends(get_db)) -> None:
        run = _require_run(db, owner_id, run_id)
        db.delete(run)
        _commit(db)

    @app.get("/api/runs/{run_id}/analysis")
    def analyze_run(run_id: int, owner_id: str = Depends(get_current_owner), db: Session = Depends(get_db)) -> dict[str, Any]:
        run = _require_run(db, owner_id, run_id)
        pace = float(run.moving_seconds if run.moving_seconds is not None else run.duration_seconds) / float(run.distance_km)
        observations = [f"Distance was {run.distance_km:.2f} km.", f"Weighted average pace was {pace:.1f} seconds per km."]
        if run.avg_hr is not None:
            observations.append(f"Average heart rate was {run.avg_hr} bpm.")
        if run.rpe is not None:
            observations.append(f"Recorded effort was {run.rpe}/10.")
        result: dict[str, Any] = {
            "kind": "rules",
            "summary": f"{run.title}: {run.distance_km:.2f} km in {run.duration_seconds // 60} min, {pace / 60:.2f} min/km.",
            "observations": observations,
        }
        if run.stream_available and run.stream is not None:
            result["activity"] = run.stream.analysis
        return result

    @app.put("/api/runs/{run_id}/streams")
    def put_streams(run_id: int, payload: StreamsInput, owner_id: str = Depends(get_current_owner), db: Session = Depends(get_db)) -> dict[str, Any]:
        run = _require_run(db, owner_id, run_id)
        samples = [sample.model_dump(mode="json", exclude_unset=True) for sample in payload.samples]
        laps = [lap.model_dump(mode="json") for lap in payload.laps]
        # An omitted/empty laps array means "infer kilometer splits". Passing
        # [] to the analyzer selects explicit empty laps and suppresses those
        # derived splits.
        analysis = _stream_analysis(samples, laps or None)
        raw = json.dumps({"samples": samples, "laps": laps}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(raw) > 12 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Activity stream payload is too large")
        compressed = gzip.compress(raw, compresslevel=6)
        stream = db.scalar(select(RunStream).where(RunStream.owner_id == owner_id, RunStream.run_id == run_id))
        if stream is None:
            stream = RunStream(owner_id=owner_id, run_id=run_id, created_at=datetime.now(timezone.utc))
            db.add(stream)
        stream.payload_gzip = compressed
        stream.sample_count = len(samples)
        stream.raw_bytes = len(raw)
        stream.compressed_bytes = len(compressed)
        stream.laps = laps
        stream.analysis = analysis
        run.stream_available = True
        moving = analysis.get("moving_seconds")
        run.moving_seconds = int(moving) if analysis.get("moving_time_available") and isinstance(moving, (int, float)) else None
        from .run_enrichment import enrich_run
        enrich_run(db, run)
        analysis = stream.analysis
        _commit(db)
        result = dict(analysis)
        result.update({"run_id": run_id, "stream_available": True})
        return result

    @app.get("/api/runs/{run_id}/streams")
    def get_streams(run_id: int, owner_id: str = Depends(get_current_owner), db: Session = Depends(get_db)) -> dict[str, Any]:
        run = _require_run(db, owner_id, run_id)
        stream = db.scalar(select(RunStream).where(RunStream.owner_id == owner_id, RunStream.run_id == run_id))
        if stream is None or not run.stream_available:
            raise HTTPException(status_code=404, detail="Activity streams are not available for this run")
        try:
            payload = json.loads(gzip.decompress(stream.payload_gzip).decode("utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=500, detail="Stored activity stream could not be read") from exc
        return {"run_id": run_id, "samples": payload.get("samples", []), "laps": payload.get("laps", []), "analysis": stream.analysis}

    @app.get("/api/stats", response_model=StatsRead)
    def stats(period: Literal["week", "month"] = Query(default="week"), date_value: date | None = Query(default=None, alias="date"), owner_id: str = Depends(get_current_owner), db: Session = Depends(get_db)) -> StatsRead:
        return _build_stats(db, period, owner_id, date_value)

    @app.get("/api/shoes", response_model=list[ShoeRead])
    def list_shoes(owner_id: str = Depends(get_current_owner), db: Session = Depends(get_db)) -> list[ShoeRead]:
        rows = db.execute(
            select(Shoe, func.coalesce(func.sum(Run.distance_km), 0.0))
            .outerjoin(Run, (Run.shoe_id == Shoe.id) & (Run.owner_id == owner_id) & _real_run_clause())
            .where(Shoe.owner_id == owner_id)
            .group_by(Shoe.id)
            .order_by(Shoe.name.asc(), Shoe.id.asc())
        ).all()
        return [_shoe_read(shoe, total_distance) for shoe, total_distance in rows]

    @app.post("/api/shoes", response_model=ShoeRead, status_code=status.HTTP_201_CREATED)
    def create_shoe(payload: ShoeCreate, owner_id: str = Depends(get_current_owner), db: Session = Depends(get_db)) -> ShoeRead:
        shoe = Shoe(owner_id=owner_id, name=payload.name, brand=payload.brand, initial_distance_km=payload.initial_distance_km, status=payload.status, purchase_date=payload.purchase_date, image_url=payload.image_url, rules=payload.rules.model_dump())
        db.add(shoe)
        _commit(db)
        db.refresh(shoe)
        return _shoe_read(shoe)

    @app.patch("/api/shoes/{shoe_id}", response_model=ShoeRead)
    def update_shoe(shoe_id: int, payload: ShoeUpdate, owner_id: str = Depends(get_current_owner), db: Session = Depends(get_db)) -> ShoeRead:
        shoe = db.scalar(select(Shoe).where(Shoe.id == shoe_id, Shoe.owner_id == owner_id))
        if shoe is None:
            raise HTTPException(status_code=404, detail="Shoe not found")
        data = payload.model_dump(exclude_unset=True)
        _reject_null_non_nullable(data, {"name", "brand", "initial_distance_km", "status"})
        if "rules" in data and isinstance(data["rules"], ShoeRules):
            data["rules"] = data["rules"].model_dump()
        for field, value in data.items():
            setattr(shoe, field, value)
        _commit(db)
        db.refresh(shoe)
        total_distance = db.scalar(select(func.coalesce(func.sum(Run.distance_km), 0.0)).where(Run.owner_id == owner_id, Run.shoe_id == shoe.id, _real_run_clause()))
        return _shoe_read(shoe, total_distance)

    @app.delete("/api/shoes/{shoe_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_shoe(shoe_id: int, owner_id: str = Depends(get_current_owner), db: Session = Depends(get_db)) -> None:
        shoe = db.scalar(select(Shoe).where(Shoe.id == shoe_id, Shoe.owner_id == owner_id))
        if shoe is None:
            raise HTTPException(status_code=404, detail="Shoe not found")
        db.delete(shoe)
        _commit(db)

    @app.post("/api/shoes/infer")
    def infer_shoes(payload: ShoeInferInput, owner_id: str = Depends(get_current_owner), db: Session = Depends(get_db)) -> dict[str, Any]:
        shoes = db.scalars(select(Shoe).where(Shoe.owner_id == owner_id)).all()
        mileage_rows = db.execute(
            select(Run.shoe_id, func.coalesce(func.sum(Run.distance_km), 0.0))
            .where(Run.owner_id == owner_id, Run.shoe_id.is_not(None), _real_run_clause())
            .group_by(Run.shoe_id)
        ).all()
        mileage = {int(shoe_id): float(total) for shoe_id, total in mileage_rows if shoe_id is not None}
        runs = db.scalars(select(Run).where(Run.owner_id == owner_id, Run.shoe_assignment != "manual", _real_run_clause()).order_by(Run.started_at.asc(), Run.id.asc())).all()
        today = datetime.now(_local_timezone()).date()
        assignments: list[dict[str, Any]] = []
        updated = 0
        for run in runs:
            candidates = suggest_for_run(run, shoes, mileage_by_shoe=mileage, run_date=_run_local_date(run), today=today)
            candidate_rows = [{"shoe_id": c.shoe_id, "shoe_name": c.shoe_name, "score": c.score, "confidence": c.confidence, "reason": c.reason} for c in candidates]
            best = candidates[0] if candidates else None
            prediction: dict[str, Any] = {
                "run_id": run.id,
                "shoe_id": best.shoe_id if best else None,
                "shoe_name": best.shoe_name if best else None,
                "confidence": best.confidence if best else 0.0,
                "reason": best.reason if best else "No eligible shoe matched the deterministic rules",
                "candidates": candidate_rows,
                "applied": False,
            }
            if payload.apply and best is not None:
                run.shoe_id = best.shoe_id
                run.shoe_assignment = "inferred"
                run.shoe_confidence = best.confidence
                run.shoe_reason = best.reason
                mileage[best.shoe_id] = mileage.get(best.shoe_id, 0.0) + float(run.distance_km)
                prediction["applied"] = True
                updated += 1
            assignments.append(prediction)
        if payload.apply:
            _commit(db)
        manual_count = db.scalar(select(func.count(Run.id)).where(Run.owner_id == owner_id, Run.shoe_assignment == "manual", _real_run_clause())) or 0
        return {"apply": payload.apply, "assignments": assignments, "updated": updated, "skipped_manual": int(manual_count)}

    @app.get("/api/shoe-catalog")
    def shoe_catalog() -> list[dict[str, Any]]:
        return _safe_catalog()

    @app.get("/api/integrations", response_model=list[IntegrationRead])
    def integrations(owner_id: str = Depends(get_current_owner), db: Session = Depends(get_db)) -> list[IntegrationRead]:
        google = db.scalar(select(GoogleConnection).where(GoogleConnection.owner_id == owner_id, GoogleConnection.provider == "google-health"))
        if google is None:
            google_status: Literal["not_configured", "not_connected", "connected", "error"] = "not_configured"
            google_ready = all(
                bool(value)
                for value in (
                    settings.google_client_id,
                    settings.google_client_secret,
                    settings.google_redirect_uri,
                    settings.token_encryption_key,
                )
            )
            google_status = "not_connected" if google_ready else "not_configured"
            google_message = (
                "Google Health is ready to connect."
                if google_ready
                else "Credentials are needed before Runwise can connect to Google Health."
            )
        else:
            google_status = "error" if google.sync_status == "error" else "connected"
            google_message = "Google Health is connected." if google_status == "connected" else (google.sync_error or "The last Google Health sync failed.")
        return [
            IntegrationRead(id="google-health", name="Google Health", status=google_status, message=google_message),
            IntegrationRead(id="strava", name="Strava", status="not_configured", message="Credentials are needed before Runwise can connect to this service."),
        ]

    @app.post("/api/demo/seed")
    def seed_demo(owner_id: str = Depends(get_current_owner), db: Session = Depends(get_db)) -> dict[str, Any]:
        if settings.auth_required:
            raise HTTPException(status_code=404, detail="Demo data is disabled in production")
        seeded_ids: list[int] = []
        skipped = 0
        for payload in _demo_payloads():
            key = _dedupe_key(payload.source, payload.source_id, owner_id=owner_id)
            existing = db.scalar(select(Run).where(Run.owner_id == owner_id, Run.dedupe_key == key)) if key else None
            if existing is not None:
                skipped += 1
                continue
            run = Run(owner_id=owner_id, title=payload.title, started_at=payload.started_at, distance_km=payload.distance_km, duration_seconds=payload.duration_seconds, run_type=payload.run_type, avg_hr=payload.avg_hr, shoe_id=None, shoe_assignment="unassigned", notes=payload.notes, rpe=payload.rpe, source=payload.source, source_id=payload.source_id, dedupe_key=key)
            db.add(run)
            db.flush()
            seeded_ids.append(run.id)
        _commit(db)
        return {"seeded": len(seeded_ids), "skipped": skipped, "run_ids": seeded_ids}

    @app.post("/api/import/csv")
    async def import_csv(file: UploadFile = File(...), owner_id: str = Depends(get_current_owner), db: Session = Depends(get_db)) -> dict[str, int]:
        contents = await file.read(settings.max_import_bytes + 1)
        if len(contents) > settings.max_import_bytes:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=f"CSV exceeds the {settings.max_import_bytes} byte limit")
        try:
            text = contents.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=422, detail="CSV must be UTF-8 encoded") from exc
        if not text.strip():
            raise HTTPException(status_code=422, detail="CSV is empty")
        try:
            reader = csv.reader(io.StringIO(text, newline=""))
            header_row = next(reader)
        except (StopIteration, csv.Error) as exc:
            raise HTTPException(status_code=422, detail="CSV is missing a header row") from exc
        headers = [header.strip() for header in header_row]
        if len(headers) != len(set(headers)):
            raise HTTPException(status_code=422, detail="CSV contains duplicate column names")
        unknown = sorted(set(headers) - CSV_ALLOWED_HEADERS)
        missing = sorted(CSV_REQUIRED_HEADERS - set(headers))
        if unknown or missing:
            detail: dict[str, Any] = {}
            if missing:
                detail["missing_columns"] = missing
            if unknown:
                detail["unknown_columns"] = unknown
            raise HTTPException(status_code=422, detail=detail)

        payloads: list[tuple[RunCreate, str | None, str, bool]] = []
        errors: list[dict[str, Any]] = []
        row_number = 1
        try:
            for row_number, row in enumerate(reader, start=2):
                if not row or all(not value.strip() for value in row):
                    continue
                if len(row) != len(headers):
                    errors.append({"row": row_number, "error": f"expected {len(headers)} columns, received {len(row)}"})
                    continue
                raw = dict(zip(headers, row))
                try:
                    payload = _csv_row_payload(raw)
                    content_fingerprint = _fingerprint(payload)
                    key = _dedupe_key(payload.source, payload.source_id, content_fingerprint, owner_id) or ""
                    payloads.append((payload, content_fingerprint, key, bool((raw.get("shoe_id") or "").strip())))
                except Exception as exc:
                    errors.append({"row": row_number, "error": str(exc)})
                if len(payloads) + len(errors) > settings.max_import_rows:
                    raise HTTPException(status_code=413, detail=f"CSV exceeds the {settings.max_import_rows} row limit")
        except csv.Error as exc:
            raise HTTPException(status_code=422, detail=f"Malformed CSV near row {row_number}") from exc
        if errors:
            raise HTTPException(status_code=422, detail={"rows": errors})
        if not payloads:
            raise HTTPException(status_code=422, detail="CSV contains no data rows")
        for payload, _, _, _ in payloads:
            _require_shoe(db, owner_id, payload.shoe_id)
        keys = [key for _, _, key, _ in payloads if key]
        existing_keys = set(db.scalars(select(Run.dedupe_key).where(Run.owner_id == owner_id, Run.dedupe_key.in_(set(keys)))).all()) if keys else set()
        seen_keys: set[str] = set()
        imported = 0
        skipped = 0
        for payload, fingerprint, key, has_shoe in payloads:
            if not key or key in existing_keys or key in seen_keys:
                skipped += 1
                continue
            seen_keys.add(key)
            db.add(Run(owner_id=owner_id, title=payload.title, started_at=payload.started_at, distance_km=payload.distance_km, duration_seconds=payload.duration_seconds, run_type=payload.run_type, avg_hr=payload.avg_hr, shoe_id=payload.shoe_id, shoe_assignment="manual" if has_shoe else "unassigned", shoe_reason="Manually selected" if has_shoe else None, notes=payload.notes, rpe=payload.rpe, source=payload.source, source_id=payload.source_id, content_fingerprint=None if payload.source_id else fingerprint, dedupe_key=key))
            imported += 1
        _commit(db, conflict_message="CSV import conflicted with an existing run")
        return {"imported": imported, "skipped": skipped}

    @app.get("/api/export")
    def export_backup(owner_id: str = Depends(get_current_owner), db: Session = Depends(get_db)) -> dict[str, Any]:
        runs = db.scalars(select(Run).where(Run.owner_id == owner_id).order_by(Run.id.asc())).all()
        shoes = db.scalars(select(Shoe).where(Shoe.owner_id == owner_id).order_by(Shoe.id.asc())).all()
        exported_runs: list[dict[str, Any]] = []
        for run in runs:
            record = _run_read(run).model_dump(mode="json")
            record.update({"source_id": run.source_id, "content_fingerprint": run.content_fingerprint})
            exported_runs.append(record)
        exported_shoes = []
        for shoe in shoes:
            total_distance = db.scalar(select(func.coalesce(func.sum(Run.distance_km), 0.0)).where(Run.owner_id == owner_id, Run.shoe_id == shoe.id, _real_run_clause()))
            exported_shoes.append(_shoe_read(shoe, total_distance).model_dump(mode="json"))
        return {"version": 2, "exported_at": _display_iso(datetime.now(timezone.utc)), "runs": exported_runs, "shoes": exported_shoes}

    @app.post("/api/shares")
    def create_share(payload: ShareCreate, owner_id: str = Depends(get_current_owner), db: Session = Depends(get_db)) -> dict[str, Any]:
        anchor = payload.date or datetime.now(_local_timezone()).date()
        run_id = payload.run_id
        if payload.kind == "run":
            if run_id is None:
                run = db.scalar(select(Run).where(Run.owner_id == owner_id).order_by(Run.started_at.desc(), Run.id.desc()))
                if run is None:
                    raise HTTPException(status_code=404, detail="No run is available to share")
            else:
                run = _require_run(db, owner_id, run_id)
            shoe = _find_shoe(db, owner_id, run.shoe_id)
            snapshot = {"kind": "run", "run": _privacy_safe_run_snapshot(run, shoe)}
            anchor = _run_local_date(run)
        else:
            stats_snapshot = _build_stats(db, payload.kind, owner_id, anchor).model_dump(mode="json")
            snapshot = {"kind": payload.kind, "date": anchor.isoformat(), "stats": stats_snapshot}
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=payload.expires_days)
        share = Share(owner_id=owner_id, kind=payload.kind, run_id=run_id if payload.kind == "run" else None, share_date=anchor, token_hash=_hash_token(token), snapshot=snapshot, created_at=now, expires_at=expires_at)
        db.add(share)
        _commit(db)
        base = (settings.public_base_url or "").rstrip("/")
        url = f"{base}/api/public/shares/{token}" if base else f"/api/public/shares/{token}"
        return {"url": url, "token": token, "expires_at": _display_iso(expires_at)}

    @app.get("/api/shares", response_model=list[ShareRead])
    def list_shares(owner_id: str = Depends(get_current_owner), db: Session = Depends(get_db)) -> list[ShareRead]:
        now = datetime.now(timezone.utc)
        rows = db.scalars(select(Share).where(Share.owner_id == owner_id).order_by(Share.created_at.desc())).all()
        return [ShareRead(id=row.id, kind=row.kind, run_id=row.run_id, date=row.share_date, created_at=_ensure_utc(row.created_at), expires_at=_ensure_utc(row.expires_at), revoked_at=_ensure_utc(row.revoked_at) if row.revoked_at else None, active=row.revoked_at is None and _ensure_utc(row.expires_at) > now) for row in rows]

    @app.delete("/api/shares/{share_id}", status_code=status.HTTP_204_NO_CONTENT)
    def revoke_share(share_id: int, owner_id: str = Depends(get_current_owner), db: Session = Depends(get_db)) -> None:
        row = db.scalar(select(Share).where(Share.id == share_id, Share.owner_id == owner_id))
        if row is None:
            raise HTTPException(status_code=404, detail="Share not found")
        row.revoked_at = datetime.now(timezone.utc)
        _commit(db)

    @app.get("/api/public/shares/{token}")
    def public_share(token: str, db: Session = Depends(get_db)) -> dict[str, Any]:
        row = db.scalar(select(Share).where(Share.token_hash == _hash_token(token)))
        now = datetime.now(timezone.utc)
        if row is None or row.revoked_at is not None or _ensure_utc(row.expires_at) <= now:
            raise HTTPException(status_code=404, detail="Share not found or expired")
        response = dict(row.snapshot)
        response["expires_at"] = _display_iso(row.expires_at)
        return response

    # OAuth route registration is kept separate from this module so provider
    # client code can evolve without widening the core API file.
    from .google_routes import register_google_routes

    register_google_routes(app, settings)
    register_coaching_routes(app)
    from .weather import register_weather_routes
    register_weather_routes(app)
    from .bulk_runs import register_bulk_run_routes
    register_bulk_run_routes(app)
    # Render serves the compiled personal app from the API origin so the
    # HttpOnly session cookie remains same-site.  The helper is a no-op for
    # backend-only test runs without frontend/dist.
    from .static_host import mount_frontend

    mount_frontend(app, settings)
    return app


app = create_app()
