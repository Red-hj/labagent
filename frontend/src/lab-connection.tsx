import {useState, useSyncExternalStore} from "react";
import {createRoot} from "react-dom/client";
import {flushSync} from "react-dom";
import {createLabConnection, type LabController, type LabOptions, type LabState, type LabTool} from "./lab-connection-state";
export {createLabConnection} from "./lab-connection-state";

function ToolForm({tool, disabled, onCollect}: {tool:LabTool; disabled:boolean; onCollect:(args:Record<string,string>) => void}) {
  const [values, setValues] = useState<Record<string,string>>(() => Object.fromEntries(
    Object.entries(tool.function.parameters.properties).map(([key, definition]) => [key, definition.enum[0]])));
  return <form onSubmit={event => { event.preventDefault(); if (!disabled) onCollect(values); }}>
    <p>{tool.function.description}</p>
    {Object.entries(tool.function.parameters.properties).map(([key, definition]) => <label key={key}>
      {key === "command" ? "要检查的内容" : key}
      <select value={values[key]} disabled={disabled} onChange={event => setValues({...values, [key]:event.target.value})}>
        {definition.enum.map(value => <option key={value} value={value}>{value}</option>)}
      </select>
    </label>)}
    <button type="submit" className="secondary-button" disabled={disabled}>采集到当前材料</button>
  </form>;
}

export function LabConnectionView({state, controller}: {state:LabState; controller:LabController}) {
  const [name, setName] = useState("我的实验电脑");
  const disabled = state.busy || state.phase !== "idle";
  return <>
    <p className="section-note">在实验电脑上运行本地连接程序，网站便可读取你选择的设备输出。输出加入当前材料后，继续核对正在做的这一步。</p>
    <details><summary>第一次连接</summary>
      <p className="section-note">先登录个人账号。实验电脑需已配置本地 MCP 检查工具；生成连接码后，在该电脑的项目目录运行下面的程序，按提示输入连接码。</p>
      <code className="lab-command">python scripts/connect_lab.py runtime/mcp.json</code>
      <label htmlFor="lab-computer-name">实验电脑名称</label>
      <input id="lab-computer-name" maxLength={60} value={name} disabled={disabled} onChange={event => setName(event.target.value)}/>
      <button id="lab-pair" type="button" className="secondary-button" disabled={disabled} onClick={() => { void controller.pair(name); }}>
        {state.phase === "pairing" ? "正在生成…" : "生成连接码"}
      </button>
      <p id="lab-pair-code" className="lab-pair-code" hidden={!state.code}>{state.code ? `连接码：${state.code}（5分钟内有效）` : ""}</p>
      <p className="section-note">连接码5分钟内有效，只在自己的实验电脑输入；重新生成会断开旧连接。本地配置与设备凭据留在电脑上。</p>
    </details>
    <p id="lab-note" className={"section-note" + (state.error ? " error" : "")} role="status">{state.message}</p>
    {state.warnings.length > 0 && <p className="section-note" role="status">{state.warnings.join("；")}</p>}
    <div className="learning-actions">
      <button id="lab-load-tools" type="button" className="secondary-button" disabled={disabled} onClick={() => { void controller.loadTools(); }}>
        {state.phase === "loading" ? "正在刷新…" : "刷新连接与检查"}
      </button>
      <button id="lab-disconnect" type="button" className="text-button" hidden={!state.bridge} disabled={disabled} onClick={() => { void controller.disconnect(); }}>
        {state.phase === "disconnecting" ? "正在断开…" : "断开实验电脑"}
      </button>
      <button id="lab-check-step" type="button" className="secondary-button" hidden={!state.checkReady} disabled={disabled} onClick={controller.check}>用刚采集的输出核对这一步</button>
    </div>
    <div id="lab-tools" aria-busy={state.phase === "collecting"}>
      {state.tools.map(tool => <ToolForm key={JSON.stringify([tool.bridge_id,tool.function.name,tool.function.parameters.properties])}
        tool={tool} disabled={disabled} onCollect={args => { void controller.collect(tool,args); }}/>) }
    </div>
  </>;
}

function LabConnection({controller}: {controller:LabController}) {
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
  return <LabConnectionView state={state} controller={controller}/>;
}

export function mountLabConnection(container: HTMLElement, options: LabOptions) {
  const controller = createLabConnection(options), root = createRoot(container);
  flushSync(() => root.render(<LabConnection controller={controller}/>));
  return controller;
}
