"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname,"../web/student.js"),"utf8");
const client = source.slice(source.indexOf("async function api("),source.indexOf("function serviceStatus("));

async function main() {
  let status = 200, payload = [], request;
  const location = {search:"", origin:"https://labagent.test"};
  const context = vm.createContext({URLSearchParams, URL, location, fetch:async (url,options) => {
    request = {url,options};
    return {ok:status < 400,status,headers:{get:()=>"application/json"},json:async()=>payload};
  }});
  vm.runInContext(client,context);
  assert.equal((await vm.runInContext('api("/api/sessions")',context)).length,0);
  payload = {};
  await assert.rejects(vm.runInContext('api("/api/diagnose")',context),/完整结果/);
  payload = {session_id:"test",deleted:true,deleted_requests:2};
  assert.equal((await vm.runInContext('api("/api/sessions/test",undefined,{method:"DELETE"})',context)).deleted,true);
  assert.equal(request.options.method,"DELETE");
  assert.equal(request.options.body,undefined);
  location.search = "?room=" + "a".repeat(32);
  await vm.runInContext('api("/api/diagnose",{question:"test"})',context);
  assert.equal(request.options.headers["X-LabAgent-Room"],"a".repeat(32));
  assert.equal(request.options.headers["Content-Type"],"application/json");
  assert.equal(vm.runInContext('mediaUrl("/api/recordings/id/image")',context),"/api/recordings/id/image?room="+"a".repeat(32));
  location.search = "?room=invalid";
  assert.equal(vm.runInContext('Object.keys(roomHeaders()).length',context),0);
  status = 409; payload = {error:"诊断正在保存，请稍后重试。"};
  await assert.rejects(vm.runInContext('api("/api/sessions/test",undefined,{method:"DELETE"})',context),error=>error.status===409 && error.message===payload.error);
  status = 404; payload = {code:"session_not_found",error:"这次对话已不存在。"};
  await assert.rejects(vm.runInContext('api("/api/sessions/test",undefined,{method:"DELETE"})',context),error=>error.code==="session_not_found" && error.status===404);
  console.log("Client API checks passed, including invite headers and media URLs");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
