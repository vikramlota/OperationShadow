from typing import Any, Protocol, cast
from ultron.state import UltronState
from ultron.graph.nodes import (
    node_plan, node_validate, node_assign, node_evaluate, node_degraded
)

MAX_PLAN_VALIDATION_ATTEMPTS = 3


class GraphRunner(Protocol):
    def invoke(
        self,
        state: UltronState,
        config: Any = None,
    ) -> UltronState: ...


def route_start(state: UltronState) -> str:
    """Routes initial invocation to planning, or observation updates to evaluation."""
    if state.get("proposed_plan") is not None:
        return "evaluate"
    return "plan"


def route_validation(state: UltronState) -> str:
    """Routes based on whether the plan passed the Safety Validator."""
    if not state.get("validation_errors"):
        return "assign"
    if state.get("retry_count", 0) >= MAX_PLAN_VALIDATION_ATTEMPTS:
        return "degraded"
    return "plan"


def route_evaluation(state: UltronState) -> str:
    """Routes based on physical triggers in the environment."""
    if state.get("trigger_rationale"):
        return "replan"
    return "end"


def build_ultron_graph() -> GraphRunner:
    """Compiles the ULTRON state machine workflow."""
    try:
        from langgraph.graph import StateGraph, START, END

        builder: Any = StateGraph(UltronState)

        # Register Nodes
        builder.add_node("plan", node_plan)
        builder.add_node("validate", node_validate)
        builder.add_node("assign", node_assign)
        builder.add_node("evaluate", node_evaluate)
        builder.add_node("replan", node_plan)
        builder.add_node("degraded", node_degraded)

        # Entry point routes: initial planning vs subsequent evaluation
        builder.add_conditional_edges(
            START,
            route_start,
            {"plan": "plan", "evaluate": "evaluate"}
        )

        builder.add_edge("plan", "validate")
        builder.add_edge("replan", "validate")

        # Conditional edge out of validation
        builder.add_conditional_edges(
            "validate",
            route_validation,
            {"assign": "assign", "degraded": "degraded", "plan": "plan"}
        )

        # Dispatched tasks complete the invocation cycle
        builder.add_edge("assign", END)
        builder.add_edge("degraded", END)

        # Conditional edge out of evaluation
        builder.add_conditional_edges(
            "evaluate",
            route_evaluation,
            {"replan": "replan", "end": END}
        )

        compiled = builder.compile()

        class CompiledGraphRunner:
            def invoke(
                self,
                state: UltronState,
                config: Any = None,
            ) -> UltronState:
                return cast(UltronState, compiled.invoke(state, config=config))

        return CompiledGraphRunner()
    except ImportError:
        # Fallback runner for testing when langgraph is not installed
        class SimpleGraphRunner:
            def _plan_and_validate_with_retries(self, st: dict[str, Any]) -> None:
                for _attempt in range(MAX_PLAN_VALIDATION_ATTEMPTS):
                    st.update(node_plan(cast(UltronState, st)))
                    st.update(node_validate(cast(UltronState, st)))
                    if not st.get("validation_errors"):
                        st.update(node_assign(cast(UltronState, st)))
                        return
                    if int(st.get("retry_count", 0)) >= MAX_PLAN_VALIDATION_ATTEMPTS:
                        break
                st.update(node_degraded(cast(UltronState, st)))

            def invoke(
                self,
                state: UltronState,
                config: Any = None,
            ) -> UltronState:
                st: dict[str, Any] = dict(state)
                # If a plan already exists, run evaluate -> replan (if needed)
                if st.get("proposed_plan") is not None:
                    st.update(node_evaluate(cast(UltronState, st)))
                    if st.get("trigger_rationale"):
                        self._plan_and_validate_with_retries(st)
                    return cast(UltronState, st)

                # Initial planning pass: plan -> validate (with bounded retries) -> assign / degraded
                self._plan_and_validate_with_retries(st)
                return cast(UltronState, st)

        return SimpleGraphRunner()


ultron_app = build_ultron_graph()
