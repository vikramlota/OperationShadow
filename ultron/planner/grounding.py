"""Evidence-grounding verification and environmental query engine (`ultron/planner/grounding.py` — Section 7).

Responsibilities:
1. Enforce application-level validation of evidence references (`validate_grounded_response`
   and `validate_plan_evidence`) so ULTRON cannot return fabricated observation IDs,
   present stale observations as current, claim an area is clear without LiDAR/sensor
   coverage, or claim multi-sensor corroboration when fusion has not validated it.
2. Answer natural-language or structured environmental queries (`answer_environmental_query`)
   strictly using `WorldModelTools` retrieval (`get_environment_snapshot`,
   `get_detected_objects`, `get_object_details`, `get_free_space`, `get_sensor_health`,
   `get_vehicle_pose`), returning a validated `GroundedEnvironmentalResponse`.
"""

from __future__ import annotations

import re
import time
from typing import List, Optional, Tuple

from ultron.config import config as ultron_config
from ultron.schemas.mission import MissionPlan
from ultron.schemas.observations import (
    EpistemicStatusEnum,
    SensorSourceModeEnum,
)
from ultron.schemas.world import (
    EvidenceReference,
    GroundedEnvironmentalResponse,
    TrackedObject,
    WorldModel,
)
from ultron.world_model.tools import WorldModelTools


