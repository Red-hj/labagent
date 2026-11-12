"use strict";
const $ = id => document.getElementById(id);
const materials = new Map();
let sessionId = null;
let modelReady = false;
let online = false;
let busy = false;
let evidenceKind = "user_submitted";
let knowledgeScope = "default";
let pasteNumber = 0;
let activeRequest = null;
let requestTimer = null;
let toastTimer = null;
let materialViews = new Map();
let demoActive = false;
const defaultQuestion = "请根据上传的实验材料分析问题，给出有原文依据的排查方向，并说明下一步需要补充哪些材料。";

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function message(text, error = false) {
  $("message").textContent = text;
  $("message").className = "message" + (error ? " error" : "");
  const toast = $("feedback-toast");
  clearTimeout(toastTimer);
  toast.textContent = text; toast.hidden = !text;
  toast.className = "feedback-toast" + (error ? " error" : "");
  if (!busy && !error) toastTimer = setTimeout(() => { toast.hidden = true; }, 6000);
}
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(response.status === 413 ? "材料较多，请先保留与本次问题相关的内容。" : data.error || "请求未完成，请稍后重试。");
  return data;
}
function setBusy(value) {
  busy = value;
  for (const id of ["choose-files", "material-files", "paste-toggle", "add-pasted", "pasted-material", "problem", "new-diagnosis", "save-report", "followup-message", "send-followup", "load-demo"]) $(id).disabled = busy;
  $("load-demo").disabled = busy || Boolean(sessionId);
  $("load-demo-repair").disabled = busy || !demoActive || !sessionId;
  $("analyze").disabled = busy || !modelReady;
  $("analyze").replaceChildren(document.createTextNode(busy ? "正在处理…" : sessionId ? "继续诊断" : "开始诊断"), element("span", "→"));
  $("send-followup").textContent = busy && activeRequest?.type === "ask" ? "正在回复…" : "发送追问 ↗";
  $("service-status").textContent = busy ? "正在处理请求" : modelReady ? "服务已就绪" : "等待模型配置";
  $("analysis-result").setAttribute("aria-busy", String(busy));
  for (const button of $("material-list").querySelectorAll("button")) button.disabled = busy;
  for (const control of document.querySelectorAll(".interaction-actions button,.request button,.request textarea")) control.disabled = busy;
}
function materialChanged(detail) {
  message(detail || (sessionId ? "材料已更新，点击继续诊断即可。" : "材料已添加，可以开始诊断。"));
  if (sessionId) $("result-state").textContent = "材料已更新 · 下方为上一次分析";
}
function renderMaterials() {
  $("material-list").replaceChildren(); materialViews = new Map();
  $("material-source").hidden = evidenceKind !== "demo";
  $("material-count").textContent = materials.size ? "已添加 " + materials.size + " 份材料" : "尚未添加材料";
  for (const [name, text] of materials) {
    const item = element("div", undefined, "material-item");
    const row = element("div", undefined, "material-row");
    const info = element("div", undefined, "file-info");
    info.append(element("div", name, "file-name"), element("div", text.split(/\r?\n/).length + " 行内容", "file-size"));
    const remove = element("button", "移除", "remove-material");
    remove.setAttribute("aria-label", "移除 " + name);
    remove.addEventListener("click", () => { if (busy) return; materials.delete(name); renderMaterials(); materialChanged(); });
    row.append(element("span", "TXT", "file-icon"), info, remove);
    const preview = element("details");
    const source = element("pre", text);
    preview.append(element("summary", "查看内容"), source);
    materialViews.set(name, {preview, source, text});
    item.append(row, preview); $("material-list").append(item);
  }
}
function locateCitation(citation) {
  const view = materialViews.get(citation.device);
  const lines = view?.text.split(/\r?\n/);
  if (!view || lines[citation.line - 1] !== citation.quote) return message("当前材料已更新，请继续诊断以更新原文定位。", true);
  const selected = element("mark", lines[citation.line - 1]); selected.tabIndex = -1;
  selected.setAttribute("aria-label", citation.device + " 第" + citation.line + "行：" + citation.quote);
  view.source.replaceChildren(document.createTextNode(lines.slice(0, citation.line - 1).join("\n") + (citation.line > 1 ? "\n" : "")), selected,
    document.createTextNode("\n" + lines.slice(citation.line).join("\n")));
  view.preview.open = true; selected.focus({preventScroll:true}); selected.scrollIntoView({block:"center", behavior:"instant"});
  message("已定位 " + citation.device + " 第" + citation.line + "行。");
}
async function loadDemo(repaired = false) {
  if (busy) return;
  if (!repaired && (sessionId || materials.size)) return message("点击“新的排查”后，可带入完整示例材料。");
  try {
    const demo = await api("/api/demos/network_access");
    const stage = demo.stages[repaired ? 1 : 0];
    for (const [name, text] of Object.entries(stage.materials)) materials.set(name, text);
    evidenceKind = "demo"; demoActive = true; renderMaterials();
    if (!repaired) $("problem").value = "PC1取得DHCP地址后，为什么访问不了10.30.0.10？";
    $("demo-status").textContent = repaired ? "复测材料已更新。点击“核对修改结果”，沿用当前会话继续。" : "故障材料已就绪。点击“开始诊断”，随后可追问原因、获取修改步骤。";
    materialChanged(repaired ? "修改与复测材料已带入，点击“核对修改结果”继续。" : "示例材料已带入，点击“开始诊断”。");
    setBusy(false);
  } catch (error) { message("示例载入失败：" + error.message, true); }
}

