"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path"),vm=require("node:vm");
const source=fs.readFileSync(path.join(__dirname,"../web/student.js"),"utf8");
const section=(from,to)=>source.slice(source.indexOf(from),source.indexOf(to));
class Node {
  constructor(tag="div",text="",cls=""){this.tag=tag;this._text=text;this.className=cls;this.children=[];this.attributes={};this.events={};this.dataset={};this.value="";this.hidden=false;this.scrollTop=0;this.scrollHeight=0;this.clientHeight=200;}
  get textContent(){return this._text+this.children.map(child=>child.textContent).join("");}
  set textContent(value){this._text=String(value);this.children=[];}
  get classList(){return {contains:key=>this.className.split(" ").includes(key),add:key=>{if(!this.classList.contains(key))this.className+=" "+key;}};}
  append(...nodes){for(const node of nodes){node.parent=this;this.children.push(node);}}
  replaceChildren(...nodes){this._text="";this.children=[];this.append(...nodes);}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(child=>child!==this);}
  setAttribute(key,value){this.attributes[key]=value;}
  addEventListener(key,fn){this.events[key]=fn;}
  click(){return this.events.click?.();}
  get lastElementChild(){return this.children.at(-1);}
  get firstElementChild(){return this.children[0];}
  descendants(){return this.children.flatMap(node=>[node,...node.descendants()]);}
  querySelector(selector){
    let matches=[this];
    for(const part of selector.split(" "))matches=matches.flatMap(node=>node.descendants()).filter(node=>part.startsWith(".")?node.classList.contains(part.slice(1)):node.tag===part);
    return matches[0]||null;
  }
}
function fixture(){
  const nodes=new Map(),intervals=new Map();let now=1000,next=0,messages=0,submissions=0;
  const $=id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id);};
  let pending=null;
  function paintPending(){
    const container=$("conversation");container.querySelector(".chat-transient")?.remove();
    const row=new Node("div",pending.text,"chat-turn chat-transient");
    const delivery=new Node("span",pending.error?(pending.delivered?"回复未完成":"发送未完成"):(pending.delivered?"已发送":"发送中…"),"chat-delivery");
    const answer=new Node("div","","chat-assistant");row.append(delivery,answer);
    if(pending.error){
      answer.append(new Node("p",pending.error));
      const retry=new Node("button","重新发送","chat-retry");retry.events.click=pending.onRetry;answer.append(retry);
    }else{
      const hint=new Node("p",pending.hint||"","pending-hint");hint.hidden=!pending.hint;
      answer.append(new Node("span",pending.message,"pending-text"),new Node("span","","typing-dots"),hint);
    }
    container.append(row);
  }
  const conversationView={
    removeLastFailure(){},
    getDraft:()=>$("followup-message").value,
    setDraft:(_,text)=>{$("followup-message").value=text;},
    beginPending:(_,input)=>{pending={...input,message:"正在发送请求…",delivered:false,hint:null};paintPending();},
    updatePending:(_,changes)=>{if(pending){pending={...pending,...changes};paintPending();}},
    failPending:(_,error,onRetry)=>{pending={...pending,error,onRetry};paintPending();},
    renderProgress:(container,entries)=>{container.replaceChildren(...entries.map(entry=>{const row=new Node("li",entry.message);row.dataset.status=entry.status;return row;}));}
  };
  const context=vm.createContext({$,sessionId:null,requestFailed:false,activeRequest:null,requestTimer:null,busy:false,
    materials:new Map([["R1.cfg","sample"]]),Date:class extends Date {static now(){return now;}},
    element:(tag,text,cls)=>new Node(tag,text||"",cls||""),setBusy:value=>{context.busy=value;},dismissToast(){},
    message:()=>messages++,setInterval:fn=>{intervals.set(++next,fn);return next;},clearInterval:id=>intervals.delete(id),
    window:{LabConversation:conversationView,LabExecution:require("../web/execution-state.js"),LabWorkspace:{showDiagnosis(){}},LabHistory:{renderTurns(){ $("conversation").scrollTop=900;}}},
    requestPayload:()=>({configs:{sample:"test"}}),showResult(){},diagnose:async()=>({llm:{status:"ok"}})});
  $("analyze").events.click=()=>submissions++;
  vm.runInContext(section("function receiveProgress(","async function diagnose("),context);
  vm.runInContext(section("function pendingTurn(","function renderChecks("),context);
  return {$,context,run:code=>vm.runInContext(code,context),advance(ms){now+=ms;for(const fn of intervals.values())fn();},
    get messages(){return messages;},get submissions(){return submissions;},get timers(){return intervals.size;}};
}
async function main(){
  {
    const p=fixture();p.run('beginRequest("analyze","请检查我上传的材料")');
    assert.equal(p.$("analysis-result").hidden,false,"first diagnosis must immediately appear as a conversation");
    assert.match(p.$("conversation").textContent,/请检查我上传的材料/);
    assert.equal(p.$("conversation").querySelector(".chat-delivery").textContent,"发送中…");
    assert.equal(p.$("processing-progress").open,false);
    assert.equal(p.messages,0,"sending must not create a duplicate toast");
    p.run('receiveProgress({type:"progress",stage:"received",message:"请求已收到"})');
    assert.equal(p.$("conversation").querySelector(".chat-delivery").textContent,"已发送");
    p.run('receiveProgress({type:"progress",stage:"cloud_connecting",message:"正在连接华为云诊断服务…"})');
    assert.match(p.$("conversation").querySelector(".pending-text").textContent,/等待回复/);
    p.advance(21000);assert.equal(p.$("conversation").querySelector(".pending-hint").hidden,false);
    p.run('receiveProgress({type:"heartbeat"})');p.advance(1000);
    assert.match(p.$("conversation").querySelector(".pending-hint").textContent,/仍在处理/);
    assert.match(p.$("conversation").querySelector(".pending-text").textContent,/等待回复/,"heartbeat must not invent a completed stage");
    p.run('finishRequest("error","服务连接中断")');
    assert.equal(p.timers,0);assert.equal(p.$("conversation").querySelector(".typing-dots"),null);
    assert.match(p.$("conversation").textContent,/服务连接中断/);
    await p.$("conversation").querySelector(".chat-retry").click();assert.equal(p.submissions,1,"first-round failures must be retryable");
  }
  {
    const p=fixture();let reject; p.context.diagnose=()=>new Promise((_,b)=>{reject=b;});
    p.$("followup-message").value="解释一下";
    const request=p.run('performInteraction("ask","解释一下")');
    assert.equal(p.$("followup-message").value,"");
    p.$("followup-message").value="这是等待期间写的新草稿";
    const timeout=new Error("The operation was aborted due to timeout");timeout.name="TimeoutError";
    reject(timeout);await request;
    assert.equal(p.$("followup-message").value,"这是等待期间写的新草稿");
    assert.equal(p.context.busy,false);assert.equal(p.context.activeRequest,null);assert.equal(p.timers,0);
    assert.equal(p.$("conversation").children.length,1,"failure must update the pending message in place");
    assert.match(p.$("conversation").textContent,/等待回复超时/);
  }
  {
    const p=fixture();let resolve;p.context.diagnose=()=>new Promise(a=>{resolve=a;});
    p.$("followup-message").value="相同文字";const request=p.run('performInteraction("ask","相同文字")');
    p.$("followup-message").value="相同文字";resolve({llm:{status:"ok"}});await request;
    assert.equal(p.$("followup-message").value,"相同文字","new drafts identical to the previous message must survive a successful reply");
  }
  {
    const p=fixture(),c=p.$("conversation");c.scrollTop=100;c.scrollHeight=1000;c.clientHeight=200;
    p.run("renderConversation([])");assert.equal(c.scrollTop,100);assert.equal(p.$("chat-new-reply").hidden,false);
    c.scrollTop=790;p.run("renderConversation([])");assert.equal(c.scrollTop,900,"following the bottom should continue showing new replies");
  }
  {
    const p=fixture(),calls=[];p.context.sessionId="a".repeat(32);
    p.context.materials.set("R1.cfg","new configuration and fresh test output");
    p.context.requestPayload=()=>({session_id:p.context.sessionId,configs:Object.fromEntries(p.context.materials)});
    p.context.diagnose=async input=>{calls.push(input);return {llm:{status:"ok"}};};
    await p.run('performInteraction("verify","核对修改后的新结果")');
    assert.equal(calls[0].session_id,"a".repeat(32));
    assert.equal(calls[0].configs["R1.cfg"],"new configuration and fresh test output");
    assert.equal(calls[0].interaction.type,"verify");
    assert.equal(p.context.sessionId,"a".repeat(32),"retests must keep the active conversation");
  }
  {
    const p=fixture();p.run('beginRequest("ask","current request")');
    p.run('receiveProgress({type:"progress",turn_id:"turn-a",sequence:1,message:"reading",item:{id:"read",status:"running"}})');
    p.run('receiveProgress({type:"progress",turn_id:"turn-b",sequence:99,message:"wrong turn"})');
    assert.equal(p.$("request-status-text").textContent,"reading");
    p.run('receiveProgress({type:"progress",turn_id:"turn-a",sequence:2,message:"read failed",item:{id:"read",status:"failed"}})');
    assert.equal(p.$("processing-events").children.length,1,"update each tool in place");
    assert.equal(p.$("processing-events").children[0].dataset.status,"failed");
    const previous=p.context.activeRequest;
    p.run('beginRequest("ask","new request")');
    p.context.oldRequest=previous;
    p.run('receiveProgress({type:"progress",turn_id:"turn-a",sequence:3,message:"late event"},oldRequest)');
    assert.equal(p.$("processing-events").children.length,0,"ignore events captured by the old request");
  }
  for (const fail of [false,true]) {
    const p=fixture();let resolve,reject,shown=0;
    p.context.showResult=()=>shown++;
    p.context.diagnose=()=>new Promise((a,b)=>{resolve=a;reject=b;});
    const work=p.run('performInteraction("ask","old request")');
    p.run('beginRequest("ask","replacement request")');
    const current=p.context.activeRequest;
    if(fail)reject(new Error("old failure"));else resolve({llm:{status:"ok"}});
    await work;
    assert.equal(shown,0,"late responses must not replace the current conversation");
    assert.equal(p.context.activeRequest,current,"old completion must not clear current request state");
    assert.equal(p.context.busy,true);
  }
  console.log("Chat requests passed: first-message feedback, acknowledgements, heartbeat waits, inline retry, preserved drafts and review scroll position");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
