"""Evidence-only experiment process reports; no fabricated scores or outcomes."""
from datetime import datetime, timezone, timedelta

STATUS = {"fault_found": "发现问题，等待修正", "needs_information": "证据不足，等待补充", "awaiting_verification": "配置检查通过，等待运行复测", "verified_from_observations": "用户提交的运行观测通过"}


def clean(value):
    return str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|")


def generate_report(history):
    latest = history["attempts"][-1]
    result, payload = latest["result"], latest["input"]
    is_open = result.get("mode") == "open_network"
    rows = ["# 网络实验诊断过程报告", "", f"会话编号：{history['session_id']}",
            f"当前结论：{'继续排查，下一步见材料请求' if is_open and result['status'] != 'verified_from_observations' else STATUS.get(result['status'], result['status'])}",
            f"案例来源：{'预置演示案例' if payload.get('evidence_kind') == 'demo' else '用户提交'}", "",
            "本报告汇总提交的实验材料、诊断过程、工具记录和测试结果。", "",
            "## 实验目标", "", clean(payload.get("question", "未填写问题")), "",
            "## 实际提交与状态变化", "", "| 次数 | 北京时间 | 配置指纹 | 提示等级 | 结果 |", "| --- | --- | --- | --- | --- |"]
    for number, attempt in enumerate(history["attempts"], 1):
        item = attempt["result"]
        stamp = datetime.fromisoformat(attempt["created_at"]).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        rows.append(f"| {number} | {stamp} | {item['config_fingerprint'][:12]} | {item['hint_level']} | {STATUS.get(item['status'], item['status'])} |")
    rows += ["", "## 历次故障与修改线索", ""]
    for number, attempt in enumerate(history["attempts"], 1):
        faults = attempt["result"]["findings"]
        if faults:
            rows.append(f"第 {number} 次提交：")
            for fault in faults:
                rows.append(f"- {clean(fault['code'])}：{clean(fault['message'])}")
                for evidence in fault.get("evidence", []):
                    if "line" in evidence and "text" in evidence:
                        rows.append(f"  - {clean(evidence.get('device', ''))} 第 {evidence['line']} 行：{clean(evidence['text'])}")
    rows += ["", "## 本次故障与证据", ""]
    if not result["findings"] and not is_open:
        rows.append("本次在受支持的检查范围内未检出故障。")
    for item in result["findings"] + result["unknowns"]:
        rows += [f"- {clean(item['code'])}：{clean(item['message'])}"]
        for evidence in item["evidence"]:
            if "line" in evidence and "text" in evidence:
                rows.append(f"  - {clean(evidence.get('device', ''))} 第 {evidence['line']} 行：{clean(evidence['text'])}")
            elif "output_index" in evidence:
                rows.append(f"  - 命令输出 {evidence['output_index']}：{clean(evidence.get('device'))} / {clean(evidence.get('command'))}")
    if is_open:
        if result.get("conversation"):
            rows += ["", "## 交互排查记录", ""]
            for turn in result["conversation"]:
                rows += ["- 你：" + clean(turn["user"]), "- LabAgent：" + clean(turn["assistant"])]
        rows += ["", "## 排查方向与原文依据", ""]
        for number, attempt in enumerate(history["attempts"], 1):
            analysis = attempt["result"].get("investigation", {})
            rows += ["", f"第 {number} 次提交：{clean(analysis.get('summary', '未生成有效分析'))}"]
            for hypothesis in analysis.get("hypotheses", []):
                rows += [f"- 排查方向：{clean(hypothesis['title'])}", f"  - 原因：{clean(hypothesis['reasoning'])}"]
                for citation in hypothesis["evidence"]:
                    rows.append(f"  - {clean(citation['device'])} / {citation['source_id']} 第{citation['line']}行：{clean(citation['quote'])}")
            for request in analysis.get("evidence_requests", []):
                rows.append(f"- 待补充 {clean(request['device'])}：{clean(request['information'])}；用途：{clean(request['why'])}")
            for step in analysis.get("steps", []):
                rows.append("- 操作建议：" + clean(step))
    rows += ["", "## 当前说明", "", result["hint"], "", "## 本次运行复测", ""]
    for check in result["runtime_checks"]:
        rows.append(f"- {clean(check['check'])}：{clean(check['status'])}；来源 {clean(check.get('source', 'user_submitted'))}")
    ping_rows = result.get("interaction_checks", {}).get("check_submitted_pings", {}).get("pings", [])
    for ping in ping_rows:
        rows.append(f"- {clean(ping['device'])} → {clean(ping['target'])}：{ping['sent']}发{ping['received']}收，{'通过' if ping['passed'] else '未通过'}。")
    for missing in result["runtime_missing"]:
        rows.append(f"- 待补充：{clean(missing)}")
    for warning in result.get("command_output_parsing", {}).get("warnings", []):
        rows.append(f"- 解析说明：{clean(warning)}")
    if not result["runtime_checks"] and not ping_rows:
        rows.append("补充本次测试输出后，可在这里查看复测结果。")
    rows += ["", "## 学习与修改记录", "", f"提交 {len(history['attempts'])} 次；输入快照变化 {history['learning_record']['config_revisions']} 次；最高提示等级 {history['learning_record']['max_hint_level']}。", "",
             "## 知识依据", ""]
    for document in result["knowledge"]:
        rows.append(f"- {clean(document['title'])}（{clean(document['source_type'])}）：{document.get('reference_url', '')}")
        if "chunk_id" in document:
            rows.append(f"  - 版本 {clean(document.get('version'))}；第 {document['line_start']}～{document['line_end']} 行；片段 {document['chunk_id']}；正文 SHA256 {document['content_hash']}")
            rows.append("  - 保存的原文片段：" + clean(document["content"]))
    if result.get("knowledge_retrieval", {}).get("warnings"):
        rows.extend("- 资料说明：" + clean(warning) for warning in result["knowledge_retrieval"]["warnings"])
    if any(attempt["result"].get("llm") for attempt in history["attempts"]):
        rows += ["", "## 模型调用记录", "",
                 "| 提交 | 模型 | 状态 | 成功读取的工具 | 耗时（毫秒） |", "| --- | --- | --- | --- | --- |"]
        for number, attempt in enumerate(history["attempts"], 1):
            model = attempt["result"].get("llm")
            if not model:
                continue
            tools = ", ".join(item["tool"] for item in model.get("tool_trace", []) if item["status"] == "ok") or "无"
            rows.append(f"| {number} | {clean(model.get('model', '未配置'))} | {clean(model['status'])} | {clean(tools)} | {model.get('elapsed_ms', '—')} |")
        model = result.get("llm", {})
        rows += ["", "本次模型补充：", "", clean(model.get("text") or model.get("message", "本次未调用模型"))]
    rows += ["", "## 执行步骤", ""]
    for step in result["trace"]:
        rows.append(f"- {step['step']}. {clean(step['component'])}：{clean(step['action'])}")
    return "\n".join(rows) + "\n"
