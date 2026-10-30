"""Environment-driven replanning trigger evaluator with debouncing (`ultron/replanner/triggers.py` — Section 8).

Evaluates the `WorldModel` for material environmental or vehicle changes:
1. Unhandled hazard reported overlapping active mission space.
2. Conflicting sensor evidence (`EpistemicStatusEnum.CONFLICTED`) requiring verification.
3. Target discovered or target position shifted significantly.
4. Newly detected obstacle blocking the planned route (`known_obstacles` or `TrackedObject` with `is_obstacle=True`).
5. Sensor becomes disconnected or errors out while enabled.
6. Relied-upon tracked observation becomes `STALE`.
7. Vehicle reports critical battery (`< 20%`), connection loss (`OFFLINE`/`UNKNOWN`), or fault.

Includes deterministic signature + timestamp debouncing so minor sensor noise does
not continuously retrigger replanning.
"""

from __future__ import annotations

import time
from typing import Optional, Tuple

from ultron.config import config as ultron_config
from ultron.schemas.observations import EpistemicStatusEnum
from ultron.schemas.world import (
    SensorHealthStatusEnum,
    TrackingStatusEnum,
    WorldModel,
)


def _should_debounce(
    world: WorldModel,
    signature: str,
    now: float,
    debounce_sec: float,
) -> bool:
    if debounce_sec <= 0.0:
        return False
    if (
        world.environment.last_replan_signature == signature
        and world.environment.last_replan_timestamp is not None
        and (now - world.environment.last_replan_timestamp) < debounce_sec
    ):
        return True
    return False


def _record_trigger(world: WorldModel, signature: str, now: float) -> None:
    world.environment.last_replan_signature = signature
    world.environment.last_replan_timestamp = now


