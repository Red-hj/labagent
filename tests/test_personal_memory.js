"use strict";
// Exercise the shipped account/memory interactions against a small DOM and API fixture.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const html = fs.readFileSync(path.join(__dirname, "../web/student.html"), "utf8");
const script = fs.readFileSync(path.join(__dirname, "../web/personal-memory.js"), "utf8");
class Node {
  constructor() { this.value=""; this.textContent=""; this.hidden=false; this.checked=false; this.children=[]; this.events={}; this.dataset={}; this.attributes={}; }
  setAttribute(name,value) { this.attributes[name]=value; }
  addEventListener(event, fn) { this.events[event]=fn; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=children; }
  focus() { this.focused=true; }
  showModal() { this.open=true; }
  close() { this.open=false; this.events.close?.(); }
}
const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map(match=>[match[1], new Node()]));
const $ = id => { assert.ok(elements.has(id), "Missing page element: "+id); return elements.get(id); };
$("personal-register-memory").checked=true;
$("personal-memory-form").reset=()=>{
  $("personal-memory-kind").value="preference"; $("personal-memory-title").value=""; $("personal-memory-content").value="";
};
let user=null, memories=[], calls=[], resets=0, history=0, shared=false, busy=false, authBarrier=null, authError=null;
const editor=new Node();
async function api(route, body, options={}) {
  calls.push({route, body, method:options.method});
  if (route==="/api/account") return {user};
  if (/\/api\/account\/(register|login)$/.test(route)) {
    if (authBarrier) await authBarrier;
    if (authError) throw authError;
    user={username:body.username, display_name:body.display_name || body.username, memory_enabled:body.memory_enabled ?? true};
    return {user};
  }
  if (route==="/api/account/logout") { user=null; return {signed_out:true}; }
  if (route==="/api/account/memory") { user.memory_enabled=body.enabled; return {enabled:body.enabled}; }
  if (route==="/api/memories" && options.method==="DELETE") { memories=[]; return {deleted:1}; }
  if (route.startsWith("/api/memories/") && options.method==="DELETE") { memories=memories.filter(m=>m.id!==route.split("/").at(-1)); return {deleted:1}; }
  if (route==="/api/memories" && body) {
    const record={...body, id:body.id || String(memories.length+1), occurrences:1};
    memories=memories.filter(m=>m.id!==record.id); memories.push(record); return {memory:record};
  }
  if (route==="/api/memories") return {enabled:user.memory_enabled, memories:memories.map(m=>({...m}))};
  throw new Error("Unexpected API: "+route);
}
const context=vm.createContext({window:{}, document:{getElementById:$, querySelector:()=>editor}});
vm.runInContext(script,context);
const personal=context.window.LabPersonal;
personal.init({api, element:(tag,text)=>Object.assign(new Node(),{tag,textContent:text}), message:()=>{}, busy:()=>busy,
  newConversation:()=>{resets++;}, refreshHistory:async()=>{history++;}, question:()=>"OSPF邻居未建立", shared:()=>shared});
const settle=()=>new Promise(resolve=>setImmediate(resolve));
async function fire(id,event="click") { await $(id).events[event]({preventDefault(){}}); await settle(); }

async function main() {
  await settle(); assert.equal($("personal-entry").textContent,"登录 / 记忆");
  await fire("personal-entry"); assert.equal($("personal-dialog").open,true);
  assert.equal($("personal-title").textContent,"继续你的排查");
  assert.equal($("personal-dialog").dataset.view,"auth");
  $("personal-password").type="password"; $("personal-password").value="do-not-retain";
  await fire("personal-password-toggle"); assert.equal($("personal-password").type,"text");
  assert.equal($("personal-password-toggle").attributes["aria-pressed"],"true");
  await fire("personal-close"); assert.equal($("personal-password").value,"");
  assert.equal($("personal-password").type,"password");
  assert.equal($("personal-password-toggle").attributes["aria-pressed"],"false");
  await fire("personal-entry");
  await fire("personal-auth-switch"); assert.equal($("personal-display-row").hidden,false);
  assert.equal($("personal-username-hint").hidden,false);
  assert.equal($("personal-title").textContent,"创建你的工作区");
  $("personal-username").value="alice"; $("personal-password").value="example-password"; $("personal-display").value="小黄";
  await fire("personal-auth","submit");
  assert.equal($("personal-entry").textContent,"小黄 · 记忆");
  assert.equal($("personal-dialog").dataset.view,"memory");
  assert.equal($("personal-auth-note").hidden,true);
  assert.equal($("personal-data-note").hidden,false);
  assert.equal($("personal-password").value,""); assert.equal(resets,1); assert.equal(history,1);
  $("personal-memory-kind").value="preference"; $("personal-memory-title").value="排查偏好"; $("personal-memory-content").value="先说明原因。";
  await fire("personal-memory-form","submit");
  assert.equal(memories.length,1); assert.equal($("personal-memory-list").children.length,1);
  const card=$("personal-memory-list").children[0], actions=card.children.at(-1);
  await actions.children[0].events.click();
  assert.equal(editor.open,true); assert.equal($("personal-memory-title").value,"排查偏好");
  $("personal-memory-content").value="先给检查步骤。"; await fire("personal-memory-form","submit");
  assert.equal(memories.length,1); assert.equal(memories[0].content,"先给检查步骤。");
  $("personal-enabled").checked=false; await fire("personal-enabled","change");
  assert.equal(user.memory_enabled,false); assert.equal(memories.length,1);
  personal.setResult({investigation:{summary:"核对OSPF区域。"}}); await fire("personal-from-current");
  assert.equal($("personal-memory-title").value,"OSPF邻居未建立");
  shared=true; await fire("personal-from-current"); assert.match($("personal-error").textContent,/个人排查/); shared=false;
  const removeActions=$("personal-memory-list").children[0].children.at(-1);
  await removeActions.children[1].events.click(); assert.equal(memories.length,1);
  await removeActions.children[1].events.click(); await settle(); assert.equal(memories.length,0);
  busy=true; await fire("personal-logout"); assert.ok(user); busy=false;
  await fire("personal-logout"); assert.equal(user,null); assert.equal($("personal-panel").hidden,true);
  assert.equal($("personal-memory-list").children.length,0); assert.equal($("personal-memory-content").value,"");
  assert.equal(calls.filter(c=>c.route==="/api/account/register").length,1);
  assert.equal($("personal-title").textContent,"继续你的排查");
  $("personal-username").value="alice"; $("personal-password").value="wrong-password";
  let release; authBarrier=new Promise(resolve=>{release=resolve;}); authError=new Error("用户名或密码不正确。");
  const pending=$("personal-auth").events.submit({preventDefault(){}});
  assert.equal($("personal-auth-submit").textContent,"正在登录…");
  assert.equal($("personal-auth-submit").disabled,true);
  assert.equal($("personal-auth-switch").disabled,true);
  assert.equal($("personal-auth").attributes["aria-busy"],"true");
  await fire("personal-auth-switch"); assert.equal($("personal-display-row").hidden,true);
  release(); await pending;
  assert.equal($("personal-auth-submit").textContent,"登录");
  assert.equal($("personal-auth-submit").disabled,false);
  assert.equal($("personal-username").disabled,false);
  assert.equal($("personal-auth").attributes["aria-busy"],"false");
  assert.equal($("personal-error").textContent,"用户名或密码不正确。");
  assert.equal($("personal-password").value,"wrong-password");
  console.log("Personal UI flows passed: account/memory flows, password visibility and clearing, pending mode guard, failed-login recovery");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
