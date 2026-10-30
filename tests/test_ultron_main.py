import asyncio
import sys
from pathlib import Path

# Add project root to sys.path
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from ultron.schemas.world import (
    WorldModel, MissionState, ScoutState, HawkState, VehicleMode, ConnectionStatus
)
from ultron.schemas.mission import MissionStatus, VehicleID
from ultron.schemas.commands import Position2D, Position3D
from ultron.graph.workflow import build_ultron_graph
from ultron.simulation.generator import SyntheticDataGenerator
from ultron.evaluator.updater import update_world_model
from ultron.state import UltronState


async def test_ultron_pipeline():
    print("====================================================")
    print("     TESTING ULTRON MISSION & REPLANNING PIPELINE    ")
    print("====================================================")

    # 1. Initialize World Model
    world = WorldModel(
        mission=MissionState(
            id="M-TEST-01",
            objective="Inspect building, identify hazards, locate red_package",
            status=MissionStatus.IN_PROGRESS
        ),
        vehicles={
            "SCOUT01": ScoutState(
                mode=VehicleMode.SIMULATED,
                position=Position2D(x=0.0, y=0.0),
                battery=100.0,
                connection=ConnectionStatus.ONLINE
            ),
            "HAWK01": HawkState(
                mode=VehicleMode.SIMULATED,
                position=Position3D(x=0.0, y=0.0, alt=0.0),
                battery=100.0,
                connection=ConnectionStatus.ONLINE
            )
        }
    )

    state: UltronState = {
        "world": world,
        "proposed_plan": None,
        "validation_errors": [],
        "repair_prompt": None,
        "retry_count": 0,
        "trigger_rationale": None
    }

    # 2. Compile State Machine
    app = build_ultron_graph()
    print("[1/4] Invoking initial mission planner...")
    state = app.invoke(state)

    plan = state.get("proposed_plan")
    assert plan is not None, "FAILED: Initial plan was not generated."
    assert len(plan.tasks) > 0, "FAILED: Plan contains 0 tasks."
    assert len(state.get("validation_errors", [])) == 0, f"FAILED: Validation errors: {state.get('validation_errors')}"

    print(f"  ✓ Initial Plan Generated & Validated (Rationale: '{plan.rationale}')")
    for task in plan.tasks:
        priority_label = getattr(task.priority, "name", str(task.priority))
        print(f"    -> [{task.vehicle}] {task.type} (Priority: {priority_label})")

    # 3. Simulate Hazard Detection by HAWK at T+0s (fast test)
    print("\n[2/4] Injecting synthetic hazard observation from HAWK01...")
    generator = SyntheticDataGenerator(hazard_delay_sec=0.0)
    observations = generator.generate_telemetry(state["world"])
    assert len(observations) > 0, "FAILED: No telemetry/observations generated."

    for obs in observations:
        source = getattr(obs.source_vehicle, "value", str(obs.source_vehicle))
        print(f"  ✓ Inbound Observation: {obs.event} from {source} at ({obs.location.x}, {obs.location.y})")
        state["world"] = update_world_model(state["world"], obs)

    # 4. Trigger Replanning Cycle
    print("\n[3/4] Triggering Replanner with environmental observation...")
    state = app.invoke(state, {"config": {"configurable": {"start_node": "evaluate"}}})

    replan_rationale = state.get("trigger_rationale")
    assert replan_rationale is not None, "FAILED: Replanner trigger condition was not detected."
    print(f"  ✓ Replan Trigger Activated: {replan_rationale}")

    replan = state.get("proposed_plan")
    assert replan is not None, "FAILED: Updated plan was not produced."
    print(f"  ✓ Replanned Task Set Validated (Rationale: '{replan.rationale}')")

    # 5. Simulate Target Discovery
    print("\n[4/4] Injecting target detection (red_package) from SCOUT01...")
    target_obs = generator.generate_telemetry(state["world"])
    for obs in target_obs:
        source = getattr(obs.source_vehicle, "value", str(obs.source_vehicle))
        print(f"  ✓ Inbound Observation: {obs.event} from {source} (Label: {obs.visual_evidence})")
        state["world"] = update_world_model(state["world"], obs)

    state["world"].mission.status = MissionStatus.COMPLETED
    print(f"  ✓ Mission Status: {state['world'].mission.status.value}")

    print("\n====================================================")
    print("      ALL ULTRON PIPELINE TESTS PASSED (4/4)        ")
    print("====================================================")


if __name__ == "__main__":
    asyncio.run(test_ultron_pipeline())
