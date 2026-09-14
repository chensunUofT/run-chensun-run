"""Pydantic request/response schemas and validation rules."""

from __future__ import annotations

from datetime import date as DateValue, datetime, timezone
import os
from typing import Any, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _clean_text(value: str, *, field_name: str, max_length: int, required: bool = False) -> str:
    cleaned = value.strip()
    if required and not cleaned:
        raise ValueError(f"{field_name} cannot be blank")
    if len(cleaned) > max_length:
        raise ValueError(f"{field_name} must be {max_length} characters or fewer")
    return cleaned


def _configured_timezone() -> ZoneInfo | timezone:
    timezone_name = os.getenv("RUNWISE_TIMEZONE", "America/New_York").strip() or "UTC"
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(
            f"RUNWISE_TIMEZONE={timezone_name!r} is not a valid IANA timezone"
        ) from exc


def as_utc(value: datetime) -> datetime:
    """Normalize incoming datetimes to UTC, treating naive input as local time."""

    if value.tzinfo is None:
        value = value.replace(tzinfo=_configured_timezone())
    return value.astimezone(timezone.utc)


def _http_url(value: str | None, *, field_name: str = "image_url") -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field_name} must be an absolute http(s) URL")
    if len(cleaned) > 2048:
        raise ValueError(f"{field_name} must be 2048 characters or fewer")
    return cleaned


class RunBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    started_at: datetime
    distance_km: float = Field(gt=0, le=1000)
    duration_seconds: int = Field(gt=0, le=86_400)
    run_type: str = Field(default="run", min_length=1, max_length=40)
    avg_hr: int | None = Field(default=None, ge=20, le=260)
    shoe_id: int | None = Field(default=None, gt=0)
    notes: str = Field(default="", max_length=5000)
    rpe: int | None = Field(default=None, ge=1, le=10)
    source: str = Field(default="manual", min_length=1, max_length=40)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return _clean_text(value, field_name="title", max_length=120, required=True)

    @field_validator("started_at")
    @classmethod
    def normalize_started_at(cls, value: datetime) -> datetime:
        return as_utc(value)

    @field_validator("run_type")
    @classmethod
    def clean_run_type(cls, value: str) -> str:
        return _clean_text(value, field_name="run_type", max_length=40, required=True).lower()

    @field_validator("notes")
    @classmethod
    def clean_notes(cls, value: str) -> str:
        return value.strip()

    @field_validator("source")
    @classmethod
    def clean_source(cls, value: str) -> str:
        return _clean_text(value, field_name="source", max_length=40, required=True).lower()


