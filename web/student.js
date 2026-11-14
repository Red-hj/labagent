"use strict";
const $ = id => document.getElementById(id);
const materials = new Map();
const recordings = new Map();
const screenshots = new Map();
let operationHistory = null;
let previewNumber = 0;
let videoReady = false;
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
let serviceState = "connecting";
let sessionProblem = false;
let requestFailed = false;
const defaultQuestion = "请根据上传的实验材料分析问题，给出有原文依据的排查方向，并说明下一步需要补充哪些材料。";
const measuredDemos = new Map([
  ["repair_comparison", "FRR修复实验.json"],
  ["ospf_area_measured", "FRR区域复测.json"],
]);

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
async function api(path, body, options = {}) {
  const response = await fetch(path, {...(body === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)}), ...options});
  const data = (response.headers.get("Content-Type") || "").includes("application/json") ? await response.json() : {};
  if (!response.ok) {
    const error = new Error(response.status === 413 ? "材料较多，请先保留与本次问题相关的内容。" : data.error || "服务暂时无法响应，请稍后重试。");
    error.status = response.status; error.code = data.code; throw error;
  }
  if (!Object.keys(data).length) throw new Error("服务尚未返回完整结果，请稍后重试。");
  return data;
}
function serviceStatus() {
  $("service-status").textContent = busy ? "正在处理请求" : sessionProblem ? "会话已失效 · 可新建排查" : requestFailed ? "本次诊断失败 · 可重试" :
    serviceState === "starting" ? "服务启动中…" : serviceState === "connecting" ? "正在连接服务…" : serviceState === "unavailable" ? "服务暂时不可用" : modelReady ? "服务已就绪" : "等待模型配置";
}
function receiveProgress(event) {
  if (event.type !== "progress") return;
  const list = $("processing-events");
  if (list.lastElementChild?.textContent !== event.message) list.append(element("li", event.message));
  while (list.children.length > 8) list.firstElementChild.remove();
  $("request-status-text").textContent = event.message;
  const pending = $("conversation").querySelector(".chat-transient .pending-text");
  if (pending) pending.textContent = event.message;
  else if (!sessionId) $("empty-result").querySelector("p").textContent = event.message;
}
async function diagnose(input) {
  const response = await fetch("/api/diagnose/stream", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(input)});
  if (!response.ok) {
    const data = (response.headers.get("Content-Type") || "").includes("application/json") ? await response.json() : {};
    throw new Error(data.error || "本次诊断未能开始，请稍后重试。");
  }
  const reader = response.body.getReader(), decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const {done, value} = await reader.read();
      buffer += decoder.decode(value, {stream:!done});
      let newline;
      while ((newline = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, newline); buffer = buffer.slice(newline + 1);
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        if (event.type === "result") return event.result;
        if (event.type === "error") throw new Error(event.message);
        receiveProgress(event);
      }
      if (done) throw new Error("连接已中断，未收到完整诊断结果。可以重新提交。");
    }
  } finally { reader.releaseLock(); }
}
function setBusy(value) {
  busy = value;
  for (const id of ["choose-files", "material-files", "paste-toggle", "add-pasted", "pasted-material", "problem", "new-diagnosis", "save-report", "followup-message", "send-followup", "load-demo"]) $(id).disabled = busy;
  $("demo-case").disabled = busy || Boolean(sessionId) || demoActive;
  $("load-demo").disabled = busy || Boolean(sessionId);
  $("load-demo-repair").disabled = busy || !demoActive || !sessionId;
  $("load-demo-repair").hidden = $("demo-case").value === "operation_history";
  const measured = measuredDemos.has($("demo-case").value);
  if (measured) $("load-demo-repair").hidden = true;
  $("load-demo").textContent = measured ? "带入实际运行对照" : "① 带入故障材料";
  $("demo-panel").querySelector(".tag").textContent = measured ? "FRRouting 实测记录 · 工具重新核对" : "合成演示材料 · 模型与工具实时分析";
  $("load-demo-final").disabled = busy || !demoActive || !sessionId;
  $("load-demo-final").hidden = $("demo-case").value !== "ospf_goals";
  $("load-demo-repair").textContent = $("demo-case").value === "ospf_goals" ? "② 带入静态路由复测" : "② 带入修改与复测材料";
  $("analyze").disabled = busy || !modelReady || sessionProblem;
  $("analyze").replaceChildren(document.createTextNode(busy ? "正在处理…" : sessionId ? "继续诊断" : "开始诊断"), element("span", "→"));
  $("send-followup").textContent = busy && activeRequest?.type === "ask" ? "正在回复…" : "发送追问 ↗";
  serviceStatus();
  $("analysis-result").setAttribute("aria-busy", String(busy));
  for (const button of $("material-list").querySelectorAll("button")) button.disabled = busy;
  for (const control of document.querySelectorAll(".interaction-actions button,.request button,.request textarea,.chat-retry,.operation-analyze")) control.disabled = busy;
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
    info.append(element("div", name, "file-name"), element("div", text.trimEnd().split(/\r?\n/).length + " 行内容", "file-size"));
    const remove = element("button", "移除", "remove-material");
    remove.setAttribute("aria-label", "移除 " + name);
    remove.addEventListener("click", () => { if (busy) return; materials.delete(name); for (const collection of [recordings, screenshots]) for (const [id, media] of collection) if (media.material_name === name) collection.delete(id); renderRecordingChoices(); renderMaterials(); materialChanged(); previewOperations(); });
    const screenshot = [...screenshots.values()].find(item => item.material_name === name);
    row.append(element("span", screenshot ? "IMG" : "TXT", "file-icon"), info, remove);
    if (screenshot) {
      const show = element("button", undefined, "screenshot-thumbnail"); show.type = "button"; show.setAttribute("aria-label", "查看截图 " + screenshot.name);
      const img = element("img"); img.src = screenshot.image_url; img.alt = screenshot.name; img.loading = "lazy";
      show.append(img, element("span", "查看原图 · 引用可定位画面")); show.addEventListener("click", () => showScreenshot(screenshot)); item.append(show);
    }
    const preview = element("details");
    const source = element("pre", text);
    preview.append(element("summary", screenshot ? "查看识别文字 / 修正" : "查看内容"), source);
    if (screenshot) {
      const edit = element("textarea"); edit.value = text; edit.rows = 7; edit.setAttribute("aria-label", "修正 " + screenshot.name + " 的识别文字");
      const save = element("button", "保存识别修正", "secondary-button"); save.type = "button";
      save.addEventListener("click", () => {
        if (busy) return;
        if (!edit.value.trim() || edit.value.length > 200000) return message("请保留1到200000字的识别内容。", true);
        materials.set(name, edit.value); renderMaterials(); materialChanged("识别文字已更新，下一轮诊断会使用修正后的内容。"); previewOperations();
      });
      const editor = element("details"); editor.append(element("summary", "修正识别文字"), edit, save); preview.append(editor);
    }
    materialViews.set(name, {preview, source, text});
    item.append(row, preview); $("material-list").append(item);
  }
}
function locateCitation(citation) {
  if (citation.source_id.startsWith("message:")) {
    const turn = [...$("conversation").querySelectorAll(".chat-user")].find(node => node.textContent.includes(citation.quote));
    if (turn) { turn.tabIndex = -1; turn.focus({preventScroll:true}); turn.scrollIntoView({block:"center"}); }
    return message(citation.device + "：" + citation.quote);
  }
  const view = materialViews.get(citation.device);
  const lines = view?.text.split(/\r?\n/);
  if (!view || lines[citation.line - 1] !== citation.quote) return message("当前材料已更新，请继续诊断以更新原文定位。", true);
  const screenshot = [...screenshots.values()].find(item => item.material_name === citation.device);
  if (screenshot) { showScreenshot(screenshot, citation); return; }
  const selected = element("mark", lines[citation.line - 1]); selected.tabIndex = -1;
  selected.setAttribute("aria-label", citation.device + " 第" + citation.line + "行：" + citation.quote);
  view.source.replaceChildren(document.createTextNode(lines.slice(0, citation.line - 1).join("\n") + (citation.line > 1 ? "\n" : "")), selected,
    document.createTextNode("\n" + lines.slice(citation.line).join("\n")));
  view.preview.open = true; selected.focus({preventScroll:true}); selected.scrollIntoView({block:"center", behavior:"instant"});
  message("已定位 " + citation.device + " 第" + citation.line + "行。");
}

