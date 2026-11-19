"use strict";
window.LabWorkspace = (() => {
  let c, room = null, base = null, memberId, actor = "", pollTimer, polling = false;
  let network = null, selectedSource = "", target = "", previewId = 0, selectedNode = "", seenResult = "";
  let conflict = null, pending = false, challenge = null;
  let roomEpoch = 0;
  let view = "diagnosis";
  const $ = id => document.getElementById(id);
  const el = (tag, text, cls) => c.element(tag, text, cls);
  const storage = (key, value) => { try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch {} };
  const tabStorage = (key, value) => { try { if (value === undefined) return sessionStorage.getItem(key); sessionStorage.setItem(key, value); } catch {} };
  const button = (text, action, cls = "secondary-button") => { const b = el("button", text, cls); b.type = "button"; b.addEventListener("click", () => run(action)); return b; };
  const field = (placeholder, value = "") => { const i = el("input"); i.placeholder = placeholder; i.value = value; i.setAttribute("aria-label", placeholder); return i; };
  async function run(action) {
    if (pending || c.busy()) return c.message("当前请求完成后可继续操作。");
    pending = true;
    $("collaboration").setAttribute("aria-busy", "true");
    $("room-status").textContent = "正在提交协作操作…";
    try { await action(); }
    catch (error) { renderRoom(); $("room-status").textContent = "操作未完成 · " + error.message; c.message(error.message, true); }
    finally { pending = false; $("collaboration").setAttribute("aria-busy", "false"); }
  }
  function getActor() {
    actor = $("room-actor").value.trim();
    if (!actor) throw new Error("先填写你的姓名，队友就能看到材料来自谁。");
    storage("labagent-actor", actor); tabStorage("labagent-actor",actor); return actor;
  }
  function patches() {
    if (!base) return [];
    const result = [];
    for (const name of new Set([...Object.keys(base.materials), ...c.materials.keys()])) {
      const old = base.materials[name], text = c.materials.get(name);
      if (text !== old?.content) result.push({name, content:text ?? null, base_revision:base.versions?.[name] || old?.revision || 0});
    }
    return result;
  }
  function dirty() { return Boolean(base && (patches().length || c.question() !== base.question)); }
  function changed() {
    if (room && $("room-status")) $("room-status").textContent = dirty() ? "本地有新材料 · 点击同步给队友" : "已同步 · 第" + room.revision + "版材料";
  }
  function showView(next) {
    view = next;
    $("diagnosis-workspace").hidden = view !== "diagnosis";
    $("network-map").hidden = view !== "map";
    $("collaboration").hidden = view !== "team";
    for (const b of document.querySelectorAll("[data-workspace]")) {
      if (b.dataset.workspace === view) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
    }
  }
  function showResultPanel(next) {
    const evidence = next === "evidence";
    $("evidence-panel").hidden = !evidence; $("diagnosis-panel").hidden = evidence;
    $("evidence-tab").setAttribute("aria-selected",String(evidence)); $("diagnosis-tab").setAttribute("aria-selected",String(!evidence));
  }
  function resultsAvailable(focus = false) {
    const count = Number(!$("repair-section").hidden) + Number(!$("process-section").hidden);
    $("evidence-count").textContent = count; $("evidence-count").hidden = !count; $("empty-evidence").hidden = Boolean(count);
    if (focus) showResultPanel("evidence");
  }
  function showDiagnosis() { showView("diagnosis"); showResultPanel("diagnosis"); }
  function sessionUrl(id) { return "/?" + (room ? "room=" + room.id + "&" : "") + "session=" + id; }
  function resultKey(value) { return value.session ? [value.session.session_id, value.session.attempts, value.session.revision, value.revision].join(":") : ""; }
  async function apply(value, restore = true) {
    const epoch = roomEpoch;
    room = value; base = structuredClone(value);
    // Establish the invite context before restoring shared sessions and media.
    history.replaceState(null, "", room.session ? sessionUrl(room.session.session_id) : "/?room=" + room.id);
    c.apply(room); renderRoom();
    const isCurrent = () => epoch === roomEpoch && room?.id === value.id && room.revision === value.revision;
    if (restore && (resultKey(room) !== seenResult || !room.session)) {
      await c.restore(room, isCurrent);
      if (!isCurrent()) return;
      seenResult = resultKey(room);
    }
    history.replaceState(null, "", room.session ? sessionUrl(room.session.session_id) : "/?room=" + room.id);
  }
  async function update(body) { return c.api("/api/rooms/" + room.id, {actor:getActor(), member_id:memberId, ...body}); }
  async function publish() {
    if (!room) return;
    const edits = patches();
    try {
      const next = await update({action:"materials", patches:edits, ...(c.question() !== base.question ? {question:c.question(), base_question:base.question} : {}),
        recordings:[...c.recordings.keys(), ...c.screenshots.keys()].filter(id => id !== "demo")});
      await apply(next, !c.busy());
      if (!c.busy()) c.message("材料已同步给队友，共" + Object.keys(next.materials).length + "份。");
    } catch (error) {
      if (error.code === "room_conflict") {
        conflict = {latest:await c.api("/api/rooms/" + room.id), edits, question:c.question(), oldQuestion:base.question};
        renderConflict();
      }
      throw error;
    }
  }
  function renderConflict() {
    const panel = $("room-conflict"); panel.hidden = !conflict; panel.replaceChildren();
    if (!conflict) return;
    panel.append(el("strong", "同一份材料有两份修改，请选择合并方式"));
    for (const edit of conflict.edits) {
      const latest = conflict.latest.materials[edit.name];
      if ((conflict.latest.versions?.[edit.name] || latest?.revision || 0) === edit.base_revision) continue;
      const detail = el("details"); detail.append(el("summary", edit.name));
      detail.append(el("p", "我的修改"), el("pre", edit.content ?? "已移除"), el("p", "队友的版本 · " + (latest?.author || "已移除")), el("pre", latest?.content ?? "已移除")); panel.append(detail);
    }
    if (conflict.question !== conflict.latest.question && conflict.oldQuestion !== conflict.latest.question) panel.append(el("p", "实验问题也有更新：我的“" + conflict.question + "”；队友的“" + conflict.latest.question + "”。"));
    panel.append(button("采用队友的冲突版本", () => resolve(false)), button("保留我的冲突修改", () => resolve(true)));
  }
  async function resolve(keepMine) {
    const saved = conflict; conflict = null;
    renderConflict();
    const remaining = saved.edits.filter(edit => keepMine || (saved.latest.versions?.[edit.name] || saved.latest.materials[edit.name]?.revision || 0) === edit.base_revision);
    await apply(saved.latest, false);
    for (const edit of remaining) { if (edit.content === null) c.materials.delete(edit.name); else c.materials.set(edit.name, edit.content); }
    if (keepMine || saved.latest.question === saved.oldQuestion) $("problem").value = saved.question;
    c.apply(room, false); changed();
    await publish();
  }
  function renderRoom() {
    const panel = $("room-body");
    const taskDraft = $("room-task-title")?.value || "", assigneeDraft = $("room-task-assignee")?.value || "";
    const taskOpen = $("room-tasks")?.open, activityOpen = $("room-activity")?.open, sourcesOpen = $("room-sources")?.open;
    panel.replaceChildren();
    $("room-title").textContent = room ? room.name : "协作排障室";
    $("create-room").hidden = Boolean(room); $("room-name").hidden = Boolean(room);
    $("room-status").textContent = room ? "已同步 · 第" + room.revision + "版材料" : "一人建实验，队友通过邀请链接补充材料";
    $("room-summary").hidden = !room; $("room-summary").replaceChildren();
    $("team-count").hidden = !room;
    if (room) {
      const pendingTasks = room.tasks.filter(t => !t.done).length;
      $("team-count").textContent = pendingTasks || new Set(Object.values(room.members).map(m => m.name)).size;
      $("room-summary").append(el("span", "◎ " + room.name + " · 第" + room.revision + "版共享材料"),button("查看协作进度" + (pendingTasks ? " · " + pendingTasks + "项待检查" : ""),() => showView("team"),"text-button"));
    }
    if (!room) return;
    const toolbar = el("div", undefined, "room-toolbar");
    toolbar.append(button("同步我的材料", publish), button("载入队友更新", async () => { if (dirty()) await publish(); else await apply(await c.api("/api/rooms/" + room.id)); c.message("已载入队友的最新材料。"); }));
    const invite = field("邀请链接", location.origin + "/?room=" + room.id); invite.readOnly = true; invite.id = "room-invite";
    toolbar.append(invite, button("复制邀请链接", async () => { try { await navigator.clipboard.writeText(invite.value); c.message("邀请链接已复制，发给两个队友即可。"); } catch { invite.select(); c.message("选中邀请链接后，按Ctrl+C复制。"); } }));
    panel.append(toolbar);
    const names = [...new Set(Object.values(room.members).map(m => m.name))];
    panel.append(el("p", "参与成员：" + (names.join("、") || "填写姓名后加入") + " · " + Object.keys(room.materials).length + "份共享材料", "section-note"));
    const taskBox = el("details"); taskBox.id = "room-tasks"; taskBox.open = taskOpen ?? room.tasks.some(t => !t.done); taskBox.append(el("summary", "分工与接力 · " + room.tasks.filter(t => !t.done).length + "项待检查"));
    const form = el("form", undefined, "room-task-form");
    const title = field("需要检查什么，例如R2的OSPF邻居",taskDraft); title.id = "room-task-title"; title.maxLength = 200;
    const assignee = field("交给谁",assigneeDraft); assignee.id = "room-task-assignee"; assignee.maxLength = 40;
    const submit = el("button", "分发检查", "secondary-button"); submit.type = "submit";
    form.append(title, assignee, submit);
    form.addEventListener("submit", event => { event.preventDefault(); run(async () => { const next = await update({action:"task", title:title.value, assignee:assignee.value}); title.value = ""; assignee.value = ""; room = next; renderRoom(); c.message("检查任务已分发。"); }); });
    taskBox.append(form);
    for (const task of room.tasks) {
      const line = el("label", undefined, "room-task"); const check = el("input"); check.type = "checkbox"; check.checked = task.done;
      check.addEventListener("change", () => run(async () => { room = await update({action:"complete", task_id:task.id}); renderRoom(); }));
      line.append(check, el("span", task.assignee + " · " + task.title + (task.done ? " · 已完成" : ""))); taskBox.append(line);
    }
    panel.append(taskBox);
    const activity = el("details"); activity.id = "room-activity"; activity.open = activityOpen || false; activity.append(el("summary", "共同排查记录"));
    for (const event of room.activity.slice(-10).reverse()) activity.append(el("p", event.actor + " · " + event.text + " · " + new Date(event.at).toLocaleTimeString("zh-CN"), "room-event"));
    panel.append(activity);
    const attribution = el("details"); attribution.id = "room-sources"; attribution.open = sourcesOpen || false; attribution.append(el("summary", "材料来源与版本"));
    for (const [name, entry] of Object.entries(room.materials)) attribution.append(el("p", name + " · " + entry.author + " · 第" + entry.revision + "版", "room-event"));
    panel.append(attribution);
  }
  async function poll() {
    if (!room || polling || pending || c.busy() || document.hidden || conflict) return;
    polling = true;
    const epoch = roomEpoch, id = room.id;
    try {
      const latest = await c.api("/api/rooms/" + id);
      if (epoch !== roomEpoch || room?.id !== id) return;
      if (dirty()) {
        if (latest.revision !== base.revision) $("room-status").textContent = "队友有更新 · 你的草稿已保留，点击同步合并";
      } else if (latest.revision !== base.revision || resultKey(latest) !== seenResult) await apply(latest);
      else if (JSON.stringify([latest.tasks,latest.activity,latest.members]) !== JSON.stringify([room.tasks,room.activity,room.members])) { room = latest; renderRoom(); }
    } catch (error) { if (epoch === roomEpoch && room?.id === id) $("room-status").textContent = "同步暂时中断 · " + error.message; }
    finally { polling = false; }
  }
  function svg(tag, attrs) { const node = document.createElementNS("http://www.w3.org/2000/svg", tag); for (const [key,value] of Object.entries(attrs || {})) node.setAttribute(key,value); return node; }
  function refs(container, items) {
    for (const ref of items) { const b = button(ref.device + " · 第" + ref.line + "行 ↗", () => { showView("diagnosis"); c.locate(ref); }, "text-button"); container.append(b, el("pre", ref.quote)); }
  }
  function detailNode(id) {
    selectedNode = id; const n = network.nodes.find(n => n.id === id); if (!n) return;
    const detail = $("map-detail"); detail.replaceChildren(el("h3", id));
    for (const i of n.interfaces) detail.append(el("p", i.name + " · " + i.address + "/" + i.prefix + (i.shutdown ? " · shutdown" : "")));
    if (n.gateway) detail.append(el("p", "默认网关 " + n.gateway.address));
    const evidence = el("details"); evidence.append(el("summary", "查看设备材料依据")); refs(evidence, n.evidence); detail.append(evidence);
    detail.append(button("从这里检查通信", () => { selectedSource = id; $("map-source").value = id; return trace(); }));
    if (room) detail.append(button("交给队友检查", () => { showView("team"); $("room-task-title").value = "检查" + id + "的接口和到" + (target || "目标") + "的运行输出"; $("room-task-title").closest("details").open = true; $("room-task-assignee").focus(); }));
  }
  function draw() {
    const host = $("map-graph"); host.replaceChildren();
    const nodes = network.nodes, levels = new Map(), root = nodes.find(n => n.id === selectedSource) || nodes[0];
    if (!root) return;
    levels.set(root.id, 0); let queue = [root.id];
    while (queue.length) { const id = queue.shift(); for (const edge of network.links) { const other = edge.source === id ? edge.target : edge.target === id ? edge.source : null; if (other && !levels.has(other)) { levels.set(other, levels.get(id) + 1); queue.push(other); } } }
    let last = Math.max(...levels.values()); for (const n of nodes) if (!levels.has(n.id)) levels.set(n.id, ++last);
    const columns = new Map(); for (const n of nodes) { const level = levels.get(n.id); if (!columns.has(level)) columns.set(level, []); columns.get(level).push(n); }
    const height = Math.max(220, Math.max(...[...columns.values()].map(a => a.length)) * 115 + 50), positions = new Map();
    for (const [level, group] of columns) for (const [index,n] of group.entries()) positions.set(n.id, {x:last ? 90 + level / last * 700 : 440, y:(index + 1) * height / (group.length + 1)});
    const canvas = svg("svg", {viewBox:"0 0 880 " + height, role:"img", "aria-label":"实验网络拓扑，点击设备查看依据"});
    const active = network.explanation?.path || [];
    for (const edge of network.links) {
      const a = positions.get(edge.source), b = positions.get(edge.target); if (!a || !b) continue;
      const lit = active.some((id,index) => index && [active[index-1], id].includes(edge.source) && [active[index-1], id].includes(edge.target));
      const line = svg("line", {x1:a.x,y1:a.y,x2:b.x,y2:b.y,class:"map-link " + (edge.kind === "subnet" ? "inferred " : "") + (lit ? "active" : "")});
      const title = svg("title"); title.textContent = edge.label; line.append(title); canvas.append(line);
    }
    for (const n of nodes) {
      const fault = network.explanation?.steps.some(s => s.device === n.id && s.status === "failed");
      const p = positions.get(n.id), g = svg("g", {class:"map-node " + (active.includes(n.id) ? "active " : "") + (fault ? "failed" : ""), tabindex:"0",role:"button","aria-label":n.id + " 查看设备依据"});
      g.append(svg("rect", {x:p.x-51,y:p.y-28,width:102,height:56,rx:14}));
      const label = svg("text", {x:p.x,y:p.y+4,"text-anchor":"middle"}); label.textContent = n.id.length > 10 ? n.id.slice(0,9) + "…" : n.id; g.append(label);
      const ip = svg("text", {x:p.x,y:p.y+48,"text-anchor":"middle",class:"map-ip"}); ip.textContent = n.interfaces[0]?.address || "待补设备信息"; g.append(ip);
      g.addEventListener("click", () => detailNode(n.id)); g.addEventListener("keydown", event => { if (["Enter"," "].includes(event.key)) { event.preventDefault(); detailNode(n.id); } }); canvas.append(g);
    }
    host.append(canvas);
  }
  function renderExplanation() {
    const panel = $("map-steps"); panel.replaceChildren();
    const result = network.explanation;
    if (!result) { panel.append(el("p", "选择源设备和目标地址，查看接口、网关、路由与测试记录。", "section-note")); return; }
    panel.append(el("p", {passed:"端到端测试通过",failed:"发现通信问题",needs_evidence:"继续补齐路径依据"}[result.status], "map-outcome " + result.status));
    const list = el("ol", undefined, "map-steps");
    for (const step of result.steps) {
      const item = el("li", undefined, step.status); item.append(el("strong", step.device + " · " + step.title), el("p", step.detail));
      if (step.evidence.length) { const d = el("details"); d.append(el("summary", "查看这一步的原文")); refs(d, step.evidence); item.append(d); }
      list.append(item);
    }
    panel.append(list, button("让 Agent 解释这条路径", () => { showDiagnosis(); c.ask("请分析" + result.source + "到" + result.target + "的通信路径，说明问题发生在哪一步及最小修改。"); }));
  }
  async function preview() {
    const id = ++previewId;
    if (!c.materials.size) { network = {nodes:[],links:[],pings:[],targets:[]}; renderMap(); return; }
    try {
      const next = await c.api("/api/network/inspect", {materials:Object.fromEntries(c.materials)});
      if (id !== previewId) return;
      network = next; renderMap();
    } catch (error) { if (id === previewId) c.message("网络地图：" + error.message, true); }
  }
  async function trace() {
    selectedSource = $("map-source").value; target = $("map-target").value.trim();
    if (!target) return c.message("填写想访问的目标IP地址。", true);
    $("map-steps").replaceChildren(el("p", "正在核对网关、路由和测试记录…", "section-note"));
    const id = ++previewId;
    try {
      const next = await c.api("/api/network/inspect", {materials:Object.fromEntries(c.materials), source:selectedSource, target});
      if (id !== previewId) return;
      network = next; draw(); renderExplanation();
    } catch (error) { if (id === previewId) $("map-steps").replaceChildren(el("p",error.message,"map-outcome failed")); throw error; }
  }
  function renderMap() {
    const section = $("network-map"); $("map-count").textContent = network.nodes.length; $("map-count").hidden = !network.nodes.length;
    if ($("map-target")) target = $("map-target").value;
    if ($("map-source")) selectedSource = $("map-source").value;
    section.replaceChildren();
    const heading = el("div", undefined, "dialogue-heading"); heading.append(el("h2", "会解释的网络地图"), el("span", "点设备看依据 · 选目标查路径")); section.append(heading);
    if (!network.nodes.length) {
      const empty = el("div",undefined,"empty-result"); empty.append(el("span","◇","empty-symbol"),el("h3", c.materials.size ? "补充设备与拓扑信息" : "先把实验材料带进来"),el("p",c.materials.size ? "上传带设备名的配置、地址信息或拓扑说明后，地图会自动整理设备和连线。" : "截图识别文字、设备配置和拓扑说明会汇成一张可检查路径的网络地图。"),button("回到工作台添加材料",() => showView("diagnosis"))); section.append(empty); return;
    }
    const form = el("form", undefined, "map-controls"), source = el("select"); source.id = "map-source"; source.setAttribute("aria-label", "源设备");
    if (!network.nodes.some(n => n.id === selectedSource)) selectedSource = network.nodes.find(n => /^PC/i.test(n.id))?.id || network.nodes[0].id;
    for (const n of network.nodes) { const option = el("option", n.id); option.value = n.id; source.append(option); } source.value = selectedSource;
    const sourcePings = network.pings.filter(p => p.test_device === selectedSource);
    const input = field("目标IPv4地址", target || sourcePings.filter(p => !p.passed).at(-1)?.target || sourcePings[0]?.target || network.targets[0] || ""); input.id = "map-target"; input.setAttribute("list", "map-targets");
    const targets = el("datalist"); targets.id = "map-targets"; for (const value of network.targets) { const o = el("option"); o.value = value; targets.append(o); }
    const send = el("button", "检查这条路径", "send-button"); send.type = "submit";
    form.append(source,el("span", "→"),input,targets,send); form.addEventListener("submit", event => { event.preventDefault(); run(trace); }); section.append(form);
    const layout = el("div", undefined, "map-layout"), graph = el("div", undefined, "map-graph"); graph.id = "map-graph";
    const detail = el("div", undefined, "map-detail"); detail.id = "map-detail"; detail.append(el("p", "点击设备，查看接口地址和对应材料。", "section-note")); layout.append(graph,detail); section.append(layout,el("p","左右滑动地图，查看其他设备。","map-pan-hint"));
    section.append(el("p", "实线：材料中的拓扑连线　虚线：接口同网段关系　加粗：所选路径", "map-legend"));
    const steps = el("div"); steps.id = "map-steps"; section.append(steps); draw(); renderExplanation(); if (selectedNode) detailNode(selectedNode);
  }
  function init(adapter) {
    c = adapter; actor = tabStorage("labagent-actor") || storage("labagent-actor") || ""; memberId = tabStorage("labagent-member") || crypto.randomUUID(); tabStorage("labagent-member",memberId);
    const section = $("collaboration"), heading = el("div", undefined, "dialogue-heading");
    const title = el("h2", "协作排障室"); title.id = "room-title"; const status = el("span"); status.id = "room-status"; status.setAttribute("role","status"); heading.append(title,status);
    const controls = el("div", undefined, "room-toolbar"), name = field("你的姓名",actor), lab = field("实验名称","网络实验小组"); name.id = "room-actor"; name.maxLength = 40; lab.id = "room-name";
    name.addEventListener("change", () => { actor = name.value.trim(); storage("labagent-actor",actor); tabStorage("labagent-actor",actor); });
    const create = button("创建协作实验",async () => {
      const value = await c.api("/api/rooms", {name:lab.value,actor:getActor()});
      room = value; base = structuredClone(value);
      history.replaceState(null, "", "/?room=" + room.id); renderRoom(); startPoll();
      await publish();
    }); create.id = "create-room";
    controls.append(name,lab,create,button("加入 / 更新姓名",async () => { if (!room) throw new Error("先创建实验，或打开队友发来的邀请链接。"); room = await update({action:"join"}); renderRoom(); c.message("已加入协作实验。"); }));
    const practice = el("details",undefined,"challenge-entry"); practice.append(el("summary","找错小练习"),button("载入一道网关练习",async () => { const demo = await c.api("/api/challenges/new"); if (!c.challenge(demo)) return; challenge = demo; const answers = $("challenge-answers"); answers.replaceChildren(el("p",demo.question),button("查看练习材料",() => showView("diagnosis"))); for (const [index,text] of demo.choices.entries()) answers.append(button(text,async () => { const result = await c.api("/api/challenges/check",{id:challenge.id,choice:index}); $("challenge-feedback").textContent = result.reply; })); answers.append(el("p", "", "section-note")); answers.lastElementChild.id = "challenge-feedback"; c.message("练习已载入，可查看材料或打开网络地图，再选择故障环节。"); }));
    const answers = el("div"); answers.id = "challenge-answers"; practice.append(answers);
    const body = el("div"); body.id = "room-body"; const conflicts = el("div",undefined,"room-conflict"); conflicts.id = "room-conflict"; conflicts.hidden = true;
    section.append(heading,controls,body,conflicts,practice); renderRoom();
    for (const b of document.querySelectorAll("[data-workspace]")) b.addEventListener("click",() => showView(b.dataset.workspace));
    $("diagnosis-tab").addEventListener("click",() => showResultPanel("diagnosis")); $("evidence-tab").addEventListener("click",() => showResultPanel("evidence"));
    for (const id of ["diagnosis-tab","evidence-tab"]) $(id).addEventListener("keydown",event => { if (["ArrowLeft","ArrowRight"].includes(event.key)) { event.preventDefault(); const next = id === "diagnosis-tab" ? "evidence" : "diagnosis"; showResultPanel(next); $(next === "evidence" ? "evidence-tab" : "diagnosis-tab").focus(); } });
    preview(); showView("diagnosis");
  }
  function startPoll() { clearInterval(pollTimer); pollTimer = setInterval(poll,4000); }
  async function start() {
    const id = new URLSearchParams(location.search).get("room");
    if (!id) return false;
    if (!/^[a-f0-9]{32}$/.test(id)) throw new Error("协作邀请链接格式错误");
    let value;
    try { value = await c.api("/api/rooms/" + id); }
    catch (error) { if (error.status !== 404) throw error; showView("team"); $("room-status").textContent = "协作实验不存在，请检查邀请链接"; c.message(error.message,true); return true; }
    room = value;
    if (actor) await c.api("/api/rooms/" + id, {actor,member_id:memberId,action:"join"});
    await apply(await c.api("/api/rooms/" + id)); startPoll(); c.message("已打开协作实验，补充材料后点击同步即可。"); return true;
  }
  async function prepareDiagnosis(input) {
    if (!room) return;
    await publish();
    Object.assign(input,{configs:Object.fromEntries(c.materials), question:c.question() || input.question,
      room_id:room.id, room_revision:room.revision, room_actor:getActor()});
    if (room.session) input.session_id = room.session.session_id; else delete input.session_id;
  }
  function leave() { clearInterval(pollTimer); ++roomEpoch; ++previewId; room = null; base = null; conflict = null; seenResult = ""; challenge = null; selectedNode = ""; target = ""; renderRoom(); renderConflict(); $("challenge-answers").replaceChildren(); showDiagnosis(); }
  return {init,start,changed,preview,sessionUrl,prepareDiagnosis,leave,resultsAvailable,showDiagnosis};
})();
