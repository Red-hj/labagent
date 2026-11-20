"use strict";
// Learning controls add to the existing conversation and material submission flow.
window.LabLearning = (() => {
  const get = id => document.getElementById(id);
  let active = false, unlocked = false, level = 1, callbacks = null, lab = null, accessEpoch = 0;
  function note(text, error = false) {
    get("learning-note").textContent = text;
    get("learning-note").classList.toggle("error", error);
  }
  function updateLevel() {
    level = Number(get("learning-level").value);
    if (level === 3 && !unlocked) { level = 1; get("learning-level").value = "1"; }
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
  async function init(options) {
    callbacks = options;
    lab = window.LabConversation.mountLabConnection(get("lab-connection-view"), options);
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
  }

  async function refreshAccess() {
    if (!callbacks) return;
    const epoch = accessEpoch;
    try {
      const data = await callbacks.api("/api/learning");
      if (epoch !== accessEpoch) return;
      unlocked = data.solution_unlocked;
      get("learning-solution-option").disabled = !unlocked;
      get("lab-connection").hidden = !data.mcp_configured && !("bridge_available" in data);
      if (!unlocked && level === 3) { level = 1; get("learning-level").value = "1"; }
    } catch (_) { if (epoch !== accessEpoch) return; unlocked = false; level = 1; get("learning-level").value = "1"; get("learning-solution-option").disabled = true; }
  }
  return {init, apply, show, enabled:() => active, refreshBusy:() => lab?.refreshBusy(),
    reset:() => { accessEpoch++; lab?.reset(); level = 1; get("learning-level").value = "1"; get("learning-current").hidden = true; refreshAccess(); }};
})();
