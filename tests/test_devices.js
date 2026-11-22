"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
const nodes = new Map();
class Node {
  constructor() { this.open=false; this.handlers={}; this.dataset={}; this.attributes={}; this.textContent=""; }
  addEventListener(name, handler) { this.handlers[name]=handler; }
  setAttribute(name,value) { this.attributes[name]=value; }
  showModal() { this.open=true; }
  close() { this.open=false; this.handlers.close?.(); }
  focus() { this.focused=true; }
  click() { this.handlers.click?.(); }
}
const node=id => {if (!nodes.has(id)) nodes.set(id,new Node()); return nodes.get(id);};
let mounted=null, listener=null, loads=0, resets=0, contexts=0, refreshes=0;
let state={bridge:null};
const controller={getSnapshot:() => state, subscribe:fn => {listener=fn;},
  loadTools:async () => {loads++;}, refreshBusy:() => {refreshes++;},
  resetContext:() => {contexts++;}, reset:() => {resets++;state={bridge:null};listener();}};
const context={document:{getElementById:node},window:{LabConversation:{mountLabConnection:(element,options) => {mounted=options;return controller;}}}};
vm.runInNewContext(fs.readFileSync("web/devices.js","utf8"),context);
const devices=context.window.LabDevices, asked=[];
devices.init({api(){},busy:() => false,addMaterials(){},ask:(...args) => asked.push(args)});
assert.equal(node("devices-entry-label").textContent,"连接电脑");
node("devices-entry").click();
assert.equal(node("lab-connection").open,true);
assert.equal(loads,1,"Opening the independent module refreshes the connection");
node("devices-entry").click(); assert.equal(loads,1);
state={bridge:{state:"online"}};listener();
assert.equal(node("devices-entry-label").textContent,"我的电脑 · 已连接");
node("devices-close").click();
assert.equal(node("lab-connection").open,false);
assert.equal(resets,0,"Closing the window must preserve the connection");
assert.equal(node("devices-entry").attributes["aria-expanded"],"false");
assert.equal(node("devices-entry").focused,true);
devices.resetContext();assert.equal(contexts,1);assert.equal(state.bridge.state,"online");
node("devices-entry").click();mounted.ask("核对新输出","verify");
assert.equal(node("lab-connection").open,false,"Collect and verify returns to the diagnosis");
assert.deepEqual(asked,[["核对新输出","verify"]]);
let accountOpened=false;node("personal-entry").handlers.click=() => {accountOpened=true;};
node("devices-entry").click();node("devices-account").click();
assert.equal(accountOpened,true);assert.equal(node("lab-connection").open,false);
devices.refreshBusy();assert.equal(refreshes,1);
devices.reset();assert.equal(resets,1);assert.equal(node("devices-entry-label").textContent,"连接电脑");
console.log("Devices module passed: independent entry, refresh, focus, account link, context and owner lifecycle");