class RunCreate(RunBase):
    source_id: str | None = Field(default=None, max_length=200)

    @field_validator("source_id")
    @classmethod
    def clean_source_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class RunUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=120)
    started_at: datetime | None = None
    distance_km: float | None = Field(default=None, gt=0, le=1000)
    duration_seconds: int | None = Field(default=None, gt=0, le=86_400)
    run_type: str | None = Field(default=None, min_length=1, max_length=40)
    avg_hr: int | None = Field(default=None, ge=20, le=260)
    # ``None`` is deliberately valid here. The presence of shoe_id, including
    # JSON null, is a manual lock and is handled by the route.
    shoe_id: int | None = Field(default=None, gt=0)
    notes: str | None = Field(default=None, max_length=5000)
    rpe: int | None = Field(default=None, ge=1, le=10)
    source: str | None = Field(default=None, min_length=1, max_length=40)
    source_id: str | None = Field(default=None, max_length=200)

    @field_validator("title")
    @classmethod
    def validate_optional_title(cls, value: str | None) -> str | None:
        return _clean_text(value, field_name="title", max_length=120, required=True) if value is not None else None

    @field_validator("started_at")
    @classmethod
    def normalize_optional_started_at(cls, value: datetime | None) -> datetime | None:
        return as_utc(value) if value is not None else None

    @field_validator("run_type")
    @classmethod
    def clean_optional_run_type(cls, value: str | None) -> str | None:
        return _clean_text(value, field_name="run_type", max_length=40, required=True).lower() if value is not None else None

    @field_validator("notes")
    @classmethod
    def clean_optional_notes(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("source")
    @classmethod
    def clean_optional_source(cls, value: str | None) -> str | None:
        return _clean_text(value, field_name="source", max_length=40, required=True).lower() if value is not None else None

    @field_validator("source_id")
    @classmethod
    def clean_optional_source_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class RunRead(RunBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    shoe_assignment: Literal["manual", "inferred", "unassigned"] = "unassigned"
    shoe_confidence: float | None = None
    shoe_reason: str | None = None
    moving_seconds: int | None = None
    stream_available: bool = False


class ShoeRules(BaseModel):
    """Deterministic eligibility rules for automatic shoe assignment."""

    model_config = ConfigDict(extra="forbid")

    min_distance_km: float | None = Field(default=None, ge=0, le=1000)
    max_distance_km: float | None = Field(default=None, ge=0, le=1000)
    min_pace_seconds: float | None = Field(default=None, gt=0, le=86_400)
    max_pace_seconds: float | None = Field(default=None, gt=0, le=86_400)
    run_types: list[str] = Field(default_factory=list, max_length=40)
    priority: int = Field(default=0, ge=-100, le=100)

    @field_validator("run_types")
    @classmethod
    def clean_run_types(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            item = _clean_text(value, field_name="run_type", max_length=40, required=True).lower()
            if item not in cleaned:
                cleaned.append(item)
        return cleaned


def _validate_rules_bounds(value: ShoeRules) -> ShoeRules:
    if value.min_distance_km is not None and value.max_distance_km is not None and value.min_distance_km > value.max_distance_km:
        raise ValueError("rules.min_distance_km cannot exceed max_distance_km")
    if value.min_pace_seconds is not None and value.max_pace_seconds is not None and value.min_pace_seconds > value.max_pace_seconds:
        raise ValueError("rules.min_pace_seconds cannot exceed max_pace_seconds")
    return value


class ShoeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    brand: str = Field(default="", max_length=120)
    initial_distance_km: float = Field(default=0, ge=0, le=100_000)
    status: str = Field(default="active", min_length=1, max_length=30)
    purchase_date: DateValue | None = None
    image_url: str | None = None
    rules: ShoeRules = Field(default_factory=ShoeRules)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return _clean_text(value, field_name="name", max_length=120, required=True)

    @field_validator("brand")
    @classmethod
    def clean_brand(cls, value: str) -> str:
        return value.strip()

    @field_validator("status")
    @classmethod
    def clean_status(cls, value: str) -> str:
        return _clean_text(value, field_name="status", max_length=30, required=True).lower()

    @field_validator("image_url")
    @classmethod
    def clean_image_url(cls, value: str | None) -> str | None:
        return _http_url(value)

    @field_validator("rules")
    @classmethod
    def validate_rules(cls, value: ShoeRules) -> ShoeRules:
        return _validate_rules_bounds(value)


class ShoeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    brand: str | None = Field(default=None, max_length=120)
    initial_distance_km: float | None = Field(default=None, ge=0, le=100_000)
    status: str | None = Field(default=None, min_length=1, max_length=30)
    purchase_date: DateValue | None = None
    image_url: str | None = None
    rules: ShoeRules | None = None

    @field_validator("name")
    @classmethod
    def clean_optional_name(cls, value: str | None) -> str | None:
        return _clean_text(value, field_name="name", max_length=120, required=True) if value is not None else None

    @field_validator("brand")
    @classmethod
    def clean_optional_brand(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("status")
    @classmethod
    def clean_optional_status(cls, value: str | None) -> str | None:
        return _clean_text(value, field_name="status", max_length=30, required=True).lower() if value is not None else None

    @field_validator("image_url")
    @classmethod
    def clean_optional_image_url(cls, value: str | None) -> str | None:
        return _http_url(value)

    @field_validator("rules")
    @classmethod
    def validate_optional_rules(cls, value: ShoeRules | None) -> ShoeRules | None:
        return _validate_rules_bounds(value) if value is not None else None


class ShoeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    brand: str
    initial_distance_km: float
    status: str
    purchase_date: DateValue | None
    image_url: str | None
    rules: dict[str, Any]
    total_distance_km: float


class CheckinInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: DateValue | None = None
    sleep_hours: float | None = Field(default=None, ge=0, le=24)
    energy: int = Field(ge=1, le=5)
    soreness: int = Field(ge=1, le=5)
    notes: str = Field(default="", max_length=2000)

    @field_validator("notes")
    @classmethod
    def clean_checkin_notes(cls, value: str) -> str:
        return value.strip()


class CheckinRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    date: DateValue
    sleep_hours: float | None
    energy: int
    soreness: int
    notes: str


class StatsBucket(BaseModel):
    label: str
    distance_km: float
    run_count: int


class StatsRead(BaseModel):
    total_distance_km: float
    total_duration_seconds: int
    run_count: int
    average_pace_seconds: float | None
    buckets: list[StatsBucket]
    start_date: DateValue | None = None
    end_date: DateValue | None = None


class IntegrationRead(BaseModel):
    id: str
    name: str
    status: Literal["not_configured", "not_connected", "connected", "error"]
    message: str


class HealthRead(BaseModel):
    status: Literal["ok"]
    mode: str


class MeRead(BaseModel):
    id: str
    user_id: str
    email: str | None = None
    role: str | None = None


class ShoeInferInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apply: bool = False


class StreamSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    elapsed_seconds: float = Field(ge=0, le=86_400)
    distance_m: float = Field(ge=0, le=2_000_000)
    heart_rate: int | None = Field(default=None, ge=20, le=260)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class StreamLap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_seconds: float = Field(ge=0, le=86_400)
    end_seconds: float = Field(gt=0, le=86_400)
    distance_m: float | None = Field(default=None, ge=0, le=2_000_000)
    elapsed_seconds: float | None = Field(default=None, gt=0, le=86_400)
    moving_seconds: float | None = Field(default=None, ge=0, le=86_400)
    active_duration_seconds: float | None = Field(default=None, ge=0, le=86_400)
    pace_seconds: float | None = Field(default=None, gt=0, le=86_400)
    label: str | None = Field(default=None, max_length=100)

    @field_validator("end_seconds")
    @classmethod
    def validate_positive_interval(cls, value: float, info) -> float:
        start = info.data.get("start_seconds")
        if start is not None and value <= start:
            raise ValueError("lap end_seconds must be greater than start_seconds")
        elapsed = info.data.get("elapsed_seconds")
        moving = info.data.get("moving_seconds")
        active = info.data.get("active_duration_seconds")
        if elapsed is not None and elapsed > value - (start or 0) + 1e-9:
            raise ValueError("lap elapsed_seconds cannot exceed its interval")
        if moving is not None and elapsed is not None and moving > elapsed + 1e-9:
            raise ValueError("lap moving_seconds cannot exceed elapsed_seconds")
        if active is not None and elapsed is not None and active > elapsed + 1e-9:
            raise ValueError("lap active_duration_seconds cannot exceed elapsed_seconds")
        return value


class StreamsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    samples: list[StreamSample] = Field(min_length=1, max_length=50_000)
    laps: list[StreamLap] = Field(default_factory=list, max_length=2_000)


class ShareCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["run", "week", "month"]
    run_id: int | None = Field(default=None, gt=0)
    date: DateValue | None = None
    expires_days: int = Field(default=7, ge=1, le=30)


class ShareRead(BaseModel):
    id: int
    kind: Literal["run", "week", "month"]
    run_id: int | None
    date: DateValue | None
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    active: bool


class GoogleSyncRequest(BaseModel):
    """Bounded Google sync options accepted as a JSON POST body."""

    model_config = ConfigDict(extra="forbid")

    start_date: DateValue | None = None
    full_history: bool = False


class GoogleDataInspection(BaseModel):
    raw_example: dict[str, Any] | None = None
    normalized_example: dict[str, Any] | None = None
    available_types: list[str] = Field(default_factory=list)
    earliest_imported_date: DateValue | None = None
    source_coverage_note: str


def model_to_dict(model: BaseModel, *, exclude_none: bool = False) -> dict[str, Any]:
    """A small compatibility helper for code paths shared by import/export."""

    return model.model_dump(exclude_none=exclude_none)
