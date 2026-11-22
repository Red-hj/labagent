"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
class Node {
  constructor() { this.value = "1"; this.hidden = false; this.children = []; this.handlers = {}; this.classList = {toggle() {}}; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
}
const nodes = new Map();
const node = id => { if (!nodes.has(id)) nodes.set(id, new Node()); return nodes.get(id); };
const context = {window:{}, document:{getElementById:node, createElement:() => new Node(), querySelectorAll:() => []}};
vm.runInNewContext(fs.readFileSync("web/learning.js", "utf8"), context);
const learning = context.window.LabLearning;
(async () => {
  let granted = false, asked = [];
  const options={busy:() => false, ask:(...args) => asked.push(args), addMaterials:() => {},
    api:async (path, input) => {
      if (path === "/api/learning") return {enabled:true, solution_unlocked:granted, can_unlock:true, mcp_configured:true, bridge_available:true};
      if (path === "/api/learning/unlock") { assert.equal(input.code, "teacher-code"); granted = true; return {solution_unlocked:true}; }
      throw new Error("Unexpected request " + path);
    }};
  await learning.init(options);
  assert.equal(learning.enabled(), true);
  assert.equal(learning.apply({configs:{}}).learning.level, 1);
  node("learning-level").value = "3"; node("learning-level").handlers.change();
  assert.equal(learning.apply({}).learning.level, 1, "Changing the selector cannot unlock solutions");
  node("learning-more").handlers.click();
  assert.equal(learning.apply({}).learning.level, 2); assert.equal(asked.length, 1);
  learning.show({investigation:{learning:{level:3,current_step:"已保存的解答",expected_observation:"观察",question:"结果？"}}});
  assert.equal(learning.apply({}).learning.level, 2, "History cannot grant access");
  node("learning-code").value = "teacher-code";
  await node("learning-unlock-form").handlers.submit({preventDefault(){}});
  assert.equal(node("learning-code").value, "", "Clear the authorization code after submission");
  assert.equal(node("learning-solution-option").disabled, false);
  granted = false; learning.reset(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(node("learning-solution-option").disabled, true);
  assert.equal(node("learning-current").hidden, true);
  console.log("Learning controls passed independently: progressive hints, account-bound access refresh, history and code clearing");
})().catch(error => { console.error(error); process.exitCode = 1; });
