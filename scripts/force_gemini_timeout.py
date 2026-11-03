#!/usr/bin/env python3
"""Offline verification script for Workstream B (`scripts/force_gemini_timeout.py`).

Sets a fake GEMINI_API_KEY, patches the raw Gemini SDK call (`_call_gemini_sdk`)
to sleep past the configured wall-clock budget, and verifies:
  (a) `invoke_gemini_planner` returns a deterministic fallback plan within `budget + 1.0s`
      and records `PlannerOutcome(source="fallback", reason="read_timeout", ...)`.
  (b) The full `build_ultron_graph().invoke(...)` path — even when `validate_plan` is forced
      to fail so `route_validation` loops back to `plan` up to 3 times — completes within
      a single budget thanks to the module-level circuit breaker.
  (c) `reset_breaker()` restores LLM attempts on subsequent calls.
  (d) An HTTP 401 authentication failure is classified as `http_401_403` and does NOT retry.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "backend" / "fastapi")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ultron.graph.workflow import build_ultron_graph
from ultron.planner.engine import (
    get_last_planner_outcome,
    invoke_gemini_planner,
    is_breaker_open,
    reset_breaker,
)
from ultron.schemas.commands import Position2D
from ultron.schemas.mission import MissionStatus
from ultron.schemas.world import ConnectionStatus, MissionState, ScoutState, WorldModel
from ultron.state import UltronState


def _make_world(mission_id: str = "M-TIMEOUT-TEST") -> WorldModel:
    return WorldModel(
        mission=MissionState(
            id=mission_id,
            objective="Inspect sector_A and report hazards.",
            status=MissionStatus.IN_PROGRESS,
        ),
        vehicles={
            "SCOUT01": ScoutState(
                position=Position2D(x=1.0, y=1.0),
                battery=90.0,
                connection=ConnectionStatus.ONLINE,
            )
        },
    )


def main() -> int:
    budget_sec = 0.6
    os.environ["GEMINI_API_KEY"] = "fake-test-gemini-key-for-offline-verification"
    os.environ["SHADOW_LLM_PLANNING_TIMEOUT_SEC"] = str(budget_sec)
    os.environ["SHADOW_LLM_REPLANNING_TIMEOUT_SEC"] = str(budget_sec)
    os.environ["SHADOW_LLM_CONNECT_TIMEOUT_SEC"] = "0.3"
    os.environ["SHADOW_LLM_BREAKER_COOLDOWN_SEC"] = "30.0"

    sdk_call_count = 0

    def _hung_sdk_call(**_kwargs: Any) -> Any:
        nonlocal sdk_call_count
        sdk_call_count += 1
        time.sleep(budget_sec * 4.0)
        raise RuntimeError("Unreachable: should have timed out before completing sleep")

    # (a) Direct planner call falls back within budget + 1s
    reset_breaker()
    world = _make_world("M-TIMEOUT-A")
    t0 = time.perf_counter()
    with patch("ultron.planner.engine._call_gemini_sdk", side_effect=_hung_sdk_call):
        plan = invoke_gemini_planner(world)
    elapsed_a = time.perf_counter() - t0
    outcome_a = get_last_planner_outcome()

    assert elapsed_a <= budget_sec + 1.0, f"Expected <= {budget_sec + 1.0:.2f}s, took {elapsed_a:.2f}s"
    assert "USED CACHED PLAN" in plan.rationale
    assert outcome_a is not None
    assert outcome_a.source == "fallback"
    assert outcome_a.reason == "read_timeout"
    assert is_breaker_open() is True
    assert sdk_call_count == 1
    print(
        f"[PASS a] Direct fallback returned in {elapsed_a:.3f}s (budget={budget_sec:.2f}s), "
        f"outcome={outcome_a}, sdk_calls={sdk_call_count}"
    )

    # (b) Full build_ultron_graph().invoke(...) with forced validation failure stays within 1 budget
    reset_breaker()
    sdk_call_count = 0
    world_b = _make_world("M-TIMEOUT-B")
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
        patch("ultron.planner.engine._call_gemini_sdk", side_effect=_hung_sdk_call),
        patch("ultron.graph.nodes.validate_plan", return_value=(False, ["Forced validation failure"])),
    ):
        final_state = graph.invoke(state)
    elapsed_b = time.perf_counter() - t1
    outcome_b = get_last_planner_outcome()

    assert elapsed_b <= budget_sec + 1.0, (
        f"Graph retry loop exceeded single budget: {elapsed_b:.2f}s > {budget_sec + 1.0:.2f}s"
    )
    assert sdk_call_count == 1, f"Breaker should prevent retry SDK calls, got {sdk_call_count}"
    assert final_state["world"].mission.status == MissionStatus.DEGRADED
    assert outcome_b is not None and outcome_b.reason.startswith("circuit_breaker_open")
    print(
        f"[PASS b] Full LangGraph invoke (3 validation attempts) completed in {elapsed_b:.3f}s "
        f"with sdk_calls={sdk_call_count}, final_outcome={outcome_b}"
    )

    # (c) reset_breaker() restores LLM attempts
    reset_breaker()
    assert is_breaker_open() is False
    with patch("ultron.planner.engine._call_gemini_sdk", side_effect=_hung_sdk_call):
        invoke_gemini_planner(_make_world("M-TIMEOUT-C"))
    assert sdk_call_count == 2, "Expected SDK to be called again after reset_breaker()"
    print(f"[PASS c] reset_breaker() restored LLM attempts (sdk_calls={sdk_call_count})")

    # (d) HTTP 401 is classified as http_401_403 and does NOT retry
    reset_breaker()
    auth_calls = 0

    class FakeUnauthorizedError(Exception):
        status_code = 401

    def _auth_fail_sdk(**_kwargs: Any) -> Any:
        nonlocal auth_calls
        auth_calls += 1
        raise FakeUnauthorizedError("401 UNAUTHENTICATED: API key not valid")

    with patch("ultron.planner.engine._call_gemini_sdk", side_effect=_auth_fail_sdk):
        plan_d = invoke_gemini_planner(_make_world("M-TIMEOUT-D"))
    outcome_d = get_last_planner_outcome()
    assert auth_calls == 1, f"401 auth error must not retry, got {auth_calls} calls"
    assert outcome_d is not None
    assert outcome_d.source == "fallback"
    assert outcome_d.reason == "http_401_403"
    assert "USED CACHED PLAN" in plan_d.rationale
    print(
        f"[PASS d] HTTP 401 classified as {outcome_d.reason} with auth_calls={auth_calls} (no retry)"
    )

    reset_breaker()
    os.environ.pop("GEMINI_API_KEY", None)
    print("ALL WORKSTREAM B TIMEOUT & CIRCUIT BREAKER CHECKS PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
