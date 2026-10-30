"""SQLAlchemy 2.0 ORM models for Operation Shadow (Architecture §6).

Implements the six documented entities and their relationships:
- Mission (missions)
- Task (tasks)
- Vehicle (vehicles)
- Observation (observations)
- Event (events)
- ReasoningTrace (reasoning_trace)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
     JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Base class for all Operation Shadow ORM models."""


class Mission(Base):
    """Mission entity (MISSIONS table in Architecture §6)."""

    __tablename__ = "missions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    tasks: Mapped[list[Task]] = relationship(
        "Task",
        back_populates="mission",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    observations: Mapped[list[Observation]] = relationship(
        "Observation",
        back_populates="mission",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    events: Mapped[list[Event]] = relationship(
        "Event",
        back_populates="mission",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    reasoning_traces: Mapped[list[ReasoningTrace]] = relationship(
        "ReasoningTrace",
        back_populates="mission",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Vehicle(Base):
    """Vehicle entity (VEHICLES table in Architecture §6)."""

    __tablename__ = "vehicles"
    __table_args__ = (
        CheckConstraint(
            "battery >= 0.0 AND battery <= 100.0",
            name="ck_vehicles_battery_range",
        ),
        CheckConstraint(
            "mode IN ('real', 'simulated')",
            name="ck_vehicles_mode_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    vehicle_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    battery: Mapped[float] = mapped_column(Float, nullable=False, default=100.0)
    position_x: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    position_y: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    position_alt: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    connection: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="simulated")
    last_heartbeat: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    tasks: Mapped[list[Task]] = relationship(
        "Task",
        back_populates="vehicle",
    )
    observations: Mapped[list[Observation]] = relationship(
        "Observation",
        back_populates="vehicle",
    )


class Task(Base):
    """Task entity (TASKS table in Architecture §6)."""

    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("priority >= 1", name="ck_tasks_priority_positive"),
        Index("ix_tasks_mission_priority", "mission_id", "priority"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("missions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    vehicle_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("vehicles.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    area: Mapped[str | None] = mapped_column(String(128), nullable=True, default=None)
    target: Mapped[str | None] = mapped_column(String(128), nullable=True, default=None)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    mission: Mapped[Mission] = relationship("Mission", back_populates="tasks")
    vehicle: Mapped[Vehicle | None] = relationship("Vehicle", back_populates="tasks")


class Observation(Base):
    """Observation entity (OBSERVATIONS table in Architecture §6)."""

    __tablename__ = "observations"
    __table_args__ = (
        CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_observations_confidence_range",
        ),
        Index("ix_observations_mission_timestamp", "mission_id", "timestamp"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("missions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    vehicle_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("vehicles.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    obs_type: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    location_x: Mapped[float] = mapped_column(Float, nullable=False)
    location_y: Mapped[float] = mapped_column(Float, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        index=True,
    )
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=None)

    mission: Mapped[Mission] = relationship("Mission", back_populates="observations")
    vehicle: Mapped[Vehicle] = relationship("Vehicle", back_populates="observations")


class Event(Base):
    """Event log entity (EVENTS table in Architecture §6)."""

    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_mission_timestamp", "mission_id", "timestamp"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("missions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        index=True,
    )
    category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)

    mission: Mapped[Mission] = relationship("Mission", back_populates="events")


class ReasoningTrace(Base):
    """Reasoning trace entity (REASONING_TRACE table in Architecture §6)."""

    __tablename__ = "reasoning_trace"
    __table_args__ = (
        Index("ix_reasoning_trace_mission_timestamp", "mission_id", "timestamp"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("missions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        index=True,
    )
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    resulting_task_ids: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )

    mission: Mapped[Mission] = relationship("Mission", back_populates="reasoning_traces")
