"""Deterministic Standing Orders definition, registry, and evaluator (`ultron/events/standing_orders.py`).

Responsibilities:
1. Load and validate declarative Standing Orders from `configs/standing_orders.json`
   using strict Pydantic v2 models (`StandingOrderConfig`, `StandingOrderDefinition`),
   failing fast (`StandingOrderConfigError`) if any configured task type, vehicle,
   priority, area/target, or duplicate ID is invalid.
2. Evaluate incoming `UltronEvent` instances against registered Standing Orders in
   deterministic priority order (`priority` DESC, combined with event severity).
3. Enforce preconditions and required World Model evidence before selecting a
   Standing Order (never executing on missing/unverified evidence, E-Stop, or
   inactive missions).
4. Construct the deterministic `MissionPlan` using `build_evidence_grounded_plan()`
   and the Standing Order's validated response template, preserving the mission's
   actual `objective`, carrying verified `evidence_ids`, keeping `"USED CACHED PLAN"`
   in `rationale`, and verifying the output through `validate_plan_evidence()` and
   `validate_plan()`.
5. Enforce per-order signature/entity cooldown and handled-condition tracking so
   repeated triggers of an already-handled condition do not repeatedly recreate tasks.
"""

from __future__ import annotations

from collections import OrderedDict
import json
import logging
import math
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ultron.config import CONFIGS_DIR
from ultron.events.router import compute_event_signature
from ultron.events.schemas import EventTypeEnum, UltronEvent, priority_rank
from ultron.planner.engine import build_evidence_grounded_plan
from ultron.planner.grounding import validate_plan_evidence
from ultron.safety.constraints import SCOUT_ALLOWED_ACTIONS
from ultron.safety.validator import validate_plan
from ultron.schemas.mission import MissionPlan, MissionStatus, Task, TaskPriority, VehicleID
from ultron.schemas.observations import EpistemicStatusEnum
from ultron.schemas.world import WorldModel

logger = logging.getLogger("operation_shadow.ultron.standing_orders")

_STANDARD_VALIDATOR_AREAS = {
    "sector_a",
    "sector_b",
    "zone_1",
    "zone_2",
    "corridor",
    "building",
    "arena",
}
_STANDARD_VALIDATOR_TARGETS = {
    "red_package",
    "target",
    "hazards",
    "hazard",
    "package",
    "thermal_anomaly",
    "obstacle",
}


class StandingOrderConfigError(ValueError):
    """Raised when a Standing Order configuration file or definition is invalid."""


