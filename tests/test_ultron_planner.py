import socket
import ssl
import time
from typing import Any
from unittest.mock import patch

import pytest

from ultron.graph.workflow import build_ultron_graph
from ultron.planner.cache import get_fallback_plan
from ultron.planner.engine import (
    classify_gemini_error,
    get_last_planner_outcome,
    invoke_gemini_planner,
    is_breaker_open,
    reset_breaker,
)
from ultron.schemas.commands import Position2D
from ultron.schemas.mission import MissionStatus, VehicleID
from ultron.schemas.world import ConnectionStatus, MissionState, ScoutState, WorldModel
from ultron.state import UltronState


def _build_test_world(mission_id: str = "M-TEST-01") -> WorldModel:
    return WorldModel(
        mission=MissionState(
            id=mission_id,
            objective="Inspect the building, find the target, and report hazards.",
            status=MissionStatus.IN_PROGRESS,
        ),
        vehicles={
            "SCOUT01": ScoutState(
                position=Position2D(x=0.0, y=0.0),
                battery=95.0,
                connection=ConnectionStatus.ONLINE,
            )
        },
    )


def test_fallback_plan_generation():
    plan = get_fallback_plan("M-TEST-01")
    assert plan.mission_id == "M-TEST-01"
    assert len(plan.tasks) == 3
    assert plan.tasks[0].vehicle == VehicleID.SCOUT
    assert plan.tasks[0].type == "NAVIGATE"
    assert plan.tasks[1].vehicle == VehicleID.SCOUT
    assert plan.tasks[1].type == "EXPLORE"
    assert plan.tasks[2].vehicle == VehicleID.SCOUT
    assert plan.tasks[2].type == "INSPECT"
    assert "USED CACHED PLAN" in plan.rationale


