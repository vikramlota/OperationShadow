#!/usr/bin/env python3
"""Interactive Command-and-Control console for talking to ULTRON."""

import os
import sys
from pathlib import Path

# Ensure project root is available in sys.path
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Load .env if present
try:
    import dotenv
    dotenv.load_dotenv(project_root / ".env")
except ImportError:
    pass

from ultron.schemas.world import (
    WorldModel,
    MissionState,
    ScoutState,
    VehicleMode,
    ConnectionStatus,
    HazardEntity,
)
from ultron.schemas.mission import MissionStatus, VehicleID
from ultron.schemas.commands import Position2D
from ultron.graph.workflow import build_ultron_graph
from ultron.state import UltronState

# Check optional ElevenLabs TTS
tts_client = None
elevenlabs_key = os.getenv("ELEVENLABS_API_KEY")
if elevenlabs_key:
    try:
        from elevenlabs.client import ElevenLabs
        tts_client = ElevenLabs(api_key=elevenlabs_key)
    except Exception as exc:
        print(f"[Warning] Failed to initialize ElevenLabs client: {exc}")


def speak_rationale(text: str) -> None:
    """Stream rationale audio through Arch Linux speakers via ElevenLabs if configured."""
    if not tts_client:
        return
    try:
        from elevenlabs import stream
        audio_stream = tts_client.text_to_speech.stream(
            text=text,
            voice_id="JBFqnCBsd6RMkjVDRZzb",  # George cinematic voice ID
            model_id="eleven_multilingual_v2",
        )
        stream(audio_stream)
    except Exception as exc:
        print(f"  [TTS Audio Stream Notice: {exc}]")


def print_banner() -> None:
    print("\n" + "=" * 68)
    print("      OPERATION SHADOW // TALK TO ULTRON C2 INTERACTIVE CONSOLE")
    print("=" * 68)
    print("Talk to ULTRON in natural language to assign missions to SCOUT01.")
    print("Commands:")
    print("  • Type any mission objective directly (e.g. 'Recon zone 2 and inspect target')")
    print("  • 'status'                  - View live SCOUT01 telemetry & world state")
    print("  • 'hazard <type> <x> <y>'   - Inject physical hazard to trigger replanning")
    print("  • 'lang <English|Hindi|...>' - Set rationale spoken/output language")
    print("  • 'tts on|off'              - Toggle ElevenLabs voice streaming")
    print("  • 'help' or 'quit'          - Commands or exit")
    print("=" * 68 + "\n")


