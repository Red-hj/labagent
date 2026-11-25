"""A bounded decision/tool loop shared by material and connected-lab tools."""
from typing import TypedDict
from .task_control import check_cancelled


class ToolState(TypedDict, total=False):
    round: int
    terminal: bool
    stopped: bool
    nodes: list


def run_tool_workflow(decide, execute, *, review=None, limit=6):
    """Keep model decisions and actual tool execution in distinct graph nodes.

    The outer session graph owns durable checkpoints. This inner graph deliberately
    does not replay tool side effects when a session is resumed with new evidence.
    """
    def decision(state):
        check_cancelled()
        stopped = decide(state["round"])
        return {"stopped": bool(stopped), "nodes": state["nodes"] + ["select_check"]}

    def tools(state):
        check_cancelled()
        terminal = execute(state["round"])
        return {"terminal": bool(terminal), "round": state["round"] + 1,
                "nodes": state["nodes"] + ["execute_check"]}

    def review_result(state):
        check_cancelled()
        return {"terminal": bool(review()), "nodes": state["nodes"] + ["review_learning_step"]}

    initial = {"round": 0, "terminal": False, "stopped": False, "nodes": []}
    try:
        from langgraph.graph import StateGraph, START, END
    except ImportError:
        state = initial
        while state["round"] < limit:
            state.update(decision(state))
            if state["stopped"]:
                break
            state.update(tools(state))
            if state["terminal"] and review:
                state.update(review_result(state))
            if state["terminal"]:
                break
        return {**state, "backend": "python"}
    graph = StateGraph(ToolState)
    graph.add_node("select_check", decision)
    graph.add_node("execute_check", tools)
    if review:
        graph.add_node("review_learning_step", review_result)
    graph.add_edge(START, "select_check")
    graph.add_conditional_edges("select_check", lambda s: END if s["stopped"] else "execute_check")
    graph.add_conditional_edges("execute_check", lambda s: "review_learning_step" if s["terminal"] and review else
                                END if s["terminal"] or s["round"] >= limit else "select_check")
    if review:
        graph.add_conditional_edges("review_learning_step", lambda s: END if s["terminal"] or s["round"] >= limit else "select_check")
    return {**graph.compile().invoke(initial, {"recursion_limit": limit * 3 + 3}), "backend": "langgraph"}