function renderTools(result) {
  const tools = result.llm?.tool_trace || [];
  $("tools-section").hidden = !tools.length;
  $("tools-heading").textContent = "本轮工具记录 · " + tools.length + " 次调用";
  $("tool-records").replaceChildren();
  const names = {read_source:"读取材料", retrieve_reference:"检索参考资料", compare_materials:"比较材料改动", check_submitted_pings:"核对Ping结果", submit_hypotheses:"整理分析与下一步"};
  for (const tool of tools) {
    const row = element("li");
    row.append(element("strong", names[tool.tool] || tool.tool), element("span", tool.status === "ok" ? "已完成" : "需要调整", "tag"));
    const details = element("details"); details.append(element("summary", "查看调用参数与结果"), element("pre", JSON.stringify({parameters:tool.arguments, result:tool.result}, null, 2)));
    row.append(details); $("tool-records").append(row);
  }
}
function renderProgress(result) {
  const completed = result.status === "verified_from_observations";
  const outcome = result.investigation?.outcome;
  const intent = result.conversation?.at(-1)?.intent;
  const current = completed ? 4 : outcome === "ready_for_retest" || intent === "verify" ? 3 : intent === "repair" ? 2 : 1;
  for (const [index, step] of [...$("diagnosis-progress").children].entries()) {
    step.className = index < current ? "done" : index === current ? "current" : "";
    if (index === current) step.setAttribute("aria-current", "step"); else step.removeAttribute("aria-current");
  }
}
async function addFiles(files) {
  if (busy || !files.length) return;
  try {
    message("正在读取 " + files.length + " 份材料…");
    const incoming = new Map();
    for (const file of files) {
      if (!/\.(txt|cfg|conf|log|md|json)$/i.test(file.name)) throw new Error("请将 " + file.name + " 导出为文本文件后上传，目前支持 txt、cfg、log、md、json。");
      if (file.size > 200000) throw new Error(file.name + " 较大，请保留与本次问题有关的部分后上传（单份200 KB以内）。");
      const bytes = await file.arrayBuffer();
      let text;
      try { text = new TextDecoder("utf-8", {fatal:true}).decode(bytes); }
      catch { text = new TextDecoder("gb18030").decode(bytes); }
      if (!text.trim()) throw new Error(file.name + " 是空文件，请添加有内容的材料。");
      incoming.set(file.name.slice(0, 120), text);
    }
    if (new Set([...materials.keys(), ...incoming.keys()]).size > 20) throw new Error("一次诊断最多添加20份材料，请先保留相关文件。");
    const updated = [...incoming.keys()].filter(name => materials.has(name)).length;
    for (const [name, text] of incoming) materials.set(name, text);
    evidenceKind = demoActive ? "demo" : "user_submitted"; renderMaterials();
    materialChanged("材料已就绪：" + incoming.size + " 份" + (updated ? "（更新同名文件 " + updated + " 份）" : "") + "。点击“" + (sessionId ? "继续诊断" : "开始诊断") + "”提交分析。");
  } catch (error) { message(error.message, true); }
}
function requestPayload() {
  if (!materials.size) throw new Error("请先添加或粘贴需要诊断的材料。");
  return {diagnostic_mode:"open_network", use_llm:true, question:$("problem").value.trim() || defaultQuestion,
    configs:Object.fromEntries(materials), knowledge_scope:knowledgeScope, evidence_kind:evidenceKind,
    ...(sessionId ? {session_id:sessionId} : {})};
}

