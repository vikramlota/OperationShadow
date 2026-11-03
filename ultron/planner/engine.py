from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import socket
import ssl
import sys
import threading
import time
from typing import Any, List, Literal, Optional, Tuple

from ultron.planner.cache import get_fallback_plan
from ultron.planner.grounding import validate_plan_evidence
from ultron.planner.prompts import SYSTEM_PROMPT
from ultron.schemas.mission import MissionPlan, Task, TaskPriority, VehicleID
from ultron.schemas.observations import EpistemicStatusEnum
from ultron.schemas.world import WorldModel
from ultron.world_model.tools import WorldModelTools

logger = logging.getLogger("operation_shadow.ultron.planner")

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

CONNECTION_ERROR_CLASSES = frozenset({"dns", "tcp_connect", "tls_handshake"})
BREAKER_TRIP_CLASSES = frozenset({"dns", "tcp_connect", "tls_handshake", "read_timeout"})


@dataclass(frozen=True)
class PlannerOutcome:
    """Outcome metadata for the most recent planner invocation without altering MissionPlan."""

    source: Literal["llm", "fallback"]
    reason: str
    elapsed_ms: float


_state_lock = threading.Lock()
_breaker_open_until: float = 0.0
_breaker_reason: str = ""
_last_planner_outcome: Optional[PlannerOutcome] = None


def _read_positive_float_env(name: str, default: float, *, allow_zero: bool = False) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = float(raw.strip())
        if allow_zero and val >= 0.0:
            return val
        if val > 0.0:
            return val
    except ValueError:
        pass
    return default


def _get_planning_timeout_sec() -> float:
    return _read_positive_float_env("SHADOW_LLM_PLANNING_TIMEOUT_SEC", 10.0)


def _get_replanning_timeout_sec() -> float:
    return _read_positive_float_env("SHADOW_LLM_REPLANNING_TIMEOUT_SEC", 8.0)


def _get_connect_timeout_sec() -> float:
    return _read_positive_float_env("SHADOW_LLM_CONNECT_TIMEOUT_SEC", 3.0)


def _get_breaker_cooldown_sec() -> float:
    return _read_positive_float_env(
        "SHADOW_LLM_BREAKER_COOLDOWN_SEC", 30.0, allow_zero=True
    )


def reset_breaker() -> None:
    """Reset the module-level Gemini circuit breaker (for tests and manual recovery)."""
    global _breaker_open_until, _breaker_reason
    with _state_lock:
        _breaker_open_until = 0.0
        _breaker_reason = ""


def is_breaker_open() -> bool:
    """Return True if the Gemini circuit breaker is currently open."""
    with _state_lock:
        return time.monotonic() < _breaker_open_until


def _trip_breaker(reason: str, cooldown_sec: float) -> None:
    global _breaker_open_until, _breaker_reason
    if cooldown_sec <= 0.0:
        return
    with _state_lock:
        _breaker_open_until = max(_breaker_open_until, time.monotonic() + cooldown_sec)
        _breaker_reason = reason


def _set_last_planner_outcome(outcome: PlannerOutcome) -> None:
    global _last_planner_outcome
    with _state_lock:
        _last_planner_outcome = outcome


def get_last_planner_outcome() -> Optional[PlannerOutcome]:
    """Return the most recent `PlannerOutcome` recorded by `invoke_gemini_planner`."""
    with _state_lock:
        return _last_planner_outcome


def _redact_secret(text: str, api_key: Optional[str]) -> str:
    if not text:
        return ""
    sanitized = text
    if api_key and len(api_key) >= 4:
        sanitized = sanitized.replace(api_key, "[REDACTED_API_KEY]")
    return sanitized


def _iter_exception_chain(exc: BaseException) -> List[BaseException]:
    chain: List[BaseException] = []
    curr: Optional[BaseException] = exc
    seen: set[int] = set()
    while curr is not None and id(curr) not in seen:
        seen.add(id(curr))
        chain.append(curr)
        curr = curr.__cause__ or curr.__context__
    return chain


