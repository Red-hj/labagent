import {useState, useSyncExternalStore} from "react";
import {createRoot} from "react-dom/client";
import {flushSync} from "react-dom";
import {createLabConnection, type LabController, type LabOptions, type LabState, type LabTool} from "./lab-connection-state";
export {createLabConnection} from "./lab-connection-state";

function ToolForm({tool, disabled, onCollect}: {tool:LabTool; disabled:boolean; onCollect:(args:Record<string,string>, verify:boolean) => void}) {
  const [values, setValues] = useState<Record<string,string>>(() => Object.fromEntries(
    Object.entries(tool.function.parameters.properties).map(([key, definition]) => [key, definition.enum[0]])));
  return <form onSubmit={event => { event.preventDefault(); if (!disabled) onCollect(values, true); }}>
    <p>{tool.function.description}</p>
    {Object.entries(tool.function.parameters.properties).map(([key, definition]) => <label key={key}>
      {key === "command" ? "要检查的内容" : key}
      <select value={values[key]} disabled={disabled} onChange={event => setValues({...values, [key]:event.target.value})}>
        {definition.enum.map(value => <option key={value} value={value}>{value}</option>)}
      </select>
    </label>)}
    <button type="submit" className="secondary-button" disabled={disabled}>采集并核对</button>
    <button type="button" className="text-button" disabled={disabled} onClick={() => onCollect(values, false)}>仅采集材料</button>
  </form>;
}

export function LabConnectionView({state, controller}: {state:LabState; controller:LabController}) {
  const [name, setName] = useState("我的实验电脑");
  const disabled = state.busy || state.phase !== "idle";
  return <>
    <p className="section-note">在实验电脑上运行本地连接程序，网站便可读取你选择的设备输出。输出加入当前材料后，继续核对正在做的这一步。</p>
    <details open={state.bridge?.state !== "online"}><summary>第一次连接：准备工具 → 输入连接码 → 采集输出</summary>
      <ol>
        <li>
          <strong>下载并准备本地连接程序</strong>
          <p className="section-note">实验电脑需要 Python 3.10 或更新版本。<a href="/api/lab/connector" download>下载实验电脑连接包</a>，解压后在 Windows 双击 connect.cmd；首次运行会询问设备地址和端口、实际读取接口输出，再等待网页连接码。</p>
          <p className="section-note">内置接入器适用于 Huawei VRP 的 Telnet 控制台。先启动模拟设备，在模拟器中完成首次登录并看到 &lt;R1&gt; 等命令提示符；按模拟器显示的实际控制台地址和端口填写。</p>
          <details><summary>手动运行或使用已有 MCP 配置</summary>
            <code className="lab-command">python scripts/connect_lab.py --setup</code>
            <p className="section-note">向导会生成本地配置，无需另外安装 ensp-mcp。准备完成后，实际读取每台设备的第一项检查：</p>
            <code className="lab-command">python scripts/connect_lab.py --probe</code>
            <p className="section-note">--check 只检查工具列表；--probe 会读取设备输出。已有 MCP 配置可在命令中指定文件路径。<a href="https://github.com/Red-hj/labagent/blob/main/docs/接口说明.md#内置-huawei-vrp-控制台接入v0211" target="_blank" rel="noreferrer">查看配置说明</a></p>
          </details>
        </li>
        <li>
          <strong>生成连接码，在实验电脑输入</strong>
          <p className="section-note">先登录个人账号。电脑名称只是便于辨认的标签。</p>
      <label htmlFor="lab-computer-name">实验电脑名称</label>
      <input id="lab-computer-name" maxLength={60} value={name} disabled={disabled} onChange={event => setName(event.target.value)}/>
      <button id="lab-pair" type="button" className="secondary-button" disabled={disabled} onClick={() => { void controller.pair(name); }}>
        {state.phase === "pairing" ? "正在生成…" : "生成连接码"}
      </button>
      <p id="lab-pair-code" className="lab-pair-code" hidden={!state.code}>{state.code ? `连接码：${state.code}（5分钟内有效）` : ""}</p>
      <p className="section-note">连接码5分钟内有效，只在自己的实验电脑输入；重新生成会断开旧连接。本地配置与设备凭据留在电脑上。</p>
          <p className="section-note">生成连接码后，在实验电脑的 LabAgent 项目目录运行下面的程序，并在终端提示处输入连接码。请保持程序运行。</p>
          <code className="lab-command">python scripts/connect_lab.py</code>
        </li>
        <li>
          <strong>回到网页刷新，选择检查</strong>
          <p className="section-note">本地程序显示“已连接”后，点击“刷新连接与检查”。出现设备检查列表才表示接入成功；选择检查并采集，输出会加入当前材料。</p>
        </li>
      </ol>
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
        tool={tool} disabled={disabled} onCollect={(args, verify) => { void controller.collect(tool,args,verify); }}/>) }
    </div>
  </>;
}

function LabConnection({controller}: {controller:LabController}) {
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
  return <LabConnectionView state={state} controller={controller}/>;
}

export function mountLabConnection(container: HTMLElement, options: LabOptions) {
  const controller = createLabConnection(options), root = createRoot(container);
  let owner = 0;
  const render = () => root.render(<LabConnection key={owner} controller={controller}/>);
  flushSync(render);
  return {...controller, reset:() => { controller.reset(); owner++; flushSync(render); }};
}
