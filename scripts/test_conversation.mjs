import assert from "node:assert/strict";
import {mkdir} from "node:fs/promises";
import {createRequire} from "node:module";
import {fileURLToPath} from "node:url";
import {build} from "esbuild-wasm";
import {createElement} from "react";
import {renderToStaticMarkup} from "react-dom/server";

process.chdir(fileURLToPath(new URL("../", import.meta.url)));
await mkdir("runtime/tests", {recursive:true});
await build({entryPoints:["frontend/src/conversation.tsx"], outfile:"runtime/tests/react-conversation.cjs",
  bundle:true, platform:"node", format:"cjs", packages:"external"});
const require = createRequire(import.meta.url);
const {SavedConversation, PendingTurn, ProgressList, Composer, MaterialsView} = require("../runtime/tests/react-conversation.cjs");
const render = turns => renderToStaticMarkup(createElement(SavedConversation, {turns, onMaterials(){}}));
const html = render([{attempt:1, user:"<script>user text</script>", assistant:"查看R1接口。", actor:"队友",
  steps:["读取接口状态", "核对两列输出"], material_names:["R1.cfg", "R2.cfg", "topology.txt"],
  evidence:[{device:"R1.cfg", line:2, quote:"<strong>up up</strong>"}]}]);
assert.ok(html.includes("&lt;script&gt;user text&lt;/script&gt;"));
assert.ok(!html.includes("<script>"));
assert.ok(html.includes("&lt;strong&gt;up up&lt;/strong&gt;"));
assert.ok(html.includes("队友"));
assert.ok(html.includes("等 3 份"));
assert.ok(html.includes("查看当时提交的材料"));
assert.ok(html.includes("核对两列输出"));
assert.ok(html.includes("本轮材料与依据"));
const failed = render([{attempt:2, user:"再看一次", assistant:"本次请求未完成", model_status:"unavailable"}]);
assert.ok(failed.includes("chat-saved-error"));
assert.ok(failed.includes('data-request-text="再看一次"'));
const introductory = render([{user:"你好", assistant:"请提供实验材料"}]);
assert.ok(!introductory.includes("本轮材料与依据"));
const pendingRequest = {text:"检查新材料", type:"analyze", materialCount:2, started:Date.now(),
  message:"正在读取接口", delivered:true, elapsed:12, hint:"可以先写下一条"};
const pending = renderToStaticMarkup(createElement(PendingTurn, {request:pendingRequest}));
assert.ok(pending.includes("已发送"));
assert.ok(pending.includes("2 份实验材料"));
assert.ok(pending.includes("12秒"));
assert.ok(pending.includes("可以先写下一条"));
const retry = renderToStaticMarkup(createElement(PendingTurn, {request:{...pendingRequest,error:"采集失败"}}));
assert.ok(retry.includes("重新发送"));
assert.ok(retry.includes("回复未完成"));
assert.ok(!retry.includes("typing-dots"));
const progress = renderToStaticMarkup(createElement(ProgressList, {entries:[{key:"item:r1", message:"接口检查失败", status:"failed"}]}));
assert.ok(progress.includes('data-status="failed"'));
const composer = renderToStaticMarkup(createElement(Composer, {disabled:false, canSend:false, sending:true, placeholder:"等待时继续写草稿", onSubmit(){}}));
assert.ok(composer.includes("等待回复"));
assert.ok(composer.includes("等待时继续写草稿"));
assert.ok(composer.includes('id="followup-message"'));
assert.ok(!composer.match(/<textarea[^>]*disabled/));
assert.ok(composer.match(/<button[^>]*disabled/));
const materialProps = {materials:[{name:"R1.cfg", text:"<R1>\r\nup up"},
  {name:"screen.txt", text:"识别文字", screenshot:{name:"接口截图", imageUrl:"/recording/image"}}],
  busy:true, onRemove(){}, onSave(){}, onScreenshot(){}};
const materialHtml = renderToStaticMarkup(createElement(MaterialsView, materialProps));
assert.ok(materialHtml.includes("&lt;R1&gt;"));
assert.ok(materialHtml.includes("2 行内容"));
assert.ok(materialHtml.includes('aria-label="移除 R1.cfg"'));
assert.ok(materialHtml.includes('aria-label="修正 接口截图 的识别文字"'));
assert.ok(materialHtml.match(/<textarea[^>]*disabled/));
const locatedHtml = renderToStaticMarkup(createElement(MaterialsView, {...materialProps,
  location:{name:"R1.cfg", line:2, quote:"up up", text:"<R1>\r\nup up"}}));
assert.ok(locatedHtml.includes('第2行：up up'));
const changedHtml = renderToStaticMarkup(createElement(MaterialsView, {...materialProps,
  location:{name:"R1.cfg", line:2, quote:"up up", text:"old snapshot"}}));
assert.ok(!changedHtml.includes("<mark"), "a citation from an older snapshot must not highlight changed text");
console.log("React conversation passed: messages, citations, pending replies, retry state, tool progress and editable waiting drafts");

const changed = render([{attempt:2, user:"核对新输出", assistant:"接口已恢复，继续核对邻居", material_changes:{
  has_previous:true, changed:true, items:[{material:"R1.log",kind:"updated",added:1,removed:1,
    redacted_only:false,truncated:false,preview:[
      {kind:"removed",line:2,text:"GE0/0/0 down"},
      {kind:"added",line:2,text:"GE0/0/0 up <script>"}]}]}}]);
assert.ok(changed.includes("1份材料有变化"));
assert.ok(changed.includes("旧第2行：GE0/0/0 down"));
assert.ok(changed.includes("新第2行：GE0/0/0 up &lt;script&gt;"));
assert.ok(!changed.includes("<script>"));
