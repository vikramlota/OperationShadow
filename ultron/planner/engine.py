import os
from typing import Any, List, Optional
from pathlib import Path
from ultron.schemas.mission import MissionPlan, Task, TaskPriority, VehicleID
from ultron.schemas.observations import EpistemicStatusEnum
from ultron.schemas.world import WorldModel
from ultron.planner.prompts import SYSTEM_PROMPT
from ultron.planner.cache import get_fallback_plan
from ultron.planner.grounding import validate_plan_evidence
from ultron.world_model.tools import WorldModelTools

import sys

# Automatically load .env only outside pytest and never override explicit environment variables
if "pytest" not in sys.modules and "PYTEST_CURRENT_TEST" not in os.environ:
    try:
        import dotenv
        root_dir = Path(__file__).resolve().parent.parent.parent
        for env_path in [root_dir / ".env", Path(".env")]:
            if env_path.is_file():
                dotenv.load_dotenv(env_path, override=False)
                break
    except ImportError:
        pass

# Default timeouts — overridden by UltronConfig when available
_PLANNING_TIMEOUT_SEC: float = 30.0
_REPLANNING_TIMEOUT_SEC: float = 20.0

try:
    from ultron.config import config as _ultron_cfg
    _PLANNING_TIMEOUT_SEC = _ultron_cfg.planning_timeout_sec
    _REPLANNING_TIMEOUT_SEC = _ultron_cfg.replanning_timeout_sec
except Exception:
    pass


