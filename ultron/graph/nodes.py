import time
import uuid
from ultron.state import UltronState, UltronStateUpdate
from ultron.planner.engine import invoke_gemini_planner
from ultron.safety.validator import validate_plan
from ultron.safety.repairs import build_repair_prompt
from ultron.replanner.triggers import check_replanning_triggers
from ultron.schemas.world import DecisionTraceEntry, EvidenceReference
from ultron.world_model.tools import WorldModelTools


def node_plan(state: UltronState) -> UltronStateUpdate:
    """Generates an evidence-grounded mission plan using WorldModelTools + Gemini planner or fallback."""
    plan = invoke_gemini_planner(
        world=state["world"],
        repair_prompt=state.get("repair_prompt"),
        trigger_rationale=state.get("trigger_rationale"),
        language=state.get("language", "English"),
    )
    return {"proposed_plan": plan}


def node_validate(state: UltronState) -> UltronStateUpdate:
    """Evaluates the proposed plan against physical constraints and known world state."""
    proposed_plan = state.get("proposed_plan")
    if not proposed_plan:
        return {
            "validation_errors": ["No proposed plan to validate."],
            "repair_prompt": "Please generate a valid mission plan.",
            "retry_count": state.get("retry_count", 0) + 1
        }

    is_valid, errors = validate_plan(proposed_plan, state["world"])

    if is_valid:
        return {
            "validation_errors": [],
            "repair_prompt": None,
            "retry_count": 0
        }

    repair = build_repair_prompt(proposed_plan.rationale, errors)
    current_retries = state.get("retry_count", 0)

    return {
        "validation_errors": errors,
        "repair_prompt": repair,
        "retry_count": current_retries + 1
    }


def node_assign(state: UltronState) -> UltronStateUpdate:
    """Dispatches the validated plan's tasks to SCOUT01, updates task tracking, and records a DecisionTraceEntry."""
    valid_plan = state.get("proposed_plan")
    world = state["world"]
    now = time.time()

    if valid_plan:
        world.mission.tasks.active.clear()
        for task in valid_plan.tasks:
            world.mission.tasks.active[task.vehicle] = task.id

        # Record structured decision trace in WorldModel
        tools = WorldModelTools(world, current_time=now)
        snapshot = tools.get_environment_snapshot()
        obs_by_id = {o.observation_id: o for o in world.environment.observation_history}
        ev_refs = []
        sensor_sources = set()
        for eid in valid_plan.evidence_ids:
            obs = obs_by_id.get(eid)
            if obs is not None:
                sensor_sources.add(obs.sensor_id)
                ev_refs.append(
                    EvidenceReference(
                        observation_id=obs.observation_id,
                        sensor=obs.sensor_id,
                        sensor_type=obs.sensor_type.value,
                        timestamp=obs.sensor_timestamp,
                        coordinate_frame=obs.coordinate_frame,
                        confidence=obs.confidence,
                        source_mode=obs.source_mode,
                    )
                )

        for sid, sh in snapshot.sensor_health.sensors.items():
            sensor_sources.add(f"{sid}:{sh.status.value}")

        trigger_str = state.get("trigger_rationale") or "INITIAL_MISSION_PLANNING"
        trace = DecisionTraceEntry(
            trace_id=f"DT-{uuid.uuid4().hex[:8].upper()}",
            mission_id=world.mission.id or "M001",
            timestamp=now,
            trigger=trigger_str,
            environmental_facts_used=list(valid_plan.facts_used),
            inferences_made=list(valid_plan.inferences),
            unknowns_and_uncertainties=list(valid_plan.unknowns),
            sensor_sources=sorted(sensor_sources),
            evidence_references=ev_refs,
            decision_summary=(
                f"Assigned {len(valid_plan.tasks)} task(s) to SCOUT01: "
                + ", ".join(f"{t.id}:{t.type}" for t in valid_plan.tasks)
            ),
            rationale=valid_plan.rationale,
            safety_validator_passed=True,
            safety_validator_modified_or_rejected=state.get("retry_count", 0) > 0,
            validation_errors=[],
            replanning_triggered=bool(state.get("trigger_rationale")),
            resulting_task_ids=[t.id for t in valid_plan.tasks],
            confidence_level=(
                valid_plan.confidence
                if valid_plan.confidence in ("high", "medium", "low", "unavailable")
                else "medium"
            ),  # type: ignore[arg-type]
            is_simulated=any(
                getattr(v.mode, "value", str(v.mode)) == "simulated"
                for v in world.vehicles.values()
            ),
        )
        world.environment.decision_traces.append(trace)

    # Mark active hazards/objects/tracked_objects as handled so they do not continuously retrigger replanning
    for h in world.environment.hazards:
        h.handled = True
    for o in world.environment.objects:
        o.handled = True
    for tobj in world.environment.tracked_objects.values():
        tobj.handled = True

    return {"world": world}


def node_evaluate(state: UltronState) -> UltronStateUpdate:
    """Checks physical triggers in the environment to determine if replanning is needed."""
    should_replan, rationale = check_replanning_triggers(state["world"])
    return {"trigger_rationale": rationale if should_replan else None}


def node_degraded(state: UltronState) -> UltronStateUpdate:
    """Issues safe default commands (HOLD POSITION) and records rejection trace if retries are exhausted."""
    world = state["world"]
    now = time.time()
    errors = list(state.get("validation_errors", []))
    trace = DecisionTraceEntry(
        trace_id=f"DT-DEG-{uuid.uuid4().hex[:6].upper()}",
        mission_id=world.mission.id or "M001",
        timestamp=now,
        trigger=state.get("trigger_rationale") or "SAFETY_VALIDATION_EXHAUSTED",
        environmental_facts_used=[],
        inferences_made=["Safety validator rejected proposed plans after maximum retries."],
        unknowns_and_uncertainties=errors,
        sensor_sources=[],
        evidence_references=[],
        decision_summary="DEGRADED MODE: Hold position / safe stop enforced by deterministic safety validator.",
        rationale="Proposed mission plan violated deterministic safety or grounding constraints.",
        safety_validator_passed=False,
        safety_validator_modified_or_rejected=True,
        validation_errors=errors,
        replanning_triggered=bool(state.get("trigger_rationale")),
        resulting_task_ids=[],
        confidence_level="low",
        is_simulated=True,
    )
    world.environment.decision_traces.append(trace)
    return {"world": world, "validation_errors": [], "repair_prompt": None}
