"""Vehicle gateway and Emergency Stop Fast Path dispatcher (§3, §5.1, §10).

Architecture §10 invariant:
- `POST /emergency-stop` sends the STOP signal directly through the Vehicle Gateway
  fast path, bypassing ULTRON planning/replanning, normal mission/task queues, and
  synchronous database writes.
- Database logging and dashboard state sync happen asynchronously *after* dispatch
  has already completed, never before.
- When `ALL` is selected, delivery is attempted to `SCOUT01` and `HAWK01`
  independently so failure on one vehicle never blocks the other.
- Physical hardware control remains locked (`SHADOW_HARDWARE_LOCKOUT=true`) until
  TRD v2.0 §7 and Hardware & IoT failsafe specifications are confirmed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
import time
from typing import Literal
import uuid

from core.config import settings
from core.logging import get_logger
from db.models import Event, Mission, Vehicle
from schemas.entities import (
    EmergencyStopResponse,
    EventRead,
    VehicleEmergencyStopOutcome,
    VehicleRead,
)
from schemas.enums import (
    EmergencyStopTargetEnum,
    EventCategoryEnum,
    VehicleConnectionStateEnum,
    VehicleIdEnum,
    VehicleModeEnum,
    VehicleStatusEnum,
)
from services.vehicle_service import VehicleService
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from ultron_stub import VehicleId
from websocket.dashboard_gateway import DashboardGateway

logger = get_logger("websocket.vehicle_gateway")

OrderTraceHook = Callable[[str], Awaitable[None] | None]


class VehicleGatewayFastPath:
    """Dedicated emergency-stop fast-path dispatcher and background post-send logger (§10)."""

    def __init__(self) -> None:
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._trace_hook: OrderTraceHook | None = None

    @property
    def pending_log_task_count(self) -> int:
        return len(self._background_tasks)

    def set_order_trace_hook(self, hook: OrderTraceHook | None) -> None:
        """Register an optional deterministic trace hook for verifying dispatch-before-DB-log order."""
        self._trace_hook = hook

    async def _emit_trace(self, step_name: str) -> None:
        if self._trace_hook is not None:
            res = self._trace_hook(step_name)
            if asyncio.iscoroutine(res):
                await res

    async def dispatch_emergency_stop(
        self,
        target: EmergencyStopTargetEnum,
        *,
        vehicle_service: VehicleService,
        session_factory: async_sessionmaker[AsyncSession],
        dashboard_gateway: DashboardGateway,
    ) -> EmergencyStopResponse:
        """Execute the Emergency Stop Fast Path (§10) without calling ULTRON or awaiting DB writes."""
        start_perf = time.perf_counter()

        target_ids: list[VehicleId] = (
            ["SCOUT01", "HAWK01"]
            if target == EmergencyStopTargetEnum.ALL
            else [
                "SCOUT01"
                if target == EmergencyStopTargetEnum.SCOUT01
                else "HAWK01"
            ]
        )

        outcomes: dict[VehicleIdEnum, VehicleEmergencyStopOutcome] = {}

        # Dispatch to each vehicle independently so one failure never blocks the other
        for vid in target_ids:
            vid_enum = VehicleIdEnum(vid)
            adapter = vehicle_service.get_adapter(vid)
            pre_state = adapter.get_state()
            try:
                stub_res = await adapter.trigger_emergency_stop()
                await self._emit_trace(f"dispatch:{vid}")
                outcomes[vid_enum] = VehicleEmergencyStopOutcome(
                    vehicle_id=vid_enum,
                    mode=VehicleModeEnum(stub_res.mode),
                    is_simulated=stub_res.is_simulated,
                    real_hardware_available=stub_res.real_hardware_available,
                    connection_at_dispatch=VehicleConnectionStateEnum(
                        stub_res.connection_at_dispatch
                    ),
                    dispatch_attempted=stub_res.dispatch_attempted,
                    dispatch_succeeded=stub_res.dispatch_succeeded,
                    vehicle_acknowledged=stub_res.vehicle_acknowledged,
                    vehicle_confirmed_stopped=stub_res.vehicle_confirmed_stopped,
                    resulting_status=VehicleStatusEnum(stub_res.resulting_status),
                    was_already_stopped=stub_res.was_already_stopped,
                    dispatched_at=stub_res.dispatched_at,
                    acknowledged_at=stub_res.acknowledged_at,
                    reason=stub_res.reason,
                )
            except Exception as exc:
                await self._emit_trace(f"dispatch_error:{vid}")
                logger.error(
                    f"Emergency stop dispatch exception for {vid}: {exc}",
                    extra={"vehicle_id": vid},
                )
                outcomes[vid_enum] = VehicleEmergencyStopOutcome(
                    vehicle_id=vid_enum,
                    mode=VehicleModeEnum(pre_state.mode),
                    is_simulated=(pre_state.mode == "simulated"),
                    real_hardware_available=False,
                    connection_at_dispatch=VehicleConnectionStateEnum(pre_state.connection),
                    dispatch_attempted=True,
                    dispatch_succeeded=False,
                    vehicle_acknowledged=False,
                    vehicle_confirmed_stopped=False,
                    resulting_status=VehicleStatusEnum(pre_state.status),
                    was_already_stopped=(pre_state.status == "EMERGENCY_STOPPED"),
                    dispatched_at=vehicle_service.clock.now(),
                    acknowledged_at=None,
                    reason=f"Emergency stop transport error on {vid}: {exc}",
                )

        elapsed_ms = round((time.perf_counter() - start_perf) * 1000.0, 4)

        confirmed_count = sum(
            1 for item in outcomes.values() if item.vehicle_confirmed_stopped
        )
        total_count = len(outcomes)
        all_confirmed = confirmed_count == total_count and total_count > 0
        any_failed = any(not item.dispatch_succeeded for item in outcomes.values())

        overall: Literal[
            "ALL_CONFIRMED_STOPPED",
            "PARTIAL_STOPPED",
            "NONE_CONFIRMED_STOPPED",
        ]
        if all_confirmed:
            overall = "ALL_CONFIRMED_STOPPED"
        elif confirmed_count > 0:
            overall = "PARTIAL_STOPPED"
        else:
            overall = "NONE_CONFIRMED_STOPPED"

        # Schedule asynchronous post-dispatch database logging and WS state sync (§10)
        bg_task = asyncio.create_task(
            self._async_post_dispatch_log(
                target=target,
                outcomes=outcomes,
                vehicle_service=vehicle_service,
                session_factory=session_factory,
                dashboard_gateway=dashboard_gateway,
            ),
            name=f"estop-async-log-{uuid.uuid4().hex[:8]}",
        )
        self._background_tasks.add(bg_task)
        bg_task.add_done_callback(self._background_tasks.discard)

        return EmergencyStopResponse(
            request_accepted=True,
            target=target,
            overall_outcome=overall,
            all_confirmed_stopped=all_confirmed,
            any_dispatch_failed=any_failed,
            hardware_lockout_active=settings.hardware_lockout,
            real_hardware_estop_available=False,
            fast_path_used=True,
            async_audit_log_scheduled=True,
            dispatch_elapsed_ms=elapsed_ms,
            safety_notice=(
                "SIMULATED MODE ONLY: Physical vehicle emergency-stop dispatch is "
                "unavailable while SHADOW_HARDWARE_LOCKOUT=true and TRD v2.0 / "
                "Hardware & IoT specifications are pending. Software E-Stop never "
                "replaces the physical vehicle kill switch or onboard failsafes."
            ),
            vehicle_outcomes=outcomes,
        )

    async def _async_post_dispatch_log(
        self,
        *,
        target: EmergencyStopTargetEnum,
        outcomes: dict[VehicleIdEnum, VehicleEmergencyStopOutcome],
        vehicle_service: VehicleService,
        session_factory: async_sessionmaker[AsyncSession],
        dashboard_gateway: DashboardGateway,
    ) -> None:
        """Persist emergency-stop state and audit event to SQLite AFTER dispatch has completed."""
        await self._emit_trace("db_log_start")
        try:
            async with session_factory() as session:
                for vid_enum, outcome in outcomes.items():
                    vid: VehicleId = (
                        "SCOUT01" if vid_enum == VehicleIdEnum.SCOUT01 else "HAWK01"
                    )
                    row = (
                        await session.execute(select(Vehicle).where(Vehicle.id == vid))
                    ).scalar_one_or_none()
                    if row is not None and outcome.vehicle_confirmed_stopped:
                        row.status = outcome.resulting_status.value
                        await session.flush()
                        await session.refresh(row)
                        await dashboard_gateway.broadcast_telemetry(
                            VehicleRead.model_validate(row)
                        )

                # If a mission exists, log a SAFETY event in the EVENTS table
                latest_mission_id = (
                    await session.execute(
                        select(Mission.id)
                        .order_by(Mission.created_at.desc(), Mission.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()

                if latest_mission_id is not None:
                    summary = ", ".join(
                        f"{k.value}: confirmed_stopped={v.vehicle_confirmed_stopped} ({v.reason})"
                        for k, v in outcomes.items()
                    )
                    event_row = Event(
                        id=f"ESTOP-{uuid.uuid4().hex[:8].upper()}",
                        mission_id=latest_mission_id,
                        timestamp=vehicle_service.clock.now(),
                        category=EventCategoryEnum.SAFETY.value,
                        message=f"Emergency stop triggered for {target.value} — {summary}",
                    )
                    session.add(event_row)
                    await session.flush()
                    await session.refresh(event_row)
                    event_read = EventRead.model_validate(event_row)
                else:
                    event_read = None

                await session.commit()

                if event_read is not None:
                    await dashboard_gateway.broadcast_event(event_read)

            await self._emit_trace("db_log_complete")
        except Exception as exc:
            await self._emit_trace("db_log_error")
            logger.error(
                f"Post-dispatch emergency-stop database logging failed (dispatch was unaffected): {exc}",
                extra={"error_code": "ESTOP_ASYNC_LOG_FAILURE"},
            )

    async def wait_for_pending_logs(self, timeout: float = 2.0) -> None:
        """Await any in-flight asynchronous post-dispatch log tasks (used in tests and shutdown)."""
        if not self._background_tasks:
            return
        tasks = list(self._background_tasks)
        with suppress(Exception):
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=timeout,
            )

    async def shutdown(self) -> None:
        """Await or cancel pending background log tasks during application shutdown."""
        await self.wait_for_pending_logs(timeout=1.0)
        for task in list(self._background_tasks):
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self._background_tasks.clear()


vehicle_gateway_fast_path = VehicleGatewayFastPath()