def validate_grounded_response(
    world: WorldModel,
    response: GroundedEnvironmentalResponse,
    *,
    current_time: Optional[float] = None,
) -> GroundedEnvironmentalResponse:
    """Application-level verification of a `GroundedEnvironmentalResponse` (§7).

    Enforces:
    1. Every `EvidenceReference.observation_id` must exist in `world.environment.observation_history`
       (or `object_id` in `world.environment.tracked_objects`). Nonexistent references are stripped
       and flagged in `warnings`, and unsupported `facts` referencing them are demoted to `unknowns`.
    2. If any referenced observation is older than `camera_stale_after_sec`, its epistemic status
       is forced to `STALE` and a staleness warning is added so old observations are never presented
       as current.
    3. If a claim or evidence reference asserts `CORROBORATED` when no multi-sensor corroboration
       exists on the tracked object, it is downgraded to `OBSERVED` with a warning.
    4. If a fact claims an area is "clear" or "free of obstacles" when `get_free_space` is not
       verified clear, that claim is rejected and moved to `unknowns`/`warnings`.
    """
    now = current_time if current_time is not None else time.time()
    stale_thresh = ultron_config.camera_stale_after_sec
    tools = WorldModelTools(world, current_time=now)
    free_space = tools.get_free_space()
    sensor_health = tools.get_sensor_health()

    obs_by_id = {o.observation_id: o for o in world.environment.observation_history}
    obj_by_id = world.environment.tracked_objects

    valid_evidence: List[EvidenceReference] = []
    invalid_ids: List[str] = []
    warnings = list(response.warnings)
    facts = list(response.facts)
    inferences = list(response.inferences)
    unknowns = list(response.unknowns)
    has_stale_evidence = False
    has_conflicted_evidence = False

    for ev in response.evidence:
        obs = obs_by_id.get(ev.observation_id)
        tracked = obj_by_id.get(ev.object_id) if ev.object_id else None

        if obs is None and tracked is None:
            invalid_ids.append(ev.observation_id)
            warnings.append(
                f"Rejected nonexistent evidence reference '{ev.observation_id}' (not found in World Model)."
            )
            continue

        # Overwrite timestamp, frame, confidence, and source_mode from authoritative World Model
        if obs is not None:
            age = now - obs.sensor_timestamp
            is_stale = age > stale_thresh
            parent_tracked: Optional[TrackedObject] = tracked
            if parent_tracked is None:
                for cand in obj_by_id.values():
                    if obs.observation_id in cand.contributing_observation_ids:
                        parent_tracked = cand
                        break

            if parent_tracked is not None and parent_tracked.epistemic_status == EpistemicStatusEnum.CONFLICTED:
                ep_status = EpistemicStatusEnum.CONFLICTED
                has_conflicted_evidence = True
            elif is_stale:
                ep_status = EpistemicStatusEnum.STALE
                has_stale_evidence = True
                warnings.append(
                    f"Observation '{obs.observation_id}' is STALE (age {age:.2f}s > {stale_thresh:.1f}s); "
                    "cannot be treated as a current observation."
                )
            elif (
                parent_tracked is not None
                and parent_tracked.corroborated_by_multiple_sensors
            ):
                ep_status = EpistemicStatusEnum.CORROBORATED
            else:
                if ev.epistemic_status == EpistemicStatusEnum.CORROBORATED:
                    warnings.append(
                        f"Downgraded '{obs.observation_id}' from CORROBORATED to OBSERVED: "
                        "multi-sensor fusion has not corroborated this observation."
                    )
                ep_status = EpistemicStatusEnum.OBSERVED

            valid_evidence.append(
                EvidenceReference(
                    observation_id=obs.observation_id,
                    object_id=parent_tracked.object_id if parent_tracked else ev.object_id,
                    sensor=obs.sensor_id,
                    sensor_type=obs.sensor_type.value,
                    timestamp=obs.sensor_timestamp,
                    coordinate_frame=obs.coordinate_frame,
                    confidence=obs.confidence,
                    epistemic_status=ep_status,
                    source_mode=obs.source_mode,
                )
            )
        elif tracked is not None:
            age = now - tracked.last_observed_timestamp
            is_stale = age > stale_thresh
            if tracked.epistemic_status == EpistemicStatusEnum.CONFLICTED:
                ep_status = EpistemicStatusEnum.CONFLICTED
                has_conflicted_evidence = True
            elif is_stale:
                ep_status = EpistemicStatusEnum.STALE
                has_stale_evidence = True
            elif tracked.corroborated_by_multiple_sensors:
                ep_status = EpistemicStatusEnum.CORROBORATED
            else:
                ep_status = EpistemicStatusEnum.OBSERVED

            first_obs_id = (
                tracked.contributing_observation_ids[-1]
                if tracked.contributing_observation_ids
                else tracked.object_id
            )
            valid_evidence.append(
                EvidenceReference(
                    observation_id=first_obs_id,
                    object_id=tracked.object_id,
                    sensor=tracked.last_known_source,
                    sensor_type=(
                        tracked.contributing_sensor_types[-1]
                        if tracked.contributing_sensor_types
                        else "depth_camera"
                    ),
                    timestamp=tracked.last_observed_timestamp,
                    coordinate_frame=tracked.coordinate_frame,
                    confidence=tracked.classification_confidence,
                    epistemic_status=ep_status,
                    source_mode=tracked.source_mode,
                )
            )

    # Filter facts for unsupported claims (nonexistent evidence IDs, unverified "area is clear", or unverified corroboration)
    validated_facts: List[str] = []
    for fact in facts:
        fact_lower = fact.lower()
        if any(bad_id.lower() in fact_lower for bad_id in invalid_ids):
            unknowns.append(f"Rejected unsupported claim referencing nonexistent observation: {fact}")
            continue
        if (
            ("area is clear" in fact_lower or "path is clear" in fact_lower or "space is clear" in fact_lower or "no obstacles" in fact_lower)
            and not free_space.is_verified_clear
        ):
            warnings.append(
                "Rejected unsupported claim that area/path is clear: active geometric LiDAR coverage is unavailable or unverified."
            )
            unknowns.append(
                "Free-space clearance is UNKNOWN (absence of detections does not guarantee a clear area)."
            )
            continue
        if (
            "corroborated by lidar" in fact_lower or "both sensors confirm" in fact_lower
        ) and not any(
            e.epistemic_status == EpistemicStatusEnum.CORROBORATED for e in valid_evidence
        ):
            warnings.append(
                "Rejected unverified multi-sensor corroboration claim: fusion prerequisites are not satisfied."
            )
            continue
        validated_facts.append(fact)

    # If the response made factual claims with zero valid evidence in the World Model, qualify the answer
    answer_text = response.answer
    confidence = response.confidence

    if invalid_ids and not valid_evidence:
        answer_text = (
            "Cannot verify claim: referenced observation IDs do not exist in the World Model. "
            "Insufficient evidence to support factual conclusion."
        )
        validated_facts = []
        confidence = "unavailable"
    elif has_conflicted_evidence:
        confidence = "low"
        if "conflict" not in answer_text.lower():
            answer_text += " [WARNING: Supporting sensor observations are CONFLICTED.]"
    elif has_stale_evidence:
        if confidence == "high":
            confidence = "low"
        if "stale" not in answer_text.lower():
            answer_text += " [NOTE: Observation is STALE and may not reflect current positions.]"

    # Determine overall source mode (simulated if any contributing source or vehicle is simulated)
    overall_source_mode = SensorSourceModeEnum.SIMULATED
    if valid_evidence and all(
        e.source_mode == SensorSourceModeEnum.PHYSICAL for e in valid_evidence
    ):
        overall_source_mode = SensorSourceModeEnum.PHYSICAL

    return GroundedEnvironmentalResponse(
        answer=answer_text,
        facts=validated_facts,
        inferences=inferences,
        unknowns=unknowns,
        warnings=list(dict.fromkeys(warnings)),
        evidence=valid_evidence,
        confidence=confidence,
        source_mode=overall_source_mode,
    )


