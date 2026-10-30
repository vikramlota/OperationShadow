import asyncio
import sys
from pathlib import Path

# Ensure project root is in sys.path
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from ultron.schemas.world import (
    WorldModel, MissionState, ScoutState, HawkState, VehicleMode, ConnectionStatus
)
from ultron.schemas.mission import MissionStatus
from ultron.schemas.commands import Position2D, Position3D
from ultron.graph.workflow import build_ultron_graph
from ultron.simulation.mock_vehicles import SimulationHarness
from ultron.state import UltronState


async def main() -> None:
    print("=== INITIALIZING OPERATION SHADOW: ULTRON ===")

    # 1. Initialize the physical reality (World Model)
    # Both set to SIMULATED to test the LangGraph logic without hardware
    initial_world = WorldModel(
        mission=MissionState(
            id="M-DEMO-01",
            objective="Inspect this unknown building. Find the target, identify hazards, and return.",
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

    # 2. Initialize the agent execution context
    initial_state: UltronState = {
        "world": initial_world,
        "proposed_plan": None,
        "validation_errors": [],
        "repair_prompt": None,
        "retry_count": 0,
        "trigger_rationale": None
    }

    # 3. Compile the state machine
    ultron_app = build_ultron_graph()

    print(">>> Dispatching initial mission objective to Planner...")

    # 4. Run the initial planning phase (START -> plan -> validate -> assign -> evaluate)
    current_state = ultron_app.invoke(initial_state)

    # Extract the validated plan
    active_plan = current_state.get("proposed_plan")
    if active_plan:
        print(f"\n[PLAN ACCEPTED] Rationale: {active_plan.rationale}")
        for task in active_plan.tasks:
            priority_label = getattr(task.priority, "name", str(task.priority))
            print(f"  -> Assigned [{task.vehicle}]: {task.type} (Priority {priority_label})")
    else:
        print("\n[CRITICAL FAILURE] LLM failed to produce a valid plan. Graph routed to DEGRADED.")
        return

    # 5. Hand off to the physical (or simulated) execution loop
    print("\n>>> Handing off to execution loop. Awaiting telemetry...")
    harness = SimulationHarness(ultron_app)

    # This loop injects the hazard at T+15s, which wakes up the Replanner
    await harness.run_simulation_loop(current_state)


if __name__ == "__main__":
    asyncio.run(main())
