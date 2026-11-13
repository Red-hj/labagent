"""Actual StateGraph orchestration with SQLite checkpoints and interrupts."""
from pathlib import Path
from typing import TypedDict
import importlib.util
import uuid

from .engine import diagnose, validate
from .parser import parse_config
from .runtime_parser import normalize_outputs
from .tools import check_link
from .knowledge import search

ROOT = Path(__file__).resolve().parents[1]


def available():
    return importlib.util.find_spec("langgraph") is not None


class State(TypedDict, total=False):
    payload: dict
    prepared: dict
    runtime: dict
    pending: list
    selected: str
    inspection: dict
    steps: list
    result: dict
    context: dict
    conversation: list
    last_materials: dict


def build_graph(checkpointer, planner=None):
    from langgraph.graph import StateGraph, START, END
    from langgraph.types import interrupt, Command

    def prepare(state):
        payload = state["payload"]
        validate(payload)
        prepared, runtime = normalize_outputs(payload)
        context = search(payload.get("question", "OSPF 实验"), scope=payload.get("knowledge_scope", "default"))
        prepared["knowledge_context"] = context["results"]
        return {"prepared": prepared, "runtime": runtime, "pending": [link["id"] for link in payload["topology"]["links"]],
                "context": context, "inspection": {}, "steps": [
                    {"component": "实验资料检索", "action": "按所选资料范围检索可追溯的原文片段，不自动改写实验要求", "scope": context["scope"], "chunks": [doc["chunk_id"] for doc in context["results"]], "warnings": context["warnings"]},
                    {"component": "运行输出解析", "action": "解析与当前配置绑定的命令输出", "outputs": runtime["trace"], "warnings": runtime["warnings"]}]}

    def plan(state):
        pending = state["pending"]
        selected, source, details = pending[0], "local_rules", {}
        # A model can select the next real check, but cannot skip pending coverage.
        if planner is not None or (state["payload"].get("use_llm") and len(state["inspection"]) < 4):
            try:
                choose = planner
                if choose is None:
                    from .agents import choose_next_link
                    choose = choose_next_link
                if planner is None:
                    decision = choose(state["prepared"], pending, state["inspection"], with_metadata=True)
                    choice = decision.get("link_id") if isinstance(decision, dict) else decision
                    details = {"model_call": decision["call"]} if isinstance(decision, dict) else {}
                else:
                    choice = choose(state["prepared"], pending, state["inspection"])
                if isinstance(choice, str) and choice in pending:
                    selected, source = choice, "model" if planner is None else "injected_planner"
            except Exception as error:
                source = "local_fallback"
                from .model import ModelError
                details = error.as_dict() if isinstance(error, ModelError) else {"error_code": "invalid_response"}
        step = {"component": "检查规划", "action": "选择下一条尚未检查的链路", "tool": "check_link", "link_id": selected, "planner": source, **details}
        return {"selected": selected, "steps": state["steps"] + [step]}

    def inspect(state):
        payload = state["prepared"]
        link = next(link for link in payload["topology"]["links"] if link["id"] == state["selected"])
        devices = {name: parse_config(name, content) for name, content in payload["configs"].items()}
        checked = check_link(link, devices, payload.get("requirements", {}).get("expected_area"))
        step = {"component": "实际诊断工具", "action": "执行链路配置检查并取得新证据", "tool": "check_link", "link_id": link["id"], "faults": [f["code"] for f in checked[0]]}
        return {"inspection": {**state["inspection"], link["id"]: checked}, "pending": [key for key in state["pending"] if key != link["id"]], "steps": state["steps"] + [step]}

    def summarize(state):
        result = diagnose(state["prepared"], _inspection=state["inspection"], _runtime=state["runtime"], _context=state["context"])
        return {"result": result}

    def wait(state):
        result = state["result"]
        reasons = [item["message"] for item in result["findings"] + result["unknowns"]] + result["runtime_missing"]
        update = interrupt({"status": result["status"], "requests": reasons, "message": "等待学生修改配置或补充运行证据；下一次提交继续同一实验。"})
        return Command(goto="prepare", update={"payload": update["payload"]})

    graph = StateGraph(State)
    for name, function in (("prepare", prepare), ("plan", plan), ("inspect", inspect), ("summarize", summarize), ("wait", wait)):
        graph.add_node(name, function)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "plan")
    graph.add_edge("plan", "inspect")
    graph.add_conditional_edges("inspect", lambda state: "plan" if state["pending"] else "summarize", ["plan", "summarize"])
    graph.add_conditional_edges("summarize", lambda state: END if state["result"]["status"] == "verified_from_observations" else "wait", [END, "wait"])
    return graph.compile(checkpointer=checkpointer)