function requestState(state, text) {
  const status = $("request-status"); status.hidden = false; status.className = "request-status " + state;
  $("request-status-text").textContent = text;
  if (state !== "pending") { clearInterval(requestTimer); requestTimer = null; $("request-elapsed").textContent = ""; }
}
function pendingTurn(text) {
  $("conversation").querySelector(".chat-transient")?.remove();
  const wrapper = element("div", undefined, "chat-turn chat-transient");
  const user = element("div", undefined, "chat-message chat-user");
  user.append(element("p", "你", "chat-label"), document.createTextNode(text));
  const response = element("div", undefined, "chat-message chat-assistant chat-pending");
  response.append(element("p", "LabAgent", "chat-label"), element("span", "已收到，正在分析并生成回复…", "pending-text"));
  wrapper.append(user, response); $("conversation").append(wrapper);
  $("conversation").scrollTop = $("conversation").scrollHeight;
}
function beginRequest(type, text) {
  activeRequest = {type, text, started:Date.now()}; setBusy(true);
  const status = {analyze:"材料已提交，正在生成诊断…", ask:"追问已发送，正在生成回复…", repair:"请求已提交，正在整理修改步骤…", update:"补充材料已提交，正在继续分析…", verify:"复测材料已提交，正在比较改动与测试结果…"}[type];
  requestState("pending", status); message(status);
  if (type === "analyze" && !sessionId) {
    $("empty-result").querySelector("h3").textContent = "正在分析材料…";
    $("empty-result").querySelector("p").textContent = "正在读取材料并整理问题原因，结果会显示在这里。";
  }
  $("request-elapsed").textContent = "已等待 0 秒";
  requestTimer = setInterval(() => { $("request-elapsed").textContent = "已等待 " + Math.floor((Date.now() - activeRequest.started) / 1000) + " 秒"; }, 1000);
  if (type !== "analyze") pendingTurn(text);
}
function finishRequest(state, text) {
  requestState(state, text); setBusy(false); message(text, state === "error");
  if (state === "error" && activeRequest?.type !== "analyze") {
    if (!$("conversation").querySelector(".chat-transient")) pendingTurn(activeRequest.text);
    const response = $("conversation").querySelector(".chat-transient .chat-assistant");
    response.className = "chat-message chat-assistant chat-failed";
    response.querySelector(".pending-text").textContent = text;
    $("conversation").scrollTop = $("conversation").scrollHeight;
  }
}
async function performInteraction(type, text) {
  if (busy) return;
  try {
    const input = requestPayload(); input.interaction = {type, message:text};
    beginRequest(type, text);
    const result = await api("/api/diagnose", input);
    showResult(result);
    if (result.llm?.status === "ok") {
      if (type === "ask" && $("followup-message").value.trim() === text) $("followup-message").value = "";
      finishRequest("success", {ask:"回复已生成，已显示在对话末尾。", repair:"修改步骤已生成。", update:"补充材料已处理，诊断已更新。", verify:"本轮复测核对完成，结果已更新。"}[type]);
    } else finishRequest("error", (result.llm?.message || "本次请求未完成。") + " 输入已保留，可以再次提交。");
  } catch (error) {
    if (activeRequest) finishRequest("error", error.message + " 输入已保留，可以再次提交。");
    else message(error.message, true);
  } finally { clearInterval(requestTimer); requestTimer = null; activeRequest = null; setBusy(false); }
}

