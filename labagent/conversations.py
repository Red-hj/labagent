"""Display complete saved conversations without expanding model context."""

DEFAULT_QUESTIONS = (
    "请根据上传的实验材料分析问题，给出有原文依据的排查方向，并说明下一步需要补充哪些材料。",
    "请分析上传的实验材料，说明问题原因与下一步操作。",
)


def conversation_title(question, goal, material):
    if question and question not in DEFAULT_QUESTIONS:
        text = question
    else:
        text = goal or material or "实验排查"
    return " ".join(text.split())[:80]


def dialogue(attempts):
    turns = []
    for number, attempt in enumerate(attempts, 1):
        payload, result = attempt["input"], attempt["result"]
        analysis = result.get("investigation", {})
        interaction = payload.get("interaction", {})
        question = payload.get("question", "")
        user = interaction.get("message") or (question if question not in DEFAULT_QUESTIONS else "") or "已提交材料，开始诊断。"
        model = result.get("llm", {})
        answer = (analysis.get("reply") or analysis.get("summary") or result.get("hint", "")) if model.get("status") in (None, "ok") else (model.get("message") or result.get("hint", "本轮诊断尚未完成。"))
        evidence = []
        for item in analysis.get("hypotheses", []):
            for citation in item.get("evidence", []):
                if citation not in evidence:
                    evidence.append(citation)
        turns.append({"attempt": number, "intent": interaction.get("type", "analyze"), "user": user,
                      "actor": payload.get("room_actor") or "你",
                      "assistant": answer, "summary": analysis.get("summary", ""),
                      "steps": analysis.get("steps", []), "evidence": evidence,
                      "created_at": attempt["created_at"], "status": result["status"], "model_status": model.get("status"),
                      "material_names": list(payload.get("configs", {})), "recording_ids": payload.get("recordings", [])})
    return turns
