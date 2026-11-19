"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const student = fs.readFileSync(path.join(__dirname,"../web/student.js"),"utf8");
const history = fs.readFileSync(path.join(__dirname,"../web/session-history.js"),"utf8");
const part = (from,to) => student.slice(student.indexOf(from),student.indexOf(to));
const deferred = () => { let resolve,reject; const promise = new Promise((a,b)=>{resolve=a;reject=b;}); return {promise,resolve,reject}; };

async function demoChecks() {
  const response = deferred(), nodes = new Map(), notes = [];
  const $ = id => { if (!nodes.has(id)) nodes.set(id,{value:id==="demo-case"?"operation_history":"",textContent:""}); return nodes.get(id); };
  let calls=0, focused=0;
  const context=vm.createContext({$, Map, busy:false, materialLoading:false, sessionId:null, materials:new Map(), recordings:new Map(),
    measuredDemos:new Map(), api:()=>{calls++;return response.promise;}, setBusy(value){context.busy=value||context.materialLoading;},
    message:text=>notes.push(text), renderMaterials(){},renderRecordingChoices(){},previewOperations:async()=>{},
    materialChanged:text=>notes.push(text), window:{LabWorkspace:{resultsAvailable:()=>focused++}}});
  vm.runInContext(part("async function loadDemo(","function renderTools("),context);
  const first=vm.runInContext("loadDemo()",context);
  assert.equal(context.busy,true);
  await vm.runInContext("loadDemo()",context);
  assert.equal(calls,1,"double click must not issue a second load");
  response.resolve({materials:{"终端操作.log":"[00:03] <R1> ping 10.0.0.2"}});
  await first;
  assert.equal(context.busy,false);
  assert.equal(context.materials.size,1);
  assert.equal(focused,1,"replay must open when its example finishes loading");
  context.materials.clear(); context.api=async()=>({stages:[]}); $("demo-case").value="ospf_goals";
  await vm.runInContext("loadDemo()",context);
  assert.equal(context.materials.size,0,"incomplete response must not partially change materials");
  assert.equal(context.busy,false);
  assert.match(notes.at(-1),/失败/);
}

async function previewChecks() {
  const slow=deferred(), histories=[], repairs=[], messages=[];
  const context=vm.createContext({previewNumber:0, materials:new Map([["log","<R1> bad command"]]),
    api:route=>route.includes("operations")?slow.promise:Promise.reject(new Error("temporary failure")),
    renderOperationHistory:value=>histories.push(value), renderRepairComparisons:value=>repairs.push(value),message:text=>messages.push(text)});
  vm.runInContext(part("async function previewOperations(","function renderRepairComparisons("),context);
  const first=vm.runInContext("previewOperations()",context);
  slow.resolve({event_count:2}); await first;
  assert.equal(histories.at(-1).event_count,2,"unavailable comparison must not discard an available timeline");
  assert.match(messages.at(-1),/修复对照/);
  const stale=deferred(); context.api=()=>stale.promise;
  const pending=vm.runInContext("previewOperations()",context);
  context.materials.clear(); await vm.runInContext("previewOperations()",context);
  stale.resolve({event_count:99,comparisons:["stale"]}); await pending;
  assert.equal(histories.at(-1).event_count,undefined,"old preview must not reappear after materials are cleared");
  assert.equal(repairs.at(-1).length,0);
}

async function replayChecks() {
  let played=0,located=0;
  const nodes=new Map(), video={readyState:1,duration:15,play:()=>{played++;return Promise.resolve();},scrollIntoView(){}};
  const $=id=>id==="replay-video"?video:(nodes.has(id)?nodes.get(id):(nodes.set(id,{}),nodes.get(id)));
  const context=vm.createContext({$,replaySeek:0, recordings:new Map([["demo",{id:"demo",video_url:"/demo"}]]),
    renderRecordingChoices(){},message(){},locateCitation:()=>located++});
  vm.runInContext(part("function locateOperation(","function renderOperationHistory("),context);
  vm.runInContext('locateOperation({material:"终端操作.log",seconds:3,time_label:"00:03"})',context);
  assert.equal(video.currentTime,3); assert.equal(played,1,"clicking an operation should start playback");
  vm.runInContext('locateOperation({material:"another-device.log",seconds:8,evidence:[{}]})',context);
  assert.equal(video.currentTime,3,"a different log must not seek the selected unrelated video");
  assert.equal(located,1);
  video.readyState=0;
  const waits=[]; video.addEventListener=(event,fn)=>waits.push(fn);
  vm.runInContext('locateOperation({material:"终端操作.log",seconds:4,time_label:"00:04"})',context);
  vm.runInContext('locateOperation({material:"终端操作.log",seconds:6,time_label:"00:06"})',context);
  waits[0](); assert.equal(video.currentTime,3,"stale pending seek must not move a newly selected position");
  waits[1](); assert.equal(video.currentTime,6);
}

function historyChecks() {
  const nodes=new Map(), events={}, saved=new Map(), media={matches:false,addEventListener:(type,fn)=>{media.changed=fn;}};
  class Node {
    constructor(){this.hidden=false;this.attributes={};this.events={};this.classes=new Set();this.classList={toggle:(key,on)=>on?this.classes.add(key):this.classes.delete(key),contains:key=>this.classes.has(key)};}
    setAttribute(key,value){this.attributes[key]=value;} removeAttribute(key){delete this.attributes[key];}
    addEventListener(key,fn){this.events[key]=fn;} focus(){this.focused=true;}
  }
  const $=id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id);};
  const surfaces=new Map([['.app-layout',new Node()],['main',new Node()],['header',new Node()]]);
  const context=vm.createContext({window:{},document:{getElementById:$,querySelector:selector=>surfaces.get(selector)||null,
    addEventListener:(type,fn)=>events[type]=fn},matchMedia:()=>media,
    localStorage:{getItem:key=>saved.get(key),setItem:(key,value)=>saved.set(key,value)}});
  vm.runInContext(history,context); context.window.LabHistory.init({});
  assert.equal($("session-sidebar").hidden,false);
  const key=extra=>({key:"b",ctrlKey:true,preventDefault(){this.prevented=true;},...extra});
  const event=key(); events.keydown(event);
  assert.equal(event.prevented,true); assert.equal($("session-sidebar").hidden,true);
  assert.equal(surfaces.get('.app-layout').classes.has('history-collapsed'),true);
  assert.equal(saved.get('labagent-history-hidden'),'true');
  events.keydown(key({shiftKey:true})); assert.equal($("session-sidebar").hidden,true);
  $("toggle-session-history").events.click(); assert.equal($("session-sidebar").hidden,false);
  media.matches=true; media.changed(); assert.equal($("session-sidebar").hidden,true);
  events.keydown(key()); assert.equal(surfaces.get('main').inert,true);
  assert.equal($("history-backdrop").hidden,false);
  $("close-session-history").events.click(); assert.equal(surfaces.get('main').inert,false);
  media.matches=false; media.changed(); assert.equal($("session-sidebar").hidden,false,"mobile close must not overwrite desktop preference");
}

(async()=>{await demoChecks();await previewChecks();await replayChecks();historyChecks();
  console.log("Workspace interactions passed: load races, partial previews, stale results, matched video seek/play and responsive Ctrl+B sidebar");
})().catch(error=>{console.error(error);process.exitCode=1;});
