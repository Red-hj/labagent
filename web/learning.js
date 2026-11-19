"use strict";
// Learning controls add to the existing conversation and material submission flow.
window.LabLearning = (() => {
  const get = id => document.getElementById(id);
  let active = false, unlocked = false, level = 1, callbacks = null, bridgeId = null, connectionEpoch = 0;
  function note(text, error = false) {
    get("learning-note").textContent = text;
    get("learning-note").classList.toggle("error", error);
  }
  function updateLevel() {
    level = Number(get("learning-level").value);
    if (level === 3 && !unlocked) { level = 1; get("learning-level").value = "1"; }
  }
  function labNote(text, error = false) {
    get("lab-note").textContent = text;
    get("lab-note").classList.toggle("error", error);
  }
  function apply(input) {
    if (active) input.learning = {level};
    return input;
  }
  function show(result) {
    const step = result.investigation?.learning;
    get("learning-current").hidden = !step;
    if (!step) return;
    get("learning-current-title").textContent = step.current_step;
    get("learning-current-observation").textContent = step.expected_observation;
    get("learning-current-question").textContent = step.question;
    // A restored answer never restores permission to request another solution.
    level = Math.min(step.level || 1, unlocked ? 3 : 2);
    get("learning-level").value = String(level);
  }
  async function loadTools() {
    const epoch = connectionEpoch;
    const data = await callbacks.api("/api/lab/tools");
    if (epoch !== connectionEpoch) return;
    const box = get("lab-tools"); box.replaceChildren();
    bridgeId = data.bridge?.id || null;
    get("lab-disconnect").hidden = !bridgeId;
    if (data.bridge?.state === "online") {
      get("lab-pair-code").textContent = ""; get("lab-pair-code").hidden = true;
      labNote(data.bridge.name + "已连接。选择下面的一项检查。");
    } else if (data.bridge) labNote(data.bridge.state === "waiting" ? "等待实验电脑输入连接码，启动后点“刷新连接与检查”。" : "实验电脑已离线。请重新启动本地程序并生成连接码。");
    else labNote("可先连接实验电脑，也可继续上传配置、截图或日志。");
    for (const tool of data.tools || []) {
      const fn = tool.function, form = document.createElement("form");
      const title = document.createElement("p"); title.textContent = fn.description; form.append(title);
      const controls = {};
      for (const [key, definition] of Object.entries(fn.parameters.properties)) {
        const label = document.createElement("label"), select = document.createElement("select");
        label.textContent = key === "command" ? "要检查的内容" : key;
        for (const value of definition.enum) { const option = document.createElement("option"); option.value = value; option.textContent = value; select.append(option); }
        controls[key] = select; label.append(select); form.append(label);
      }
      const button = document.createElement("button"); button.type = "submit"; button.className = "secondary-button"; button.textContent = "采集到当前材料"; form.append(button);
      form.addEventListener("submit", async event => {
        event.preventDefault();
        if (callbacks.busy()) return labNote("请等本轮回复完成后再采集。", true);
        button.disabled = true; callbacks.setCollecting?.(true); labNote("正在从已连接的实验设备读取输出…");
        try {
          const data = await callbacks.api("/api/lab/collect", {tool:fn.name, ...(tool.bridge_id ? {bridge_id:tool.bridge_id} : {}), arguments:Object.fromEntries(Object.entries(controls).map(([key, control]) => [key, control.value]))});
          if (epoch !== connectionEpoch) return;
          callbacks.addMaterials(data.materials);
          get("lab-check-step").hidden = false;
          labNote("设备输出已加入当前材料。点“核对这一步”继续，会话保留。");
        } catch (error) { if (epoch === connectionEpoch) labNote(error.message, true); }
        finally { button.disabled = false; callbacks.setCollecting?.(false); }
      });
      box.append(form);
    }
    if (data.warnings?.length) labNote(data.warnings.join("；"));
  }
  async function init(options) {
    callbacks = options;
    try {
      const data = await callbacks.api("/api/learning");
      active = data.enabled; unlocked = data.solution_unlocked;
      get("learning-panel").hidden = !active;
      get("learning-solution-option").disabled = !unlocked;
      get("learning-unlock").hidden = !data.can_unlock;
      get("lab-connection").hidden = !data.mcp_configured && !("bridge_available" in data);
      if (active) {
        get("problem").placeholder = "做到哪一步了？哪里不懂，或出现了什么现象？还没准备好截图也可以先问。";
        get("followup-message").placeholder = "说说你刚做了什么、看到了什么，或哪里还不懂…";
        document.querySelectorAll('.interaction-actions [data-intent="repair"]').forEach(button => { button.textContent = "引导我做下一步"; });
        note("先给一小步提示，操作后把观察结果发回来；还不懂时可以继续拆解。");
      }
    } catch (_) { return; }
    get("learning-level").addEventListener("change", updateLevel);
    get("learning-smaller").addEventListener("click", () => callbacks.ask("我还不会当前这一步，请换一种解释，把操作拆得更小；先不要给出最终答案。"));
    get("learning-more").addEventListener("click", () => { level = 2; get("learning-level").value = "2"; callbacks.ask("请把当前这一步说得更具体，说明在哪里操作、需要观察什么，关键答案先让我自己判断。", "ask"); });
    get("learning-check").addEventListener("click", () => callbacks.ask("请核对我刚补充的操作和输出，判断当前这一步是否完成，再引导下一步；缺少输出时先告诉我看哪里。", "verify"));
    get("learning-unlock-form").addEventListener("submit", async event => {
      event.preventDefault();
      if (callbacks.busy()) return note("请等本轮回复完成后再验证权限。", true);
      const code = get("learning-code"), button = get("learning-unlock-button"); button.disabled = true;
      try {
        await callbacks.api("/api/learning/unlock", {code:code.value});
        unlocked = true; get("learning-solution-option").disabled = false;
        note("已获得两小时的解答权限，需要时可选择“当前步骤解答”。");
      } catch (error) { note(error.message, true); }
      finally { code.value = ""; button.disabled = false; }
    });
    get("lab-load-tools").addEventListener("click", () => loadTools().catch(error => labNote(error.message, true)));
    get("lab-pair").addEventListener("click", async () => {
      if (callbacks.busy()) return labNote("请等当前操作完成后再连接。", true);
      const epoch = ++connectionEpoch, button = get("lab-pair"); button.disabled = true;
      get("lab-tools").replaceChildren();
      try {
        const data = await callbacks.api("/api/lab/bridge", {name:get("lab-computer-name").value});
        if (epoch !== connectionEpoch) return;
        bridgeId = data.id;
        get("lab-tools").replaceChildren(); get("lab-disconnect").hidden = false;
        get("lab-pair-code").textContent = "连接码：" + data.code + "（5分钟内有效）"; get("lab-pair-code").hidden = false;
        labNote("在实验电脑的连接程序中输入连接码，再刷新可用检查。");
      } catch (error) { if (epoch === connectionEpoch) labNote(error.message, true); }
      finally { button.disabled = false; }
    });
    get("lab-disconnect").addEventListener("click", async () => {
      if (callbacks.busy()) return labNote("请等当前操作完成后再断开。", true);
      const id = bridgeId;
      try {
        await callbacks.api("/api/lab/bridge/disconnect", {id});
        connectionEpoch++; bridgeId = null; get("lab-tools").replaceChildren();
        get("lab-pair-code").textContent = ""; get("lab-pair-code").hidden = true;
        get("lab-disconnect").hidden = true;
        labNote("实验电脑已断开，已采集的材料仍保留。");
      } catch (error) { labNote(error.message, true); }
    });
    get("lab-check-step").addEventListener("click", () => {
      if (!callbacks.busy()) callbacks.ask("请用我刚从实验电脑采集的输出核对当前这一步，说明观察到了什么，再引导我做下一小步。", "verify");
    });
  }
  async function refreshAccess() {
    if (!callbacks) return;
    const epoch = connectionEpoch;
    try {
      const data = await callbacks.api("/api/learning");
      if (epoch !== connectionEpoch) return;
      unlocked = data.solution_unlocked;
      get("learning-solution-option").disabled = !unlocked;
      get("lab-connection").hidden = !data.mcp_configured && !("bridge_available" in data);
      if (!unlocked && level === 3) { level = 1; get("learning-level").value = "1"; }
    } catch (_) { if (epoch !== connectionEpoch) return; unlocked = false; level = 1; get("learning-level").value = "1"; get("learning-solution-option").disabled = true; }
  }
  return {init, apply, show, enabled:() => active,
    reset:() => { connectionEpoch++; bridgeId = null; get("lab-tools").replaceChildren(); get("lab-pair-code").textContent = ""; get("lab-pair-code").hidden = true; get("lab-check-step").hidden = true; get("lab-disconnect").hidden = true; labNote(""); level = 1; get("learning-level").value = "1"; get("learning-current").hidden = true; refreshAccess(); }};
})();
