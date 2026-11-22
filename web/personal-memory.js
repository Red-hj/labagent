"use strict";
window.LabPersonal = (() => {
  const $ = id => document.getElementById(id);
  let c, user = null, registering = false, saving = false, editing = null, latest = null;
  const names = {issue:"历史问题", preference:"排查偏好", environment:"实验环境"};
  const channel = typeof BroadcastChannel === "function" ? new BroadcastChannel("labagent-personal-account") : null;
  if (channel) channel.onmessage = () => location.reload();

  function renderAccount() {
    $("personal-entry").textContent = user ? user.display_name + " · 记忆" : "登录 / 记忆";
    $("personal-dialog").dataset.view = user ? "memory" : "auth";
    $("personal-title").textContent = user ? "我的记忆" : registering ? "创建你的工作区" : "继续你的排查";
    $("personal-eyebrow").textContent = user ? "PERSONAL MEMORY" : registering ? "MAKE IT YOURS" : "YOUR WORKSPACE";
    $("personal-subtitle").textContent = user ? "整理常见问题、实验环境和排查偏好。" : registering ? "从这次开始，让每次排查都有迹可循。" : "登录后，接着聊，也记得你的排查习惯。";
    $("personal-auth").hidden = Boolean(user);
    $("personal-auth-note").hidden = Boolean(user);
    $("personal-data-note").hidden = !user;
    $("personal-panel").hidden = !user;
    $("personal-name").textContent = user ? user.display_name + " · " + user.username : "";
    $("personal-enabled").checked = Boolean(user?.memory_enabled);
  }
  function authMode() {
    if (saving) return;
    registering = !registering;
    renderAuthMode();
  }
  function renderAuthMode() {
    $("personal-display-row").hidden = !registering;
    $("personal-register-options").hidden = !registering;
    $("personal-username-hint").hidden = !registering;
    $("personal-auth-submit").textContent = registering ? "创建个人账号" : "登录";
    $("personal-auth-switch").textContent = registering ? "已有账号，去登录" : "没有账号，创建一个";
    $("personal-password").autocomplete = registering ? "new-password" : "current-password";
    resetPasswordVisibility();
    renderAccount();
    $("personal-error").textContent = "";
  }
  function resetPasswordVisibility() {
    $("personal-password").type = "password";
    $("personal-password-toggle").textContent = "显示";
    $("personal-password-toggle").setAttribute("aria-label", "显示密码");
    $("personal-password-toggle").setAttribute("aria-pressed", "false");
  }
  function authPending(pending) {
    $("personal-auth").setAttribute("aria-busy", String(pending));
    for (const id of ["personal-auth-submit", "personal-auth-switch", "personal-username", "personal-password", "personal-password-toggle", "personal-display", "personal-register-memory"]) $(id).disabled = pending;
    $("personal-auth-submit").textContent = pending ? (registering ? "正在创建…" : "正在登录…") : (registering ? "创建个人账号" : "登录");
  }
  function resetForm() {
    editing = null; $("personal-memory-form").reset();
    $("personal-memory-save").textContent = "保存记忆";
    $("personal-memory-cancel").hidden = true;
  }
  function guard() {
    if (saving || c.busy()) { $("personal-error").textContent = "当前请求结束后即可操作。"; return false; }
    $("personal-error").textContent = ""; return true;
  }
  async function refresh() {
    const state = await c.api("/api/account"), previousOwner = user?.id;
    user = state.user;
    if (previousOwner !== user?.id) c.identityChanged?.();
    renderAccount();
    if (!user) { $("personal-memory-list").replaceChildren(); resetForm(); return; }
    const saved = await c.api("/api/memories");
    user.memory_enabled = saved.enabled; $("personal-enabled").checked = saved.enabled;
    $("personal-count").textContent = saved.memories.length ? `${saved.memories.length} 条记忆` : "还没有记忆。可以手动添加，个人排查完成一轮后也会记录历史问题。";
    const list = $("personal-memory-list"); list.replaceChildren();
    for (const memory of saved.memories) {
      const card = c.element("article", undefined, "memory-item");
      card.append(c.element("h4", memory.title), c.element("p", memory.content),
        c.element("small", names[memory.kind] + (memory.kind === "issue" ? ` · ${memory.occurrences} 次独立排查` : "")));
      const actions = c.element("div", undefined, "memory-item-actions");
      const edit = c.element("button", "编辑", "text-button"); edit.type = "button";
      edit.addEventListener("click", () => {
        if (!guard()) return;
        document.querySelector(".personal-memory-editor").open = true;
        editing = memory.id; $("personal-memory-kind").value = memory.kind;
        $("personal-memory-title").value = memory.title; $("personal-memory-content").value = memory.content;
        $("personal-memory-save").textContent = "保存修改"; $("personal-memory-cancel").hidden = false;
        $("personal-memory-title").focus();
      });
      const remove = c.element("button", "删除", "text-button"); remove.type = "button";
      remove.addEventListener("click", () => {
        if (!guard()) return;
        actions.replaceChildren(c.element("span", "删除这条记忆？"));
        const yes = c.element("button", "确认删除", "text-button"), no = c.element("button", "取消", "text-button");
        yes.type = no.type = "button";
        yes.addEventListener("click", async () => {
          if (!guard()) return;
          saving = true;
          try { await c.api("/api/memories/" + memory.id, undefined, {method:"DELETE"}); resetForm(); await refresh(); }
          catch (error) { $("personal-error").textContent = error.message; }
          finally { saving = false; }
        });
        no.addEventListener("click", () => actions.replaceChildren(edit, remove)); actions.append(yes, no);
      });
      actions.append(edit, remove); card.append(actions); list.append(card);
    }
  }
  async function authenticate(event) {
    event.preventDefault(); if (!guard()) return; saving = true;
    authPending(true);
    try {
      const credentials = {username:$("personal-username").value.trim(), password:$("personal-password").value};
      if (registering) Object.assign(credentials, {display_name:$("personal-display").value.trim() || credentials.username,
        memory_enabled:$("personal-register-memory").checked});
      await c.api(registering ? "/api/account/register" : "/api/account/login", credentials);
      $("personal-password").value = ""; resetPasswordVisibility(); latest = null; c.newConversation();
      await refresh(); registering = false; renderAuthMode(); await c.refreshHistory(); channel?.postMessage("changed"); c.afterSignIn?.();
      c.message("个人账号已登录，后续排查将使用你的记忆设置。");
    } catch (error) { $("personal-error").textContent = error.message; }
    finally { saving = false; authPending(false); }
  }
  async function logout() {
    if (!guard()) return; saving = true;
    try { await c.api("/api/account/logout", {}); latest = null; resetForm(); c.newConversation(); await refresh(); await c.refreshHistory(); channel?.postMessage("changed"); }
    catch (error) { $("personal-error").textContent = error.message; }
    finally { saving = false; }
  }
  function init(context) {
    c = context;
    $("personal-entry").addEventListener("click", async () => {
      $("personal-error").textContent = ""; $("personal-dialog").showModal();
      try { await refresh(); } catch (error) { $("personal-error").textContent = error.message; }
      if (!user && $("personal-dialog").open) $("personal-username").focus();
    });
    $("personal-close").addEventListener("click", () => $("personal-dialog").close());
    $("personal-dialog").addEventListener("close", () => {
      $("personal-password").value = ""; resetPasswordVisibility(); $("personal-entry").focus();
    });
    $("personal-password-toggle").addEventListener("click", () => {
      const visible = $("personal-password").type === "password";
      $("personal-password").type = visible ? "text" : "password";
      $("personal-password-toggle").textContent = visible ? "隐藏" : "显示";
      $("personal-password-toggle").setAttribute("aria-label", visible ? "隐藏密码" : "显示密码");
      $("personal-password-toggle").setAttribute("aria-pressed", String(visible));
    });
    $("personal-auth-switch").addEventListener("click", authMode);
    $("personal-auth").addEventListener("submit", authenticate);
    $("personal-logout").addEventListener("click", logout);
    $("personal-memory-cancel").addEventListener("click", resetForm);
    $("personal-enabled").addEventListener("change", async () => {
      if (!guard()) { $("personal-enabled").checked = user.memory_enabled; return; }
      saving = true;
      try { await c.api("/api/account/memory", {enabled:$("personal-enabled").checked}); await refresh(); }
      catch (error) { $("personal-enabled").checked = user.memory_enabled; $("personal-error").textContent = error.message; }
      finally { saving = false; }
    });
    $("personal-memory-form").addEventListener("submit", async event => {
      event.preventDefault(); if (!guard()) return; saving = true; $("personal-memory-save").disabled = true;
      try {
        await c.api("/api/memories", {id:editing, kind:$("personal-memory-kind").value,
          title:$("personal-memory-title").value, content:$("personal-memory-content").value});
        resetForm(); await refresh();
      } catch (error) { $("personal-error").textContent = error.message; }
      finally { saving = false; $("personal-memory-save").disabled = false; }
    });
    $("personal-from-current").addEventListener("click", () => {
      if (!guard()) return;
      if (!latest || c.shared()) { $("personal-error").textContent = "先完成一次个人排查，再将要记住的内容填入这里。"; return; }
      resetForm(); $("personal-memory-kind").value = "issue";
      document.querySelector(".personal-memory-editor").open = true;
      $("personal-memory-title").value = (c.question() || latest.investigation?.experiment_goal || "这次排查").slice(0,120);
      $("personal-memory-content").value = (latest.investigation?.summary || latest.hint || "").slice(0,600);
      $("personal-error").textContent = "请核对并修改要记住的内容，再点击保存。";
    });
    $("personal-clear").addEventListener("click", () => {
      if (guard()) $("personal-clear-confirm").hidden = false;
    });
    $("personal-clear-cancel").addEventListener("click", () => $("personal-clear-confirm").hidden = true);
    $("personal-clear-yes").addEventListener("click", async () => {
      if (!guard()) return; saving = true;
      try { await c.api("/api/memories", undefined, {method:"DELETE"}); resetForm(); await refresh(); $("personal-clear-confirm").hidden = true; }
      catch (error) { $("personal-error").textContent = error.message; }
      finally { saving = false; }
    });
    refresh().catch(() => { $("personal-entry").textContent = "个人账号 / 记忆"; });
  }
  return {init, signedIn:() => Boolean(user), setResult:result => { latest = result; }};
})();