def answer_environmental_query(
    world: WorldModel,
    query: str,
    *,
    current_time: Optional[float] = None,
) -> GroundedEnvironmentalResponse:
    """Answer an environmental question strictly from verified World Model evidence (§6, §7).

    Uses `WorldModelTools` to inspect current objects, observations, free space,
    sensor health, and vehicle pose. Never invents objects, distances, or clear space.
    """
    now = current_time if current_time is not None else time.time()
    tools = WorldModelTools(world, current_time=now)
    snapshot = tools.get_environment_snapshot()
    health = snapshot.sensor_health
    detected = snapshot.detected_objects
    free_space = snapshot.free_space
    q_lower = query.lower().strip()

    facts: List[str] = []
    inferences: List[str] = []
    unknowns: List[str] = []
    warnings: List[str] = list(snapshot.warnings)
    evidence: List[EvidenceReference] = []

    if not health.lidar_available:
        unknowns.append("LiDAR geometric scan and multi-sensor corroboration are UNAVAILABLE.")

    # Check if user is asking whether an area/path is clear or free of obstacles
    if any(phrase in q_lower for phrase in ("is the area clear", "is the path clear", "free space", "safe to move", "clear of obstacles")):
        if free_space.is_verified_clear and free_space.free_space_radius_m is not None:
            ans = (
                f"Free space is verified up to {free_space.free_space_radius_m:.2f}m "
                f"in frame '{free_space.coordinate_frame}' based on active LiDAR coverage."
            )
            facts.append(ans)
            return validate_grounded_response(
                world,
                GroundedEnvironmentalResponse(
                    answer=ans,
                    facts=facts,
                    inferences=inferences,
                    unknowns=unknowns,
                    warnings=warnings,
                    evidence=evidence,
                    confidence="high" if health.all_healthy else "medium",
                ),
                current_time=now,
            )
        else:
            ans = (
                f"Cannot determine that the area is clear: {free_space.reason}"
            )
            unknowns.append(free_space.reason)
            return validate_grounded_response(
                world,
                GroundedEnvironmentalResponse(
                    answer=ans,
                    facts=facts,
                    inferences=inferences,
                    unknowns=list(dict.fromkeys(unknowns)),
                    warnings=warnings,
                    evidence=evidence,
                    confidence="unavailable" if not snapshot.can_assess_from_live_sensors else "low",
                ),
                current_time=now,
            )

    # If no sensors are available and no observations exist in the World Model
    if not snapshot.can_assess_from_live_sensors and detected.total_count == 0:
        ans = (
            "The environment cannot currently be assessed from live sensors: "
            "both depth camera and LiDAR streams have no active observations."
        )
        unknowns.append("No active camera or LiDAR observations are present in the World Model.")
        return validate_grounded_response(
            world,
            GroundedEnvironmentalResponse(
                answer=ans,
                facts=[],
                inferences=[],
                unknowns=unknowns,
                warnings=warnings,
                evidence=[],
                confidence="unavailable",
            ),
            current_time=now,
        )

    # Check if query asks about a specific label or object ID
    matched_objects: List[TrackedObject] = []
    for obj in detected.objects:
        if obj.object_id.lower() in q_lower or obj.label.lower() in q_lower:
            matched_objects.append(obj)

    # Check if query mentions an object noun that is NOT in the World Model
    common_query_nouns = [
        w
        for w in re.findall(r"[a-zA-Z_]+", q_lower)
        if w
        not in {
            "what",
            "where",
            "is",
            "are",
            "the",
            "there",
            "a",
            "an",
            "any",
            "detected",
            "object",
            "objects",
            "obstacle",
            "obstacles",
            "in",
            "front",
            "of",
            "camera",
            "rover",
            "scout",
            "scout01",
            "how",
            "far",
            "distance",
            "range",
            "status",
            "environment",
            "see",
            "visible",
            "current",
            "currently",
        }
    ]

    if not matched_objects and common_query_nouns:
        # Check if user asked about a specific nonexistent object (e.g., "forklift", "dog", "table")
        known_labels = {o.label.lower() for o in detected.objects}
        unmatched_nouns = [n for n in common_query_nouns if n not in known_labels]
        if unmatched_nouns and not any(
            kw in q_lower for kw in ("what objects", "what is detected", "environment", "obstacles")
        ):
            target_noun = unmatched_nouns[0]
            ans = (
                f"Cannot confirm '{target_noun}': no observation of '{target_noun}' exists in the World Model."
            )
            unknowns.append(f"No sensor observation supports the presence or distance of '{target_noun}'.")
            # Still attach existing objects as context facts if helpful, or keep strictly focused
            return validate_grounded_response(
                world,
                GroundedEnvironmentalResponse(
                    answer=ans,
                    facts=facts,
                    inferences=inferences,
                    unknowns=unknowns,
                    warnings=warnings,
                    evidence=[],
                    confidence="unavailable",
                ),
                current_time=now,
            )

    target_list = matched_objects if matched_objects else detected.objects
    if not target_list:
        ans = (
            "No objects are currently tracked in the World Model. "
            "Note that absence of detections does not guarantee the environment is clear of obstacles."
        )
        unknowns.append("Unobserved regions and LiDAR obstacle boundaries remain UNKNOWN.")
        return validate_grounded_response(
            world,
            GroundedEnvironmentalResponse(
                answer=ans,
                facts=[],
                inferences=[],
                unknowns=unknowns,
                warnings=warnings,
                evidence=[],
                confidence="low",
            ),
            current_time=now,
        )

    summary_parts: List[str] = []
    has_conflict = False
    has_stale = False
    all_corroborated = True

    for obj in target_list:
        details = tools.get_object_details(obj.object_id)
        range_val = obj.position.computed_range_m()
        mode_tag = "SIMULATED" if obj.source_mode == SensorSourceModeEnum.SIMULATED else "PHYSICAL"

        if obj.epistemic_status == EpistemicStatusEnum.CONFLICTED:
            has_conflict = True
            all_corroborated = False
            fact_str = (
                f"[{obj.object_id}] '{obj.label}' has CONFLICTED sensor measurements in frame '{obj.coordinate_frame}': "
                f"{obj.conflict_details}"
            )
            warnings.append(fact_str)
            summary_parts.append(fact_str)
        elif details.is_stale or obj.epistemic_status == EpistemicStatusEnum.STALE:
            has_stale = True
            all_corroborated = False
            fact_str = (
                f"[{obj.object_id}] '{obj.label}' was previously observed at {range_val:.2f}m "
                f"(pos=({obj.position.x:.2f}, {obj.position.y:.2f}, {obj.position.z:.2f})m in '{obj.coordinate_frame}', "
                f"confidence={obj.classification_confidence:.2f}, source={mode_tag}), "
                f"but the observation is STALE (age {details.age_seconds:.1f}s)."
            )
            facts.append(fact_str)
            summary_parts.append(fact_str)
        else:
            corrob_str = (
                "CORROBORATED by camera+LiDAR"
                if obj.corroborated_by_multiple_sensors
                else f"OBSERVED by {obj.last_known_source} only ({mode_tag})"
            )
            if not obj.corroborated_by_multiple_sensors:
                all_corroborated = False
            fact_str = (
                f"[{obj.object_id}] '{obj.label}' detected at {range_val:.2f}m "
                f"(pos=({obj.position.x:.2f}, {obj.position.y:.2f}, {obj.position.z:.2f})m ±{obj.position_uncertainty_m:.2f}m "
                f"in frame '{obj.coordinate_frame}', confidence={obj.classification_confidence:.2f}, status={corrob_str})."
            )
            facts.append(fact_str)
            summary_parts.append(fact_str)

        if obj.is_obstacle:
            inferences.append(
                f"Object '{obj.label}' ({obj.object_id}) at {range_val:.2f}m is classified as a potential navigation obstacle."
            )

        for obs in details.observations:
            evidence.append(
                EvidenceReference(
                    observation_id=obs.observation_id,
                    object_id=obj.object_id,
                    sensor=obs.sensor_id,
                    sensor_type=obs.sensor_type.value,
                    timestamp=obs.sensor_timestamp,
                    coordinate_frame=obs.coordinate_frame,
                    confidence=obs.confidence,
                    epistemic_status=obj.epistemic_status,
                    source_mode=obs.source_mode,
                )
            )

    conf_level: str = "medium"
    if has_conflict or has_stale:
        conf_level = "low"
    elif all_corroborated and target_list:
        conf_level = "high"

    return validate_grounded_response(
        world,
        GroundedEnvironmentalResponse(
            answer=" ".join(summary_parts),
            facts=facts,
            inferences=inferences,
            unknowns=list(dict.fromkeys(unknowns)),
            warnings=list(dict.fromkeys(warnings)),
            evidence=evidence,
            confidence=conf_level,  # type: ignore[arg-type]
        ),
        current_time=now,
    )


