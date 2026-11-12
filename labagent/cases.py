"""Public demo inputs shared by the workbench and HTTP API."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def demo_cases():
    cases = {}
    for path in sorted((ROOT / "data" / "cases").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        cases[path.stem] = {key: value for key, value in data.items() if key != "expected"}
    for path in sorted((ROOT / "data" / "team_cases").glob("lab_*/input.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        title = (path.parent / "notes.md").read_text(encoding="utf-8").splitlines()[0].lstrip("# ")
        cases["team_" + path.parent.name] = {
            **{key: value for key, value in data.items() if key != "expected"},
            "title": "B组 · " + path.parent.name.removeprefix("lab_") + " · " + title,
            "description": "两台路由器连接两个终端网段。载入配置后开始检查，可修改配置并补充邻居、路由与 Ping 输出继续复测。",
            "knowledge_scope": "b-ospf",
            "diagnostic_mode": "ospf_rules",
        }
    return cases
