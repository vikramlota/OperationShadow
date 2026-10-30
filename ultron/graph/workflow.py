from ultron.state import UltronState
from ultron.graph.nodes import (
    node_plan, node_validate, node_assign, node_evaluate, node_degraded
)


def route_start(state: UltronState) -> str:
    """Routes initial invocation to planning, or observation updates to evaluation."""
    if state.get("proposed_plan") is not None:
        return "evaluate"
    return "plan"


def route_validation(state: UltronState) -> str:
    """Routes based on whether the plan passed the Safety Validator."""
    if not state.get("validation_errors"):
        return "assign"
    if state.get("retry_count", 0) >= 3:
        return "degraded"
    return "plan"


def route_evaluation(state: UltronState) -> str:
    """Routes based on physical triggers in the environment."""
    if state.get("trigger_rationale"):
        return "replan"
    return "end"


def build_ultron_graph():
    """Compiles the ULTRON state machine workflow."""
    try:
        from langgraph.graph import StateGraph, START, END

        builder = StateGraph(UltronState)

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

        return builder.compile()
    except (ImportError, Exception):
        # Fallback runner for testing when langgraph is not installed
        class SimpleGraphRunner:
            def invoke(self, state, config=None):
                st = dict(state)
                # If a plan already exists, run evaluate -> replan (if needed)
                if st.get("proposed_plan") is not None:
                    st.update(node_evaluate(st))
                    if st.get("trigger_rationale"):
                        st.update(node_plan(st))
                        st.update(node_validate(st))
                        if not st.get("validation_errors"):
                            st.update(node_assign(st))
                        else:
                            st.update(node_degraded(st))
                    return st

                # Initial planning pass: plan -> validate -> assign
                st.update(node_plan(st))
                st.update(node_validate(st))
                if not st.get("validation_errors"):
                    st.update(node_assign(st))
                else:
                    st.update(node_degraded(st))
                return st

        return SimpleGraphRunner()


ultron_app = build_ultron_graph()
