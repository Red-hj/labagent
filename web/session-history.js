"use strict";
window.LabHistory = (() => {
  const $ = id => document.getElementById(id);
  const pageSize = 30;
  const narrow = matchMedia("(max-width:1100px)");
  let c, rows = [], current = null, token = 0, timer, controller, snapshot = null, materialToken = 0;
  let deleteTarget = null, deleting = false;
  let desktopHidden = false;
  try { desktopHidden = localStorage.getItem("labagent-history-hidden") === "true"; } catch {}
  const labels = {needs_information:"继续排查", verified_from_observations:"已核验", resolved_by_user_report:"已恢复", goals_pending:"目标待完成", comparison_complete:"对照完成"};

  function drawer(open, focus = true) {
    open = Boolean(open);
    const modal = open && narrow.matches;
    $("session-sidebar").hidden = !open;
    $("session-sidebar").classList.toggle("history-open", modal);
    document.querySelector(".app-layout").classList.toggle("history-collapsed", !open && !narrow.matches);
    $("history-backdrop").hidden = !modal;
    $("toggle-session-history").setAttribute("aria-expanded", String(open));
    $("toggle-session-history").setAttribute("aria-label", open ? "隐藏历史对话" : "显示历史对话");
    $("toggle-session-history").title = (open ? "隐藏" : "显示") + "历史对话（Ctrl+B）";
    document.querySelector("main").inert = modal;
    document.querySelector("header").inert = modal;
    if (!narrow.matches) {
      desktopHidden = !open;
      try { localStorage.setItem("labagent-history-hidden",String(desktopHidden)); } catch {}
    }
    if (modal) {
      $("session-sidebar").setAttribute("role", "dialog");
      $("session-sidebar").setAttribute("aria-modal", "true");
      if (focus) $("history-search").focus();
    } else {
      $("session-sidebar").removeAttribute("role");
      $("session-sidebar").removeAttribute("aria-modal");
    }
    if (!open && focus) $("toggle-session-history").focus();
  }
  function closeDrawer() { if (narrow.matches) drawer(false); }
  function toggleDrawer() { drawer($("session-sidebar").hidden); }
  function dayLabel(value) {
    const date = new Date(value), now = new Date();
    const sameDay = (a,b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
    if (sameDay(date, now)) return "今天";
    now.setDate(now.getDate() - 1);
    return sameDay(date, now) ? "昨天" : date.toLocaleDateString("zh-CN", {year:"numeric",month:"long"});
  }
  function renderList() {
    const list = $("session-list"); list.replaceChildren();
    let group = "";
    for (const row of rows) {
      const nextGroup = dayLabel(row.updated_at);
      if (group !== nextGroup) { list.append(c.element("h3", nextGroup, "history-group")); group = nextGroup; }
      const link = c.element("a", undefined, "session-item");
      link.href = "/?" + (row.room_id ? "room=" + encodeURIComponent(row.room_id) + "&" : "") + "session=" + row.session_id;
      link.title = row.title; link.dataset.session = row.session_id;
      if (row.session_id === current) link.setAttribute("aria-current", "page");
      link.append(c.element("span", row.title, "session-title"), c.element("span", row.preview || "打开这次排查", "session-preview"));
      const meta = c.element("span", undefined, "session-meta");
      meta.append(c.element("span", `${row.requests} 轮 · ${row.room_id ? "协作实验" : labels[row.status] || "已保存"}`),
                  c.element("time", new Date(row.updated_at).toLocaleTimeString("zh-CN", {hour:"2-digit",minute:"2-digit"})));
      link.append(meta);
      link.addEventListener("click", event => {
        if (c.busy()) { event.preventDefault(); c.message("当前请求结束后即可切换对话。"); }
        else if (row.session_id === current) { event.preventDefault(); closeDrawer(); c.showDiagnosis(); }
      });
      const item = c.element("div", undefined, "session-row");
      const remove = c.element("button", "删除", "delete-session"); remove.type = "button";
      remove.setAttribute("aria-label", "删除对话：" + row.title);
      remove.addEventListener("click", () => {
        if (c.busy()) return c.message("当前请求结束后即可删除对话。");
        deleteTarget = row;
        $("delete-session-title").textContent = "删除“" + row.title + "”？";
        $("delete-session-note").textContent = "全部问答和诊断记录将被删除，无法恢复。" + (row.room_id ? "团队成员也将无法打开这段对话；共享材料和任务保留。" : "");
        $("delete-session-error").textContent = "";
        $("delete-session-dialog").showModal();
      });
      item.append(link, remove); list.append(item);
    }
  }
  async function refresh(more = false) {
    const mine = ++token;
    controller?.abort(); controller = new AbortController();
    $("more-session-history").disabled = true;
    if (!more) $("history-status").textContent = "正在读取历史记录…";
    const query = new URLSearchParams({limit:String(pageSize), q:$("history-search").value.trim()});
    if (more && rows.length) query.set("before", rows.at(-1).cursor);
    try {
      const page = await c.api("/api/sessions?" + query, undefined, {signal:controller.signal});
      if (mine !== token) return;
      const combined = more ? [...rows, ...page] : page;
      rows = [...new Map(combined.map(row => [row.session_id,row])).values()];
      renderList();
      $("history-status").textContent = rows.length ? "" : $("history-search").value.trim() ? "没有找到匹配的对话。" : "还没有历史对话。开始排查后，会自动保存在这里。";
      $("more-session-history").hidden = page.length < pageSize;
    } catch (error) {
      if (mine === token && error.name !== "AbortError") $("history-status").textContent = "历史记录加载失败：" + error.message + " 点击刷新重试。";
    } finally { if (mine === token) $("more-session-history").disabled = false; }
  }
  function activate(id) {
    current = id;
    for (const link of $("session-list").querySelectorAll("a")) {
      if (link.dataset.session === id) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
    }
    $("new-conversation").disabled = c.busy();
  }
  function setSnapshot(value) { snapshot = value; }
  async function confirmDelete() {
    if (!deleteTarget || deleting) return;
    if (c.busy()) { $("delete-session-error").textContent = "当前请求结束后即可删除对话。"; return; }
    const row = deleteTarget;
    deleting = true;
    $("confirm-delete-session").disabled = true;
    $("cancel-delete-session").disabled = true;
    $("confirm-delete-session").textContent = "正在删除…";
    $("delete-session-error").textContent = "";
    try {
      try { await c.api("/api/sessions/" + row.session_id, undefined, {method:"DELETE"}); }
      catch (error) { if (error.status !== 404 || error.code !== "session_not_found") throw error; }
      $("delete-session-dialog").close(); deleteTarget = null;
      if (current === row.session_id) { closeDrawer(); c.newConversation(); }
      rows = rows.filter(item => item.session_id !== row.session_id); renderList();
      await refresh();
      c.message("对话已删除。");
    } catch (error) { $("delete-session-error").textContent = "删除失败：" + error.message; }
    finally {
      deleting = false;
      $("confirm-delete-session").disabled = false;
      $("cancel-delete-session").disabled = false;
      $("confirm-delete-session").textContent = "删除对话";
    }
  }
  async function showMaterials(turn, citation) {
    const id = current, mine = ++materialToken;
    const dialog = $("history-material-dialog"), content = $("history-material-content");
    $("history-material-title").textContent = `第 ${turn.attempt} 轮材料与依据`;
    $("history-material-note").textContent = "正在读取本轮保存的材料…"; content.replaceChildren();
    if (!dialog.open) dialog.showModal();
    try {
      if (!snapshot || snapshot.session_id !== id || snapshot.attempts.length < turn.attempt) snapshot = await c.api("/api/sessions/" + id);
      if (mine !== materialToken || !dialog.open) return;
      const attempt = snapshot.attempts[turn.attempt - 1];
      if (!attempt) throw new Error("这轮记录暂时无法读取，请刷新后重试。");
      const configs = attempt.input.configs || {};
      $("history-material-note").textContent = "这是当时提交的材料快照；当前排查继续使用最新一轮材料。";
      if (citation) content.append(c.element("p", `${citation.device || "材料依据"} · 第 ${citation.line} 行：${citation.quote}`, "section-note"));
      for (const [name, text] of Object.entries(configs)) {
        const details = c.element("details"); details.append(c.element("summary", name));
        const pre = c.element("pre", text);
        const lines = text.split(/\r?\n/);
        if (citation?.device === name && lines[citation.line - 1] === citation.quote) {
          details.open = true;
          pre.replaceChildren(document.createTextNode(lines.slice(0,citation.line - 1).join("\n") + (citation.line > 1 ? "\n" : "")),
            c.element("mark", citation.quote), document.createTextNode("\n" + lines.slice(citation.line).join("\n")));
        }
        details.append(pre); content.append(details);
      }
      const media = c.element("div", undefined, "history-media"); content.append(media);
      const results = await Promise.allSettled((attempt.input.recordings || []).map(recording => c.api("/api/recordings/" + recording)));
      if (mine !== materialToken || !dialog.open) return;
      for (const result of results) {
        if (result.status !== "fulfilled") { media.append(c.element("p", "部分原图或录屏已无法读取，识别文字仍保留在本轮材料中。", "section-note")); continue; }
        const item = result.value;
        if (item.kind === "image") {
          const button = c.element("button", undefined); button.type = "button";
          const image = c.element("img"); image.src = c.mediaUrl(item.image_url); image.alt = item.name; image.loading = "lazy";
          button.append(image, c.element("span", "查看原图 · " + item.name));
          button.addEventListener("click", () => c.showScreenshot(item, citation?.device === item.material_name ? citation : null));
          media.append(button);
        } else {
          const link = c.element("a", "打开录屏 · " + item.name); link.href = c.mediaUrl(item.video_url); link.target = "_blank"; link.rel = "noopener"; media.append(link);
        }
      }
    } catch (error) { if (mine === materialToken) $("history-material-note").textContent = error.message; }
  }
  function renderTurns(turns) {
    window.LabConversation.renderTurns($("conversation"), turns, showMaterials);
    $("conversation-count").textContent = `${turns.length} 轮对话 · 可继续排查`;
    $("conversation").scrollTop = $("conversation").scrollHeight;
  }
  function init(context) {
    c = context;
    $("confirm-delete-session").addEventListener("click", confirmDelete);
    $("cancel-delete-session").addEventListener("click", () => { $("delete-session-dialog").close(); deleteTarget = null; });
    $("delete-session-dialog").addEventListener("cancel", event => { if (deleting) event.preventDefault(); else deleteTarget = null; });
    $("toggle-session-history").addEventListener("click", toggleDrawer);
    $("close-session-history").addEventListener("click", () => drawer(false));
    $("history-backdrop").addEventListener("click",closeDrawer);
    $("close-history-materials").addEventListener("click", () => $("history-material-dialog").close());
    $("new-conversation").addEventListener("click", () => { if (!c.busy()) { closeDrawer(); c.newConversation(); } });
    $("refresh-session-history").addEventListener("click", () => refresh());
    $("more-session-history").addEventListener("click", () => refresh(true));
    $("history-search").addEventListener("input", () => { clearTimeout(timer); controller?.abort(); ++token; timer = setTimeout(() => refresh(),250); });
    narrow.addEventListener("change", () => drawer(!narrow.matches && !desktopHidden, false));
    drawer(!narrow.matches && !desktopHidden, false);
    document.addEventListener("keydown", event => {
      if ((event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && !event.repeat && !event.isComposing && event.key.toLowerCase() === "b" && !document.querySelector("dialog[open]")) {
        event.preventDefault(); toggleDrawer(); return;
      }
      if (!$("session-sidebar").classList.contains("history-open") || $("delete-session-dialog").open || $("history-material-dialog").open) return;
      if (event.key === "Escape") { event.preventDefault(); closeDrawer(); }
      if (event.key === "Tab") {
        const focusable = [...$("session-sidebar").querySelectorAll("button:not([disabled]),input,a")].filter(node => !node.hidden);
        if (event.shiftKey && document.activeElement === focusable[0]) { event.preventDefault(); focusable.at(-1).focus(); }
        else if (!event.shiftKey && document.activeElement === focusable.at(-1)) { event.preventDefault(); focusable[0].focus(); }
      }
    });
  }
  return {init,refresh,activate,setSnapshot,renderTurns};
})();
