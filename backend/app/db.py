"""SQLAlchemy setup and persistence models.

Every application-owned row carries ``owner_id``. The API always supplies the
owner from the authenticated request (or the fixed local development owner),
and the Supabase migration adds matching row-level security policies.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Generator

from fastapi import Request
from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    text,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import DeclarativeBase, Mapped, Session as SQLAlchemySession, mapped_column, relationship, sessionmaker


class Base(DeclarativeBase):
    """Declarative base for the Runwise database."""


# The production migration uses PostgreSQL's native UUID type.  A variant keeps
# the legacy SQLite adapter as a dashed 36-character string so existing local
# databases do not need an owner-id rewrite, while PostgreSQL comparisons are
# typed as UUID and cannot fail with a UUID/VARCHAR operator mismatch.
OWNER_ID_TYPE = String(36).with_variant(PostgreSQLUUID(as_uuid=False), "postgresql")


class Owner(Base):
    """Application owner registry independent of Supabase Auth users.

    Personal mode uses one fixed UUID backed by a verified Google identity;
    Supabase JWT mode can add one row per verified subject. Private data uses
    this table as its optional referential anchor instead of assuming every
    owner is an ``auth.users`` record.
    """

    __tablename__ = "runwise_owners"

    owner_id: Mapped[str] = mapped_column(OWNER_ID_TYPE, primary_key=True)
    google_sub: Mapped[str | None] = mapped_column(String(200), nullable=True, unique=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Shoe(Base):
    __tablename__ = "shoes"
    __table_args__ = (Index("ix_shoes_owner_id", "owner_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(OWNER_ID_TYPE, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    brand: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    initial_distance_km: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")
    purchase_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    rules: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    runs: Mapped[list["Run"]] = relationship(
        back_populates="shoe",
        passive_deletes=True,
    )


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        # Dedupe identity is private to one owner. The key is also prefixed by
        # owner in the legacy SQLite adapter, preserving this invariant for an
        # old database that still has a global unique constraint.
        UniqueConstraint("owner_id", "dedupe_key", name="uq_runs_owner_dedupe_key"),
        Index("ix_runs_owner_id", "owner_id"),
        Index("ix_runs_owner_started_at", "owner_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(OWNER_ID_TYPE, nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_utc_offset_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    distance_km: Mapped[float] = mapped_column(Float, nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    run_type: Mapped[str] = mapped_column(String(40), nullable=False, default="run")
    run_type_assignment: Mapped[str] = mapped_column(String(20), nullable=False, default="unassigned")
    avg_hr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shoe_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("shoes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # A manual assignment is a user lock, including an explicit null shoe. An
    # inferred assignment may be replaced by a later inference pass.
    shoe_assignment: Mapped[str] = mapped_column(String(20), nullable=False, default="unassigned")
    shoe_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    shoe_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rpe: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="manual", index=True)
    # These fields support repeat-safe imports and remain intentionally absent
    # from the normal run response.
    source_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    content_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(300), nullable=True)
    moving_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stream_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    shoe: Mapped[Shoe | None] = relationship(back_populates="runs")
    stream: Mapped["RunStream | None"] = relationship(
        back_populates="run",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Checkin(Base):
    __tablename__ = "checkins"
    __table_args__ = (
        UniqueConstraint("owner_id", "date", name="uq_checkins_owner_date"),
        Index("ix_checkins_owner_id", "owner_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(OWNER_ID_TYPE, nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    sleep_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    energy: Mapped[int] = mapped_column(Integer, nullable=False)
    soreness: Mapped[int] = mapped_column(Integer, nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")


class RunStream(Base):
    """Private compressed activity samples for one run.

    The initial adapter keeps a gzip-compressed JSON payload in the database.
    A future deployment can move this payload to a private Supabase Storage
    bucket while retaining the metadata and owner-scoped row here.
    """

    __tablename__ = "run_streams"
    __table_args__ = (
        UniqueConstraint("owner_id", "run_id", name="uq_run_streams_owner_run"),
        Index("ix_run_streams_owner_id", "owner_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(OWNER_ID_TYPE, nullable=False)
    run_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    payload_gzip: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    compressed_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    laps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    analysis: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    run: Mapped[Run] = relationship(back_populates="stream")


class Share(Base):
    """Immutable privacy-safe share snapshot with a revocable public token."""

    __tablename__ = "shares"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_shares_token_hash"),
        Index("ix_shares_owner_id", "owner_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(OWNER_ID_TYPE, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    run_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    share_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GoogleConnection(Base):
    """Encrypted server-side Google OAuth credentials and sync metadata."""

    __tablename__ = "google_connections"
    __table_args__ = (
        UniqueConstraint("owner_id", "provider", name="uq_google_connections_owner_provider"),
        Index("ix_google_connections_owner_id", "owner_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(OWNER_ID_TYPE, nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="google-health")
    provider_account_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    refresh_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    access_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_cursor: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sync_status: Mapped[str] = mapped_column(String(30), nullable=False, default="connected")
    sync_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class OAuthState(Base):
    """Single-use short-lived OAuth state bound to an owner."""

    __tablename__ = "oauth_states"
    __table_args__ = (
        UniqueConstraint("state_hash", name="uq_oauth_states_state_hash"),
        Index("ix_oauth_states_owner_id", "owner_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(OWNER_ID_TYPE, nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The verifier is short lived and encrypted with the same server-only
    # Fernet key as refresh tokens. It is never returned to a browser.
    code_verifier_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)


def create_engine_and_session(database_url: str):
    """Create an engine/session factory for SQLite or PostgreSQL URLs."""

    # Render/Supabase examples often still use postgres://. SQLAlchemy accepts
    # the canonical postgresql+psycopg spelling across supported versions.
    if database_url.startswith("postgres://"):
        database_url = "postgresql+psycopg://" + database_url.removeprefix("postgres://")
    elif database_url.startswith("postgresql://"):
        database_url = "postgresql+psycopg://" + database_url.removeprefix("postgresql://")

    is_sqlite_memory = database_url in {
        "sqlite://",
        "sqlite+pysqlite://",
        "sqlite:///:memory:",
        "sqlite+pysqlite:///:memory:",
    } or ":memory:" in database_url
    if database_url.startswith("sqlite") and not is_sqlite_memory:
        raw_path = database_url.split("?", 1)[0].removeprefix("sqlite:///")
        if raw_path:
            Path(raw_path).parent.mkdir(parents=True, exist_ok=True)

    engine_kwargs: dict[str, object] = {"future": True, "pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        if is_sqlite_memory:
            engine_kwargs["poolclass"] = StaticPool
        engine_kwargs["connect_args"] = {"check_same_thread": False}

    engine = create_engine(database_url, **engine_kwargs)

    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
        future=True,
    )
    return engine, session_factory


@event.listens_for(SQLAlchemySession, "after_begin")
def _set_transaction_owner_claim(session, _transaction, connection) -> None:
    """Reapply the verified owner claim whenever a transaction begins.

    PostgreSQL's ``set_config(..., true)`` is transaction-local.  A request can
    commit more than once (OAuth state consumption followed by credential
    persistence, or a run write followed by refresh), so setting it only when
    the dependency first opens a session would leave later statements without
    an RLS claim.  The owner is stored in SQLAlchemy ``Session.info`` by
    ``get_db`` and never comes from a browser field.
    """

    owner_id = session.info.get("runwise_owner_id")
    if isinstance(owner_id, str) and connection.dialect.name == "postgresql":
        connection.execute(
            text("select set_config('request.jwt.claim.sub', :owner, true)"),
            {"owner": owner_id},
        )


def get_db(request: Request) -> Generator:
    """FastAPI dependency that opens one session per request."""

    session_factory = request.app.state.session_factory
    with session_factory() as session:
        # The owner dependency records a verified owner on request.state
        # before this dependency runs. Provisioning the app-owned anchor is
        # safe because the value cannot originate from a request body/query.
        owner_id = getattr(request.state, "owner_id", None)
        if owner_id is None:
            settings = getattr(request.app.state, "settings", None)
            if settings is not None and getattr(settings, "mode", None) == "personal":
                # Public share lookup and OAuth callback are capability routes
                # in personal mode. They still use the one fixed owner GUC.
                owner_id = getattr(settings, "personal_owner_id", None)
        if isinstance(owner_id, str):
            session.info["runwise_owner_id"] = owner_id
        if isinstance(owner_id, str) and session.get(Owner, owner_id) is None:
            session.add(Owner(owner_id=owner_id, created_at=datetime.now(timezone.utc)))
            session.flush()
        yield session