class StandingOrderTaskSpec(BaseModel):
    """Declarative task template entry inside a Standing Order."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    vehicle: VehicleID = Field(default=VehicleID.SCOUT)
    type: str = Field(min_length=1, max_length=64)
    area: Optional[str] = Field(default=None, max_length=128)
    target: Optional[str] = Field(default=None, max_length=128)
    priority: int = Field(ge=1, le=3)

    @field_validator("vehicle")
    @classmethod
    def _validate_scout_only(cls, value: VehicleID) -> VehicleID:
        if value != VehicleID.SCOUT:
            raise ValueError(
                f"Standing Order task vehicle must be 'SCOUT01', got '{value}'."
            )
        return value

    @field_validator("type")
    @classmethod
    def _validate_action_type(cls, value: str) -> str:
        upper = str(value).strip().upper()
        if upper not in SCOUT_ALLOWED_ACTIONS:
            raise ValueError(
                f"Task type '{value}' is not in SCOUT_ALLOWED_ACTIONS ({sorted(SCOUT_ALLOWED_ACTIONS)})."
            )
        return upper

    @field_validator("area")
    @classmethod
    def _validate_standard_area(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if cleaned.lower() not in _STANDARD_VALIDATOR_AREAS:
            raise ValueError(
                f"Standing Order task area '{cleaned}' is not in standard validator areas "
                f"({sorted(_STANDARD_VALIDATOR_AREAS)})."
            )
        return cleaned

    @field_validator("target")
    @classmethod
    def _validate_standard_target(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if cleaned.lower() not in _STANDARD_VALIDATOR_TARGETS:
            raise ValueError(
                f"Standing Order task target '{cleaned}' is not in standard validator targets "
                f"({sorted(_STANDARD_VALIDATOR_TARGETS)})."
            )
        return cleaned


class StandingOrderResponseTemplate(BaseModel):
    """Deterministic response template for a Standing Order."""

    model_config = ConfigDict(extra="forbid")

    branch: Literal["critical_battery", "sensor_conflict", "blocked_route", "hazard_hold"]
    rationale_prefix: str = Field(min_length=1, max_length=256)
    tasks: List[StandingOrderTaskSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_template_invariants(self) -> "StandingOrderResponseTemplate":
        if "USED CACHED PLAN" not in self.rationale_prefix:
            raise ValueError(
                "Standing Order rationale_prefix must include the exact substring 'USED CACHED PLAN'."
            )
        seen_types: set[Tuple[VehicleID, str]] = set()
        for t in self.tasks:
            sig = (t.vehicle, t.type)
            if sig in seen_types:
                raise ValueError(
                    f"Standing Order template contains duplicate concurrent action '{t.type}' for {t.vehicle.value}."
                )
            seen_types.add(sig)
        return self


class StandingOrderPreconditions(BaseModel):
    """Preconditions required before a Standing Order can match an event."""

    model_config = ConfigDict(extra="forbid")

    requires_active_mission: bool = True
    requires_vehicle_online: bool = True
    max_battery_pct: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    min_battery_pct: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    requires_world_evidence: bool = True
    requires_route_blocked: bool = False
    min_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class StandingOrderDefinition(BaseModel):
    """Declarative definition of a single Standing Order."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64, pattern=r"^SO-[A-Z0-9-]+$")
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=1000)
    supported_event_types: List[EventTypeEnum] = Field(min_length=1)
    priority: int = Field(ge=1, le=1000)
    cooldown_sec: float = Field(ge=0.0, le=3600.0, default=10.0)
    operator_intervention_required: bool = False
    preconditions: StandingOrderPreconditions
    required_evidence: List[str] = Field(min_length=1)
    response_template: StandingOrderResponseTemplate


