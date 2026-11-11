"""Import a clearly labeled demo note and record offline retrieval examples."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import json
from labagent.knowledge import import_document, search


def main():
    example = json.loads((ROOT / "data/examples/ospf_course_note.json").read_text(encoding="utf-8"))
    imported = import_document(example)
    queries = ["邻居 Full 但 R1 ping 不通 R3，应该检查回程路由吗？", "修改配置后能继续使用旧运行输出吗？", "如何组织分级教学提示？"]
    report = {"scope": "demo-ospf", "source": "项目自编演示资料，不是独立检索质量评测", "document": imported["document"], "queries": [search(query, scope="demo-ospf") for query in queries]}
    folder = ROOT / "runtime/knowledge-demo"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "retrieval.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for item in report["queries"]:
        print("Results:", len(item["results"]), "First document:", item["results"][0]["id"] if item["results"] else "none")
    print(folder / "retrieval.json")


if __name__ == "__main__":
    main()
