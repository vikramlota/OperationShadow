"""Mission REST endpoints (§3, §7: POST /mission, GET /mission/{id}, GET /mission/{id}/replay, GET /mission/{id}/report, POST /mission/{id}/execute, and ULTRON environmental intelligence)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from core.config import settings
from db.session import AsyncSessionLocal, get_db_session
from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field
from schemas.entities import (
    MissionCreateRequest,
    MissionCreateResponse,
    MissionDetailRead,
    MissionReplayResponse,
    MissionReportResponse,
)
from services.mission_service import MissionService, mission_service
from services.tts import speak_rationale
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/mission", tags=["mission"])


class EnvironmentalQueryRequest(BaseModel):
    """Request payload for querying ULTRON's evidence-grounded environmental intelligence."""

    query: str = Field(min_length=1, max_length=1000)


def get_mission_service(request: Request) -> MissionService:
    """Resolve MissionService from app.state or fall back to default singleton."""
    svc: MissionService | None = getattr(request.app.state, "mission_service", None)
    return svc if svc is not None else mission_service


@router.post("", response_model=MissionCreateResponse)
async def create_mission_endpoint(
    payload: MissionCreateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service: Annotated[MissionService, Depends(get_mission_service)],
) -> MissionCreateResponse:
    """Create a new mission via the in-process ULTRON LangGraph workflow and persist tasks (§7)."""
    response, rationale = await service.create_mission_with_rationale(session, payload)
    if rationale:
        background_tasks.add_task(speak_rationale, rationale)

    # When physical serial execution is enabled in runtime config (.env / ultron/.env),
    # automatically execute the planned tasks on SCOUT01 in the background upon DISPATCH.
    if not settings.hardware_lockout and settings.scout_serial_enabled:
        session_factory = getattr(request.app.state, "session_factory", AsyncSessionLocal)
        simulator = getattr(request.app.state, "scout_control_simulator", None)
        serial_writer = getattr(simulator, "serial_writer", None) if simulator is not None else None
        background_tasks.add_task(
            service.execute_mission_on_rover,
            session_factory,
            response.mission_id,
            serial_writer,
        )
    return response


@router.post("/{mission_id}/execute", response_model=MissionDetailRead)
async def execute_mission_endpoint(
    mission_id: str,
    request: Request,
    service: Annotated[MissionService, Depends(get_mission_service)],
) -> MissionDetailRead:
    """Execute a mission's tasks on SCOUT01 over ESP32 serial and return the updated mission state."""
    session_factory = getattr(request.app.state, "session_factory", AsyncSessionLocal)
    simulator = getattr(request.app.state, "scout_control_simulator", None)
    serial_writer = getattr(simulator, "serial_writer", None) if simulator is not None else None
    return await service.execute_mission_on_rover(
        session_factory,
        mission_id,
        serial_writer,
    )


@router.get("/intelligence/snapshot")
async def get_environmental_intelligence_snapshot_endpoint(
    service: Annotated[MissionService, Depends(get_mission_service)],
) -> dict[str, Any]:
    """Return structured World Model snapshot, tracked objects, sensor health, and decision traces."""
    agent = service.agent
    if hasattr(agent, "get_environment_snapshot_model"):
        snap = agent.get_environment_snapshot_model()
        world_obj = getattr(agent, "world", None)
        traces = (
            [dt.model_dump(mode="json") for dt in world_obj.environment.decision_traces]
            if world_obj is not None
            else []
        )
        out: dict[str, Any] = dict(snap.model_dump(mode="json"))
        out["decision_traces"] = traces
        return out
    wm_snap = await agent.get_world_model_snapshot()
    return {
        "is_stub": wm_snap.is_stub,
        "active_mission_id": wm_snap.active_mission_id,
        "vehicles": wm_snap.vehicles,
        "observations": wm_snap.observations,
        "decision_traces": [],
    }


@router.post("/intelligence/query")
async def query_environmental_intelligence_endpoint(
    payload: EnvironmentalQueryRequest,
    service: Annotated[MissionService, Depends(get_mission_service)],
) -> dict[str, Any]:
    """Answer an environmental question using ULTRON's evidence-grounded WorldModelTools."""
    agent = service.agent
    if hasattr(agent, "query_environment"):
        res = agent.query_environment(payload.query)
        result_dict: dict[str, Any] = dict(res.model_dump(mode="json"))
        return result_dict
    return {
        "answer": "Cannot assess environment: ULTRON is running in minimal stub mode without WorldModelTools.",
        "facts": [],
        "inferences": [],
        "unknowns": ["ULTRON stub mode active."],
        "warnings": [],
        "evidence": [],
        "confidence": "unavailable",
        "source_mode": "simulated",
    }


@router.get("/{mission_id}", response_model=MissionDetailRead)
async def get_mission_endpoint(
    mission_id: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service: Annotated[MissionService, Depends(get_mission_service)],
) -> MissionDetailRead:
    """Return full mission state including objective, tasks, and status (§7)."""
    return await service.get_mission(session, mission_id)


@router.get("/{mission_id}/replay", response_model=MissionReplayResponse)
async def get_mission_replay_endpoint(
    mission_id: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service: Annotated[MissionService, Depends(get_mission_service)],
) -> MissionReplayResponse:
    """Return chronological timeline of events and reasoning-trace entries (§7, §8.5)."""
    return await service.get_mission_replay(session, mission_id)


@router.get("/{mission_id}/report", response_model=MissionReportResponse)
async def get_mission_report_endpoint(
    mission_id: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service: Annotated[MissionService, Depends(get_mission_service)],
    format: Annotated[Literal["json", "markdown"], Query()] = "json",
) -> Response | MissionReportResponse:
    """Generate and return a deterministic After-Action Mission Report from persisted records (Phase 3)."""
    report = await service.generate_mission_report(session, mission_id)
    if format == "markdown":
        return PlainTextResponse(
            content=report.markdown_export,
            media_type="text/markdown; charset=utf-8",
        )
    return report


@router.post("/{mission_id}/complete", response_model=MissionDetailRead)
async def complete_mission_endpoint(
    mission_id: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service: Annotated[MissionService, Depends(get_mission_service)],
) -> MissionDetailRead:
    """Complete an active simulated mission and finalize its tasks."""
    return await service.complete_mission(session, mission_id)
