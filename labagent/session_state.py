"""Shared outcome labels and experiment goals for UI and reports."""
import re

RESOLVED_OUTCOMES = {"user_reported_resolved", "observations_passed"}
RESOLVED_STATUSES = {"resolved_by_user_report", "verified_from_observations"}
STATUS_LABELS = {"needs_information": "继续排查", "resolved_by_user_report": "用户反馈已恢复",
                 "verified_from_observations": "提交的测试输出核验通过", "goals_pending": "访问恢复反馈已记录，实验目标待完成", "comparison_complete": "候选修复对照完成"}


def experiment_goal(payload):
    requirements = payload.get("requirements", {})
    if isinstance(requirements.get("goal"), str) and requirements["goal"].strip():
        return requirements["goal"].strip()[:500]
    for text in payload.get("configs", {}).values():
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if re.search(r"实验目标|实验目的|访问目标|测试目标", line):
                body = re.sub(r"^.*?(?:实验目标|实验目的|访问目标|测试目标)[与及现象\s:：、]*", "", line).strip()
                if body and not body.startswith(("已", "通过")):
                    return body[:500]
                if index + 1 < len(lines) and lines[index + 1].strip():
                    return lines[index + 1].strip()[:500]
    question = payload.get("question", "").strip()
    if question and not question.startswith(("请根据上传的实验材料", "请分析上传的实验材料")):
        return question[:500]
    return "完成提交材料中的网络访问目标"
