import asyncio
from typing import Any
from ultron.state import UltronState
from ultron.simulation.generator import SyntheticDataGenerator
from ultron.evaluator.updater import update_world_model


class SimulationHarness:
    """
    Runs the ULTRON state machine using synthetic data for simulated vehicles.
    Mixed mode is supported: one vehicle can be real while the other is simulated.
    """
    def __init__(self, app_graph: Any) -> None:
        self.app = app_graph
        self.generator = SyntheticDataGenerator()

    async def run_simulation_loop(self, initial_state: UltronState) -> None:
        """
        Continuously pumps synthetic data into the world model and triggers the graph.
        """
        current_state = initial_state
        print("=== OPERATION SHADOW: SIMULATION HARNESS STARTED ===")

        while current_state["world"].mission.status != "COMPLETED":
            # 1. Generate synthetic observations based on active tasks
            new_observations = self.generator.generate_telemetry(current_state["world"])

            # 2. Apply observations to the World Model deterministically
            for obs in new_observations:
                source = getattr(obs.source_vehicle, "value", str(obs.source_vehicle))
                print(f"[{source}] Synthetic Observation: {obs.event} at ({obs.location.x}, {obs.location.y})")
                current_state["world"] = update_world_model(current_state["world"], obs)

            # 3. If new data arrived, wake up the state machine to evaluate triggers and replan
            if new_observations:
                current_state = self.app.invoke(current_state)

                replan_plan = current_state.get("proposed_plan")
                if replan_plan and current_state.get("trigger_rationale"):
                    print(f"\n[REPLAN PROCESSED] Trigger: {current_state['trigger_rationale']}")
                    print(f"Rationale: {replan_plan.rationale}")

                # If target was detected, mark mission completed
                if any(obs.event == "target_detected" for obs in new_observations):
                    print("\n>>> Target located and verified. Mission successfully completed!")
                    current_state["world"].mission.status = "COMPLETED"
                    break

                # Check if the graph routed to degraded mode
                if current_state.get("validation_errors") and current_state.get("retry_count", 0) >= 3:
                    print("Mission Degraded: Plan rejected after retries.")
                    break

            await asyncio.sleep(1.0)

        print("=== SIMULATION CONCLUDED ===")
