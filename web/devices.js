"use strict";
// Computer connections belong to the account; collected evidence belongs to a conversation.
window.LabDevices = (() => {
  const get = id => document.getElementById(id);
  let controller = null, returnAfterSignIn = false;
  function open() {
    const dialog = get("lab-connection");
    if (dialog.open) return;
    dialog.showModal(); get("devices-entry").setAttribute("aria-expanded", "true");
    void controller.loadTools();
  }
  function afterSignIn() {
    if (!returnAfterSignIn || !window.LabPersonal.signedIn()) return;
    returnAfterSignIn = false;
    get("personal-dialog").close(); open();
  }
  function init(options) {
    const dialog = get("lab-connection"), entry = get("devices-entry");
    controller = window.LabConversation.mountLabConnection(get("lab-connection-view"), {
      ...options,
      signedIn:() => window.LabPersonal.signedIn(),
      signIn:() => { returnAfterSignIn = true; dialog.close(); get("personal-entry").click(); },
      ask:(text, type) => { dialog.close(); options.ask(text, type); }
    });
    const showStatus = () => {
      const state = controller.getSnapshot();
      entry.dataset.state = state.bridge?.state || "none";
      get("devices-entry-label").textContent = state.bridge?.state === "online" ? "我的电脑 · 已连接" :
        state.bridge?.state === "waiting" ? "连接电脑 · 待配对" : "连接电脑";
    };
    controller.subscribe(showStatus);
    showStatus();
    entry.addEventListener("click", open);
    get("personal-dialog").addEventListener("close", () => { if (!window.LabPersonal.signedIn()) returnAfterSignIn = false; });
    get("devices-close").addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => {
      entry.setAttribute("aria-expanded", "false"); entry.focus();
    });
    get("devices-account").addEventListener("click", () => {
      dialog.close(); get("personal-entry").click();
    });
  }
  return {init, afterSignIn, refreshBusy:() => controller?.refreshBusy(),
    resetContext:() => controller?.resetContext(), reset:() => controller?.reset()};
})();
