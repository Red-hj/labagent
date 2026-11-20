import assert from "node:assert/strict";
import {mkdir} from "node:fs/promises";
import {createRequire} from "node:module";
import {fileURLToPath} from "node:url";
import {build} from "esbuild-wasm";
import {createElement} from "react";
import {renderToStaticMarkup} from "react-dom/server";

process.chdir(fileURLToPath(new URL("../", import.meta.url)));
await mkdir("runtime/tests", {recursive:true});
await build({entryPoints:["frontend/src/lab-connection.tsx"], outfile:"runtime/tests/lab-connection.cjs",
  bundle:true, platform:"node", format:"cjs", packages:"external"});
const require = createRequire(import.meta.url);
const {createLabConnection, LabConnectionView} = require("../runtime/tests/lab-connection.cjs");
const deferred = () => { let resolve, reject; const promise = new Promise((yes,no) => {resolve=yes; reject=no;}); return {promise,resolve,reject}; };
const tool = {function:{name:"lab_ensp_r1", description:"读取 R1 <接口>", parameters:{properties:{
  device:{enum:["R1","R2"]}, command:{enum:["display ip interface brief","display ospf peer"]}}}}};
function fixture() {
  const calls=[], materials=[], asked=[];
  let busy=false, bridge=null, warnings=[], delayed=null;
  const controller=createLabConnection({busy:() => busy, setCollecting:value => {busy=value;},
    addMaterials:output => materials.push(output), ask:(...args) => asked.push(args),
    api:async (path, input) => {
      calls.push({path,input});
      if (delayed?.path === path) {const result=delayed; delayed=null; return result.promise;}
      if (path === "/api/lab/bridge") {bridge={id:"workstation",name:input.name,state:"waiting"}; return {id:bridge.id,code:"AABBCCDDEEFF0011"};}
      if (path === "/api/lab/bridge/disconnect") {bridge=null; return {};}
      if (path === "/api/lab/tools") return {bridge, warnings, tools:[{...tool,...(bridge ? {bridge_id:bridge.id}: {})}]};
      if (path === "/api/lab/collect") return {materials:{"R1.log":"synthetic observation"}};
      throw Error("Unexpected API "+path);
    }});
  return {controller,calls,materials,asked, state:controller.getSnapshot,
    render:() => renderToStaticMarkup(createElement(LabConnectionView,{controller,state:controller.getSnapshot()})),
    online:() => {bridge={...bridge,state:"online"};}, warn:messages => {warnings=messages;},
    busy:value => {busy=value; controller.refreshBusy();},
    delay:path => {const result=deferred(); delayed={path,...result}; return result;}};
}
const args={device:"R2",command:"display ospf peer"};
{
  const f=fixture();
  await f.controller.loadTools();
  await f.controller.collect(f.state().tools[0],args);
  assert.deepEqual(f.calls.at(-1).input,{tool:tool.function.name,arguments:args},"Server-side MCP tools do not require a bridge id");
  assert.equal(f.materials[0]["R1.log"],"synthetic observation");
  f.controller.check(); assert.equal(f.asked.at(-1)[1],"verify");
  await f.controller.pair("课堂电脑");
  assert.match(f.render(),/连接码：AABBCCDDEEFF0011/);
  f.online(); f.warn(["备用工具离线"]); await f.controller.loadTools();
  assert.equal(f.state().code,null,"Online refresh removes the one-time code");
  assert.match(f.render(),/课堂电脑已连接/); assert.match(f.render(),/备用工具离线/);
  assert.match(f.render(),/读取 R1 &lt;接口&gt;/); assert.match(f.render(),/value="R2"/);
  for (const id of ["lab-note","lab-load-tools","lab-tools","lab-check-step"]) assert.ok(f.render().includes(`id="${id}"`));
  const barrier=f.delay("/api/lab/collect");
  const request=f.controller.collect(f.state().tools[0],args);
  assert.equal(f.state().phase,"collecting"); assert.equal(f.state().busy,true);
  assert.match(f.render(),/aria-busy="true"/); assert.match(f.render(),/<select[^>]*disabled/);
  const before=f.calls.length;
  await f.controller.collect(f.state().tools[0],args); await f.controller.loadTools();
  assert.equal(f.calls.length,before,"A pending device check cannot be submitted twice");
  assert.deepEqual(f.calls.at(-1).input,{tool:tool.function.name,bridge_id:"workstation",arguments:args});
  barrier.resolve({materials:{"R2.log":"new output"}}); await request;
  assert.equal(f.state().busy,false); assert.equal(f.state().phase,"idle");
  await f.controller.disconnect();
  assert.equal(f.state().bridge,null); assert.equal(f.state().tools.length,0);
  assert.equal(f.materials.length,2,"Disconnect preserves collected materials");
}
{
  const f=fixture(); await f.controller.loadTools();
  const barrier=f.delay("/api/lab/collect"); const request=f.controller.collect(f.state().tools[0],args);
  barrier.reject(Error("设备连接已断开")); await request;
  assert.equal(f.calls.filter(c => c.path === "/api/lab/collect").length,1,"Failed device checks are never automatically replayed");
  assert.equal(f.state().error,true); assert.equal(f.state().busy,false);
  assert.match(f.render(),/设备连接已断开/); assert.equal(f.materials.length,0);
  await f.controller.collect(f.state().tools[0],args);
  assert.equal(f.state().error,false); assert.equal(f.materials.length,1,"An explicit new collection can succeed");
}
for (const rejects of [false,true]) {
  const f=fixture(); await f.controller.loadTools();
  const barrier=f.delay("/api/lab/collect"); const request=f.controller.collect(f.state().tools[0],args);
  f.controller.reset();
  if (rejects) barrier.reject(Error("old account error")); else barrier.resolve({materials:{"late.log":"old account output"}});
  await request;
  assert.equal(f.materials.length,0); assert.equal(f.state().error,false);
  assert.equal(f.state().checkReady,false); assert.equal(f.state().busy,false);
}
for (const path of ["/api/lab/tools","/api/lab/bridge","/api/lab/bridge/disconnect"]) {
  const f=fixture(); await f.controller.pair("旧电脑");
  const barrier=f.delay(path);
  const request=path.endsWith("tools") ? f.controller.loadTools() : path.endsWith("disconnect") ? f.controller.disconnect() : f.controller.pair("旧电脑");
  f.controller.reset(); await f.controller.pair("新电脑");
  barrier.resolve(path.endsWith("tools") ? {bridge:{id:"old",state:"online"},tools:[tool]} : {id:"old",code:"OLD"});
  await request;
  assert.equal(f.state().bridge.name,"新电脑"); assert.equal(f.state().code,"AABBCCDDEEFF0011");
  assert.equal(f.state().tools.length,0,"Late connection replies cannot replace the current owner");
}
{
  const f=fixture(); f.busy(true);
  await f.controller.pair("电脑"); await f.controller.loadTools();
  assert.equal(f.calls.length,0); assert.match(f.render(),/id="lab-pair"[^>]*disabled/);
  f.busy(false); await f.controller.pair("电脑"); assert.equal(f.calls.length,1);
}
console.log("React MCP passed: pairing, tool parameters, collection, busy controls, explicit retry and stale owner replies");