function showScreenshot(screenshot, citation) {
  const dialog = $("screenshot-dialog"), svg = $("screenshot-svg"), highlight = $("screenshot-highlight");
  $("screenshot-title").textContent = screenshot.name;
  svg.setAttribute("viewBox", "0 0 " + screenshot.width + " " + screenshot.height);
  for (const [key, value] of Object.entries({href:screenshot.image_url, width:screenshot.width, height:screenshot.height})) $("screenshot-image").setAttribute(key, value);
  $("open-screenshot").href = screenshot.image_url;
  const region = citation && (screenshot.regions || []).find(item => item.line === citation.line && item.text === citation.quote);
  highlight.toggleAttribute("hidden", !region);
  if (region) {
    const [x1,y1,x2,y2] = region.box;
    for (const [key, value] of Object.entries({x:Math.max(0,x1-3), y:Math.max(0,y1-3), width:x2-x1+6, height:y2-y1+6})) highlight.setAttribute(key, value);
  }
  $("screenshot-location").textContent = citation ? "第" + citation.line + "行：" + citation.quote + (region ? "" : " · 文字已修正，可对照原图查看") : "点击诊断中的材料引用，可高亮图中对应文字。";
  if (!dialog.open) dialog.showModal();
}
async function loadDemo(repaired = false, final = false) {
  if (busy) return;
  if (!repaired && (sessionId || materials.size)) return message("点击“新的排查”后，可带入完整示例材料。");
  try {
    const goalsDemo = $("demo-case").value === "ospf_goals";
    const demo = await api("/api/demos/" + $("demo-case").value);
    if (measuredDemos.has($("demo-case").value)) {
      for (const [name,text] of Object.entries(demo.materials)) materials.set(name,text);
      evidenceKind = "user_submitted"; demoActive = true;
      $("problem").value = $("demo-case").value === "ospf_area_measured"
        ? "根据两阶段 FRRouting 记录，解释 R2 的区域改动怎样影响邻接、远端路由和双向 Ping。为什么直连 Ping 在故障阶段仍然成功？"
        : "比较这次实际实验中两种修复方案的效果：为什么Ping通了仍可能没有完成实验？推荐满足全部目标的方案，并解释配置改动。";
      renderMaterials(); await previewOperations(); setBusy(false);
      $("demo-status").textContent = "已载入 FRRouting 实测记录。右侧重新核对故障与修复结果，可展开查看依据。";
      return materialChanged("实验运行记录已载入，右侧可查看实际对照结果。");
    }
    if ($("demo-case").value === "operation_history") {
      for (const [name, text] of Object.entries(demo.materials)) materials.set(name, text);
      recordings.set("demo", {id:"demo", name:"合成终端演示录屏", video_url:"/api/demos/operation_history/video"});
      evidenceKind = "demo"; demoActive = true;
      $("problem").value = "R1 ping不通10.2.2.2，请回溯操作记录，找到关键错误步骤并说明怎样继续。";
      renderMaterials(); renderRecordingChoices(); await previewOperations(); setBusy(false);
      $("demo-status").textContent = "已带入操作记录和配套录屏。可点击时间点回放，再开始诊断。";
      return materialChanged("操作记录与配套录屏已带入，点击时间点即可回放。");
    }
    const stage = demo.stages[final ? 2 : repaired ? 1 : 0];
    for (const [name, text] of Object.entries(stage.materials)) materials.set(name, text);
    evidenceKind = "demo"; demoActive = true; renderMaterials();
    if (!repaired) $("problem").value = goalsDemo ? "按实验说明排查R1到10.3.3.3的访问，并核对OSPF要求。" : "PC1取得DHCP地址后，为什么访问不了10.30.0.10？";
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
  const names = {read_source:"读取材料", retrieve_reference:"检索参考资料", compare_materials:"比较材料改动", check_submitted_pings:"核对Ping结果", check_experiment_goals:"核对实验目标", plan_diagnostic_checks:"选择下一项检查", inspect_operation_timeline:"回溯操作与报错", submit_hypotheses:"整理分析与下一步"};
  for (const tool of tools) {
    names.compare_repair_candidates = "比较实际修复结果";
    const row = element("li");
    row.append(element("strong", names[tool.tool] || tool.tool), element("span", tool.status === "ok" ? "已完成" : "需要调整", "tag"));
    const details = element("details"); details.append(element("summary", "查看调用参数与结果"), element("pre", JSON.stringify({parameters:tool.arguments, result:tool.result}, null, 2)));
    row.append(details); $("tool-records").append(row);
  }
}
function renderProgress(result) {
  const completed = ["verified_from_observations", "resolved_by_user_report"].includes(result.status);
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
  setBusy(true);
  const errors = [];
  try {
    message("正在读取 " + files.length + " 份材料…");
    for (const file of files) {
      try {
      if (/\.(png|jpe?g|webp|mp4|webm|mov|mkv)$/i.test(file.name)) { await uploadRecording(file); continue; }
      if (!/\.(txt|cfg|conf|log|md|json)$/i.test(file.name)) throw new Error(file.name + "：请选择截图、文本文件或录屏。");
      if (file.size > 200000) throw new Error(file.name + " 较大，请保留与本次问题有关的部分后上传（单份200 KB以内）。");
      const bytes = await file.arrayBuffer();
      let text;
      try { text = new TextDecoder("utf-8", {fatal:true}).decode(bytes); }
      catch { text = new TextDecoder("gb18030").decode(bytes); }
      if (!text.trim()) throw new Error(file.name + " 是空文件，请添加有内容的材料。");
      const name = file.name.slice(0,120);
      if (materials.size >= 20 && !materials.has(name)) throw new Error("一次诊断最多添加20份材料。");
      materials.set(name, text); renderMaterials();
      } catch (error) { errors.push(file.name + "：" + error.message); }
    }
    evidenceKind = demoActive && !measuredDemos.has($("demo-case").value) ? "demo" : "user_submitted"; renderMaterials();
    if (errors.length) { requestState("error", errors.join("；")); message("已保留成功读取的材料。" + errors.join("；"), true); }
    else materialChanged("材料已就绪，共 " + materials.size + " 份。点击“" + (sessionId ? "继续诊断" : "开始诊断") + "”提交分析。");
    await previewOperations();
  } catch (error) { message(error.message, true); }
  finally { setBusy(false); }
}
function requestPayload() {
  if (!materials.size) throw new Error("请先添加或粘贴需要诊断的材料。");
  return {diagnostic_mode:"open_network", use_llm:true, question:$("problem").value.trim() || defaultQuestion,
    configs:Object.fromEntries(materials), knowledge_scope:knowledgeScope, evidence_kind:evidenceKind,
    recordings:[...recordings.keys(), ...screenshots.keys()].filter(id => id !== "demo"),
    ...(sessionId ? {session_id:sessionId} : {})};
}

async function previewOperations() {
  const number = ++previewNumber;
  if (!materials.size) { renderRepairComparisons([]); return renderOperationHistory({}); }
  try {
    const input = {materials:Object.fromEntries(materials)};
    const [history, repairs] = await Promise.all([api("/api/operations/inspect",input),api("/api/repair-labs/inspect",input)]);
    if (number === previewNumber) { renderOperationHistory(history); renderRepairComparisons(repairs.comparisons); }
  } catch (error) { message(error.message, true); }
}

function renderRepairComparisons(comparisons) {
  $("repair-section").hidden = !comparisons?.length;
  $("empty-result").hidden = Boolean(comparisons?.length) || Boolean(sessionId);
  const container = $("repair-comparisons"); container.replaceChildren();
  for (const comparison of comparisons || []) {
    if (comparison.error) { container.append(element("p",comparison.material + "：" + comparison.error,"message error")); continue; }
    const source = comparison.environment.image || [comparison.environment.engine, comparison.environment.platform].filter(Boolean).join(" · ");
    container.append(element("h4",comparison.title),element("p",source || "FRRouting 实际运行记录","section-note"));
    const grid = element("div",undefined,"repair-grid");
    for (const phase of comparison.phases) {
      const card = element("article",undefined,"repair-card " + (phase.status === "passed" ? "passed" : "incomplete"));
      card.append(element("h4",phase.title),element("strong",phase.passed_count + " / " + phase.total + " 项目标满足","repair-score"));
      for (const check of phase.checks) {
        const row = element("div",undefined,"repair-check " + (check.passed ? "passed" : "failed"));
        row.append(element("strong",(check.passed ? "✓ " : "○ ") + check.title),element("p",check.detail)); card.append(row);
      }
      if (Object.keys(phase.commands).length) {
        const commands = element("details"); commands.append(element("summary","查看执行的修改命令"),element("pre",Object.entries(phase.commands).map(([device,lines])=>device + "\n" + lines.join("\n")).join("\n\n"))); card.append(commands);
      }
      const raw = element("details"); raw.append(element("summary","查看实际检查依据"));
      for (const check of phase.checks) raw.append(element("p",check.title),element("pre",check.evidence)); card.append(raw);
      if (phase.changes.length) {
        const diffs = element("details"); diffs.append(element("summary","查看配置改动"));
        for (const change of phase.changes) diffs.append(element("pre",change.diff)); card.append(diffs);
      }
      grid.append(card);
    }
    container.append(grid);
    const recommended = comparison.phases.find(phase=>phase.id === comparison.recommended);
    if (recommended) container.append(element("p","满足全部实验目标：" + recommended.title,"repair-recommendation"));
    const action = comparison.environment.action_url;
    if (/^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/actions\/runs\/\d+$/.test(action || "")) {
      const link = element("a","查看这次实际运行 ↗","text-button"); link.href = action; link.target = "_blank"; link.rel = "noopener"; container.append(link);
    }
  }
}
function renderRecordingChoices() {
  const select = $("replay-recording"), selected = select.value;
  select.replaceChildren();
  for (const [id, recording] of recordings) {
    const option = element("option", recording.name); option.value = id; select.append(option);
  }
  if (recordings.has(selected)) select.value = selected;
  $("recording-label").hidden = select.hidden = !recordings.size;
  $("replay-video").hidden = $("recording-note").hidden = !recordings.size;
  const recording = recordings.get(select.value);
  if (recording && $("replay-video").getAttribute("src") !== recording.video_url) $("replay-video").src = recording.video_url;
  if (!recording) { $("replay-video").removeAttribute("src"); $("replay-video").load(); }
  $("process-section").hidden = !recordings.size && !operationHistory?.event_count;
}
function locateOperation(event) {
  const recording = [...recordings.values()].find(item => item.material_name === event.material);
  if (recording) { $("replay-recording").value = recording.id; renderRecordingChoices(); }
  const video = $("replay-video");
  if (event.seconds !== null && recordings.size) {
    const seek = () => {
      if (Number.isFinite(video.duration) && event.seconds > video.duration) return message("这个时间点超出当前录屏长度，请选择对应录屏。", true);
      video.currentTime = event.seconds; video.pause();
      video.scrollIntoView({block:"center"});
      message("已跳到 " + event.time_label + " 的画面。");
    };
    if (video.readyState >= 1) seek(); else video.addEventListener("loadedmetadata", seek, {once:true});
  } else locateCitation(event.evidence[0]);
}
function renderOperationHistory(history) {
  operationHistory = history;
  $("process-section").hidden = !history.event_count && !recordings.size;
  $("operation-count").textContent = history.event_count ? history.event_count + " 步 · " + history.error_count + " 处报错" : "正在准备操作时间线";
  const focus = (history.events || []).find(item => item.id === (history.focus_id || history.first_issue_id));
  const container = $("operation-focus"); container.replaceChildren(); container.hidden = !focus;
  if (focus) {
    container.append(element("span", focus.recovered ? "报错后已重新输入" : "关键操作", "eyebrow"),
      element("h4", (focus.seconds === null ? "第" + focus.line + "行" : focus.time_label) + " · " + focus.device + " · " + focus.view_label),
      element("pre", focus.command), element("p", focus.explanation || "结合当前配置与实验目标核对这一步的影响。"));
    if (focus.error) container.append(element("p", focus.error, "operation-error-text"));
    const jump = element("button", recordings.size && focus.seconds !== null ? "查看这个时间点 ↗" : "定位操作原文 ↗", "secondary-button"); jump.type = "button";
    jump.addEventListener("click", () => locateOperation(focus));
    const ask = element("button", "分析这一步", "text-button operation-analyze"); ask.type = "button";
    ask.addEventListener("click", () => {
      const text = "请分析" + focus.material + "中" + (focus.seconds === null ? "第" + focus.line + "行" : focus.time_label) + "、" + focus.device + "执行“" + focus.command + "”这一步的影响，结合后续操作和当前配置给出继续步骤。";
      if (sessionId) performInteraction("ask", text);
      else { $("problem").value = text; $("analyze").click(); }
    });
    container.append(jump, ask);
  }
  $("operation-timeline").replaceChildren();
  for (const event of history.events || []) {
    const row = element("li", undefined, "operation-event " + event.status + (event.recovered ? " recovered" : ""));
    const jump = element("button", event.seconds === null ? "第" + event.line + "行" : event.time_label, "text-button operation-time"); jump.type = "button";
    jump.addEventListener("click", () => locateOperation(event));
    row.append(jump, element("strong", event.device + " · " + event.view_label), element("span", event.recovered ? "后续已重试" : {error:"命令报错",returned:"终端已返回",issued:"命令已输入"}[event.status], "tag"), element("pre", event.command));
    if (event.error) row.append(element("p", event.error, "operation-error-text"));
    $("operation-timeline").append(row);
  }
  $("operation-details").hidden = !history.event_count;
}
async function waitForRecording(id) {
  const url = new URL(window.location.href); url.searchParams.set("recording", id); window.history.replaceState(null, "", url);
  while (true) {
    const status = await api("/api/recordings/" + id);
    requestState("pending", status.message + (status.progress ? " · " + status.progress + "%" : ""));
    if (status.status === "error") throw new Error(status.message);
    if (status.status === "ready") {
      const collection = status.kind === "image" ? screenshots : recordings;
      if (materials.size >= 20 && !materials.has(status.material_name) && ![...collection.values()].some(item => item.name === status.name)) throw new Error("材料已达20份，请移除部分材料后重新添加。");
      for (const [oldId, old] of collection) if (old.name === status.name && oldId !== id) { materials.delete(old.material_name); collection.delete(oldId); }
      collection.set(id, status); materials.set(status.material_name, status.transcript);
      renderMaterials(); renderRecordingChoices(); await previewOperations();
      requestState("success", status.kind === "image" ? "截图已识别，可查看原图和修正文字。" : "录屏文字已提取，操作时间线已更新。");
      const clean = new URL(window.location.href); clean.searchParams.delete("recording"); window.history.replaceState(null, "", clean);
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 1500));
  }
}
async function uploadRecording(file) {
  const image = /\.(png|jpe?g|webp)$/i.test(file.name), label = image ? "截图" : "录屏";
  if (!videoReady) throw new Error("请先安装requirements-replay.txt中的文字识别组件。");
  if (file.size > (image ? 20 : 80) * 1024 * 1024) throw new Error("单份" + label + "请保持在" + (image ? "20" : "80") + "MB以内。");
  const collection = image ? screenshots : recordings;
  if (collection.size >= (image ? 10 : 5) && ![...collection.values()].some(item => item.name === file.name)) throw new Error("一次排查最多添加10份截图、5份录屏。");
  if (materials.size >= 20 && ![...collection.values()].some(item => item.name === file.name)) throw new Error("一次诊断最多添加20份材料。");
  requestState("pending", "正在上传" + label + "…");
  try {
    const status = await new Promise((resolve, reject) => {
      const request = new XMLHttpRequest(); request.open("POST", "/api/recordings"); request.timeout = 180000;
      request.setRequestHeader("Content-Type", file.type || "application/octet-stream"); request.setRequestHeader("X-Recording-Name", encodeURIComponent(file.name));
      request.upload.addEventListener("progress", event => { if (event.lengthComputable) requestState("pending", "正在上传" + label + " · " + Math.round(event.loaded / event.total * 100) + "%"); });
      request.addEventListener("load", () => { try { const data = JSON.parse(request.responseText); request.status < 300 ? resolve(data) : reject(new Error(data.error || label + "上传失败")); } catch { reject(new Error(label + "上传未完成，请重试。")); } });
      request.addEventListener("error", () => reject(new Error(label + "连接中断，请重新上传。")));
      request.addEventListener("timeout", () => reject(new Error(label + "上传超时，请裁剪相关区域后上传。")));
      request.send(file);
    });
    await waitForRecording(status.id);
  } catch (error) { requestState("error", error.message); throw error; }
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
  requestFailed = false;
  activeRequest = {type, text, started:Date.now()}; setBusy(true);
  const status = {analyze:"材料已提交，正在生成诊断…", ask:"追问已发送，正在生成回复…", repair:"请求已提交，正在整理修改步骤…", update:"补充材料已提交，正在继续分析…", verify:"复测材料已提交，正在比较改动与测试结果…"}[type];
  requestState("pending", status); message(status);
  $("processing-events").replaceChildren(); $("processing-progress").hidden = false; $("processing-progress").open = true;
  if (type === "analyze" && !sessionId) {
    $("empty-result").querySelector("h3").textContent = "正在分析材料…";
    $("empty-result").querySelector("p").textContent = "正在读取材料并整理问题原因，结果会显示在这里。";
  }
  $("request-elapsed").textContent = "已等待 0 秒";
  requestTimer = setInterval(() => { $("request-elapsed").textContent = "已等待 " + Math.floor((Date.now() - activeRequest.started) / 1000) + " 秒"; }, 1000);
  if (type !== "analyze") pendingTurn(text);
}
function finishRequest(state, text) {
  requestFailed = state === "error";
  requestState(state, text); setBusy(false); message(text, state === "error");
  $("processing-progress").open = false;
  if (state === "error" && activeRequest?.type !== "analyze") {
    if (!$("conversation").querySelector(".chat-transient")) pendingTurn(activeRequest.text);
    const response = $("conversation").querySelector(".chat-transient .chat-assistant");
    response.className = "chat-message chat-assistant chat-failed";
    response.querySelector(".pending-text").textContent = text;
    const failed = {...activeRequest};
    const retry = element("button", "重试这次请求", "text-button chat-retry"); retry.type = "button";
    retry.addEventListener("click", () => performInteraction(failed.type, failed.text)); response.append(retry);
    if (failed.type === "ask" && !$("followup-message").value.trim()) $("followup-message").value = failed.text;
    $("conversation").scrollTop = $("conversation").scrollHeight;
  }
}
async function performInteraction(type, text) {
  if (busy) return;
  try {
    const input = requestPayload(); input.interaction = {type, message:text};
    beginRequest(type, text);
    if (type === "ask" && $("followup-message").value.trim() === text) $("followup-message").value = "";
    const result = await diagnose(input);
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
  const container = $("verification-checks"); container.replaceChildren(); container.hidden = !checks.compare_materials && !checks.check_submitted_pings?.pings?.length;
  if (checks.compare_materials) {
    const comparison = checks.compare_materials;
    container.append(element("strong", comparison.has_previous ? (comparison.changed ? "已对比本轮材料更新" : "当前材料与上一轮相同") : "已读取当前材料"));
    for (const change of comparison.changes) container.append(element("p", change.material + " · " + ({added:"新增",updated:"已更新",removed:"已移除"}[change.change] || change.change)));
  }
  if (checks.check_submitted_pings?.pings?.length) {
    const pings = checks.check_submitted_pings.pings;
    container.append(element("p", pings.length ? "已核对上传材料中的 " + pings.length + " 条 Ping 记录：" : "请补充包含收发统计的 Ping 输出。"));
    for (const ping of pings) container.append(element("p", ping.device + " → " + ping.target + "：" + ping.sent + " 发 " + ping.received + " 收 · " + (ping.passed ? "该条观测通过" : "该条观测未通过"), ping.passed ? "check-pass" : "check-fail"));
  }
}
function renderGoalsAndPlan(analysis, status) {
  const checks = analysis.goal_assessment?.checks || [];
  $("goals-section").hidden = !checks.length;
  $("goal-checks").replaceChildren();
  $("goals-summary").textContent = "已满足 " + checks.filter(check => check.status === "passed").length + " / " + checks.length + " 项 · 按实验说明逐项检查";
  for (const check of checks) {
    const row = element("div", undefined, "goal-check " + check.status);
    row.append(element("strong", check.title), element("span", {passed:"已满足", failed:"未满足", needs_evidence:"待补充输出"}[check.status], "tag"), element("p", check.detail));
    const details = element("details"); details.append(element("summary", "查看实验要求与检查依据"));
    for (const [label, citation] of [["实验要求", check.instruction], ...check.evidence.map(item => ["检查依据", item])]) {
      const locate = element("button", label + " · " + citation.device + " 第" + citation.line + "行 ↗", "text-button"); locate.type = "button";
      locate.addEventListener("click", () => locateCitation(citation));
      details.append(locate, element("pre", citation.quote));
    }
    row.append(details); $("goal-checks").append(row);
  }
  const plan = analysis.diagnostic_plan;
  const selected = plan?.selected;
  $("plan-section").hidden = !selected || ["resolved_by_user_report", "verified_from_observations"].includes(status);
  $("diagnostic-plan").replaceChildren();
  if (!selected) return;
  const container = $("diagnostic-plan");
  container.append(element("strong", selected.device), element("pre", selected.command), element("p", selected.purpose), element("p", plan.reason, "section-note"));
  for (const hypothesis of plan.hypotheses) {
    container.append(element("p", {active:"待检查", supported:"已有依据", ruled_out:"已排除"}[hypothesis.state] + " · " + hypothesis.title, "plan-hypothesis " + hypothesis.state));
  }
  const details = element("details"); details.append(element("summary", "比较候选检查与预期结果"));
  for (const test of plan.tests) {
    const row = element("div", undefined, "plan-alternative");
    row.append(element("strong", test.device + " · " + test.command), element("p", "可区分 " + test.pair_count + " 组原因 · 采集成本 " + test.cost));
    for (const prediction of test.predictions) {
      const hypothesis = plan.hypotheses.find(item => item.id === prediction.hypothesis_id);
      row.append(element("p", (hypothesis?.title || prediction.hypothesis_id) + " → 预期 " + prediction.result));
    }
    details.append(row);
  }
  container.append(details);
}
function showResult(result) {
  sessionId = result.session_id;
  window.history.replaceState(null, "", "/?session=" + sessionId);
  $("empty-result").hidden = true; $("analysis-result").hidden = false; $("save-report").hidden = false;
  const analysis = result.investigation || {};
  renderOperationHistory(result.operation_history || {});
  renderRepairComparisons(result.repair_comparisons || []);
  const resolution = $("resolution-status"); resolution.replaceChildren();
  resolution.hidden = !["resolved_by_user_report", "verified_from_observations"].includes(result.status);
  if (!resolution.hidden) {
    resolution.append(element("strong", result.status === "resolved_by_user_report" ? "用户反馈已恢复" : "测试输出核验通过"));
    resolution.append(element("p", analysis.experiment_goal || "本次实验目标"));
    for (const evidence of analysis.verification?.evidence || []) resolution.append(element("p", "恢复反馈：" + evidence.quote));
  }
  renderProgress(result); renderTools(result); renderGoalsAndPlan(analysis, result.status);
  const ok = result.llm?.status === "ok";
  const intent = result.conversation?.at(-1)?.intent;
  $("result-state").textContent = ok ? (result.status === "verified_from_observations" && intent !== "ask" ? "本轮测试通过 · 排查完成" : {ask:"本次回复", repair:"修改步骤", verify:"本轮材料核对", update:"补充材料后的进展"}[intent] || "本次排查发现") : "本次分析尚未完成";
  if (ok && result.status === "comparison_complete") $("result-state").textContent = "候选修复对照完成";
  if (result.status === "comparison_complete") $("diagnosis-progress").hidden = true;
  else $("diagnosis-progress").hidden = false;
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
      materials.set(name, input.value); evidenceKind = demoActive && !measuredDemos.has($("demo-case").value) ? "demo" : "user_submitted"; renderMaterials();
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
$("load-demo-final").addEventListener("click", () => loadDemo(true, true));
$("replay-recording").addEventListener("change", renderRecordingChoices);
$("demo-case").addEventListener("change", () => setBusy(false));
$("choose-files").addEventListener("click", () => $("material-files").click());
$("material-files").addEventListener("change", async event => { await addFiles(event.target.files); event.target.value = ""; });
document.addEventListener("paste", async event => {
  const images = [...(event.clipboardData?.items || [])].filter(item => item.kind === "file" && item.type.startsWith("image/"));
  if (!images.length) return;
  event.preventDefault();
  if (busy) return message("正在处理当前请求，完成后可继续粘贴截图。");
  const files = images.map((item, index) => {
    const file = item.getAsFile();
    if (!file) return null;
    const suffix = {"image/png":"png", "image/jpeg":"jpg", "image/webp":"webp"}[file.type];
    return new File([file], "粘贴截图-" + Date.now() + "-" + index + "." + (suffix || "unsupported"), {type:file.type});
  }).filter(Boolean);
  await addFiles(files);
});
$("close-screenshot").addEventListener("click", () => $("screenshot-dialog").close());
for (const type of ["dragenter", "dragover"]) $("drop-zone").addEventListener(type, event => { event.preventDefault(); if (!busy) $("drop-zone").classList.add("dragging"); });
$("drop-zone").addEventListener("dragleave", () => $("drop-zone").classList.remove("dragging"));
$("drop-zone").addEventListener("drop", async event => { event.preventDefault(); $("drop-zone").classList.remove("dragging"); await addFiles(event.dataTransfer.files); });
$("paste-toggle").addEventListener("click", () => { $("paste-panel").hidden = !$("paste-panel").hidden; $("paste-toggle").setAttribute("aria-expanded", String(!$("paste-panel").hidden)); if (!$("paste-panel").hidden) $("pasted-material").focus(); });
$("add-pasted").addEventListener("click", () => {
  const text = $("pasted-material").value;
  if (!text.trim()) return message("请先粘贴材料内容。", true);
  if (materials.size >= 20 || text.length > 200000) return message("请保留与本次问题相关的材料，每份200 KB以内，最多20份。", true);
  let name; do { name = "粘贴材料" + (++pasteNumber) + ".txt"; } while (materials.has(name));
  materials.set(name, text); evidenceKind = demoActive && !measuredDemos.has($("demo-case").value) ? "demo" : "user_submitted"; $("pasted-material").value = ""; $("paste-panel").hidden = true; $("paste-toggle").setAttribute("aria-expanded", "false"); renderMaterials(); materialChanged(); previewOperations();
});
$("analyze").addEventListener("click", async () => {
  if (busy) return;
  try {
    const input = requestPayload(); beginRequest("analyze", "已提交材料，开始诊断。");
    const result = await diagnose(input); showResult(result);
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
  sessionProblem = false; requestFailed = false;
  materials.clear(); sessionId = null; evidenceKind = "user_submitted"; knowledgeScope = "default"; demoActive = false;
  recordings.clear(); screenshots.clear(); renderRecordingChoices(); renderOperationHistory({}); renderRepairComparisons([]);
  $("demo-case").value = "network_access";
  $("demo-status").textContent = "先带入故障材料，再点击“开始诊断”。";
  $("problem").value = ""; $("pasted-material").value = ""; $("paste-panel").hidden = true; $("paste-toggle").setAttribute("aria-expanded", "false");
  $("followup-message").value = ""; $("conversation").replaceChildren();
  $("analysis-result").hidden = true; $("empty-result").hidden = false; $("save-report").hidden = true;
  $("empty-result").querySelector("h3").textContent = "准备好材料，就可以开始。";
  $("empty-result").querySelector("p").textContent = "这里会显示问题原因、材料依据和下一步操作。";
  $("request-status").hidden = true;
  $("processing-progress").hidden = true; $("processing-events").replaceChildren();
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
async function connectService() {
  const wakeup = setTimeout(() => { serviceState = "starting"; serviceStatus(); message("服务正在启动，连接成功后即可诊断。"); }, 4000);
  try {
    for (let attempt = 0; attempt < 3; attempt++) {
      try { return await api("/api/health", undefined, {signal:AbortSignal.timeout(30000)}); }
      catch (error) {
        if (attempt === 2 || (error.status && ![502,503,504].includes(error.status))) throw error;
        serviceState = "starting"; serviceStatus(); message("服务正在启动，正在重新连接…");
        await new Promise(resolve => setTimeout(resolve, 1500));
      }
    }
  } finally { clearTimeout(wakeup); }
}
async function start() {
  const health = await connectService(); serviceState = "ready"; modelReady = health.llm_configured; online = health.online; videoReady = health.replay_available; setBusy(false);
  if (online) {
    $("save-report").textContent = "下载实验报告";
    document.querySelector("footer span:last-child").textContent = "测试过程保存在当前在线服务";
  }
  const savedSession = new URLSearchParams(window.location.search).get("session");
  if (savedSession && /^[a-f0-9]{32}$/.test(savedSession)) {
    let saved;
    try { saved = await api("/api/sessions/" + savedSession); }
    catch (error) {
      if (error.status !== 404) throw error;
      sessionProblem = true; setBusy(false);
      $("empty-result").querySelector("h3").textContent = "这次会话已失效";
      $("empty-result").querySelector("p").textContent = "服务已连接。点击“新的排查”，重新提供材料后即可继续。";
      return message("这次会话已不存在。请点击“新的排查”并重新提供材料。", true);
    }
    const latest = saved.attempts[saved.attempts.length - 1];
    for (const [name, text] of Object.entries(latest.input.configs)) materials.set(name, text);
    for (const [index, output] of (latest.input.command_outputs || []).entries()) materials.set((output.device + "-输出" + (index + 1) + ".log").slice(0, 120), output.command + "\n" + output.text);
    $("problem").value = latest.input.question === defaultQuestion ? "" : latest.input.question || "";
    knowledgeScope = latest.input.knowledge_scope || "default"; evidenceKind = latest.input.evidence_kind || "user_submitted";
    for (const id of latest.input.recordings || []) {
      try { const media = await api("/api/recordings/" + id); (media.kind === "image" ? screenshots : recordings).set(id, media); } catch (error) { message(error.message, true); }
    }
    renderRecordingChoices();
    const measuredDemo = [...measuredDemos].find(([, name]) => materials.has(name));
    demoActive = Boolean(measuredDemo) || evidenceKind === "demo" && (materials.has("终端跨网段访问.txt") || materials.has("实验说明.txt") || materials.has("终端操作.log"));
    if (measuredDemo) $("demo-case").value = measuredDemo[0];
    if (demoActive && materials.has("终端操作.log")) {
      $("demo-case").value = "operation_history";
      recordings.set("demo", {id:"demo", name:"合成终端演示录屏", video_url:"/api/demos/operation_history/video"}); renderRecordingChoices();
    }
    if (demoActive && materials.has("实验说明.txt")) $("demo-case").value = "ospf_goals";
    renderMaterials();
    if (latest.result.mode === "open_network") { showResult({...latest.result, session_id:savedSession}); setBusy(false); message("已恢复这次诊断，可以继续交流或补充材料。"); }
    else { message("已带入保存的材料，可以开始诊断。"); }
  }
  const pendingRecording = new URLSearchParams(window.location.search).get("recording");
  if (pendingRecording && /^[a-f0-9]{32}$/.test(pendingRecording)) {
    setBusy(true);
    try { await waitForRecording(pendingRecording); message("材料识别已恢复，可以继续诊断。"); }
    catch (error) { requestState("error", error.message); message(error.message, true); }
    finally { setBusy(false); }
  }
  if (!modelReady) message("请由项目维护者先配置模型服务，配置完成后刷新页面即可。", true);
}
start().catch(error => { serviceState = "unavailable"; modelReady = false; setBusy(false); message("暂时无法连接服务：" + error.message, true); });
