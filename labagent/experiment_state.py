"""Compact experiment continuity stored inside the existing investigation record."""
from copy import deepcopy

from .session_state import experiment_goal


def experiment_context(previous, payload, previous_materials=None):
    """Refresh historical locators before a model sees the next material snapshot."""
    previous = previous or {}
    state = deepcopy(previous.get("experiment_state", {}))
    state.setdefault("turn_count", 0)
    state.setdefault("goal", previous.get("experiment_goal") or experiment_goal(payload))
    state.setdefault("ruled_out", [])
    current, old = payload.get("configs", {}), previous_materials or {}
    for item in state["ruled_out"]:
        if item["evidence_state"] == "materials_unchanged":
            names = item["material_names"]
            if not names or any(name not in current or name not in old or current[name] != old[name] for name in names):
                item["evidence_state"] = "needs_recheck"
    return state


def summarize_experiment(previous, payload, sources, analysis, previous_materials=None, *, succeeded=True):
    state = experiment_context(previous, payload, previous_materials)
    turn = state["turn_count"] + int(succeeded)
    state.update(turn_count=turn, goal=analysis.get("experiment_goal") or state["goal"])
    stale = [source["device"] for source in sources.values()
             if source.get("stale_neighbor") and source["kind"] != "user_message"]
    if stale:
        state["stale_neighbor_materials"] = stale
    else:
        state.pop("stale_neighbor_materials", None)
    if not succeeded:
        return state
    state["current_step"] = {key: analysis["learning"][key] for key in
                             ("stage", "current_step", "action", "question")} if analysis.get("learning") else {
                                 "current_step": analysis.get("summary", ""), "actions": analysis.get("steps", [])}
    state["pending_requests"] = deepcopy(analysis.get("pending_requests", analysis.get("evidence_requests", [])))
    reopened = {item["title"] for item in analysis.get("hypotheses", [])}
    state["ruled_out"] = [item for item in state["ruled_out"] if item["title"] not in reopened]
    # Only accepted, line-checked exclusions enter continuity. Locators have no
    # source_id: reordered config:N and truncated message:N are not stable IDs.
    for hypothesis in analysis.get("ruled_out", []):
        locators, names, material_only = [], [], True
        for citation in hypothesis["evidence"]:
            source = sources[citation["source_id"]]
            is_material = citation["source_id"].startswith("config:") and source["device"] in payload["configs"]
            material_only &= is_material
            if is_material and source["device"] not in names:
                names.append(source["device"])
            locators.append({"material": source["device"], "kind": source["kind"],
                             "line": citation["line"], "excerpt": citation["quote"][:300]})
        record = {"title": hypothesis["title"], "reasoning": hypothesis["reasoning"], "observed_turn": turn,
                  "material_names": names, "locators": locators,
                  "evidence_state": "materials_unchanged" if material_only else "historical_report"}
        state["ruled_out"] = [item for item in state["ruled_out"] if item["title"] != record["title"]]
        state["ruled_out"].append(record)
    state["ruled_out"] = state["ruled_out"][-12:]
    state["role"] = "historical_analysis_only"
    return state
