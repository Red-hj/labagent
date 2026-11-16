"use strict";
// Exercise the real page feedback code with a controllable clock and small DOM.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname,"../web/student.js"),"utf8");
const feedback = source.slice(0,source.indexOf("async function api("));

function page() {
  let time = 0, nextId = 0;
  const timers = new Map(), elements = new Map(), events = new Map();
  for (const id of ["message","feedback-toast","feedback-toast-text","close-feedback"]) {
    elements.set(id,{hidden:true,textContent:"",className:"",handlers:{},addEventListener(type,action){this.handlers[type]=action;}});
  }
  const context = vm.createContext({document:{getElementById:id=>elements.get(id),addEventListener:(type,action)=>events.set(type,action)},
    setTimeout:(action,delay)=>{const id=++nextId;timers.set(id,{at:time+delay,action});return id;},clearTimeout:id=>timers.delete(id)});
  vm.runInContext(feedback,context);
  return {elements, events, run:code=>vm.runInContext(code,context),
    advance(ms){time+=ms;for(const [id,timer] of [...timers])if(timer.at<=time){timers.delete(id);timer.action();}}};
}

{
  const p=page(); p.run('message("截图没有读到清晰文字",true)');
  p.advance(7999); assert.equal(p.elements.get("feedback-toast").hidden,false);
  p.advance(1); assert.equal(p.elements.get("feedback-toast").hidden,true);
  assert.equal(p.elements.get("message").textContent,"截图没有读到清晰文字");
}
{
  const p=page(); p.run('busy=true; message("正在读取材料")');
  p.advance(4500); assert.equal(p.elements.get("feedback-toast").hidden,true);
}
{
  const p=page(); p.run('message("材料已添加")'); p.advance(1000);
  p.run('message("新错误",true)'); p.advance(3500);
  assert.equal(p.elements.get("feedback-toast").hidden,false);
  assert.equal(p.elements.get("feedback-toast-text").textContent,"新错误");
  p.advance(4500); assert.equal(p.elements.get("feedback-toast").hidden,true);
}
{
  const p=page(); p.run('message("错误",true)');
  p.elements.get("close-feedback").handlers.click();
  assert.equal(p.elements.get("feedback-toast").hidden,true);
  p.advance(3000); p.run('message("新提示")'); p.advance(3000);
  assert.equal(p.elements.get("feedback-toast").hidden,false);
}
{
  const p=page(); p.run('message("错误",true)');
  p.events.get("keydown")({key:"Escape"});
  assert.equal(p.elements.get("feedback-toast").hidden,true);
  assert.equal(p.elements.get("message").textContent,"错误");
}
console.log("5 feedback checks passed: timed error, busy state, replacement, close, Escape");