class StandingOrdersConfigFile(BaseModel):
    """Top-level schema for `configs/standing_orders.json`."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1)
    standing_orders: List[StandingOrderDefinition] = Field(min_length=1)

    @model_validator(mode="after")
    def _ensure_unique_ids(self) -> "StandingOrdersConfigFile":
        seen_ids: set[str] = set()
        for order in self.standing_orders:
            if order.id in seen_ids:
                raise ValueError(f"Duplicate Standing Order ID '{order.id}' in configuration.")
            seen_ids.add(order.id)
        return self


class StandingOrderExecutionResult(BaseModel):
    """Result of evaluating and executing a Standing Order for an event."""

    model_config = ConfigDict(extra="forbid")

    matched: bool
    executed: bool
    standing_order_id: Optional[str] = None
    standing_order_name: Optional[str] = None
    operator_intervention_required: bool = False
    status: Literal[
        "executed",
        "no_matching_order",
        "precondition_failed",
        "missing_required_evidence",
        "suppressed_already_handled",
        "validation_failed",
    ]
    reason: str
    plan: Optional[MissionPlan] = None
    validation_errors: List[str] = Field(default_factory=list)


class StandingOrderRegistry:
    """Registry and deterministic evaluator for ULTRON Standing Orders."""

    def __init__(
        self,
        orders: List[StandingOrderDefinition],
        *,
        max_handled_signatures: int = 256,
    ) -> None:
        self._orders_by_id: Dict[str, StandingOrderDefinition] = {}
        for order in sorted(orders, key=lambda o: (-o.priority, o.id)):
            if order.id in self._orders_by_id:
                raise StandingOrderConfigError(f"Duplicate Standing Order ID '{order.id}'.")
            self._orders_by_id[order.id] = order

        self._max_handled_signatures = max(16, int(max_handled_signatures))
        # Maps (standing_order_id, condition_key) -> last_executed_timestamp
        self._executed_conditions: OrderedDict[Tuple[str, str], float] = OrderedDict()

        # Observability counters
        self.executed_count: int = 0
        self.suppressed_repeat_count: int = 0
        self.missing_evidence_count: int = 0
        self.validation_failure_count: int = 0

    @classmethod
    def from_json_file(cls, path: Optional[Path] = None) -> "StandingOrderRegistry":
        """Load and validate Standing Orders from `configs/standing_orders.json`."""
        resolved_path = path if path is not None else (CONFIGS_DIR / "standing_orders.json")
        if not resolved_path.is_file():
            raise StandingOrderConfigError(
                f"Standing Orders configuration file not found: {resolved_path}"
            )
        try:
            raw_data = json.loads(resolved_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise StandingOrderConfigError(
                f"Failed to parse Standing Orders JSON at {resolved_path}: {exc}"
            ) from exc
        return cls.from_dict(raw_data)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StandingOrderRegistry":
        """Validate a configuration dictionary and construct a `StandingOrderRegistry`."""
        try:
            parsed = StandingOrdersConfigFile.model_validate(data)
        except (ValidationError, ValueError, TypeError) as exc:
            raise StandingOrderConfigError(
                f"Invalid Standing Orders configuration: {exc}"
            ) from exc
        return cls(parsed.standing_orders)

    @property
    def orders(self) -> List[StandingOrderDefinition]:
        return list(self._orders_by_id.values())

    def get_order(self, order_id: str) -> Optional[StandingOrderDefinition]:
        return self._orders_by_id.get(order_id)

    def reset_execution_history(self) -> None:
        self._executed_conditions.clear()

    def candidate_orders_for_event(self, event: UltronEvent) -> List[StandingOrderDefinition]:
        """Return registered Standing Orders supporting `event.event_type` ordered by deterministic precedence."""
        ev_rank = priority_rank(event.priority) if event.priority is not None else 1
        candidates = [
            o
            for o in self._orders_by_id.values()
            if event.event_type in o.supported_event_types
        ]
        return sorted(candidates, key=lambda o: (-(o.priority + ev_rank * 10), o.id))

    def has_potential_match(self, event: UltronEvent) -> bool:
        return len(self.candidate_orders_for_event(event)) > 0

    def _condition_dedup_key(self, order: StandingOrderDefinition, event: UltronEvent, world: WorldModel) -> str:
        """Build a stable condition key so an already-handled condition is not re-executed repeatedly."""
        mission_id = world.mission.id or event.mission_id or "M001"
        if order.response_template.branch == "critical_battery":
            return f"{mission_id}:critical_battery:{event.vehicle_id or 'SCOUT01'}"
        if event.tracked_object_ids:
            return f"{mission_id}:{order.id}:{','.join(sorted(event.tracked_object_ids))}"
        if "hazard_id" in event.evidence_payload:
            return f"{mission_id}:{order.id}:{event.evidence_payload['hazard_id']}"
        sig = event.dedup_signature or compute_event_signature(event)
        return f"{mission_id}:{order.id}:{sig}"

    def _check_preconditions(
        self,
        order: StandingOrderDefinition,
        event: UltronEvent,
        world: WorldModel,
    ) -> Tuple[bool, str]:
        pre = order.preconditions

        # 0. Mission lifecycle check
        if pre.requires_active_mission:
            status_val = getattr(
                world.mission.status, "value", str(world.mission.status)
            ).upper()
            if not world.mission.id or status_val not in ("IN_PROGRESS", "ACTIVE", "PLANNING"):
                return False, f"mission_not_active:{status_val}"

        # 1. Vehicle existence & E-Stop check
        scout = world.vehicles.get("SCOUT01")
        if scout is None:
            return False, "scout01_missing_from_world_model"
        status_str = str(scout.status).strip().upper()
        if status_str == "EMERGENCY_STOPPED":
            return False, "scout01_emergency_stopped"

        # 2. Connectivity check
        conn_str = getattr(scout.connection, "value", str(scout.connection)).strip().upper()
        if pre.requires_vehicle_online:
            if conn_str not in ("ONLINE", "CONNECTED"):
                return False, f"scout01_not_online:{conn_str}"
            if status_str in ("OFFLINE", "UNKNOWN", "DISCONNECTED", "FAULT"):
                return False, f"scout01_status_invalid:{status_str}"
            if not getattr(scout, "pose_valid", True):
                return False, "scout01_pose_invalid"

        # 3. Battery bounds check
        bat_raw = getattr(scout, "battery", None)
        if (
            bat_raw is None
            or isinstance(bat_raw, bool)
            or not isinstance(bat_raw, (int, float))
            or not math.isfinite(float(bat_raw))
        ):
            return False, "scout01_battery_unavailable"
        bat_val = float(bat_raw)

        if pre.max_battery_pct is not None and bat_val >= pre.max_battery_pct:
            return (
                False,
                f"battery_{bat_val:.1f}_not_below_max_{pre.max_battery_pct:.1f}",
            )
        if pre.min_battery_pct is not None and bat_val <= pre.min_battery_pct:
            return (
                False,
                f"battery_{bat_val:.1f}_not_above_min_{pre.min_battery_pct:.1f}",
            )

        # 4. Route-blocked precondition
        if pre.requires_route_blocked:
            blocks_route = bool(event.evidence_payload.get("blocks_route", False))
            if not blocks_route and not world.environment.known_obstacles:
                return False, "obstacle_does_not_block_route"

        # 5. Confidence threshold
        if pre.min_confidence is not None:
            if event.confidence is None or event.confidence < pre.min_confidence:
                return (
                    False,
                    f"confidence_{event.confidence}_below_min_{pre.min_confidence:.2f}",
                )

        return True, "ok"

    def _verify_required_world_evidence(
        self,
        order: StandingOrderDefinition,
        event: UltronEvent,
        world: WorldModel,
    ) -> Tuple[bool, List[str], str]:
        """Verify that all required evidence exists in `world` and return `(ok, verified_evidence_ids, reason)`."""
        valid_obs_ids = {o.observation_id for o in world.environment.observation_history}
        valid_tracked_ids = set(world.environment.tracked_objects.keys())
        all_valid_ids = valid_obs_ids.union(valid_tracked_ids)

        branch = order.response_template.branch
        verified_ids: List[str] = [
            eid for eid in event.observation_ids if eid in all_valid_ids
        ]

        if branch == "critical_battery":
            scout = world.vehicles.get("SCOUT01")
            if scout is None or scout.battery >= 20.0:
                return False, [], "missing_critical_battery_evidence_in_world_model"
            return True, verified_ids, "ok"

        if branch == "sensor_conflict":
            conflicted = [
                obj
                for obj in world.environment.tracked_objects.values()
                if not obj.handled and obj.epistemic_status == EpistemicStatusEnum.CONFLICTED
            ]
            if not conflicted:
                return False, [], "no_unhandled_conflicted_object_in_world_model"
            for oid in conflicted[0].contributing_observation_ids:
                if oid in all_valid_ids and oid not in verified_ids:
                    verified_ids.append(oid)
            if not verified_ids and conflicted[0].object_id in all_valid_ids:
                verified_ids.append(conflicted[0].object_id)
            if not verified_ids:
                return False, [], "conflicted_object_lacks_valid_evidence_ids"
            return True, verified_ids, "ok"

        if branch == "blocked_route":
            active_obstacles = [
                o
                for o in world.environment.tracked_objects.values()
                if not o.handled
                and (o.is_obstacle or o.label.lower() in {"chair", "box", "bench", "obstacle", "wall"})
                and o.epistemic_status != EpistemicStatusEnum.STALE
            ]
            if not active_obstacles and not world.environment.known_obstacles:
                return False, [], "no_verified_obstacle_in_world_model"
            for obs_obj in active_obstacles:
                for oid in obs_obj.contributing_observation_ids:
                    if oid in all_valid_ids and oid not in verified_ids:
                        verified_ids.append(oid)
            if active_obstacles and not verified_ids:
                return False, [], "obstacle_lacks_verified_observation_ids"
            return True, verified_ids, "ok"

        if branch == "hazard_hold":
            unhandled_hazards = [
                h for h in world.environment.hazards if not getattr(h, "handled", False)
            ]
            hazard_tracked = [
                o
                for o in world.environment.tracked_objects.values()
                if not o.handled and o.is_hazard and o.epistemic_status != EpistemicStatusEnum.STALE
            ]
            if not unhandled_hazards and not hazard_tracked:
                return False, [], "no_verified_hazard_in_world_model"
            for h in unhandled_hazards:
                if h.observation_id and h.observation_id in all_valid_ids and h.observation_id not in verified_ids:
                    verified_ids.append(h.observation_id)
            for ht in hazard_tracked:
                for oid in ht.contributing_observation_ids:
                    if oid in all_valid_ids and oid not in verified_ids:
                        verified_ids.append(oid)
            if not verified_ids:
                return False, [], "hazard_lacks_verified_observation_ids"
            return True, verified_ids, "ok"

        if order.preconditions.requires_world_evidence and not verified_ids:
            return False, [], "missing_verified_evidence_ids"
        return True, verified_ids, "ok"

    def _build_plan_for_order(
        self,
        order: StandingOrderDefinition,
        event: UltronEvent,
        world: WorldModel,
        verified_evidence_ids: List[str],
    ) -> MissionPlan:
        """Construct a deterministic `MissionPlan` via `build_evidence_grounded_plan` and template tasks."""
        mission_id = world.mission.id or event.mission_id or "M001"
        mission_objective = (
            world.mission.objective.strip()
            if world.mission.objective and world.mission.objective.strip()
            else "Inspect the building, find the target, and report hazards."
        )

        trigger_text = f"[{order.id}] {event.summary}"
        if order.response_template.branch == "blocked_route" and "obstacle" not in trigger_text.lower():
            trigger_text = f"[{order.id}] Route blocked by obstacle: {event.summary}"

        # Reuse the Phase 1 evidence-grounded plan builder for facts, inferences, unknowns, and baseline tasks
        base_plan = build_evidence_grounded_plan(
            world,
            trigger_rationale=trigger_text,
        )

        # Ensure tasks match the Standing Order's validator-compliant template while preserving evidence_ids
        template_tasks: List[Task] = [
            Task(
                id=spec.id,
                vehicle=spec.vehicle,
                type=spec.type,
                area=spec.area,
                target=spec.target,
                priority=TaskPriority(spec.priority),
                evidence_ids=list(verified_evidence_ids),
            )
            for spec in order.response_template.tasks
        ]

        combined_evidence = list(
            dict.fromkeys(list(base_plan.evidence_ids) + list(verified_evidence_ids))
        )
        rationale = f"{order.response_template.rationale_prefix} {event.summary}"
        if "USED CACHED PLAN" not in rationale:
            rationale = f"USED CACHED PLAN — {rationale}"

        candidate = base_plan.model_copy(
            update={
                "mission_id": mission_id,
                "objective": mission_objective,
                "rationale": rationale,
                "tasks": template_tasks,
                "evidence_ids": combined_evidence,
            }
        )
        validated_plan, _ = validate_plan_evidence(world, candidate)
        return validated_plan

    def evaluate_and_execute(
        self,
        event: UltronEvent,
        world: WorldModel,
        *,
        specific_order_id: Optional[str] = None,
    ) -> StandingOrderExecutionResult:
        """Evaluate `event` against registered Standing Orders (or `specific_order_id`) and return a validated `MissionPlan`."""
        if specific_order_id is not None:
            order = self.get_order(specific_order_id)
            if order is None:
                return StandingOrderExecutionResult(
                    matched=False,
                    executed=False,
                    standing_order_id=specific_order_id,
                    status="no_matching_order",
                    reason=f"unregistered_standing_order:{specific_order_id}",
                )
            candidates = [order]
        else:
            candidates = self.candidate_orders_for_event(event)

        if not candidates:
            return StandingOrderExecutionResult(
                matched=False,
                executed=False,
                status="no_matching_order",
                reason=f"no_standing_order_for_event_type:{event.event_type.value}",
            )

        last_failure_status: Literal[
            "precondition_failed",
            "missing_required_evidence",
            "suppressed_already_handled",
            "validation_failed",
        ] = "precondition_failed"
        last_failure_reason = "no_candidate_satisfied_preconditions"
        last_order_id: Optional[str] = None
        last_order_name: Optional[str] = None

        for order in candidates:
            last_order_id = order.id
            last_order_name = order.name

            # 1. Preconditions check
            pre_ok, pre_reason = self._check_preconditions(order, event, world)
            if not pre_ok:
                last_failure_status = "precondition_failed"
                last_failure_reason = f"{order.id}:{pre_reason}"
                continue

            # 2. Required World Model evidence check
            ev_ok, verified_ids, ev_reason = self._verify_required_world_evidence(
                order, event, world
            )
            if not ev_ok:
                self.missing_evidence_count += 1
                last_failure_status = "missing_required_evidence"
                last_failure_reason = f"{order.id}:{ev_reason}"
                continue

            # 3. Duplicate / cooldown suppression for already-handled condition
            cond_key = (order.id, self._condition_dedup_key(order, event, world))
            prev_ts = self._executed_conditions.get(cond_key)
            if prev_ts is not None and (event.received_at - prev_ts) < order.cooldown_sec:
                self.suppressed_repeat_count += 1
                return StandingOrderExecutionResult(
                    matched=True,
                    executed=False,
                    standing_order_id=order.id,
                    standing_order_name=order.name,
                    operator_intervention_required=order.operator_intervention_required,
                    status="suppressed_already_handled",
                    reason=f"condition_already_handled_within_{order.cooldown_sec:.1f}s",
                )

            # 4. Build deterministic plan and validate via Safety Validator
            plan = self._build_plan_for_order(order, event, world, verified_ids)
            is_valid, val_errors = validate_plan(plan, world)
            if not is_valid:
                self.validation_failure_count += 1
                logger.warning(
                    "Standing Order %s generated plan rejected by safety validator: %s",
                    order.id,
                    val_errors,
                )
                return StandingOrderExecutionResult(
                    matched=True,
                    executed=False,
                    standing_order_id=order.id,
                    standing_order_name=order.name,
                    operator_intervention_required=order.operator_intervention_required,
                    status="validation_failed",
                    reason=f"safety_validator_rejected:{'; '.join(val_errors)}",
                    validation_errors=val_errors,
                )

            # Record handled condition with bounded LRU
            self._executed_conditions[cond_key] = event.received_at
            self._executed_conditions.move_to_end(cond_key)
            while len(self._executed_conditions) > self._max_handled_signatures:
                self._executed_conditions.popitem(last=False)

            self.executed_count += 1
            return StandingOrderExecutionResult(
                matched=True,
                executed=True,
                standing_order_id=order.id,
                standing_order_name=order.name,
                operator_intervention_required=order.operator_intervention_required,
                status="executed",
                reason="standing_order_executed",
                plan=plan,
            )

        return StandingOrderExecutionResult(
            matched=False,
            executed=False,
            standing_order_id=last_order_id,
            standing_order_name=last_order_name,
            status=last_failure_status,
            reason=last_failure_reason,
        )
