"""Asynchronous SQLAlchemy 2.0 session, engine factory, additive Phase 3 schema migration, and initialization lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from core.config import settings
from db.models import Base, Observation, Task, Vehicle
from sqlalchemy import event, select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def _enable_sqlite_foreign_keys(dbapi_connection: Any, _connection_record: Any) -> None:
    """Ensure SQLite enforces FOREIGN KEY constraints on every connection."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def create_db_engine(database_url: str | None = None, *, echo: bool = False) -> AsyncEngine:
    """Create an AsyncEngine with SQLite foreign-key enforcement enabled."""
    url = database_url or settings.database_url
    async_engine = create_async_engine(
        url,
        echo=echo,
        future=True,
    )
    if url.startswith("sqlite"):
        event.listen(async_engine.sync_engine, "connect", _enable_sqlite_foreign_keys)
    return async_engine


def create_session_factory(
    target_engine: AsyncEngine | None = None,
) -> async_sessionmaker[AsyncSession]:
    """Create an async_sessionmaker bound to the specified AsyncEngine."""
    return async_sessionmaker(
        bind=target_engine or engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


engine: AsyncEngine = create_db_engine()
AsyncSessionLocal: async_sessionmaker[AsyncSession] = create_session_factory(engine)

DEFAULT_VEHICLES: tuple[dict[str, Any], ...] = (
    {
        "id": "SCOUT01",
        "vehicle_type": "ground",
        "status": "IDLE",
        "battery": 100.0,
        "position_x": 0.0,
        "position_y": 0.0,
        "position_alt": 0.0,
        "connection": "DISCONNECTED",
        "mode": "simulated",
        "last_heartbeat": None,
    },
)


async def ensure_phase3_schema_migrations(conn: AsyncConnection) -> None:
    """Idempotently apply additive SQLite migrations and immutability triggers for Phase 3.

    Never drops or alters existing mission data (`missions`, `tasks`, `vehicles`,
    `observations`, `events`, `reasoning_trace`). Ensures pre-Phase-3 SQLite
    databases gain the `mission_snapshots` table, indexes, and append-only trigger.
    """
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS mission_snapshots (
                id VARCHAR(64) NOT NULL PRIMARY KEY,
                mission_id VARCHAR(64) NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL DEFAULT 1 CHECK (sequence >= 1),
                timestamp DATETIME NOT NULL,
                trigger_type VARCHAR(64) NOT NULL,
                trigger_reason TEXT NOT NULL,
                phase VARCHAR(32) NOT NULL DEFAULT 'STATE_TRANSITION',
                associated_event_id VARCHAR(64),
                associated_trace_id VARCHAR(64),
                paired_before_snapshot_id VARCHAR(64),
                snapshot_payload JSON NOT NULL
            )
            """
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_mission_snapshots_mission_seq "
            "ON mission_snapshots (mission_id, sequence)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_mission_snapshots_mission_ts "
            "ON mission_snapshots (mission_id, timestamp)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_mission_snapshots_associated_event_id "
            "ON mission_snapshots (associated_event_id)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_mission_snapshots_associated_trace_id "
            "ON mission_snapshots (associated_trace_id)"
        )
    )
    # Enforce append-only immutability at the SQLite database level as well
    await conn.execute(
        text(
            """
            CREATE TRIGGER IF NOT EXISTS trg_mission_snapshots_immutable
            BEFORE UPDATE ON mission_snapshots
            BEGIN
                SELECT RAISE(ABORT, 'MissionSnapshot records are immutable and append-only');
            END;
            """
        )
    )


async def seed_default_vehicles(session: AsyncSession) -> None:
    """Idempotently seed SCOUT01 and reset stale CONNECTED states on cold boot."""
    result = await session.execute(
        select(Vehicle).where(Vehicle.id.in_(["SCOUT01"]))
    )
    existing_by_id = {v.id: v for v in result.scalars().all()}

    for spec in DEFAULT_VEHICLES:
        existing = existing_by_id.get(spec["id"])
        if existing is None:
            mode = settings.default_vehicle_mode
            settings.validate_requested_mode(mode)
            session.add(
                Vehicle(
                    id=spec["id"],
                    vehicle_type=spec["vehicle_type"],
                    status=spec["status"],
                    battery=spec["battery"],
                    position_x=spec["position_x"],
                    position_y=spec["position_y"],
                    position_alt=spec["position_alt"],
                    connection=spec["connection"],
                    mode=mode,
                    last_heartbeat=spec["last_heartbeat"],
                )
            )
        elif existing.connection == "CONNECTED":
            existing.connection = "DISCONNECTED"
            if existing.status in ("ACTIVE", "EXECUTING"):
                existing.status = "IDLE"

    await session.flush()

    # Reassign any historical legacy HAWK01 records to SCOUT01 to prevent NOT NULL constraint violations
    await session.execute(
        update(Observation).where(Observation.vehicle_id == "HAWK01").values(vehicle_id="SCOUT01")
    )
    await session.execute(
        update(Task).where(Task.vehicle_id == "HAWK01").values(vehicle_id="SCOUT01")
    )

    # Clean up any legacy HAWK01 record
    hawk_row = (await session.execute(select(Vehicle).where(Vehicle.id == "HAWK01"))).scalar_one_or_none()
    if hawk_row is not None:
        await session.delete(hawk_row)

    await session.commit()


async def init_db(
    target_engine: AsyncEngine | None = None,
    *,
    seed_defaults: bool = True,
) -> None:
    """Initialize registered metadata tables, run additive migrations, and optionally seed default vehicles."""
    active_engine = target_engine or engine
    async with active_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await ensure_phase3_schema_migrations(conn)

    if seed_defaults:
        factory = create_session_factory(active_engine)
        async with factory() as session:
            await seed_default_vehicles(session)


async def check_db_connection(target_engine: AsyncEngine | None = None) -> bool:
    """Verify asynchronous SQLite connectivity and foreign_keys pragma enforcement."""
    active_engine = target_engine or engine
    async with active_engine.connect() as conn:
        result = await conn.execute(text("SELECT 1"))
        return result.scalar_one() == 1


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> AsyncIterator[AsyncSession]:
    """Transactional async session context manager with automatic rollback on error."""
    factory = session_factory or AsyncSessionLocal
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an asynchronous SQLAlchemy session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
