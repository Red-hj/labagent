"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../web/task-control.js'),'utf8');
const id='c'.repeat(32),result={session_id:'d'.repeat(32),llm:{status:'ok'},execution:{turn_id:id,status:'completed'}};
const deferred=()=>{let resolve;const promise=new Promise(yes=>resolve=yes);return {promise,resolve};};
function fixture({storage=new Map(), snapshots=[], fetcher}={}) {
  const button={hidden:true,disabled:false,events:{},addEventListener(name,fn){this.events[name]=fn;}};
  const requests=[],events=[],waiting=[];
  const context=vm.createContext({TextDecoder,AbortSignal,crypto:{randomUUID:()=>id},
    document:{getElementById:()=>button},sessionStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
    setTimeout:fn=>{queueMicrotask(fn);return 1;},fetch:async(...args)=>{requests.push(args);return fetcher(...args);}});
  vm.runInContext(source,context);
  context.LabTasks.init({headers:()=>({}),progress:event=>events.push(event),waiting:text=>waiting.push(text),
    api:async(path,body)=>{requests.push([path,body]);const next=snapshots.shift();if(next instanceof Error)throw next;return typeof next==='function'?next(path,body):next;}});
  return {api:context.LabTasks,button,requests,events,waiting,storage};
}
function stream(read) {return {ok:true,body:{getReader:()=>({read,releaseLock(){}})}};}
const bytes=event=>new TextEncoder().encode(JSON.stringify(event)+'\n');
async function main() {
  {
    let reads=0;
    const f=fixture({snapshots:[{state:'running',events:[]},{state:'completed',events:[],result}],fetcher:async()=>stream(async()=>{
      if(reads++===0)return {done:false,value:bytes({type:'progress',turn_id:id,sequence:1,message:'started'})};
      throw new TypeError('network disconnected');
    })});
    assert.equal(await f.api.run({configs:{R1:'sample'}},{}),result);
    assert.equal(f.requests.filter(([path])=>path==='/api/diagnose/stream').length,1,'Network recovery must not resend the diagnosis');
    assert.equal(f.requests.filter(([path])=>path.includes('?status_only=1')).length,2);
    assert.equal(f.storage.size,0);assert.equal(f.button.hidden,true);
  }
  {
    const storage=new Map([['labagent-active-task:personal',id]]);
    const snapshot={id,state:'running',input:{configs:{R1:'saved input'}},events:[]};
    const f=fixture({storage,snapshots:[snapshot,{state:'completed',events:[],result}],fetcher:()=>assert.fail('Reload must not submit a new diagnosis')});
    const saved=await f.api.restore();assert.equal(saved.input.configs.R1,'saved input');
    assert.equal(await f.api.watch(saved.id,{},saved),result);
    assert.ok(f.requests.every(([,body])=>body===undefined));assert.equal(storage.size,0);
  }
  {
    const barrier=deferred();let reads=0;
    const f=fixture({snapshots:[{state:'cancelling'}],fetcher:async()=>stream(async()=>{
      if(reads++===0)return {done:false,value:bytes({type:'progress',turn_id:id,sequence:1,message:'started'})};
      return barrier.promise;
    })});
    const running=f.api.run({configs:{R1:'sample'}},{});
    const rejected=assert.rejects(running,error=>error.code==='task_stopped');
    await Promise.resolve();await Promise.resolve();
    await f.button.events.click();assert.equal(f.button.disabled,true);assert.equal(f.button.textContent,'正在停止…');
    assert.equal(f.requests[1][0],'/api/tasks/'+id+'/cancel');
    barrier.resolve({done:false,value:bytes({type:'cancelled',message:'已停止',turn_id:id,sequence:2})});
    await rejected;
    assert.equal(f.button.hidden,true);assert.equal(f.storage.get('labagent-active-task:personal'),id,'A stopped first request keeps its input recoverable');
  }
  {
    const denied=new Error('not found');denied.status=404;
    const f=fixture({storage:new Map([['labagent-active-task:personal',id]]),snapshots:[denied]});
    await assert.rejects(f.api.restore(),/not found/);assert.equal(f.storage.size,0);
  }
  {
    const f=fixture({fetcher:async()=>({ok:false,json:async()=>({error:'invalid materials'})})});
    await assert.rejects(f.api.run({},{}),/invalid materials/);assert.equal(f.requests.length,1);assert.equal(f.storage.size,0);
  }
  {
    const studentSource=fs.readFileSync(require('node:path').join(__dirname,'../web/student.js'),'utf8');
    const restoreBlock=studentSource.slice(studentSource.indexOf('async function restoreTask('),studentSource.indexOf('async function start('));
    const submissionBlock=studentSource.slice(studentSource.indexOf('function restoreSubmittedMaterials('),
      studentSource.indexOf('function commandOutputMaterials('));
    const nodes=new Map();
    const $=id=>{if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',hidden:false,className:'',setAttribute(){},replaceChildren(){},append(){}});return nodes.get(id);};
    const materials=new Map(), recordings=new Map(), screenshots=new Map();
    let watched=0, previews=0;
    const context=vm.createContext({$,URLSearchParams,location:{search:'?session='+'e'.repeat(32)},
      materials,commandOutputs:[],experimentInputs:{},recordings,screenshots,
      defaultQuestion:'default',knowledgeScope:'default',evidenceKind:'user_submitted',
      activeRequest:null,requestTimer:null,roomHeaders:()=>({}),renderRecordingChoices(){},renderMaterials(){},previewOperations:async()=>{previews++;},
      beginRequest:()=>({}),finishRequest(){},showResult(){},message(){},setBusy(){},clearInterval(){},
      api:async path=>({id:'img1',kind:'image',material_name:'screen.txt',name:'screen.png',image_url:'/api/recordings/img1/image'}),
      window:{LabTasks:{restore:async()=>({id,session_id:null,input:{configs:{'screen.txt':'ocr text'},recordings:['img1'],question:'check',
          command_outputs:[{device:'R1',command:'ip route',text:'route data',complete:true,config_fingerprint:'bound'}]}}),watch:async()=>{watched++;return {};}}}});
    vm.runInContext(submissionBlock + restoreBlock,context);
    assert.equal(await vm.runInContext('restoreTask()',context),false,'Selected historical session must not be overwritten by an unrelated first task');
    assert.equal(watched,0);
    context.location.search='';
    assert.equal(await vm.runInContext('restoreTask()',context),true,'First task without URL session should restore configs and media');
    assert.equal(materials.get('screen.txt'),'ocr text');
    assert.equal(context.commandOutputs[0].text,'route data');
    assert.equal(context.commandOutputs[0].complete,true);
    assert.equal(materials.size,1);
    assert.equal(screenshots.get('img1').name,'screen.png');
    assert.equal(previews,1);
  }
  console.log('Task controls passed: stop acknowledgement, stream recovery, refresh without replay, account isolation and retained interrupted input');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