def main() -> None:
    print_banner()

    # Initialize live WorldModel with single ground rover SCOUT01
    world = WorldModel(
        mission=MissionState(
            id="M-TALK-01",
            objective="Standby for operator tasking.",
            status=MissionStatus.IDLE,
        ),
        vehicles={
            "SCOUT01": ScoutState(
                mode=VehicleMode.SIMULATED,
                position=Position2D(x=1.5, y=1.5),
                battery=98.5,
                connection=ConnectionStatus.ONLINE,
            )
        },
    )

    ultron_app = build_ultron_graph()
    current_language = "English"
    use_tts = bool(tts_client)

    if use_tts:
        print("  [Voice Output: ACTIVE via ElevenLabs]")
    else:
        print("  [Voice Output: TEXT ONLY (Set ELEVENLABS_API_KEY in .env for audio)]")

    mission_counter = 1

    while True:
        try:
            prompt_str = f"\nULTRON [SCOUT01 | {current_language}] >> "
            user_input = input(prompt_str).strip()
        except (KeyboardInterrupt, EOFError):
            print("\n[Exiting ULTRON console. Mission Control standing down.]")
            break

        if not user_input:
            continue

        cmd_lower = user_input.lower()
        if cmd_lower in ("quit", "exit", "q"):
            print("[Exiting ULTRON console. Standby.]")
            break

        if cmd_lower == "help":
            print_banner()
            continue

        if cmd_lower == "status":
            scout = world.vehicles["SCOUT01"]
            print(f"\n--- SCOUT01 TELEMETRY READOUT ---")
            print(f"  Connection: {scout.connection.value}")
            print(f"  Mode:       {scout.mode.value}")
            print(f"  Battery:    {scout.battery:.1f}%")
            print(f"  Position:   X={scout.position.x:.2f}m, Y={scout.position.y:.2f}m")
            print(f"  Status:     {scout.status}")
            print(f"  Hazards:    {len(world.environment.hazards)}")
            print(f"  Objects:    {len(world.environment.objects)}")
            print(f"  Explored:   {list(world.environment.explored_areas)}")
            continue

        if cmd_lower.startswith("lang "):
            new_lang = user_input[5:].strip()
            if new_lang:
                current_language = new_lang
                print(f"[Language set to: {current_language}]")
            continue

        if cmd_lower.startswith("tts "):
            toggle = cmd_lower[4:].strip()
            if toggle == "on":
                if not elevenlabs_key:
                    print("[Error] No ELEVENLABS_API_KEY found in .env.")
                else:
                    use_tts = True
                    print("[TTS Voice Streaming ENABLED]")
            elif toggle == "off":
                use_tts = False
                print("[TTS Voice Streaming DISABLED]")
            continue

        if cmd_lower.startswith("hazard "):
            parts = user_input.split()
            if len(parts) >= 4:
                h_type = parts[1]
                try:
                    hx, hy = float(parts[2]), float(parts[3])
                except ValueError:
                    print("[Error] Invalid coordinates. Usage: hazard <type> <x> <y>")
                    continue

                hid = f"H-{len(world.environment.hazards) + 1:02d}"
                world.environment.hazards.append(
                    HazardEntity(
                        id=hid,
                        location=Position2D(x=hx, y=hy),
                        confidence=0.95,
                        source_vehicle=VehicleID.SCOUT,
                        type=h_type,
                    )
                )
                print(f"\n[PHYSICAL HAZARD INJECTED: '{h_type}' at ({hx}, {hy})]")
                print(">>> Triggering ULTRON Replanner with environmental observation...")

                state: UltronState = {
                    "world": world,
                    "proposed_plan": getattr(world.mission, "active_plan", None),
                    "validation_errors": [],
                    "repair_prompt": None,
                    "retry_count": 0,
                    "trigger_rationale": None,
                    "language": current_language,
                }
                state = ultron_app.invoke(
                    state, {"config": {"configurable": {"start_node": "evaluate"}}}
                )
                replan = state.get("proposed_plan")
                if replan:
                    print(f"\n\033[1;36m[ULTRON REPLAN RATIONALE]\033[0m: {replan.rationale}")
                    for task in replan.tasks:
                        p_label = getattr(task.priority, "name", str(task.priority))
                        area_str = f" in {task.area}" if task.area else ""
                        target_str = f" target={task.target}" if task.target else ""
                        print(f"  \033[1;32m✓\033[0m [{task.vehicle}] {task.type} (Priority: {p_label}){area_str}{target_str}")
                    if use_tts:
                        speak_rationale(replan.rationale)
                else:
                    print("[ULTRON] Replan completed without active adjustments.")
                continue
            else:
                print("[Usage] hazard <type> <x> <y> (e.g. hazard fire 4.5 3.0)")
                continue

        # Standard natural language mission submission
        mission_id = f"M-TALK-{mission_counter:03d}"
        mission_counter += 1
        world.mission.id = mission_id
        world.mission.objective = user_input
        world.mission.status = MissionStatus.IN_PROGRESS

        state = {
            "world": world,
            "proposed_plan": None,
            "validation_errors": [],
            "repair_prompt": None,
            "retry_count": 0,
            "trigger_rationale": None,
            "language": current_language,
        }

        print(f"\n>>> ULTRON is processing mission [{mission_id}]...")
        result_state = ultron_app.invoke(state)
        plan = result_state.get("proposed_plan")

        if plan:
            print(f"\n\033[1;36m[ULTRON RATIONALE]\033[0m: {plan.rationale}")
            print("\033[1;33m[DISPATCHED TASKS FOR SCOUT01]:\033[0m")
            for task in plan.tasks:
                p_label = getattr(task.priority, "name", str(task.priority))
                area_str = f" | Area: {task.area}" if task.area else ""
                target_str = f" | Target: {task.target}" if task.target else ""
                print(f"  \033[1;32m➜\033[0m Task [{task.id}]: {task.type} (Priority: {p_label}){area_str}{target_str}")

            if use_tts:
                print("  [Streaming cinematic voice rationale through speakers...]")
                speak_rationale(plan.rationale)
        else:
            errors = result_state.get("validation_errors", [])
            print(f"\n[ULTRON REJECTED PLAN] Validation errors: {errors}")


if __name__ == "__main__":
    main()
