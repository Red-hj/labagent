"""Show locally recorded AgentArts call statistics; makes no cloud requests."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import argparse
import json
from labagent.monitoring import summarize


def main():
    parser = argparse.ArgumentParser(description="查看项目内的云端请求监控，无额外云服务费用")
    parser.add_argument("--json", action="store_true", help="输出统计JSON")
    args = parser.parse_args()
    path = ROOT / "runtime/monitor/agentarts.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []
    result = summarize(rows)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif not rows:
        print("暂无记录。配置 LABAGENT_AGENTARTS_MONITOR=1 后，网页和命令行的云端请求会自动记录。")
    else:
        print(f"已记录 {result['requests']} 次请求，连接成功 {result['transport_successes']} 次，诊断成功 {result['diagnosis_successes']} 次。")
        print(f"平均耗时 {result['average_ms']/1000:.2f} 秒，P95 {result['p95_ms']/1000:.2f} 秒。")
        print(f"模型调用 {result['model_calls']} 次，Token 用量 {result['total_tokens']}。")
        print("工具调用：" + ("、".join(f"{name} {count}次" for name,count in result['tools'].items()) or "暂无"))
        print("失败记录：" + ("、".join(f"{name} {count}次" for name,count in result['failures'].items()) or "0次"))
        print("最近请求：" + result['latest']['created_at'])


if __name__ == "__main__":
    main()
