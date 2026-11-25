"use strict";
(function (root) {
  // Pure state transitions shared by the current page and future React views.
  function initial(sessionId = null) {
    return {turnId:null, sessionId, sequence:0, status:"pending", experimentStatus:null, items:{}, entries:[]};
  }
  function reduce(state, event) {
    if (!event || !["progress", "result", "error", "cancelled"].includes(event.type)) return state;
    if (["completed", "failed", "cancelled"].includes(state.status)) return state;
    if (event.turn_id && state.turnId && event.turn_id !== state.turnId) return state;
    if (event.session_id && state.sessionId && event.session_id !== state.sessionId) return state;
    if (Number.isInteger(event.sequence) && event.sequence <= state.sequence) return state;
    const next = {...state, turnId:event.turn_id || state.turnId, sessionId:event.session_id || state.sessionId,
      sequence:Number.isInteger(event.sequence) ? event.sequence : state.sequence, status:"running"};
    if (event.item?.id) {
      const item = event.item, previous = state.items[item.id];
      if (previous && ["completed", "failed", "cancelled"].includes(previous.status)) return state;
      next.items = {...state.items, [item.id]:{...item}};
    }
    if (event.type === "progress" && typeof event.message === "string") {
      const key = event.item?.id ? "item:" + event.item.id : null;
      const entry = {key, message:event.message, status:event.item?.status || "progress"};
      next.entries = state.entries.slice();
      const index = key ? next.entries.findIndex(row => row.key === key) : -1;
      if (index >= 0) next.entries[index] = entry;
      else if (key || next.entries.at(-1)?.message !== entry.message) next.entries.push(entry);
      next.entries = next.entries.slice(-8);
    }
    if (event.type === "result") {
      next.status = event.result?.execution?.status || "completed";
      next.experimentStatus = event.result?.status || null;
    } else if (event.type === "error") next.status = "failed";
    else if (event.type === "cancelled") next.status = "cancelled";
    return next;
  }
  const api = {initial, reduce};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.LabExecution = api;
})(globalThis);