function renderConversation(turns) {
  $("conversation").replaceChildren();
  for (const turn of turns) {
    const wrapper = element("div", undefined, "chat-turn");
    const user = element("div", undefined, "chat-message chat-user");
    user.append(element("p", "你", "chat-label"), document.createTextNode(turn.intent === "analyze" ? "已提交材料，开始排查。" : turn.user));
    const assistant = element("div", undefined, "chat-message chat-assistant");
    assistant.append(element("p", "LabAgent", "chat-label"), document.createTextNode(turn.assistant));
    wrapper.append(user, assistant); $("conversation").append(wrapper);
  }
  $("conversation").scrollTop = $("conversation").scrollHeight;
}

function renderChecks(checks) {
  const container = $("verification-checks"); container.replaceChildren(); container.hidden = !Object.keys(checks).length;
  if (checks.compare_materials) {
    const comparison = checks.compare_materials;
    container.append(element("strong", comparison.has_previous ? (comparison.changed ? "已对比本轮材料更新" : "当前材料与上一轮相同") : "已读取当前材料"));
    for (const change of comparison.changes) container.append(element("p", change.material + " · " + ({added:"新增",updated:"已更新",removed:"已移除"}[change.change] || change.change)));
  }
  if (checks.check_submitted_pings) {
    const pings = checks.check_submitted_pings.pings;
    container.append(element("p", pings.length ? "已核对上传材料中的 " + pings.length + " 条 Ping 记录：" : "请补充包含收发统计的 Ping 输出。"));
    for (const ping of pings) container.append(element("p", ping.device + " → " + ping.target + "：" + ping.sent + " 发 " + ping.received + " 收 · " + (ping.passed ? "该条观测通过" : "该条观测未通过"), ping.passed ? "check-pass" : "check-fail"));
  }
}
function showResult(result) {
  sessionId = result.session_id;
  window.history.replaceState(null, "", "/?session=" + sessionId);
  $("empty-result").hidden = true; $("analysis-result").hidden = false; $("save-report").hidden = false;
  const analysis = result.investigation || {};
  renderProgress(result); renderTools(result);
  const ok = result.llm?.status === "ok";
  const intent = result.conversation?.at(-1)?.intent;
  $("result-state").textContent = ok ? (result.status === "verified_from_observations" && intent !== "ask" ? "本轮测试通过 · 排查完成" : {ask:"本次回复", repair:"修改步骤", verify:"本轮材料核对", update:"补充材料后的进展"}[intent] || "本次排查发现") : "本次分析尚未完成";
  $("analysis-summary").textContent = ok ? analysis.summary || result.hint : result.llm?.message || result.hint;
  $("steps-section").hidden = !analysis.steps?.length;
  $("guidance-steps").replaceChildren();
  for (const step of analysis.steps || []) $("guidance-steps").append(element("li", step));
  renderChecks(result.interaction_checks || {});
  renderConversation(result.conversation || [{intent:"analyze", user:defaultQuestion, assistant:analysis.reply || analysis.summary || result.hint}]);
  $("hypotheses").replaceChildren();
  for (const hypothesis of analysis.hypotheses || []) {
    const item = element("section", undefined, "finding");
    item.append(element("span", "排查方向", "tag"), element("h3", hypothesis.title), element("p", hypothesis.reasoning));
    const detail = element("details"); detail.append(element("summary", "查看材料依据（" + hypothesis.evidence.length + "处）"));
    for (const citation of hypothesis.evidence) {
      const row = element("div", undefined, "citation");
      const locate = element("button", "定位原文 ↗", "text-button"); locate.type = "button";
      locate.addEventListener("click", () => locateCitation(citation));
      row.append(element("p", citation.device + " · 第" + citation.line + "行"), element("pre", citation.quote), locate); detail.append(row);
    }
    item.append(detail); $("hypotheses").append(item);
  }
  $("evidence-requests").replaceChildren();
  const requests = analysis.pending_requests || analysis.evidence_requests || [];
  $("requests-section").hidden = !requests.length;
  for (const [index, request] of requests.entries()) {
    const row = element("div", undefined, "request");
    row.append(element("strong", request.device), element("p", request.information), element("p", request.why, "request-reason"));
    const add = element("button", "补充这份材料", "text-button"); add.type = "button";
    const panel = element("div"); panel.hidden = true;
    const label = element("label", "补充材料 " + (index + 1)); label.htmlFor = "request-material-" + index;
    const input = element("textarea"); input.id = label.htmlFor; input.rows = 4; input.placeholder = "粘贴采集到的输出，也可以说明当前还无法提供的情况…";
    const submit = element("button", "提交并继续排查", "send-button evidence-submit"); submit.type = "button";
    add.addEventListener("click", () => { panel.hidden = !panel.hidden; if (!panel.hidden) input.focus(); });
    submit.addEventListener("click", async () => {
      if (!input.value.trim()) { input.focus(); return message("请先粘贴这项材料。", true); }
      if (materials.size >= 20 || input.value.length > 200000) return message("请精简这次补充的内容。", true);
      let name; do { name = request.device.slice(0, 60) + "-补充" + (++pasteNumber) + ".log"; } while (materials.has(name));
      materials.set(name, input.value); evidenceKind = demoActive ? "demo" : "user_submitted"; renderMaterials();
      await performInteraction("update", "我已补充 " + request.device + " 的材料，请继续验证：" + request.information);
    });
    panel.append(label, input, submit); row.append(add, panel); $("evidence-requests").append(row);
  }
  $("references-section").hidden = !result.knowledge?.length;
  $("references").replaceChildren();
  for (const document of result.knowledge || []) {
    const reference = element("div"); reference.append(element("p", document.title), element("p", document.content));
    reference.append(element("p", [document.version, "第" + document.line_start + "～" + document.line_end + "行", document.source_name].filter(Boolean).join(" · "), "source-location"));
    if (/^https?:\/\//i.test(document.reference_url || "")) {
      const link = element("a", "查看来源"); link.href = document.reference_url; link.target = "_blank"; link.rel = "noopener noreferrer"; reference.append(link);
    }
    $("references").append(reference);
  }
}