def run_workflow(payload, database=None, planner=None):
    validate(payload)
    if payload.get("diagnostic_mode") == "open_network":
        return run_open_workflow(payload, database)
    if not available():
        if payload.get("command_outputs") or payload.get("routing_queries"):
            prepared, runtime = normalize_outputs(payload)
            result = diagnose(prepared, _runtime=runtime)
        else:
            result = diagnose(payload)
        result["workflow"] = {"backend": "local_rules", "paused": False, "message": "未安装 LangGraph，当前仅执行兼容检查。"}
        return result
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.types import Command
    location = Path(database or ROOT / "runtime" / "workflow.sqlite3")
    location.parent.mkdir(parents=True, exist_ok=True)
    thread_id = payload.get("session_id") or uuid.uuid4().hex
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 150}
    with SqliteSaver.from_conn_string(str(location)) as saver:
        graph = build_graph(saver, planner)
        snapshot = graph.get_state(config)
        resumed = bool(snapshot.next)
        invocation = Command(resume={"payload": payload}) if resumed else {"payload": payload}
        graph.invoke(invocation, config)
        snapshot = graph.get_state(config)
        result = dict(snapshot.values["result"])
        result["workflow"] = {"backend": "langgraph", "thread_id": thread_id, "paused": bool(snapshot.next), "resumed": resumed,
                              "phase": "waiting_for_student" if snapshot.next else "completed", "steps": snapshot.values["steps"]}
        result["trace"] = [{**step, "step": index} for index, step in enumerate(snapshot.values["steps"] + result["trace"], 1)]
        return result


def run_open_workflow(payload, database=None, completion=None):
    """A separate graph namespace prevents mixing rule and open checkpoints."""
    from .investigation import investigate
    validate(payload)
    if not available():
        result = investigate(payload, completion=completion)
        result["workflow"] = {"backend": "local_rules", "paused": False, "message": "未安装LangGraph，开放排查未提供持久化恢复。"}
        return result
    from langgraph.graph import StateGraph, START, END
    from langgraph.types import interrupt, Command
    from langgraph.checkpoint.sqlite import SqliteSaver

    def inspect_materials(state):
        previous = state.get("result", {}).get("investigation")
        conversation = list(state.get("conversation", []))
        payload = state["payload"]
        result = investigate(payload, previous, completion, conversation=conversation, previous_materials=state.get("last_materials"))
        if result["llm"]["status"] == "ok":
            interaction = payload.get("interaction", {})
            conversation.append({"intent": interaction.get("type", "analyze"), "user": interaction.get("message") or payload["question"],
                                 "assistant": result["investigation"].get("reply") or result["investigation"]["summary"], "created_at": result["created_at"]})
            conversation = conversation[-8:]
        result["conversation"] = conversation
        return {"result": result, "conversation": conversation, "last_materials": payload["configs"]}

    def wait_for_evidence(state):
        update = interrupt({"status": "needs_information", "requests": state["result"]["runtime_missing"],
                            "message": "等待新的材料或运行证据；模型假设尚未确认。"})
        return Command(goto="inspect_materials", update={"payload": update["payload"]})

    thread_id = payload.get("session_id") or uuid.uuid4().hex
    config = {"configurable": {"thread_id": "open:" + thread_id}, "recursion_limit": 30}
    location = Path(database or ROOT / "runtime" / "workflow.sqlite3")
    location.parent.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(location)) as saver:
        builder = StateGraph(State)
        builder.add_node("inspect_materials", inspect_materials)
        builder.add_node("wait_for_evidence", wait_for_evidence)
        builder.add_edge(START, "inspect_materials")
        builder.add_conditional_edges("inspect_materials", lambda state: END if state["result"]["status"] in ("verified_from_observations", "resolved_by_user_report") else "wait_for_evidence", [END, "wait_for_evidence"])
        graph = builder.compile(checkpointer=saver)
        snapshot = graph.get_state(config)
        resumed = bool(snapshot.next)
        graph.invoke(Command(resume={"payload": payload}) if resumed else {"payload": payload}, config)
        snapshot = graph.get_state(config)
        result = dict(snapshot.values["result"])
        result["workflow"] = {"backend": "langgraph", "thread_id": thread_id, "paused": bool(snapshot.next),
                              "resumed": resumed, "phase": "waiting_for_evidence" if snapshot.next else "completed", "steps": result["trace"]}
        return result