def test_gemini_timeout_budget_circuit_breaker_and_auth_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Workstream B:
    (a) Fallback plan returned within budget + 1s when SDK sleeps past budget.
    (b) Full build_ultron_graph().invoke(...) path with forced validation failure stays within one budget via circuit breaker.
    (c) reset_breaker() restores LLM attempts.
    (d) HTTP 401 is classified as http_401_403 and does not retry.
    """
    budget_sec = 0.4
    monkeypatch.setenv("GEMINI_API_KEY", "fake-unit-test-gemini-key")
    monkeypatch.setenv("SHADOW_LLM_PLANNING_TIMEOUT_SEC", str(budget_sec))
    monkeypatch.setenv("SHADOW_LLM_REPLANNING_TIMEOUT_SEC", str(budget_sec))
    monkeypatch.setenv("SHADOW_LLM_CONNECT_TIMEOUT_SEC", "0.2")
    monkeypatch.setenv("SHADOW_LLM_BREAKER_COOLDOWN_SEC", "30.0")

    sdk_calls = 0

    def _sleeping_sdk(**_kwargs: Any) -> Any:
        nonlocal sdk_calls
        sdk_calls += 1
        time.sleep(budget_sec * 4.0)
        raise RuntimeError("Should have timed out before finishing sleep")

    # (a) Single planner call falls back within budget + 1s
    reset_breaker()
    world_a = _build_test_world("M-B-A")
    t0 = time.perf_counter()
    with patch("ultron.planner.engine._call_gemini_sdk", side_effect=_sleeping_sdk):
        plan_a = invoke_gemini_planner(world_a)
    elapsed_a = time.perf_counter() - t0
    outcome_a = get_last_planner_outcome()

    assert elapsed_a <= budget_sec + 1.0
    assert "USED CACHED PLAN" in plan_a.rationale
    assert outcome_a is not None
    assert outcome_a.source == "fallback"
    assert outcome_a.reason == "read_timeout"
    assert is_breaker_open() is True
    assert sdk_calls == 1

    # (b) Full build_ultron_graph().invoke(...) path with forced validation failure stays within 1 budget
    reset_breaker()
    sdk_calls = 0
    world_b = _build_test_world("M-B-B")
    state: UltronState = {
        "world": world_b,
        "proposed_plan": None,
        "validation_errors": [],
        "repair_prompt": None,
        "retry_count": 0,
        "trigger_rationale": None,
        "language": "English",
    }
    graph = build_ultron_graph()
    t1 = time.perf_counter()
    with (
        patch("ultron.planner.engine._call_gemini_sdk", side_effect=_sleeping_sdk),
        patch(
            "ultron.graph.nodes.validate_plan",
            return_value=(False, ["Forced validation failure"]),
        ),
    ):
        res_state = graph.invoke(state)
    elapsed_b = time.perf_counter() - t1
    outcome_b = get_last_planner_outcome()

    assert elapsed_b <= budget_sec + 1.0
    assert sdk_calls == 1
    assert res_state["world"].mission.status == MissionStatus.DEGRADED
    assert outcome_b is not None
    assert outcome_b.source == "fallback"
    assert outcome_b.reason.startswith("circuit_breaker_open")

    # (c) reset_breaker() restores LLM attempts
    reset_breaker()
    assert is_breaker_open() is False
    with patch("ultron.planner.engine._call_gemini_sdk", side_effect=_sleeping_sdk):
        invoke_gemini_planner(_build_test_world("M-B-C"))
    assert sdk_calls == 2

    # (d) HTTP 401 is classified as http_401_403 and does not retry
    reset_breaker()
    auth_attempts = 0

    class FakeAuth401Error(Exception):
        status_code = 401

    def _auth_error_sdk(**_kwargs: Any) -> Any:
        nonlocal auth_attempts
        auth_attempts += 1
        raise FakeAuth401Error("HTTP 401 Unauthorized: invalid key")

    with patch("ultron.planner.engine._call_gemini_sdk", side_effect=_auth_error_sdk):
        plan_d = invoke_gemini_planner(_build_test_world("M-B-D"))
    outcome_d = get_last_planner_outcome()

    assert auth_attempts == 1
    assert "USED CACHED PLAN" in plan_d.rationale
    assert outcome_d is not None
    assert outcome_d.source == "fallback"
    assert outcome_d.reason == "http_401_403"

    reset_breaker()


def test_connection_retry_when_budget_remains_and_error_classifications(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify connection-class error retries at most once when >=40% budget remains, and error classifier categories."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-unit-test-gemini-key")
    monkeypatch.setenv("SHADOW_LLM_PLANNING_TIMEOUT_SEC", "2.0")
    monkeypatch.setenv("SHADOW_LLM_BREAKER_COOLDOWN_SEC", "30.0")

    reset_breaker()
    conn_attempts = 0

    def _fast_tls_fail(**_kwargs: Any) -> Any:
        nonlocal conn_attempts
        conn_attempts += 1
        raise ssl.SSLError("[SSL] _ssl.c:1064: The handshake operation timed out")

    with patch("ultron.planner.engine._call_gemini_sdk", side_effect=_fast_tls_fail):
        plan = invoke_gemini_planner(_build_test_world("M-CONN-RETRY"))
    outcome = get_last_planner_outcome()

    assert conn_attempts == 2  # Initial attempt + 1 retry
    assert outcome is not None
    assert outcome.reason == "tls_handshake"
    assert "USED CACHED PLAN" in plan.rationale
    assert is_breaker_open() is True
    reset_breaker()

    assert classify_gemini_error(socket.gaierror(8, "nodename nor servname provided")) == "dns"
    assert classify_gemini_error(ConnectionRefusedError("Connection refused")) == "tcp_connect"
    assert classify_gemini_error(ssl.SSLError(" handshake timed out ")) == "tls_handshake"
    assert classify_gemini_error(TimeoutError("read timed out")) == "read_timeout"
    assert classify_gemini_error(RuntimeError("HTTP 429 RESOURCE_EXHAUSTED")) == "http_429"
    assert classify_gemini_error(RuntimeError("503 Service Unavailable")) == "http_5xx"
    assert classify_gemini_error(ValueError("unexpected schema error")) == "other"


def test_fallback_plan_preserves_arbitrary_objectives_and_passes_validator() -> None:
    """Verify Workstream C: fallback plans built for 3 arbitrary objectives carry that objective and pass validate_plan."""
    from ultron.planner.engine import build_evidence_grounded_plan
    from ultron.safety.validator import validate_plan

    arbitrary_objectives = [
        "Respond to perimeter motion alert at north entrance and verify corridor status.",
        "Standing Order Alpha: patrol loading bay after 22:00 and log any thermal anomalies.",
        "Investigate acoustic trigger near storage locker 4 and hold position if obstructed.",
    ]

    for idx, custom_objective in enumerate(arbitrary_objectives, start=1):
        mission_id = f"M-EVENT-{idx:02d}"
        world = _build_test_world(mission_id)
        world.mission.objective = custom_objective

        # 1. Direct get_fallback_plan(mission_id, objective=custom_objective)
        direct_plan = get_fallback_plan(mission_id, objective=custom_objective)
        assert direct_plan.objective == custom_objective
        assert "USED CACHED PLAN" in direct_plan.rationale
        ok_direct, errs_direct = validate_plan(direct_plan, world)
        assert ok_direct is True, f"Direct fallback failed validation for '{custom_objective}': {errs_direct}"
        assert errs_direct == []

        # 2. Evidence-grounded baseline fallback (build_evidence_grounded_plan) threads world.mission.objective
        grounded_plan = build_evidence_grounded_plan(world)
        assert grounded_plan.objective == custom_objective
        assert "USED CACHED PLAN" in grounded_plan.rationale
        ok_grounded, errs_grounded = validate_plan(grounded_plan, world)
        assert ok_grounded is True, f"Grounded fallback failed validation for '{custom_objective}': {errs_grounded}"
        assert errs_grounded == []