$("load-demo").addEventListener("click", () => loadDemo());
$("load-demo-repair").addEventListener("click", () => loadDemo(true));
$("choose-files").addEventListener("click", () => $("material-files").click());
$("material-files").addEventListener("change", async event => { await addFiles(event.target.files); event.target.value = ""; });
for (const type of ["dragenter", "dragover"]) $("drop-zone").addEventListener(type, event => { event.preventDefault(); if (!busy) $("drop-zone").classList.add("dragging"); });
$("drop-zone").addEventListener("dragleave", () => $("drop-zone").classList.remove("dragging"));
$("drop-zone").addEventListener("drop", async event => { event.preventDefault(); $("drop-zone").classList.remove("dragging"); await addFiles(event.dataTransfer.files); });
$("paste-toggle").addEventListener("click", () => { $("paste-panel").hidden = !$("paste-panel").hidden; $("paste-toggle").setAttribute("aria-expanded", String(!$("paste-panel").hidden)); if (!$("paste-panel").hidden) $("pasted-material").focus(); });
$("add-pasted").addEventListener("click", () => {
  const text = $("pasted-material").value;
  if (!text.trim()) return message("请先粘贴材料内容。", true);
  if (materials.size >= 20 || text.length > 200000) return message("请保留与本次问题相关的材料，每份200 KB以内，最多20份。", true);
  let name; do { name = "粘贴材料" + (++pasteNumber) + ".txt"; } while (materials.has(name));
  materials.set(name, text); evidenceKind = demoActive ? "demo" : "user_submitted"; $("pasted-material").value = ""; $("paste-panel").hidden = true; $("paste-toggle").setAttribute("aria-expanded", "false"); renderMaterials(); materialChanged();
});
$("analyze").addEventListener("click", async () => {
  if (busy) return;
  try {
    const input = requestPayload(); beginRequest("analyze", "已提交材料，开始诊断。");
    const result = await api("/api/diagnose", input); showResult(result);
    finishRequest(result.llm?.status === "ok" ? "success" : "error", result.llm?.status === "ok" ? "诊断完成，结果已显示并保存。" : (result.llm?.message || "本次诊断未完成。") + " 材料已保留，可以再次提交。");
  } catch (error) {
    if (activeRequest) finishRequest("error", error.message + " 材料已保留，可以再次提交。");
    else message(error.message, true);
  } finally { clearInterval(requestTimer); requestTimer = null; activeRequest = null; setBusy(false); }
});
for (const button of document.querySelectorAll("[data-intent]")) button.addEventListener("click", () => {
  const text = {ask:"请解释当前疑点为什么会造成这个现象，用实验中的材料说明。", repair:"请基于当前材料给出具体修改步骤，以及每一步之后应观察什么。", verify:"我已更新相关材料，请先比较与上一轮的改动，再核对上传的Ping结果，说明还需要验证什么。"}[button.dataset.intent];
  performInteraction(button.dataset.intent, text);
});
$("followup-form").addEventListener("submit", event => {
  event.preventDefault(); const text = $("followup-message").value.trim();
  if (text) performInteraction("ask", text); else $("followup-message").focus();
});
$("followup-message").addEventListener("keydown", event => {
  if (event.key !== "Enter" || event.shiftKey || event.isComposing || event.keyCode === 229) return;
  event.preventDefault();
  if (!busy && !event.repeat) $("followup-form").requestSubmit();
});
$("new-diagnosis").addEventListener("click", () => {
  materials.clear(); sessionId = null; evidenceKind = "user_submitted"; knowledgeScope = "default"; demoActive = false;
  $("demo-status").textContent = "先带入故障材料，再点击“开始诊断”。";
  $("problem").value = ""; $("pasted-material").value = ""; $("paste-panel").hidden = true; $("paste-toggle").setAttribute("aria-expanded", "false");
  $("followup-message").value = ""; $("conversation").replaceChildren();
  $("analysis-result").hidden = true; $("empty-result").hidden = false; $("save-report").hidden = true;
  $("empty-result").querySelector("h3").textContent = "准备好材料，就可以开始。";
  $("empty-result").querySelector("p").textContent = "这里会显示问题原因、材料依据和下一步操作。";
  $("request-status").hidden = true;
  window.history.replaceState(null, "", "/"); renderMaterials(); setBusy(false); message("已开始新的诊断，请添加材料。");
});
$("save-report").addEventListener("click", async () => {
  $("save-report").disabled = true;
  try {
    const report = await api("/api/export", {session_id:sessionId});
    if (online) {
      const url = URL.createObjectURL(new Blob([report.markdown], {type:"text/markdown;charset=utf-8"}));
      const link = element("a"); link.href = url; link.download = report.filename;
      document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      message("实验报告已下载。");
    } else message("实验报告已保存到项目文件夹。");
  }
  catch (error) { message(error.message, true); }
  finally { $("save-report").disabled = false; }
});
async function start() {
  const health = await api("/api/health"); modelReady = health.llm_configured; online = health.online; setBusy(false);
  if (online) {
    $("save-report").textContent = "下载实验报告";
    document.querySelector("footer span:last-child").textContent = "测试过程保存在当前在线服务";
  }
  const savedSession = new URLSearchParams(window.location.search).get("session");
  if (savedSession && /^[a-f0-9]{32}$/.test(savedSession)) {
    const saved = await api("/api/sessions/" + savedSession);
    const latest = saved.attempts[saved.attempts.length - 1];
    for (const [name, text] of Object.entries(latest.input.configs)) materials.set(name, text);
    for (const [index, output] of (latest.input.command_outputs || []).entries()) materials.set((output.device + "-输出" + (index + 1) + ".log").slice(0, 120), output.command + "\n" + output.text);
    $("problem").value = latest.input.question === defaultQuestion ? "" : latest.input.question || "";
    knowledgeScope = latest.input.knowledge_scope || "default"; evidenceKind = latest.input.evidence_kind || "user_submitted";
    demoActive = evidenceKind === "demo" && materials.has("终端跨网段访问.txt");
    renderMaterials();
    if (latest.result.mode === "open_network") { showResult({...latest.result, session_id:savedSession}); setBusy(false); message("已恢复这次诊断，可以继续交流或补充材料。"); }
    else { message("已带入保存的材料，可以开始诊断。"); }
  }
  if (!modelReady) message("请由项目维护者先配置模型服务，配置完成后刷新页面即可。", true);
}
start().catch(error => { setBusy(false); message("暂时无法连接服务：" + error.message, true); });
