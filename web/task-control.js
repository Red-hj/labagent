"use strict";
(function(root) {
  let options, current = null;
  const key = () => "labagent-active-task:" + (options.headers()["X-LabAgent-Room"] || "personal");
  const remember = id => { try { id ? sessionStorage.setItem(key(), id) : sessionStorage.removeItem(key()); } catch {} };
  function stored() { try { return sessionStorage.getItem(key()); } catch { return null; } }
  function stopped(message) { const error = new Error(message || "已停止本轮检查，材料已保留。"); error.code = "task_stopped"; error.keepTask = true; return error; }
  function display(state = "running") {
    const button = document.getElementById("stop-request");
    button.hidden = !current;
    button.disabled = state === "cancelling" || state === "finalizing";
    button.textContent = state === "cancelling" ? "正在停止…" : state === "finalizing" ? "正在保存…" : "停止";
  }
  function finish(id, keep = false) { if (current?.id === id) { current = null; display(); if (!keep && stored() === id) remember(null); } }
  function accept(event, request) {
    options.progress(event, request);
    if (event.type === "result") return event.result;
    if (event.type === "cancelled") throw stopped(event.message);
    if (event.type === "error") { const error = new Error(event.message); error.terminal = true; error.keepTask = true; throw error; }
    return null;
  }
  async function watch(id, request, snapshot = null) {
    current = {id, request}; remember(id); display(snapshot?.state);
    let keep = false;
    try {
      for (;;) {
        try { snapshot = snapshot || await options.api("/api/tasks/" + id + "?status_only=1"); }
        catch (error) {
          if ([401,403,404].includes(error.status)) { error.terminal = true; throw error; }
          options.waiting("连接暂时中断，正在恢复任务状态；检查不会重新发起。");
          await new Promise(resolve => setTimeout(resolve, 2000)); continue;
        }
        for (const event of snapshot.events || []) options.progress(event, request);
        display(snapshot.state);
        if (snapshot.result) return snapshot.result;
        if (["cancelled", "interrupted"].includes(snapshot.state)) throw stopped(snapshot.message);
        if (snapshot.state === "failed") { const error = new Error(snapshot.message || "本轮处理失败，材料已保留。"); error.terminal = true; error.keepTask = true; throw error; }
        snapshot = null;
        await new Promise(resolve => setTimeout(resolve, 1500));
      }
    } catch (error) { keep = Boolean(error.keepTask); throw error; }
    finally { finish(id, keep); }
  }
  async function run(input, request) {
    const id = crypto.randomUUID().replaceAll("-", "");
    request.taskId = id; current = {id, request}; remember(id); display();
    try {
      const response = await fetch("/api/diagnose/stream", {method:"POST", credentials:"same-origin",
        signal:AbortSignal.timeout(330000), headers:{"Content-Type":"application/json", ...options.headers()},
        body:JSON.stringify({...input, request_id:id})});
      if (!response.ok) {
        const data = await response.json();
        if (data.code === "task_exists") return await watch(id, request);
        const error = new Error(data.error || "本轮诊断未能开始。"); error.terminal = true; throw error;
      }
      const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = "";
      try {
        for (;;) {
          const {done, value} = await reader.read(); buffer += decoder.decode(value, {stream:!done});
          let newline;
          while ((newline = buffer.indexOf("\n")) >= 0) {
            const line = buffer.slice(0, newline); buffer = buffer.slice(newline + 1);
            if (line.trim()) { const result = accept(JSON.parse(line), request); if (result) return result; }
          }
          if (done) break;
        }
      } finally { reader.releaseLock(); }
      return await watch(id, request);
    } catch (error) {
      if (error.terminal || error.code === "task_stopped") { finish(id, Boolean(error.keepTask)); throw error; }
      return await watch(id, request);
    } finally { finish(id); }
  }
  async function cancel() {
    if (!current) return;
    const task = current; display("cancelling");
    try {
      const snapshot = await options.api("/api/tasks/" + task.id + "/cancel", {});
      if (current !== task) return;
      display(snapshot.state);
      options.waiting(snapshot.state === "cancelling" ? "正在停止，等待已发出的检查返回；已有结果会保留。" : "本轮已结束，正在读取结果。");
    } catch (error) { if (current === task) { display(); options.waiting("停止请求尚未确认：" + error.message); } }
  }
  root.LabTasks = {init(value) { options = value; document.getElementById("stop-request").addEventListener("click", cancel); }, run, watch,
    async restore() {
      const id = stored(); if (!id) return null;
      try { return await options.api("/api/tasks/" + id); }
      catch (error) { if ([401,403,404].includes(error.status)) remember(null); throw error; }
    }};
})(globalThis);
