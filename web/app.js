"use strict";
const $ = id => document.getElementById(id);
const pretty = value => JSON.stringify(value, null, 2);
let sessionId = null;
let lastResult = null;
let configEditors = new Map();
let scenario = null;
let stageIndex = 0;
let evidenceKind = "user_submitted";

function updateModeUI() {
  const open = $("diagnostic-mode").value === "open_network";
  $("mode-description").textContent = open ? "上传配置、终端信息或日志，模型按需读取并给出带出处的假设。补充新证据后，可继续同一会话。" : "使用明确的 OSPF 检查规则，支持修改后复测。";
  $("materials-label").textContent = open ? "设备材料、终端信息与日志" : "设备配置";
  $("materials-description").textContent = open ? "支持导入文本配置或日志，新文件名会新增材料。可以先不填写拓扑；按补充请求更新材料后，再次提交同一会话。" : "文件名须对应设备名，如 R1.txt。也可以直接修改下方配置。";
  $("add-material").hidden = !open;
  $("hint-options").hidden = open;
}

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "请求失败");
  return data;
}
function message(text, error = false) { $("message").textContent = text; $("message").className = error ? "error" : ""; }
function element(tag, text, className) { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (className) el.className = className; return el; }
function renderPassages(container, passages) {
  for (const doc of passages) {
    const item = element("div", undefined, "evidence-item");
    item.append(element("span", doc.source_type, "tag"), element("h3", doc.title), element("p", doc.content));
    item.append(element("p", (doc.version || "未注明版本") + " · 第 " + doc.line_start + "～" + doc.line_end + " 行 · " + (doc.source_name || "本地说明"), "source-location"));
    const source = element("details"); source.append(element("summary", "查看片段标识与内容指纹"), element("pre", pretty({chunk_id:doc.chunk_id, content_hash:doc.content_hash, char_start:doc.char_start, char_end:doc.char_end}))); item.append(source);
    if (typeof doc.reference_url === "string" && /^https?:\/\//i.test(doc.reference_url)) {
      const link = element("a", "查看参考资料", "knowledge-link"); link.href = doc.reference_url; link.target = "_blank"; link.rel = "noopener noreferrer"; item.append(link);
    }
    container.append(item);
  }
}
async function refreshScopes(selected) {
  const data = await api("/api/knowledge/scopes");
  $("knowledge-scope").replaceChildren();
  for (const scope of data.scopes) { const option = element("option", scope === "default" ? "default · 通用资料" : scope); option.value = scope; $("knowledge-scope").append(option); }
  if (data.scopes.includes(selected)) $("knowledge-scope").value = selected;
}
function resetSession() { sessionId = null; lastResult = null; window.history.replaceState(null, "", "/workbench"); $("result").hidden = true; $("empty").hidden = false; $("export").disabled = true; message("新实验已开始；当前输入已保留。"); }
function renderConfigs(configs) {
  $("configs").replaceChildren(); configEditors = new Map();
  for (const [name, content] of Object.entries(configs)) {
    const label = element("label", name, "config-label");
    const editor = element("textarea", undefined, "code config-editor");
    editor.value = content; editor.rows = 9; editor.id = "device-" + configEditors.size; label.htmlFor = editor.id;
    editor.addEventListener("input", invalidateObservations);
    configEditors.set(name, editor); $("configs").append(label, editor);
  }
}
function invalidateObservations() {
  $("fresh-observations").checked = false; $("binding-status").textContent = "配置改变后，请重新采集观测并绑定。";
  if (lastResult) { $("status-label").textContent = "输入已修改 · 下方为上一次结果"; $("status-banner").className = "status-banner neutral"; $("status-explanation").textContent = "请重新诊断，更新配置检查和复测状态。"; }
}
function payload() {
  const input = {question: $("question").value, configs: Object.fromEntries([...configEditors].map(([name, editor]) => [name, editor.value])), topology: JSON.parse($("topology").value), requirements: JSON.parse($("requirements").value), observations: JSON.parse($("observations").value || "{}"), hint_level: Number($("hint-level").value), use_llm: $("use-llm").checked};
  input.command_outputs = JSON.parse($("command-outputs").value || "[]"); input.routing_queries = JSON.parse($("routing-queries").value || "[]"); input.evidence_kind = evidenceKind;
  input.knowledge_scope = $("knowledge-scope").value;
  input.diagnostic_mode = $("diagnostic-mode").value;
  if (input.diagnostic_mode === "open_network" && !input.use_llm) throw new Error("开放排查需要勾选已配置模型；也可切回 OSPF 规则检查。");
  if (sessionId) input.session_id = sessionId;
  return input;
}
async function loadCase() {
  const selection = $("case-select").value;
  if (!selection) return message("请先选择要载入的案例。");
  scenario = selection.startsWith("scenario:") ? await api("/api/scenarios/" + selection.slice(9)) : null;
  stageIndex = 0;
  const data = scenario ? scenario.stages[0] : await api("/api/cases/" + selection);
  resetSession(); applyInput(data);
}
function applyInput(data) {
  $("diagnostic-mode").value = data.diagnostic_mode || "ospf_rules"; updateModeUI();
  renderConfigs(data.configs);
  $("question").value = data.question; $("topology").value = pretty(data.topology || {}); $("requirements").value = pretty(data.requirements || {});
  $("observations").value = pretty(data.observations || {neighbors: [], pings: []});
  $("case-description").textContent = data.description || "";
  $("knowledge-scope").value = data.knowledge_scope || "default";
  $("hint-level").value = data.hint_level || 1;
  $("use-llm").checked = !$("use-llm").disabled && data.use_llm === true;
  evidenceKind = data.evidence_kind || "user_submitted";
  $("command-outputs").value = pretty(data.command_outputs || []); $("routing-queries").value = pretty(data.routing_queries || []);
  $("next-stage").hidden = !scenario || stageIndex >= scenario.stages.length - 1;
  $("next-stage").disabled = true;
  $("fresh-observations").checked = false;
  $("binding-status").textContent = data.observations?.config_fingerprint ? "案例内含已绑定的演示观测，仅用于演示。" : "";
  message("已载入：" + data.title);
}
const statusNames = {fault_found: "发现需要处理的问题", needs_information: "需要补充信息", awaiting_verification: "配置检查通过 · 待运行复测", verified_from_observations: "用户提交的复测观测通过"};
const explanations = {fault_found: "以下结果来自确定性检查和用户提交的运行观测，请结合证据逐步排查。", needs_information: "现有输入或受支持的检查范围不足以确认结果。", awaiting_verification: "静态配置通过不代表网络已恢复，请提交当前配置对应的运行证据。", verified_from_observations: "当前配置与绑定观测符合实验要求。本地引擎没有连接设备执行命令。"};
function showResult(result) {
  lastResult = result; sessionId = result.session_id;
  window.history.replaceState(null, "", "/workbench?session=" + sessionId);
  $("next-stage").disabled = false;
  $("empty").hidden = true; $("result").hidden = false; $("export").disabled = false;
  $("status-label").textContent = statusNames[result.status]; $("status-explanation").textContent = explanations[result.status];
  const open = result.mode === "open_network";
  $("rule-stats").hidden = open;
  if (open) { $("status-label").textContent = "开放排查 · 待验证假设"; $("status-explanation").textContent = "结合下方原文核对假设，按请求补充运行证据后继续排查。"; }
  $("status-banner").className = "status-banner" + (result.status === "verified_from_observations" ? " good" : result.status === "awaiting_verification" ? " neutral" : "");
  $("fault-count").textContent = result.findings.length; $("unknown-count").textContent = result.unknowns.length; $("check-count").textContent = result.checks.length;
  $("hint-label").textContent = open ? "当前排查进展" : "第 " + result.hint_level + " 级提示"; $("hint-text").textContent = result.hint;
  $("llm-text").textContent = result.llm ? (result.llm.text || result.llm.message) : "";
  const model = result.llm;
  const modelStates = {ok: open ? "已读取原始材料并核对引用" : "已读取工具证据并返回解释", unavailable: "调用失败，材料已保留", no_evidence: "未取得有效工具结果", step_limit: "达到调用上限", not_configured: "尚未启用或配置"};
  $("llm-status").textContent = model ? [model.provider, model.model, modelStates[model.status] || model.status, model.elapsed_ms === undefined ? "" : (model.elapsed_ms / 1000).toFixed(1) + " 秒"].filter(Boolean).join(" · ") : "未启用模型辅助，本次使用本地教学提示。";
  $("llm-tools").hidden = !model?.tool_trace?.length;
  $("llm-tool-list").textContent = pretty((model?.tool_trace || []).map(item => ({tool: item.tool, arguments: item.arguments, status: item.status, result: item.result})));
  const evidence = $("tab-evidence"); evidence.replaceChildren();
  if (open) {
    for (const hypothesis of result.investigation?.hypotheses || []) {
      const item = element("div", undefined, "evidence-item");
      item.append(element("span", "待验证假设 · 原文引用已核对", "tag"), element("h3", hypothesis.title), element("p", hypothesis.reasoning));
      for (const citation of hypothesis.evidence) item.append(element("p", citation.device + " · " + citation.source_id + " 第 " + citation.line + " 行", "muted small"), element("pre", citation.quote));
      evidence.append(item);
    }
    for (const request of result.investigation?.evidence_requests || []) {
      const item = element("div", undefined, "evidence-item"); item.append(element("h3", "待补充 · " + request.device), element("p", request.information), element("p", "用于验证：" + request.why, "muted small")); evidence.append(item);
    }
    for (const warning of result.knowledge_retrieval?.warnings || []) evidence.append(element("p", warning, "muted small"));
  }
  for (const item of [...result.findings, ...result.unknowns]) {
    const detail = element("details", undefined, "evidence-item"); detail.open = result.hint_level > 1;
    detail.append(element("summary", item.message)); detail.append(element("p", [item.link_id, item.devices.join(" / "), item.code].filter(Boolean).join(" · "), "muted"));
    if (item.expected !== undefined) detail.append(element("p", "实验要求：" + item.expected + "；实际：" + item.actual));
    detail.append(element("pre", pretty(item.evidence))); evidence.append(detail);
  }
  if (result.checks.length) {
    const detail = element("details"); detail.append(element("summary", "已通过的配置检查（" + result.checks.length + "）"), element("pre", pretty(result.checks))); evidence.append(detail);
  }
  if (result.runtime_missing.length && (!open || !result.investigation?.evidence_requests?.length)) { const item = element("div", undefined, "evidence-item"); item.append(element("h3", open ? "继续排查需要" : "下一次复测需要")); for (const text of result.runtime_missing) item.append(element("p", "• " + text)); evidence.append(item); }
  if (result.runtime_checks.length) { const item = element("details"); item.append(element("summary", "用户提交的运行观测核对"), element("pre", pretty(result.runtime_checks))); evidence.append(item); }
  const trace = $("tab-trace"); trace.replaceChildren(element("p", "以下为实际执行的规则流程与工具记录。", "muted small"));
  for (const step of result.trace) { const row = element("div", undefined, "trace-step"); row.append(element("strong", step.step + " · " + step.component), element("p", step.action), element("pre", pretty(step))); trace.append(row); }
  const knowledge = $("tab-knowledge"); knowledge.replaceChildren();
  knowledge.append(element("p", "资料范围：" + result.knowledge_scope + "。以下为检索片段；实验明确要求以输入设置为准。", "muted small"));
  renderPassages(knowledge, result.knowledge);
  if (!result.knowledge.length) knowledge.append(element("p", "未检索到相关片段，请补充资料或调整问题。", "muted small"));
  for (const warning of result.knowledge_retrieval?.warnings || []) knowledge.append(element("p", warning, "muted small"));
  const record = $("tab-record"); record.replaceChildren();
  const learning = result.learning_record;
  if (learning) {
    record.append(element("p", open ? "本次排查已提交 " + learning.requests + " 次，配置材料变化 " + learning.config_revisions + " 次。" : "本次会话已提交 " + learning.requests + " 次，配置变化 " + learning.config_revisions + " 次，最高使用第 " + learning.max_hint_level + " 级提示。"));
    if (!open) record.append(element("p", learning.assessment, "muted small"));
  }
  record.append(element("p", "会话：" + sessionId, "muted small"), element("p", "配置指纹：" + result.config_fingerprint, "muted small"));
  $("raw-result").textContent = pretty(result);
  if (result.workflow) record.append(element("p", "流程：" + result.workflow.backend + "；" + (result.workflow.paused ? "已保存，等待补充后继续" : "本次流程已结束"), "muted small"));
  api("/api/sessions/" + sessionId + "/report").then(data => { if (sessionId === result.session_id) $("report-text").textContent = data.markdown; }).catch(error => message(error.message, true));
}
$("next-stage").addEventListener("click", () => { if (scenario && stageIndex < scenario.stages.length - 1) { stageIndex++; applyInput(scenario.stages[stageIndex]); invalidateObservations(); message("已载入下一阶段合成演示输入，会话已保留。点击再次复测继续。"); } });
$("load-case").addEventListener("click", () => loadCase().catch(error => message(error.message, true)));
$("new-session").addEventListener("click", resetSession);
$("diagnostic-mode").addEventListener("change", () => { resetSession(); scenario = null; $("next-stage").hidden = true; updateModeUI(); });
$("add-material").addEventListener("click", () => {
  const values = Object.fromEntries([...configEditors].map(([name, editor]) => [name, editor.value]));
  let number = configEditors.size + 1; while (values["材料" + number] !== undefined) number++;
  values["材料" + number] = ""; renderConfigs(values); invalidateObservations();
});
for (const id of ["topology", "requirements", "routing-queries", "command-outputs", "knowledge-scope"]) $(id).addEventListener("input", invalidateObservations);
$("diagnose").addEventListener("click", async () => {
  const button = $("diagnose"); button.disabled = true; message($("use-llm").checked ? "正在检查配置并等待模型读取证据，请稍候…" : "正在核对配置与证据…");
  try { showResult(await api("/api/diagnose", payload())); message("已完成，本次记录保存在项目 runtime 文件夹内。"); }
  catch (error) { message(error instanceof SyntaxError ? "JSON 格式有误，请检查拓扑、实验要求或运行观测。" : error.message, true); }
  finally { button.disabled = false; }
});
$("bind-observations").addEventListener("click", async () => {
  try {
    if (!$("fresh-observations").checked) throw new Error("请先确认观测来自当前配置的一次新采集。");
    const input = payload(); const data = await api("/api/fingerprint", input);
    input.observations.config_fingerprint = data.config_fingerprint; $("observations").value = pretty(input.observations);
    for (const output of input.command_outputs) output.config_fingerprint = data.config_fingerprint;
    $("command-outputs").value = pretty(input.command_outputs);
    $("binding-status").textContent = "已绑定当前配置。修改配置后须重新采集。"; message("运行观测已绑定当前配置。");
  } catch (error) { message(error.message, true); }
});
$("config-files").addEventListener("change", async event => {
  try {
    for (const file of event.target.files) {
      if (file.size > 200000) throw new Error("单份配置文件不能超过 200 KB。");
      const name = file.name.replace(/\.[^.]+$/, "");
      const key = [...configEditors.keys()].find(k => k.toLowerCase() === name.toLowerCase());
      if (!key && $("diagnostic-mode").value === "open_network") {
        const values = Object.fromEntries([...configEditors].map(([device, editor]) => [device, editor.value]));
        values[name] = await file.text(); renderConfigs(values);
      } else {
        if (!key) throw new Error("文件名 " + file.name + " 未对应当前设备，请先调整拓扑与设备输入。");
        configEditors.get(key).value = await file.text();
      }
    }
    evidenceKind = "user_submitted";
    invalidateObservations(); message("配置文件已导入，请重新诊断。");
  } catch (error) { message(error.message, true); }
  event.target.value = "";
});
$("export").addEventListener("click", async () => {
  try { const data = await api("/api/export", {session_id: sessionId}); message("记录已保存：" + data.path + "；报告：" + data.report_path); }
  catch (error) { message(error.message, true); }
});
document.querySelectorAll("[data-tab]").forEach(button => button.addEventListener("click", () => {
  document.querySelectorAll("[data-tab]").forEach(other => { other.classList.toggle("active", other === button); other.setAttribute("aria-selected", String(other === button)); });
  document.querySelectorAll(".tab-content").forEach(content => content.hidden = content.id !== "tab-" + button.dataset.tab);
}));
async function start() {
  const health = await api("/api/health"); $("health").textContent = (health.workflow_backend === "langgraph" ? "LangGraph 工作流" : "本地兼容检查") + (health.llm_configured ? " · " + (health.llm?.provider || "模型") + " 已配置" : " · 本地规划"); $("use-llm").disabled = !health.llm_configured;
  const cases = await api("/api/cases"); for (const item of cases) { const option = element("option", item.title); option.value = item.id; $("case-select").append(option); }
  const scenarios = await api("/api/scenarios"); for (const item of scenarios) { const option = element("option", item.title); option.value = "scenario:" + item.id; $("case-select").append(option); }
  await refreshScopes("default");
  const savedSession = new URLSearchParams(window.location.search).get("session");
  if (savedSession && /^[a-f0-9]{32}$/.test(savedSession)) {
    const saved = await api("/api/sessions/" + savedSession);
    const latest = saved.attempts[saved.attempts.length - 1];
    const savedOption = element("option", "已保存的材料 · 选择案例可另开实验"); savedOption.value = ""; savedOption.disabled = true; $("case-select").prepend(savedOption); $("case-select").value = "";
    applyInput({...latest.input, title:"已保存的排查", description:"更新材料后可继续此会话。"});
    $("use-llm").checked = !$("use-llm").disabled && latest.input.use_llm === true;
    showResult({...latest.result, session_id:savedSession, learning_record:saved.learning_record});
    message("已恢复保存的材料与结果，可以补充证据后继续。");
  } else { await loadCase(); }
}
$("knowledge-file").addEventListener("change", async event => {
  try {
    const file = event.target.files[0]; if (!file) return;
    if (file.size > 500000) throw new Error("资料文件须小于500KB。");
    const content = new TextDecoder("utf-8", {fatal:true}).decode(await file.arrayBuffer());
    $("knowledge-content").value = content; $("knowledge-title").value = file.name.replace(/\.[^.]+$/, ""); $("knowledge-source").value = file.name;
    $("knowledge-message").textContent = "文件已读入，请确认标题、范围和来源后保存。";
  } catch (error) { $("knowledge-message").textContent = "文件读取失败，请使用 UTF-8 的 txt/md 文本：" + error.message; }
  event.target.value = "";
});
$("knowledge-import").addEventListener("click", async () => {
  const button = $("knowledge-import"); button.disabled = true;
  try {
    const result = await api("/api/knowledge/import", {title:$("knowledge-title").value, content:$("knowledge-content").value, scope:$("knowledge-import-scope").value, version:$("knowledge-version").value, source_name:$("knowledge-source").value, reference_url:$("knowledge-url").value});
    await refreshScopes(result.document.scope); invalidateObservations();
    $("knowledge-message").textContent = result.created ? "资料已保存，已切换到所属范围，可以试查。" : "相同资料已存在，已切换到所属范围。";
  } catch (error) { $("knowledge-message").textContent = error.message; }
  finally { button.disabled = false; }
});
$("knowledge-search").addEventListener("click", async () => {
  try {
    const result = await api("/api/knowledge/search", {query:$("knowledge-query").value || $("question").value, scope:$("knowledge-scope").value});
    $("knowledge-preview").replaceChildren(); renderPassages($("knowledge-preview"), result.results);
    $("knowledge-message").textContent = result.results.length ? "已找到 " + result.results.length + " 个相关片段，来自所选范围和通用说明。" : "未找到相关片段，请补充资料或调整问题。";
    for (const warning of result.warnings) $("knowledge-preview").append(element("p", warning, "muted small"));
  } catch (error) { $("knowledge-message").textContent = error.message; }
});
start().catch(error => message("本地服务连接失败：" + error.message, true));