def check_replanning_triggers(
    world: WorldModel,
    *,
    current_time: Optional[float] = None,
    debounce_sec: Optional[float] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Evaluates the World Model to determine if a dynamic environment-aware replan is required.
    Returns `(should_replan, trigger_rationale)`.
    """
    now = current_time if current_time is not None else time.time()
    effective_debounce = (
        debounce_sec if debounce_sec is not None else 0.0
    )

    # 1. Unhandled Hazard Reported Overlapping Active Area
    unhandled_hazards = [
        h for h in world.environment.hazards if not getattr(h, "handled", False)
    ]
    if unhandled_hazards:
        latest_hazard = unhandled_hazards[-1]
        source_id = getattr(
            latest_hazard.source_vehicle, "value", str(latest_hazard.source_vehicle)
        )
        sig = f"hazard:{latest_hazard.id}:{latest_hazard.type}"
        if not _should_debounce(world, sig, now, effective_debounce):
            _record_trigger(world, sig, now)
            return True, (
                f"Hazard '{latest_hazard.type}' reported by {source_id} "
                f"at ({latest_hazard.location.x}, {latest_hazard.location.y}) "
                f"(confidence {latest_hazard.confidence:.2f}). Rerouting active routes."
            )

    # 2. Conflicting Sensor Evidence (`CONFLICTED`)
    for obj in world.environment.tracked_objects.values():
        if (
            not obj.handled
            and obj.epistemic_status == EpistemicStatusEnum.CONFLICTED
        ):
            sig = f"conflict:{obj.object_id}"
            if not _should_debounce(world, sig, now, effective_debounce):
                _record_trigger(world, sig, now)
                return True, (
                    f"Conflicting sensor evidence on {obj.object_id} ('{obj.label}') "
                    f"in frame '{obj.coordinate_frame}': {obj.conflict_details or 'position mismatch'}."
                )

    # 3. Target Discovered (TrackedObject or legacy ObjectEntity)
    obj_words = {
        w.lower()
        for w in world.mission.objective.replace(",", " ").replace(".", " ").split()
        if len(w) > 2
    }
    for tobj in world.environment.tracked_objects.values():
        if not tobj.handled and not tobj.is_obstacle and not tobj.is_hazard:
            if (
                tobj.label.lower() == world.mission.objective.lower()
                or tobj.label.lower() in {"red_package", "target"}
                or tobj.label.lower() in obj_words
            ):
                sig = f"target_tracked:{tobj.object_id}:{round(tobj.position.x, 1)}:{round(tobj.position.y, 1)}"
                if not _should_debounce(world, sig, now, effective_debounce):
                    _record_trigger(world, sig, now)
                    return True, (
                        f"Target '{tobj.label}' ({tobj.object_id}) located by {tobj.last_known_source} "
                        f"at ({tobj.position.x:.2f}, {tobj.position.y:.2f}) in '{tobj.coordinate_frame}' "
                        f"(observations: {', '.join(tobj.contributing_observation_ids)}). Initiating target response sequence."
                    )

    for obj in world.environment.objects:
        if not getattr(obj, "handled", False) and (
            obj.label == world.mission.objective or obj.label == "red_package"
        ):
            source_id = getattr(obj.source_vehicle, "value", str(obj.source_vehicle))
            sig = f"target_legacy:{obj.id}"
            if not _should_debounce(world, sig, now, effective_debounce):
                _record_trigger(world, sig, now)
                return True, (
                    f"Target '{obj.label}' located by {source_id} "
                    f"at ({obj.location.x}, {obj.location.y}). Initiating mission completion sequence."
                )

    # 4. Newly Detected Obstacle Intersecting Route / Path Blocked
    for tobj in world.environment.tracked_objects.values():
        if (
            not tobj.handled
            and tobj.is_obstacle
            and tobj.epistemic_status != EpistemicStatusEnum.STALE
        ):
            range_m = tobj.position.computed_range_m()
            if range_m <= max(2.5, ultron_config.route_obstacle_clearance_m * 3.0):
                sig = f"obstacle_tracked:{tobj.object_id}:{round(tobj.position.x, 1)}:{round(tobj.position.y, 1)}"
                if not _should_debounce(world, sig, now, effective_debounce):
                    _record_trigger(world, sig, now)
                    return True, (
                        f"Obstacle '{tobj.label}' ({tobj.object_id}) detected at range {range_m:.2f}m "
                        f"pos=({tobj.position.x:.2f}, {tobj.position.y:.2f}) in '{tobj.coordinate_frame}' "
                        f"(obs={', '.join(tobj.contributing_observation_ids)}). Planned route obstructed; replanning required."
                    )

    if world.environment.known_obstacles:
        latest_obstacle = world.environment.known_obstacles[-1]
        sig = f"obstacle_map:{round(latest_obstacle.x, 2)}:{round(latest_obstacle.y, 2)}"
        if not _should_debounce(world, sig, now, effective_debounce):
            _record_trigger(world, sig, now)
            return (
                True,
                f"SCOUT01 reported path blocked at ({latest_obstacle.x}, {latest_obstacle.y}). Rerouting required.",
            )

    # 5. Enabled Sensor Failure / Disconnection or Stale Relied-Upon Object
    for sid, sh in world.environment.sensor_health.items():
        if sh.enabled and sh.status in (
            SensorHealthStatusEnum.DISCONNECTED,
            SensorHealthStatusEnum.ERROR,
        ) and sh.valid_message_count > 0:
            sig = f"sensor_loss:{sid}:{sh.status.value}"
            if not _should_debounce(world, sig, now, effective_debounce):
                _record_trigger(world, sig, now)
                return (
                    True,
                    f"Sensor '{sid}' ({sh.sensor_type.value}) transitioned to {sh.status.value}; "
                    "replanning under degraded perception constraints.",
                )

    for tobj in world.environment.tracked_objects.values():
        if (
            not tobj.handled
            and tobj.epistemic_status == EpistemicStatusEnum.STALE
            and tobj.tracking_status == TrackingStatusEnum.STALE
        ):
            sig = f"stale_obj:{tobj.object_id}"
            if not _should_debounce(world, sig, now, effective_debounce):
                _record_trigger(world, sig, now)
                return (
                    True,
                    f"Observation for '{tobj.label}' ({tobj.object_id}) has become STALE; "
                    "re-verification scan required before acting on last known position.",
                )

    # 6. Vehicle Degradation or Failure
    for vid, state in world.vehicles.items():
        if state.battery < 20.0:
            sig = f"battery_critical:{vid}"
            if not _should_debounce(world, sig, now, effective_debounce):
                _record_trigger(world, sig, now)
                return (
                    True,
                    f"Critical battery ({state.battery:.1f}%) reported by {vid}. Issuing safe return command.",
                )
        conn_val = state.connection.value if hasattr(state.connection, "value") else str(state.connection)
        if conn_val in ("OFFLINE", "UNKNOWN"):
            sig = f"conn_loss:{vid}:{conn_val}"
            if not _should_debounce(world, sig, now, effective_debounce):
                _record_trigger(world, sig, now)
                return (
                    True,
                    f"Telemetry connection lost ({conn_val}) for {vid}. Reassigning critical tasks to remaining active vehicles.",
                )
        if state.status in ("FAULT", "EMERGENCY_STOPPED"):
            sig = f"vehicle_state:{vid}:{state.status}"
            if not _should_debounce(world, sig, now, effective_debounce):
                _record_trigger(world, sig, now)
                return (
                    True,
                    f"Vehicle {vid} reported unexpected state '{state.status}'. Halting active task dispatch.",
                )

    return False, None