def validate_plan_evidence(
    world: WorldModel,
    plan: MissionPlan,
    *,
    current_time: Optional[float] = None,
) -> Tuple[MissionPlan, List[str]]:
    """Validate and sanitize `MissionPlan` evidence references against the authoritative `WorldModel` (§7)."""
    now = current_time if current_time is not None else time.time()
    tools = WorldModelTools(world, current_time=now)
    snapshot = tools.get_environment_snapshot()

    valid_obs_ids = {o.observation_id for o in world.environment.observation_history}
    valid_obj_ids = set(world.environment.tracked_objects.keys())
    all_valid_ids = valid_obs_ids.union(valid_obj_ids)

    warnings: List[str] = []
    cleaned_evidence_ids: List[str] = []
    for eid in plan.evidence_ids:
        if eid in all_valid_ids:
            cleaned_evidence_ids.append(eid)
        else:
            warnings.append(f"Stripped nonexistent evidence ID '{eid}' from MissionPlan.")

    # Populate authoritative environmental facts & unknowns from WorldModel snapshot if missing
    facts_used = list(plan.facts_used)
    for obj in snapshot.detected_objects.objects:
        fact_line = (
            f"{obj.object_id}:{obj.label}@({obj.position.x:.2f},{obj.position.y:.2f},{obj.position.z:.2f})m"
            f"[{obj.coordinate_frame} status={obj.epistemic_status.value} mode={obj.source_mode.value}]"
        )
        if fact_line not in facts_used:
            facts_used.append(fact_line)
        for oid in obj.contributing_observation_ids:
            if oid not in cleaned_evidence_ids:
                cleaned_evidence_ids.append(oid)

    unknowns = list(plan.unknowns)
    if not snapshot.sensor_health.lidar_available:
        lidar_note = "LiDAR is UNAVAILABLE; 360° obstacle geometry and multi-sensor corroboration are unverified."
        if lidar_note not in unknowns:
            unknowns.append(lidar_note)
    if not snapshot.transform_map_validated:
        tf_note = "Global map transform (map -> odom -> oak_parent_frame) is unconfirmed; camera coordinates are frame-relative."
        if tf_note not in unknowns:
            unknowns.append(tf_note)

    updated_plan = plan.model_copy(
        update={
            "evidence_ids": cleaned_evidence_ids,
            "facts_used": facts_used,
            "unknowns": unknowns,
        }
    )
    return updated_plan, warnings