def classify_gemini_error(exc: BaseException) -> str:
    """Classify a Gemini / transport exception into a canonical failure category.

    Categories:
      - `dns`
      - `tcp_connect`
      - `tls_handshake`
      - `read_timeout`
      - `http_401_403`
      - `http_429`
      - `http_5xx`
      - `other`
    """
    chain = _iter_exception_chain(exc)

    for item in chain:
        status_code = (
            getattr(item, "code", None)
            or getattr(item, "status_code", None)
            or getattr(getattr(item, "response", None), "status_code", None)
        )
        if isinstance(status_code, int):
            if status_code in (401, 403):
                return "http_401_403"
            if status_code == 429:
                return "http_429"
            if 500 <= status_code <= 599:
                return "http_5xx"

    for item in chain:
        if isinstance(item, socket.gaierror):
            return "dns"
        if isinstance(item, ssl.SSLError):
            return "tls_handshake"

    combined = " | ".join(
        f"{type(item).__module__}.{type(item).__name__}: {item}" for item in chain
    ).lower()

    if any(
        tok in combined
        for tok in (
            "gaierror",
            "name or service not known",
            "nodename nor servname",
            "temporary failure in name resolution",
            "getaddrinfo failed",
            "dns",
        )
    ):
        return "dns"

    if any(
        tok in combined
        for tok in (
            "ssl",
            "tls",
            "handshake",
            "certificate_verify_failed",
            "cert:",
        )
    ):
        return "tls_handshake"

    if any(
        tok in combined
        for tok in (
            "http 401",
            "http 403",
            "status_code=401",
            "status_code=403",
            "401 unauthorized",
            "403 forbidden",
            "unauthenticated",
            "permission_denied",
            "api_key_invalid",
            "invalid api key",
        )
    ):
        return "http_401_403"

    if any(
        tok in combined
        for tok in (
            "http 429",
            "status_code=429",
            "429 too many requests",
            "resource_exhausted",
            "rate limit",
            "quota exceeded",
        )
    ):
        return "http_429"

    if any(
        tok in combined
        for tok in (
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "status_code=500",
            "status_code=502",
            "status_code=503",
            "status_code=504",
            "500 internal",
            "502 bad gateway",
            "503 service unavailable",
            "504 gateway timeout",
        )
    ):
        return "http_5xx"

    for item in chain:
        cls_name = type(item).__name__.lower()
        if "connecttimeout" in cls_name or "connecterror" in cls_name:
            return "tcp_connect"
        if isinstance(item, (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError)):
            return "tcp_connect"

    if any(
        tok in combined
        for tok in (
            "connection refused",
            "connection reset",
            "network is unreachable",
            "no route to host",
            "connect timeout",
            "connecterror",
            "all connection attempts failed",
        )
    ):
        return "tcp_connect"

    for item in chain:
        if isinstance(item, (FuturesTimeoutError, TimeoutError, socket.timeout)):
            return "read_timeout"
        cls_name = type(item).__name__.lower()
        if "timeout" in cls_name:
            return "read_timeout"

    if "timed out" in combined or "timeout" in combined:
        return "read_timeout"

    return "other"


