"""Deterministic, owner-scoped race coaching for Runwise.

The coaching feature deliberately keeps its state in three small relational
tables and derives the plan from the athlete's own run history.  It does not
call an LLM or an external service.  ``register_coaching_routes`` is kept
separate from :mod:`app.main` so the application can opt into the feature
without making the core API module depend on coaching implementation details.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import math
import os
from statistics import median
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from .auth import get_current_owner
from .db import Base, OWNER_ID_TYPE, Run, get_db
from .fitness import compute_fitness


# Pydantic resolves annotations in a model's class namespace.  The session
# field itself is named ``date``, so retain an unshadowed alias for its type.
DateValue = date


ScheduleRunType = Literal["easy", "quality", "long"]

DEFAULT_WEEKLY_SCHEDULE: tuple[dict[str, int | str], ...] = (
    {"weekday": 1, "run_type": "easy"},
    {"weekday": 3, "run_type": "quality"},
    {"weekday": 5, "run_type": "easy"},
    {"weekday": 6, "run_type": "long"},
)
SESSION_TYPES = frozenset({"easy", "quality", "long", "race", "rest"})
MAX_PLAN_DAYS = 366
RIEGEL_EXPONENT = 1.06
HISTORY_DAYS = 180


class CoachingGoal(Base):
    """One active race goal per owner."""

    __tablename__ = "coaching_goals"
    __table_args__ = (
        UniqueConstraint("owner_id", name="uq_coaching_goals_owner_id"),
        Index("ix_coaching_goals_owner_id", "owner_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(OWNER_ID_TYPE, nullable=False)
    race_date: Mapped[date] = mapped_column(Date, nullable=False)
    distance_km: Mapped[float] = mapped_column(Float, nullable=False)
    target_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    weekly_schedule: Mapped[list["CoachingSchedule"]] = relationship(
        back_populates="goal",
        cascade="all, delete-orphan",
        order_by="CoachingSchedule.weekday",
    )
    sessions: Mapped[list["CoachingSession"]] = relationship(
        back_populates="goal",
        cascade="all, delete-orphan",
        order_by="CoachingSession.date",
    )


class CoachingSchedule(Base):
    """A recurring weekday and workout type belonging to a goal."""

    __tablename__ = "coaching_schedules"
    __table_args__ = (
        UniqueConstraint("owner_id", "goal_id", "weekday", name="uq_coaching_schedule_owner_goal_weekday"),
        Index("ix_coaching_schedules_owner_goal", "owner_id", "goal_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(OWNER_ID_TYPE, nullable=False)
    goal_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("coaching_goals.id", ondelete="CASCADE"),
        nullable=False,
    )
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)
    run_type: Mapped[str] = mapped_column(String(20), nullable=False)

    goal: Mapped[CoachingGoal] = relationship(back_populates="weekly_schedule")


class CoachingSession(Base):
    """A generated or manually edited workout day."""

    __tablename__ = "coaching_sessions"
    __table_args__ = (
        UniqueConstraint("owner_id", "goal_id", "date", name="uq_coaching_sessions_owner_goal_date"),
        Index("ix_coaching_sessions_owner_goal_date", "owner_id", "goal_id", "date"),
        Index("ix_coaching_sessions_completed_run", "owner_id", "completed_run_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(OWNER_ID_TYPE, nullable=False)
    goal_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("coaching_goals.id", ondelete="CASCADE"),
        nullable=False,
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    run_type: Mapped[str] = mapped_column(String(20), nullable=False)
    distance_km: Mapped[float] = mapped_column(Float, nullable=False)
    target_pace_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    completed_run_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    manually_edited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    goal: Mapped[CoachingGoal] = relationship(back_populates="sessions")


# Friendly aliases make the model names useful to callers that think in terms
# of a training day/session, while keeping one canonical SQLAlchemy mapping.
TrainingGoal = CoachingGoal
TrainingDay = CoachingSchedule
TrainingSession = CoachingSession


class _ScheduleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weekday: int = Field(ge=0, le=6)
    run_type: ScheduleRunType


class _GoalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    race_date: date
    distance_km: float = Field(gt=0, le=1000)
    target_seconds: int = Field(gt=0, le=86_400 * 7)
    weekly_schedule: list[_ScheduleInput] | None = Field(default=None, max_length=6)


class _SessionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: DateValue | None = None
    run_type: Literal["easy", "quality", "long", "race", "rest"] | None = None
    distance_km: float | None = Field(default=None, gt=0, le=1000)
    target_pace_seconds: float | None = Field(default=None, gt=0, le=86_400)
    description: str | None = Field(default=None, max_length=2000)


def _timezone() -> ZoneInfo | timezone:
    name = os.getenv("RUNWISE_TIMEZONE", "America/New_York").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(f"RUNWISE_TIMEZONE={name!r} is not a valid IANA timezone") from exc


def _today() -> date:
    return datetime.now(_timezone()).date()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _run_date(run: Run) -> date:
    offset = getattr(run, "source_utc_offset_seconds", None)
    if offset is not None and -86400 < offset < 86400:
        return _as_utc(run.started_at).astimezone(timezone(timedelta(seconds=offset))).date()
    return _as_utc(run.started_at).astimezone(_timezone()).date()


def _is_excluded_source(source: object) -> bool:
    normalized = str(source or "").strip().lower()
    return normalized in {"demo", "sample", "example", "fixture", "seed"} or normalized.startswith(("demo", "sample"))


def _real_runs(runs: list[Run] | tuple[Run, ...], *, today: date) -> list[Run]:
    """Return dated, user/provider runs eligible for coaching evidence."""

    now_utc = datetime.now(timezone.utc)
    return [
        run
        for run in runs
        if not _is_excluded_source(run.source)
        and _run_date(run) <= today
        and _as_utc(run.started_at) <= now_utc
        and float(run.distance_km) > 0
        and int(run.duration_seconds) > 0
    ]


def _validate_schedule(entries: list[_ScheduleInput] | None) -> list[dict[str, int | str]]:
    values = (
        [_ScheduleInput.model_validate(item) for item in DEFAULT_WEEKLY_SCHEDULE]
        if entries is None
        else entries
    )
    if not 2 <= len(values) <= 6:
        raise HTTPException(status_code=422, detail="weekly_schedule must contain between 2 and 6 days")
    weekdays = [entry.weekday for entry in values]
    if len(set(weekdays)) != len(weekdays):
        raise HTTPException(status_code=422, detail="weekly_schedule weekdays must be unique")
    quality_count = sum(entry.run_type == "quality" for entry in values)
    if quality_count > 1:
        raise HTTPException(status_code=422, detail="weekly_schedule may contain at most one quality day")
    return [
        {"weekday": int(entry.weekday), "run_type": str(entry.run_type)}
        for entry in sorted(values, key=lambda item: item.weekday)
    ]


def _parse_goal_body(body: dict[str, Any]) -> _GoalInput:
    # Accept both the direct contract (the goal fields at the top level) and a
    # small ``{"goal": {...}}`` envelope for clients that wrap mutations.
    candidate: Any = body
    if set(body) == {"goal"}:
        candidate = body.get("goal")
    if not isinstance(candidate, dict):
        raise HTTPException(status_code=422, detail="goal must be an object")
    try:
        parsed = _GoalInput.model_validate(candidate)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    _validate_schedule(parsed.weekly_schedule)
    return parsed


def _parse_session_body(body: dict[str, Any]) -> _SessionPatch:
    # ``target_pace_seconds`` is the wire name in the response contract.  The
    # shorter aliases keep hand-authored clients ergonomic without widening the
    # stored model or response shape.
    normalized = dict(body)
    for alias in ("pace_seconds", "pace"):
        if alias in normalized and "target_pace_seconds" not in normalized:
            normalized["target_pace_seconds"] = normalized[alias]
        normalized.pop(alias, None)
    try:
        return _SessionPatch.model_validate(normalized)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc


def _commit(db: Session) -> None:
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise


def _get_goal(db: Session, owner_id: str) -> CoachingGoal | None:
    return db.scalar(
        select(CoachingGoal)
        .where(CoachingGoal.owner_id == owner_id)
        .order_by(CoachingGoal.id.desc())
    )


def _goal_schedule(goal: CoachingGoal) -> list[dict[str, int | str]]:
    return [
        {"weekday": int(entry.weekday), "run_type": str(entry.run_type)}
        for entry in sorted(goal.weekly_schedule, key=lambda item: item.weekday)
    ]


def _round_distance(value: float) -> float:
    return round(max(0.1, float(value)), 2)


def _round_pace(value: float) -> int:
    return max(1, int(round(float(value))))


def _equivalent_goal_pace(run: Run, goal_distance: float) -> float:
    duration = _prediction_duration(run)
    projected = duration * (goal_distance / float(run.distance_km)) ** RIEGEL_EXPONENT
    return projected / goal_distance


def _evidence_duration(run: Run) -> float:
    """Use moving time when it is a valid provider measurement."""

    moving = getattr(run, "moving_seconds", None)
    if moving is not None and float(moving) > 0:
        return float(moving)
    return float(run.duration_seconds)


def _prediction_duration(run: Run) -> float:
    # A race's finish time is an elapsed result even when a provider also
    # reports moving time.  For ordinary runs, moving time is useful when it
    # is present because it removes long pauses from the performance evidence.
    if str(run.run_type).strip().lower() == "race":
        return float(run.duration_seconds)
    return _evidence_duration(run)


def _interval_like(run: Run) -> bool:
    label = f"{str(run.run_type or '').lower()} {str(run.title or '').lower()}"
    return any(token in label for token in ("interval", "repetition", "repeats", "800m", "400m", "track"))


def _prediction(
    goal: CoachingGoal,
    runs: list[Run],
    *,
    today: date,
) -> dict[str, Any] | None:
    eligible = [
        run
        for run in _real_runs(runs, today=today)
        if today - timedelta(days=HISTORY_DAYS) <= _run_date(run) <= today
    ]
    if not eligible:
        return None

    def _fitness_for(run: Run) -> dict[str, Any]:
        # Weather is optional and deliberately read only from already-loaded
        # run/stream metadata.  Coaching never performs a provider lookup.
        stream = getattr(run, "stream", None)
        analysis = getattr(run, "analysis", None)
        if not isinstance(analysis, dict):
            candidate = getattr(stream, "analysis", None)
            analysis = candidate if isinstance(candidate, dict) else {}
        weather = getattr(run, "weather", None)
        if weather is None:
            candidate = analysis.get("weather")
            weather = candidate if isinstance(candidate, dict) else None
        return compute_fitness(run, weather, analysis)

    evidence: list[tuple[Run, dict[str, Any]]] = [
        (run, _fitness_for(run)) for run in eligible
    ]

    def _class(result: dict[str, Any]) -> str:
        factors = result.get("factors")
        return str(factors.get("evidence_class", "general")) if isinstance(factors, dict) else "general"

    def _duration(result: dict[str, Any]) -> float | None:
        factors = result.get("factors")
        if not isinstance(factors, dict):
            return None
        value = factors.get("neutral_duration_seconds")
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) <= 0:
            return None
        return float(value)

    # A race or time trial is the strongest anchor.  A recent result wins so
    # one unusually fast old race cannot silently set current training paces.
    races = [
        (run, result)
        for run, result in evidence
        if _class(result) in {"race", "time_trial"}
        and bool(result.get("factors", {}).get("prediction_eligible"))
        and _duration(result) is not None
    ]
    evidence_kind = "race"
    if races:
        reference, fitness = max(
            races,
            key=lambda item: (_run_date(item[0]), float(item[0].distance_km), int(item[0].id)),
        )
    else:
        # Tempo is useful supporting evidence, but carries wider uncertainty
        # than a race and is only considered after all race evidence is gone.
        tempos = [
            (run, result)
            for run, result in evidence
            if _class(result) == "tempo"
            and bool(result.get("factors", {}).get("prediction_eligible"))
            and _duration(result) is not None
        ]
        if tempos:
            reference, fitness = min(
                tempos,
                key=lambda item: (
                    float(item[1]["factors"]["prediction_duration_seconds"]) * (float(goal.distance_km) / float(item[1]["factors"]["distance_km"])) ** RIEGEL_EXPONENT,
                    -_run_date(item[0]).toordinal(),
                    -int(item[0].id),
                ),
            )
            evidence_kind = "tempo"
        else:
            # Keep the legacy low-confidence fallback for users with no race
            # or tempo, but make its duration explicitly slower than a literal
            # all-out interpretation.  Easy pace is never promoted to race
            # evidence, and interval whole-run averages remain excluded.
            sustained = [
                (run, result)
                for run, result in evidence
                if _class(result) in {"easy", "long", "general"}
                and not _interval_like(run)
                and float(run.distance_km) >= 3.0
                and _duration(result) is not None
                and isinstance(result.get("factors", {}).get("prediction_duration_seconds"), (int, float))
            ]
            if not sustained:
                return None
            reference, fitness = min(
                sustained,
                key=lambda item: (
                    float(item[1]["factors"]["prediction_duration_seconds"]) * (float(goal.distance_km) / float(item[1]["factors"]["distance_km"])) ** RIEGEL_EXPONENT,
                    -_run_date(item[0]).toordinal(),
                    -int(item[0].id),
                ),
            )
            evidence_kind = "training_proxy"

    factors = fitness.get("factors") if isinstance(fitness, dict) else {}
    projection_duration = factors.get("prediction_duration_seconds") if isinstance(factors, dict) else None
    if not isinstance(projection_duration, (int, float)) or not math.isfinite(float(projection_duration)) or float(projection_duration) <= 0:
        projection_duration = _duration(fitness)
    if projection_duration is None:
        return None
    seconds = float(projection_duration) * (
        float(goal.distance_km) / float(factors.get("distance_km") or reference.distance_km)
    ) ** RIEGEL_EXPONENT
    if evidence_kind == "race":
        confidence = str(fitness.get("confidence", "high"))
        spread = 0.08
        method = "VDOT-adjusted Riegel projection (race/time-trial)"
    elif evidence_kind == "tempo":
        confidence = "medium"
        spread = 0.12
        method = "VDOT-supported Riegel projection (tempo; uncertain)"
    else:
        confidence = "low"
        spread = 0.18
        method = "Conservative Riegel projection (training proxy; easy pace is not all-out)"
    if isinstance(factors, dict) and float(factors.get("environment_time_multiplier") or 1.0) > 1.000001:
        method += "; weather/elevation correction is heuristic"
    return {
        "seconds": _round_pace(seconds),
        "low_seconds": _round_pace(seconds * (1 - spread)),
        "high_seconds": _round_pace(seconds * (1 + spread)),
        "method": method,
        "confidence": confidence,
        "reference_run_id": int(reference.id),
        "fitness_score": fitness.get("score"),
        "fitness_method": fitness.get("method"),
        "fitness_factors": factors,
    }


def _training_paces(
    goal: CoachingGoal,
    runs: list[Run],
    prediction: dict[str, Any] | None,
    *,
    today: date,
) -> dict[str, int | None]:
    eligible = [
        run
        for run in _real_runs(runs, today=today)
        if today - timedelta(days=HISTORY_DAYS) <= _run_date(run) <= today
    ]
    goal_pace = float(goal.target_seconds) / float(goal.distance_km)
    if eligible:
        sustained = [
            run
            for run in eligible
            if not _interval_like(run)
            if float(run.distance_km) >= 3.0
            and 120.0 <= _prediction_duration(run) / float(run.distance_km) <= 1_200.0
        ]
        races = [run for run in eligible if str(run.run_type).strip().lower() == "race"]
        pace_runs = sustained + [run for run in races if run not in sustained]
        if pace_runs:
            evidence_paces = [_equivalent_goal_pace(run, float(goal.distance_km)) for run in pace_runs]
            evidence_pace = float(median(evidence_paces))
        else:
            evidence_pace = goal_pace
    else:
        # There is no measured baseline.  The target is only a planning input,
        # so derive a conservative starter range and describe the uncertainty
        # in each generated workout's text.
        evidence_pace = goal_pace

    # A target that is substantially quicker than current evidence must not
    # force equally aggressive workouts.  Pace is seconds/km, so max() is the
    # slower bound.  The 5% bridge permits gradual progress without pretending
    # the goal has already been demonstrated.
    effective_race_pace = max(goal_pace, evidence_pace * 0.95)
    return {
        "easy": _round_pace(max(evidence_pace * 1.18, effective_race_pace * 1.14)),
        "tempo": _round_pace(max(evidence_pace * 1.05, effective_race_pace * 1.04)),
        "interval": _round_pace(max(evidence_pace * 0.94, effective_race_pace * 0.96)),
        "long": _round_pace(max(evidence_pace * 1.15, effective_race_pace * 1.12)),
    }


def _weekly_totals(runs: list[Run], *, today: date) -> dict[date, float]:
    totals: dict[date, float] = defaultdict(float)
    for run in _real_runs(runs, today=today):
        run_date = _run_date(run)
        if today - timedelta(days=HISTORY_DAYS) <= run_date <= today:
            monday = run_date - timedelta(days=run_date.weekday())
            totals[monday] += float(run.distance_km)
    return dict(totals)


def _baseline_volume(runs: list[Run], goal: CoachingGoal, *, today: date) -> tuple[float, bool]:
    totals = _weekly_totals(runs, today=today)
    if not totals:
        # Smallest useful baseline: enough for two short sessions, bounded so
        # a marathon goal never implies an abrupt marathon-level workload.
        minimum = min(25.0, max(8.0, float(goal.distance_km) * 0.55))
        return minimum, True
    recent = sorted(totals.items(), key=lambda item: item[0])[-8:]
    values = [value for _, value in recent if value > 0]
    if not values:
        minimum = min(25.0, max(8.0, float(goal.distance_km) * 0.55))
        return minimum, True
    # Median protects a recovering athlete from one unusually large week while
    # still allowing a stable high-volume runner to start from their evidence.
    baseline = float(median(values))
    return min(160.0, max(8.0, baseline)), False


def _weeks_for_plan(today: date, race_date: date) -> int:
    return max(1, math.ceil(((race_date - today).days + 1) / 7))


def _phase_volume(
    week_index: int,
    total_weeks: int,
    baseline: float,
    *,
    no_history: bool,
) -> tuple[float, str]:
    taper_weeks = 2 if total_weeks >= 6 else 1
    build_weeks = max(0, total_weeks - taper_weeks)
    if week_index >= build_weeks:
        taper_index = week_index - build_weeks
        taper_factor = 0.70 if taper_index == 0 else 0.45
        if week_index == total_weeks - 1 and total_weeks > 1:
            taper_factor = min(taper_factor, 0.40)
        return baseline * (1.18 if no_history else 1.30) * taper_factor, "taper"

    progress = (week_index + 1) / max(1, build_weeks)
    peak_factor = 1.18 if no_history else 1.30
    volume = baseline * (1.0 + (peak_factor - 1.0) * progress)
    # Week four, eight, ... is a recovery week.  This happens before taper and
    # is intentionally bounded rather than an arbitrary zero-volume rest.
    if (week_index + 1) % 4 == 0:
        volume *= 0.78
        return volume, "deload"
    return volume, "build"


def _workout_distances(
    types: list[str],
    target_volume: float,
    goal_distance: float,
    baseline: float,
    *,
    phase: str,
    week_index: int,
    race_week: bool,
) -> dict[str, float]:
    """Allocate a bounded weekly volume to recurring workout slots."""

    counts: dict[str, int] = defaultdict(int)
    for run_type in types:
        counts[run_type] += 1
    # Race-distance classes prevent a short race goal from producing a long
    # run that is shorter than the easy slots.  The baseline cap remains the
    # primary guard for a runner without enough current volume.
    if goal_distance <= 5.0:
        race_long_cap = 12.0
    elif goal_distance <= 10.0:
        race_long_cap = 16.0
    elif goal_distance <= 21.2:
        race_long_cap = 22.0
    elif goal_distance <= 42.3:
        race_long_cap = 32.0
    else:
        race_long_cap = goal_distance * 0.75
    long_cap = min(
        race_long_cap,
        max(3.0, float(baseline) * 0.45 * (1.0 + min(0.35, week_index * 0.05))),
    )
    if race_week:
        long_cap *= 0.65
    long_distance = min(long_cap, max(3.0, target_volume * (0.25 if phase == "taper" else 0.34))) if counts.get("long") else 0.0
    quality_distance = min(
        float(goal_distance) * 0.45,
        max(2.5, target_volume * (0.13 if phase == "taper" else 0.20)),
    ) if counts.get("quality") else 0.0
    easy_count = counts.get("easy", 0)
    remainder = max(0.0, target_volume - long_distance - quality_distance)
    easy_distance = remainder / easy_count if easy_count else 0.0
    if easy_count:
        easy_distance = max(2.0, easy_distance)
    result: dict[str, float] = {}
    for run_type in types:
        if run_type == "long":
            result[run_type] = _round_distance(long_distance)
        elif run_type == "quality":
            result[run_type] = _round_distance(quality_distance)
        else:
            result[run_type] = _round_distance(easy_distance)
    return result


def _description(
    run_type: str,
    *,
    no_history: bool = False,
    phase: str = "build",
    quality_kind: str = "tempo",
    distance_km: float | None = None,
) -> str:
    uncertainty = " Limited history: start conservatively and adjust by feel." if no_history else ""
    if run_type == "easy":
        return f"Easy aerobic run at a conversational effort.{uncertainty}"
    if run_type == "quality":
        distance = float(distance_km or 0)
        if phase == "taper":
            text = f"Warm up 5 min; 3 x 1 min at {quality_kind} pace with easy recovery; cool down 5 min; stay within the planned distance."
            return text + uncertainty
        if distance < 4.0:
            text = f"Warm up briefly; 4 x 1 min at {quality_kind} pace with 1 min easy recovery; cool down; stay within the planned distance."
            return text + uncertainty
        if distance < 6.0:
            text = f"Warm up 8 min; 3 x 3 min at {quality_kind} pace with 90 sec easy recovery; cool down 8 min; stay within the planned distance."
            return text + uncertainty
        if quality_kind == "interval":
            text = "Warm up 10 min; 6 x 1 min at interval pace with 2 min easy recovery; cool down 10 min; stay within the planned distance."
            return text + uncertainty
        text = "Warm up 10 min; 3 x 5 min at tempo pace with 2 min easy recovery; cool down 10 min; stay within the planned distance."
        return text + uncertainty
    if run_type == "long":
        return f"Long relaxed run; keep the effort easy and even.{uncertainty}"
    return "Race day: run the planned distance and use the target as a guide."


def _build_generated_sessions(
    goal: CoachingGoal,
    schedule: list[dict[str, int | str]],
    paces: dict[str, int | None],
    runs: list[Run],
    *,
    today: date,
) -> list[dict[str, Any]]:
    baseline, no_history = _baseline_volume(runs, goal, today=today)
    total_weeks = _weeks_for_plan(today, goal.race_date)
    first_monday = today - timedelta(days=today.weekday())
    schedule_by_weekday = {int(item["weekday"]): str(item["run_type"]) for item in schedule}
    generated: list[dict[str, Any]] = []
    for week_index in range(total_weeks):
        week_monday = first_monday + timedelta(days=week_index * 7)
        week_dates = [
            (week_monday + timedelta(days=weekday), run_type)
            for weekday, run_type in schedule_by_weekday.items()
            if today <= week_monday + timedelta(days=weekday) <= goal.race_date
        ]
        volume, phase = _phase_volume(week_index, total_weeks, baseline, no_history=no_history)
        race_week = week_monday <= goal.race_date <= week_monday + timedelta(days=6)
        distances = _workout_distances(
            [run_type for _, run_type in week_dates],
            volume,
            float(goal.distance_km),
            baseline,
            phase=phase,
            week_index=week_index,
            race_week=race_week,
        )
        for workout_date, run_type in week_dates:
            quality_kind = "interval" if week_index % 2 else "tempo"
            workout_pace = (
                paces.get(quality_kind)
                if run_type == "quality"
                else paces.get(run_type)
            )
            generated.append(
                {
                    "date": workout_date,
                    "run_type": run_type,
                    "distance_km": distances[run_type],
                    "target_pace_seconds": int(workout_pace or paces.get("easy") or 1),
                    "description": _description(
                        run_type,
                        no_history=no_history,
                        phase=phase,
                        quality_kind=quality_kind,
                        distance_km=distances[run_type],
                    ),
                }
            )

    # A race-day session is always present, even when its weekday is absent
    # from the recurring schedule.  Replace a generated slot on that date.
    generated = [item for item in generated if item["date"] != goal.race_date]
    generated.append(
        {
            "date": goal.race_date,
            "run_type": "race",
            "distance_km": float(goal.distance_km),
            "target_pace_seconds": _round_pace(float(goal.target_seconds) / float(goal.distance_km)),
            "description": _description("race"),
        }
    )
    generated.sort(key=lambda item: item["date"])
    return generated


def _regenerate(
    db: Session,
    goal: CoachingGoal,
    owner_id: str,
    runs: list[Run],
    *,
    today: date,
) -> None:
    """Replace future generated rows while retaining user edits and history."""

    schedule = _goal_schedule(goal)
    prediction = _prediction(goal, runs, today=today)
    paces = _training_paces(goal, runs, prediction, today=today)
    generated = _build_generated_sessions(goal, schedule, paces, runs, today=today)
    existing = db.scalars(
        select(CoachingSession)
        .where(CoachingSession.owner_id == owner_id, CoachingSession.goal_id == goal.id)
        .order_by(CoachingSession.date, CoachingSession.id)
    ).all()
    preserve = {
        session.date: session
        for session in existing
        if session.date <= today or bool(session.manually_edited)
    }
    for session in existing:
        if session.date > today and not session.manually_edited:
            db.delete(session)
    db.flush()

    for item in generated:
        session = preserve.get(item["date"])
        if session is not None:
            continue
        db.add(
            CoachingSession(
                owner_id=owner_id,
                goal_id=goal.id,
                date=item["date"],
                run_type=item["run_type"],
                distance_km=float(item["distance_km"]),
                target_pace_seconds=float(item["target_pace_seconds"]),
                description=item["description"],
                completed_run_id=None,
                manually_edited=False,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
        )
    db.flush()


def _match_completed(
    sessions: list[CoachingSession],
    runs: list[Run],
    *,
    today: date,
) -> dict[int, int]:
    """Match each real run to at most one same-local-date planned session."""

    candidates = _real_runs(runs, today=today)
    by_date: dict[date, list[Run]] = defaultdict(list)
    for run in candidates:
        by_date[_run_date(run)].append(run)
    used: set[int] = set()
    matches: dict[int, int] = {}
    for session in sorted(sessions, key=lambda row: (row.date, row.id)):
        if str(session.run_type).strip().lower() == "rest":
            continue
        same_day = [run for run in by_date.get(session.date, []) if int(run.id) not in used]
        if not same_day:
            continue
        reference_distance = float(session.distance_km)
        maximum_difference = max(1.5, reference_distance * 0.35)
        same_day = [
            run
            for run in same_day
            if abs(float(run.distance_km) - reference_distance) <= maximum_difference
        ]
        if not same_day:
            continue
        selected = min(
            same_day,
            key=lambda run: (
                abs(float(run.distance_km) - reference_distance),
                abs(int(run.duration_seconds) - int(float(session.target_pace_seconds) * reference_distance)),
                -int(run.id),
            ),
        )
        used.add(int(selected.id))
        matches[int(session.id)] = int(selected.id)
    return matches


def _session_json(session: CoachingSession, completed_run_id: int | None) -> dict[str, Any]:
    is_rest = str(session.run_type).strip().lower() == "rest"
    return {
        "id": int(session.id),
        "date": session.date.isoformat(),
        "run_type": str(session.run_type),
        "distance_km": 0.0 if is_rest else float(session.distance_km),
        "target_pace_seconds": 0 if is_rest else _round_pace(float(session.target_pace_seconds)),
        "description": str(session.description or ""),
        "completed_run_id": int(completed_run_id) if completed_run_id is not None else None,
        "manually_edited": bool(session.manually_edited),
    }


def _state(db: Session, owner_id: str) -> dict[str, Any]:
    goal = _get_goal(db, owner_id)
    if goal is None:
        return {
            "goal": None,
            "prediction": None,
            "sessions": [],
            "paces": {"easy": None, "tempo": None, "interval": None, "long": None},
        }
    sessions = db.scalars(
        select(CoachingSession)
        .where(CoachingSession.owner_id == owner_id, CoachingSession.goal_id == goal.id)
        .order_by(CoachingSession.date, CoachingSession.id)
    ).all()
    runs = db.scalars(select(Run).where(Run.owner_id == owner_id)).all()
    today = _today()
    prediction = _prediction(goal, runs, today=today)
    paces = _training_paces(goal, runs, prediction, today=today)
    matches = _match_completed(sessions, runs, today=today)
    return {
        "goal": {
            "race_date": goal.race_date.isoformat(),
            "distance_km": float(goal.distance_km),
            "target_seconds": int(goal.target_seconds),
            "weekly_schedule": _goal_schedule(goal),
        },
        "prediction": prediction,
        "sessions": [_session_json(session, matches.get(int(session.id))) for session in sessions],
        "paces": paces,
    }


def _safe_patch_response(db: Session, owner_id: str, session: CoachingSession) -> dict[str, Any]:
    runs = db.scalars(select(Run).where(Run.owner_id == owner_id)).all()
    match = _match_completed([session], runs, today=_today()).get(int(session.id))
    return _session_json(session, match)


def register_coaching_routes(app: FastAPI) -> None:
    """Register the persisted coaching API on an existing FastAPI app.

    Calling this more than once is harmless, which helps tests and app
    factories that assemble routes from optional feature modules.
    """

    if getattr(app.state, "_runwise_coaching_registered", False):
        return
    app.state._runwise_coaching_registered = True

    @app.get("/api/coaching")
    def get_coaching(owner_id: str = Depends(get_current_owner), db: Session = Depends(get_db)) -> dict[str, Any]:
        return _state(db, owner_id)

    @app.put("/api/coaching/goal")
    def put_coaching_goal(
        body: dict[str, Any],
        owner_id: str = Depends(get_current_owner),
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        parsed = _parse_goal_body(body)
        schedule = _validate_schedule(parsed.weekly_schedule)
        today = _today()
        if parsed.race_date <= today:
            raise HTTPException(status_code=422, detail="race_date must be in the future")
        if parsed.race_date > today + timedelta(days=MAX_PLAN_DAYS - 1):
            raise HTTPException(status_code=422, detail="race_date must be within one year")

        goal = _get_goal(db, owner_id)
        now = datetime.now(timezone.utc)
        if goal is None:
            goal = CoachingGoal(
                owner_id=owner_id,
                race_date=parsed.race_date,
                distance_km=float(parsed.distance_km),
                target_seconds=int(parsed.target_seconds),
                created_at=now,
                updated_at=now,
            )
            db.add(goal)
            db.flush()
        else:
            goal.race_date = parsed.race_date
            goal.distance_km = float(parsed.distance_km)
            goal.target_seconds = int(parsed.target_seconds)
            goal.updated_at = now
            old_schedule = list(goal.weekly_schedule)
            goal.weekly_schedule.clear()
            for entry in old_schedule:
                db.delete(entry)
            db.flush()
        for entry in schedule:
            goal.weekly_schedule.append(
                CoachingSchedule(
                    owner_id=owner_id,
                    goal_id=goal.id,
                    weekday=int(entry["weekday"]),
                    run_type=str(entry["run_type"]),
                )
            )
        db.flush()
        runs = db.scalars(select(Run).where(Run.owner_id == owner_id)).all()
        _regenerate(db, goal, owner_id, runs, today=today)
        _commit(db)
        return _state(db, owner_id)

    @app.post("/api/coaching/generate")
    def generate_coaching(
        owner_id: str = Depends(get_current_owner),
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        goal = _get_goal(db, owner_id)
        if goal is None:
            raise HTTPException(status_code=404, detail="No coaching goal is configured")
        today = _today()
        runs = db.scalars(select(Run).where(Run.owner_id == owner_id)).all()
        _regenerate(db, goal, owner_id, runs, today=today)
        goal.updated_at = datetime.now(timezone.utc)
        _commit(db)
        return _state(db, owner_id)

    @app.patch("/api/coaching/sessions/{session_id}")
    def patch_coaching_session(
        session_id: int,
        body: dict[str, Any],
        owner_id: str = Depends(get_current_owner),
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        session = db.scalar(
            select(CoachingSession).where(
                CoachingSession.id == session_id,
                CoachingSession.owner_id == owner_id,
            )
        )
        if session is None:
            raise HTTPException(status_code=404, detail="Coaching session not found")
        goal = db.scalar(
            select(CoachingGoal).where(
                CoachingGoal.id == session.goal_id,
                CoachingGoal.owner_id == owner_id,
            )
        )
        if goal is None:
            raise HTTPException(status_code=404, detail="Coaching goal not found")
        patch = _parse_session_body(body)
        fields = patch.model_fields_set
        new_date = patch.date if "date" in fields else session.date
        new_type = patch.run_type if "run_type" in fields else session.run_type
        if new_date is None or new_date > goal.race_date:
            raise HTTPException(status_code=422, detail="session date must be on or before race_date")
        if "date" in fields:
            conflict = db.scalar(
                select(CoachingSession.id).where(
                    CoachingSession.owner_id == owner_id,
                    CoachingSession.goal_id == session.goal_id,
                    CoachingSession.date == new_date,
                    CoachingSession.id != session.id,
                )
            )
            if conflict is not None:
                raise HTTPException(status_code=409, detail="A coaching session already exists on that date")
        if new_type == "rest":
            if new_date == goal.race_date:
                raise HTTPException(status_code=422, detail="race day cannot be removed")
            # Keep a manually edited rest marker so regeneration does not
            # silently recreate a day the athlete intentionally removed.  Rest
            # rows remain available for editing in the sessions list.
            session.date = new_date
            session.run_type = "rest"
            session.distance_km = 0.0
            session.target_pace_seconds = 0.0
            session.description = (
                str(patch.description or "").strip()
                if "description" in fields and patch.description is not None
                else "Rest day"
            )
            session.manually_edited = True
            session.updated_at = datetime.now(timezone.utc)
            _commit(db)
            return _safe_patch_response(db, owner_id, session)
        if str(new_type) not in SESSION_TYPES:
            raise HTTPException(status_code=422, detail="run_type must be easy, quality, long, race, or rest")
        if new_type == "race" and new_date != goal.race_date:
            raise HTTPException(status_code=422, detail="race sessions must use race_date")
        if new_date == goal.race_date and new_type != "race":
            raise HTTPException(status_code=422, detail="race_date must contain the race session")
        if "distance_km" in fields and patch.distance_km is None:
            raise HTTPException(status_code=422, detail="distance_km cannot be null")
        if "target_pace_seconds" in fields and patch.target_pace_seconds is None:
            raise HTTPException(status_code=422, detail="target_pace_seconds cannot be null")
        if "date" in fields:
            session.date = new_date
        if "run_type" in fields:
            session.run_type = str(new_type)
            if str(new_type) != "rest" and float(session.distance_km) <= 0:
                # A rest marker can be restored to a workout by changing its
                # type.  Fill omitted numeric fields from conservative goal
                # defaults; an explicit value always wins below.
                session.distance_km = min(float(goal.distance_km), max(2.0, float(goal.distance_km) * 0.25))
                session.target_pace_seconds = float(goal.target_seconds) / float(goal.distance_km)
        if "distance_km" in fields:
            session.distance_km = float(patch.distance_km)  # type: ignore[arg-type]
        if "target_pace_seconds" in fields:
            session.target_pace_seconds = float(patch.target_pace_seconds)  # type: ignore[arg-type]
        if "description" in fields:
            session.description = str(patch.description or "").strip()
        session.manually_edited = True
        session.updated_at = datetime.now(timezone.utc)
        try:
            db.flush()
        except Exception as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail="Unable to update coaching session") from exc
        _commit(db)
        return _safe_patch_response(db, owner_id, session)
