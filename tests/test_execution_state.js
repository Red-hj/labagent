"use strict";
const assert = require("node:assert/strict");
const {initial, reduce} = require("../web/execution-state.js");
const event = (sequence, changes = {}) => ({type:"progress", turn_id:"turn-a", session_id:"session-a", sequence,
  stage:"tool", message:"正在检查", ...changes});
let state = reduce(initial("session-a"), event(1, {event:"turn.started"}));
const old = state;
assert.equal(reduce(state, event(2, {turn_id:"turn-b"})), state);
assert.equal(reduce(state, event(2, {session_id:"session-b"})), state);
assert.equal(reduce(state, event(1)), state);
state = reduce(state, event(2, {item:{id:"one", tool:"read_source", status:"running"}}));
state = reduce(state, event(3, {item:{id:"two", tool:"retrieve_reference", status:"running"}}));
state = reduce(state, event(4, {message:"读取失败", item:{id:"one", tool:"read_source", status:"failed"}}));
assert.equal(state.items.one.status, "failed");
assert.equal(state.items.two.status, "running");
assert.equal(state.entries.filter(row => row.key === "item:one").length, 1);
assert.equal(old.entries.length, 1, "state transitions must leave prior snapshots intact");
assert.equal(reduce(state, event(5, {item:{id:"one", status:"running"}})), state, "late updates cannot reopen finished tools");
state = reduce(state, event(6, {item:{id:"two", tool:"retrieve_reference", status:"completed"}}));
state = reduce(state, event(7, {type:"result", result:{execution:{status:"completed"}, status:"needs_information"}}));
assert.equal(state.status, "completed");
assert.equal(state.experimentStatus, "needs_information", "a completed turn can still require experiment evidence");
assert.equal(reduce(state, event(8)), state, "completed turns reject late progress");
let fresh = initial();
fresh = reduce(fresh, {type:"progress", stage:"received", message:"兼容旧服务"});
fresh = reduce(fresh, {type:"result", result:{status:"needs_information"}});
assert.equal(fresh.status, "completed");
assert.equal(fresh.turnId, null);
for (let index = 1, bounded = initial(); index <= 20; index++) {
  bounded = reduce(bounded, event(index, {message:"进度" + index}));
  assert.ok(bounded.entries.length <= 8);
}
const failure = reduce(initial(), event(1, {type:"error", message:"请求失败"}));
assert.equal(failure.status, "failed");
console.log("Execution state passed: interleaved tools, stale events, immutable state, legacy streams and independent experiment outcomes");