def _call_gemini_sdk(
    *,
    api_key: str,
    model_name: str,
    user_prompt: str,
    timeout_sec: float,
    connect_timeout_sec: float,
) -> Any:
    """Execute the raw synchronous Google GenAI SDK `generate_content` call.

    Extracted as a small single-responsibility function so unit tests and
    offline verification scripts can patch it deterministically.
    """
    from google import genai
    from google.genai import types as genai_types
    import httpx

    eff_timeout = max(0.1, float(timeout_sec))
    eff_connect = max(0.1, min(float(connect_timeout_sec), eff_timeout))
    httpx_timeout = httpx.Timeout(eff_timeout, connect=eff_connect)

    client = genai.Client(
        api_key=api_key,
        http_options={
            "timeout": max(1, int(eff_timeout * 1000)),
            "client_args": {"timeout": httpx_timeout},
        },
    )
    return client.models.generate_content(
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


def _invoke_gemini_with_outcome(
    world: WorldModel,
    repair_prompt: Optional[str] = None,
    trigger_rationale: Optional[str] = None,
    language: str = "English",
) -> Tuple[MissionPlan, PlannerOutcome]:
    """Internal helper executing bounded Gemini planning + fallback and returning `(MissionPlan, PlannerOutcome)`."""
    t_start = time.monotonic()
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

    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key:
        fallback_plan = build_evidence_grounded_plan(
            world,
            trigger_rationale=trigger_rationale,
            repair_prompt=repair_prompt,
        )
        outcome = PlannerOutcome(
            source="fallback",
            reason="api_key_unset",
            elapsed_ms=round((time.monotonic() - t_start) * 1000.0, 2),
        )
        _set_last_planner_outcome(outcome)
        return fallback_plan, outcome

    with _state_lock:
        now_mono = time.monotonic()
        breaker_active = now_mono < _breaker_open_until
        active_breaker_reason = _breaker_reason

    if breaker_active:
        logger.warning(
            "Gemini circuit breaker is OPEN (reason=%s); skipping LLM call and using deterministic fallback.",
            active_breaker_reason or "previous_failure",
        )
        fallback_plan = build_evidence_grounded_plan(
            world,
            trigger_rationale=trigger_rationale,
            repair_prompt=repair_prompt,
        )
        outcome = PlannerOutcome(
            source="fallback",
            reason=f"circuit_breaker_open:{active_breaker_reason or 'previous_failure'}",
            elapsed_ms=round((time.monotonic() - t_start) * 1000.0, 2),
        )
        _set_last_planner_outcome(outcome)
        return fallback_plan, outcome

    model_name = os.getenv("SHADOW_LLM_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
    budget_sec = (
        _get_replanning_timeout_sec()
        if trigger_rationale
        else _get_planning_timeout_sec()
    )
    connect_timeout_sec = _get_connect_timeout_sec()
    cooldown_sec = _get_breaker_cooldown_sec()
    deadline = t_start + budget_sec

    last_error_class = "other"
    for attempt in range(1, 3):  # At most 1 retry (max 2 attempts total)
        remaining_sec = deadline - time.monotonic()
        if remaining_sec <= 0.0:
            last_error_class = "read_timeout"
            _trip_breaker(last_error_class, cooldown_sec)
            break

        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ultron-gemini")
        future = executor.submit(
            _call_gemini_sdk,
            api_key=api_key,
            model_name=model_name,
            user_prompt=user_prompt,
            timeout_sec=remaining_sec,
            connect_timeout_sec=min(connect_timeout_sec, remaining_sec),
        )
        try:
            response = future.result(timeout=remaining_sec)
            executor.shutdown(wait=False, cancel_futures=True)

            if hasattr(response, "parsed") and response.parsed:
                raw_plan = MissionPlan.model_validate(response.parsed)
                validated_plan, _ = validate_plan_evidence(world, raw_plan)
                outcome = PlannerOutcome(
                    source="llm",
                    reason="ok",
                    elapsed_ms=round((time.monotonic() - t_start) * 1000.0, 2),
                )
                _set_last_planner_outcome(outcome)
                return validated_plan, outcome

            last_error_class = "other"
            logger.warning(
                "Gemini planner returned empty/unparsed response (model=%s, attempt=%d).",
                model_name,
                attempt,
            )
            break
        except FuturesTimeoutError as timeout_exc:
            # Abandon hung SDK worker thread immediately without waiting
            executor.shutdown(wait=False, cancel_futures=True)
            last_error_class = classify_gemini_error(timeout_exc)
            logger.warning(
                "Gemini planner call timed out after %.2fs budget (class=%s, model=%s, attempt=%d).",
                budget_sec,
                last_error_class,
                model_name,
                attempt,
            )
            _trip_breaker(last_error_class, cooldown_sec)
            break
        except Exception as exc:
            executor.shutdown(wait=False, cancel_futures=True)
            last_error_class = classify_gemini_error(exc)
            safe_err = _redact_secret(str(exc), api_key)
            logger.warning(
                "Gemini planner call failed (class=%s, model=%s, attempt=%d): %s",
                last_error_class,
                model_name,
                attempt,
                safe_err,
            )
            remaining_after = deadline - time.monotonic()
            can_retry = (
                attempt == 1
                and last_error_class in CONNECTION_ERROR_CLASSES
                and remaining_after >= (0.4 * budget_sec)
            )
            if can_retry:
                logger.info(
                    "Retrying Gemini planner once after connection-class failure (%s, %.2fs remaining of %.2fs budget).",
                    last_error_class,
                    remaining_after,
                    budget_sec,
                )
                continue
            if last_error_class in BREAKER_TRIP_CLASSES:
                _trip_breaker(last_error_class, cooldown_sec)
            break

    fallback_plan = build_evidence_grounded_plan(
        world,
        trigger_rationale=trigger_rationale,
        repair_prompt=repair_prompt,
    )
    outcome = PlannerOutcome(
        source="fallback",
        reason=last_error_class,
        elapsed_ms=round((time.monotonic() - t_start) * 1000.0, 2),
    )
    _set_last_planner_outcome(outcome)
    return fallback_plan, outcome


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
    plan, _ = _invoke_gemini_with_outcome(
        world,
        repair_prompt=repair_prompt,
        trigger_rationale=trigger_rationale,
        language=language,
    )
    return plan