def build_evidence_grounded_plan(
    world: WorldModel,
    trigger_rationale: Optional[str] = None,
    repair_prompt: Optional[str] = None,
) -> MissionPlan:
    """Construct a deterministic, environment-aware `MissionPlan` using `WorldModelTools` (§6, §7, §8).

    When environmental observations, hazards, conflicts, or route-blocking obstacles
    are present in `world`, adapts tasks and rationale to the verified evidence while
    refusing to invent unvalidated alternative routes. When the environment has no
    active triggers, uses the validated baseline task structure enriched with
    current sensor/unknown metadata.
    """
    tools = WorldModelTools(world)
    snapshot = tools.get_environment_snapshot()
    detected = snapshot.detected_objects
    health = snapshot.sensor_health
    free_space = snapshot.free_space
    pose = tools.get_vehicle_pose("SCOUT01")

    facts: List[str] = []
    inferences: List[str] = []
    unknowns: List[str] = []
    evidence_ids: List[str] = []

    if pose.found and pose.pose_valid:
        facts.append(
            f"SCOUT01 online at ({pose.position_x:.2f}, {pose.position_y:.2f})m, battery={pose.battery:.1f}%, mode={pose.mode}."
        )
    else:
        unknowns.append("SCOUT01 live pose or telemetry connection is unverified.")

    if not health.lidar_available:
        unknowns.append(
            "LiDAR is UNAVAILABLE; 360° obstacle geometry and multi-sensor corroboration cannot be verified."
        )
    if not snapshot.transform_map_validated:
        unknowns.append(
            "Global map transform (map -> odom -> oak_parent_frame) is unconfirmed; camera coordinates remain frame-relative."
        )

    active_obstacles = [
        o
        for o in detected.objects
        if (o.is_obstacle or o.label.lower() in {"chair", "box", "bench", "obstacle", "wall"})
        and o.epistemic_status != EpistemicStatusEnum.STALE
    ]
    conflicted_objs = [
        o for o in detected.objects if o.epistemic_status == EpistemicStatusEnum.CONFLICTED
    ]
    active_hazards = [
        h for h in world.environment.hazards if not getattr(h, "handled", False)
    ]

    for obj in detected.objects:
        facts.append(
            f"[{obj.object_id}] '{obj.label}' ({obj.epistemic_status.value}) at "
            f"({obj.position.x:.2f}, {obj.position.y:.2f}, {obj.position.z:.2f})m in '{obj.coordinate_frame}'."
        )
        for oid in obj.contributing_observation_ids:
            if oid not in evidence_ids:
                evidence_ids.append(oid)

    for h in active_hazards:
        facts.append(
            f"[Hazard {h.id}] '{h.type}' at ({h.location.x:.2f}, {h.location.y:.2f})m (conf={h.confidence:.2f})."
        )
        if h.observation_id and h.observation_id not in evidence_ids:
            evidence_ids.append(h.observation_id)

    # Case 1: Vehicle critical battery
    if pose.battery is not None and pose.battery < 20.0:
        inferences.append("Critical battery requires immediate return to base and task termination.")
        plan = MissionPlan(
            mission_id=world.mission.id or "M001",
            objective=world.mission.objective,
            rationale=(
                f"USED CACHED PLAN — Critical battery ({pose.battery:.1f}%) on SCOUT01. "
                "Aborting exploration and executing safe RETURN_HOME and REPORT."
            ),
            tasks=[
                Task(
                    id="T-SAFE-01",
                    vehicle=VehicleID.SCOUT,
                    type="RETURN_HOME",
                    priority=TaskPriority.HIGH,
                    evidence_ids=list(evidence_ids),
                ),
                Task(
                    id="T-SAFE-02",
                    vehicle=VehicleID.SCOUT,
                    type="REPORT",
                    priority=TaskPriority.MEDIUM,
                    evidence_ids=list(evidence_ids),
                ),
            ],
            facts_used=facts,
            inferences=inferences,
            unknowns=unknowns,
            evidence_ids=evidence_ids,
            confidence="high",
        )
        validated, _ = validate_plan_evidence(world, plan)
        return validated

    # Case 2: Conflicting sensor evidence
    if conflicted_objs:
        c_obj = conflicted_objs[0]
        inferences.append(
            f"Conflicting sensor measurements on '{c_obj.label}' ({c_obj.object_id}) prevent safe advancement without re-scanning."
        )
        plan = MissionPlan(
            mission_id=world.mission.id or "M001",
            objective=world.mission.objective,
            rationale=(
                f"USED CACHED PLAN — Sensor conflict detected on {c_obj.object_id} ('{c_obj.label}'). "
                "Halting forward navigation to SCAN and REPORT discrepancy."
            ),
            tasks=[
                Task(
                    id="T-CONF-01",
                    vehicle=VehicleID.SCOUT,
                    type="STOP",
                    priority=TaskPriority.HIGH,
                    evidence_ids=list(c_obj.contributing_observation_ids),
                ),
                Task(
                    id="T-CONF-02",
                    vehicle=VehicleID.SCOUT,
                    type="SCAN",
                    target="target",
                    priority=TaskPriority.MEDIUM,
                    evidence_ids=list(c_obj.contributing_observation_ids),
                ),
                Task(
                    id="T-CONF-03",
                    vehicle=VehicleID.SCOUT,
                    type="REPORT",
                    priority=TaskPriority.LOW,
                    evidence_ids=list(c_obj.contributing_observation_ids),
                ),
            ],
            facts_used=facts,
            inferences=inferences,
            unknowns=unknowns,
            evidence_ids=evidence_ids,
            confidence="low",
        )
        validated, _ = validate_plan_evidence(world, plan)
        return validated

    # Case 3: Route blocked by obstacle and no verified free-space alternative route
    if (active_obstacles or world.environment.known_obstacles) and trigger_rationale and (
        "obstacle" in trigger_rationale.lower() or "blocked" in trigger_rationale.lower()
    ):
        inferences.append(
            "Planned path is obstructed by a detected obstacle; because full free-space geometry is unverified, "
            "no speculative alternative route is invented."
        )
        plan = MissionPlan(
            mission_id=world.mission.id or "M001",
            objective=world.mission.objective,
            rationale=(
                f"USED CACHED PLAN — {trigger_rationale} "
                "No validated alternative route geometry is currently available; holding position to SCAN and REPORT."
            ),
            tasks=[
                Task(
                    id="T-OBS-01",
                    vehicle=VehicleID.SCOUT,
                    type="STOP",
                    priority=TaskPriority.HIGH,
                    evidence_ids=list(evidence_ids),
                ),
                Task(
                    id="T-OBS-02",
                    vehicle=VehicleID.SCOUT,
                    type="SCAN",
                    area="sector_A",
                    priority=TaskPriority.MEDIUM,
                    evidence_ids=list(evidence_ids),
                ),
                Task(
                    id="T-OBS-03",
                    vehicle=VehicleID.SCOUT,
                    type="REPORT",
                    priority=TaskPriority.LOW,
                    evidence_ids=list(evidence_ids),
                ),
            ],
            facts_used=facts,
            inferences=inferences,
            unknowns=unknowns,
            evidence_ids=evidence_ids,
            confidence="medium" if health.any_sensor_available else "low",
        )
        validated, _ = validate_plan_evidence(world, plan)
        return validated

    # Default / baseline plan enriched with World Model evidence & unknowns
    base = get_fallback_plan(world.mission.id or "M001")
    if trigger_rationale:
        base.rationale = f"USED CACHED PLAN — Replanned due to evidence trigger: {trigger_rationale}"
    base.facts_used = facts
    base.inferences = inferences
    base.unknowns = unknowns
    base.evidence_ids = evidence_ids
    base.confidence = "medium" if health.any_sensor_available else "low"
    for t in base.tasks:
        t.evidence_ids = list(evidence_ids)
    validated, _ = validate_plan_evidence(world, base)
    return validated


def invoke_gemini_planner(
    world: WorldModel,
    repair_prompt: Optional[str] = None,
    trigger_rationale: Optional[str] = None,
    language: str = "English",
) -> MissionPlan:
    """
    Invokes the Gemini LLM to generate or repair an evidence-grounded MissionPlan.
    Always queries `WorldModelTools` first so current environment snapshot, sensor
    health, tracked objects, and unknowns are explicitly supplied to the planner.
    Falls back to `build_evidence_grounded_plan` if the API key is unset or times out.
    """
    tools = WorldModelTools(world)
    snapshot = tools.get_environment_snapshot()

    user_prompt = (
        f"CURRENT WORLD STATE:\n{world.model_dump_json(indent=2)}\n\n"
        f"VERIFIED ENVIRONMENT SNAPSHOT (FROM WORLD MODEL TOOLS):\n"
        f"{snapshot.model_dump_json(indent=2)}\n\n"
        f"RATIONALE_LANGUAGE: {language}\n\n"
    )

    if trigger_rationale:
        user_prompt += (
            f"REPLAN TRIGGER DETECTED: {trigger_rationale}\n"
            "Adjust the mission plan based strictly on the verified observations.\n"
        )

    if repair_prompt:
        user_prompt += (
            f"VALIDATION FAILURE FEEDBACK:\n{repair_prompt}\nFix the plan.\n"
        )

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return build_evidence_grounded_plan(
            world,
            trigger_rationale=trigger_rationale,
            repair_prompt=repair_prompt,
        )

    model_name = os.getenv("SHADOW_LLM_MODEL", "gemini-2.5-flash")
    timeout_sec = _REPLANNING_TIMEOUT_SEC if trigger_rationale else _PLANNING_TIMEOUT_SEC

    try:
        from google import genai
        from google.genai import types as genai_types

        client = genai.Client(
            api_key=api_key,
            http_options={"timeout": int(timeout_sec * 1000)},
        )

        response: Any = client.models.generate_content(
            model=model_name,
            contents=user_prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=MissionPlan,
                temperature=0.1,
                automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
            ),
        )
        if hasattr(response, "parsed") and response.parsed:
            raw_plan = MissionPlan.model_validate(response.parsed)
            validated_plan, _ = validate_plan_evidence(world, raw_plan)
            return validated_plan
        return build_evidence_grounded_plan(
            world,
            trigger_rationale=trigger_rationale,
            repair_prompt=repair_prompt,
        )

    except Exception as e:
        print(f"LLM Call Failed or Timed Out ({model_name}): {e}")
        return build_evidence_grounded_plan(
            world,
            trigger_rationale=trigger_rationale,
            repair_prompt=repair_prompt,
        )
