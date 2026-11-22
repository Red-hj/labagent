import {useEffect, useState, useSyncExternalStore} from "react";
import {createRoot} from "react-dom/client";
import {flushSync} from "react-dom";
import {createLabConnection, type LabController, type LabOptions, type LabState, type LabTool} from "./lab-connection-state";
export {createLabConnection} from "./lab-connection-state";

const paths = {
  monitor:"M4 4h16v12H4zM8 21h8M12 16v5", download:"M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5",
  arrow:"M5 12h14m-6-6 6 6-6 6", terminal:"m5 7 5 5-5 5m8 0h6", copy:"M9 8h11v13H9zM15 8V3H4v13h5",
  check:"m5 12 4 4L19 6", refresh:"M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-1l2 6M4 12l2 6a7 7 0 0 0 12-1",
  shield:"m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z", plug:"M8 3v5m8-5v5M6 8h12v3a6 6 0 0 1-12 0zm6 9v4"
};
function Icon({name}: {name:keyof typeof paths}) {
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name]}/></svg>;
}

function ToolForm({tool, disabled, onCollect}: {tool:LabTool; disabled:boolean; onCollect:(args:Record<string,string>, verify:boolean) => void}) {
  const [values, setValues] = useState<Record<string,string>>(() => Object.fromEntries(
    Object.entries(tool.function.parameters.properties).map(([key, definition]) => [key, definition.enum[0]])));
  return <form className="device-tool-card" onSubmit={event => { event.preventDefault(); if (!disabled) onCollect(values, true); }}>
    <div className="device-tool-title"><span className="device-tool-icon"><Icon name="terminal"/></span><h4>{tool.function.description}</h4></div>
    <div className="device-tool-fields">
    {Object.entries(tool.function.parameters.properties).map(([key, definition]) => <label key={key}>
      {key === "command" ? "检查内容" : key === "device" ? "实验设备" : key}
      <select value={values[key]} disabled={disabled} onChange={event => setValues({...values, [key]:event.target.value})}>
        {definition.enum.map(value => <option key={value} value={value}>{value}</option>)}
      </select>
    </label>)}
    </div>
    <div className="device-tool-actions"><button type="submit" className="device-primary" disabled={disabled}>采集并核对<Icon name="arrow"/></button>
    <button type="button" className="device-link" disabled={disabled} onClick={() => onCollect(values, false)}>仅采集材料</button></div>
  </form>;
}

function SetupCards({state, controller, disabled}: {state:LabState; controller:LabController; disabled:boolean}) {
  const [name, setName] = useState("我的实验电脑");
  const [copied, setCopied] = useState(false), [copyError, setCopyError] = useState(false);
  useEffect(() => { setCopied(false); setCopyError(false); }, [state.code]);
  async function copyCode() {
    if (!state.code) return;
    try { await navigator.clipboard.writeText(state.code); setCopied(true); setCopyError(false); }
    catch { setCopyError(true); }
  }
  return <div className="device-setup-grid">
    <section className="device-setup-card">
      <div className="device-card-heading"><span className="device-number">01</span><h3>准备连接程序</h3><span className="device-card-tag">在实验电脑上</span></div>
      <p className="device-description">让 LabAgent 读取实验设备的实际输出。</p>
      <a className="device-download" href="/api/lab/connector" download><span className="device-download-icon"><Icon name="download"/></span><span><strong>下载实验电脑连接包</strong><small>Windows 启动脚本 · Python 3.10+</small></span><Icon name="arrow"/></a>
      <ol className="device-instructions"><li>解压后双击 <code>connect.cmd</code>。</li><li>按向导填写设备控制台地址和端口。</li><li>保持程序运行，输入本页生成的连接码。</li></ol>
      <p className="device-caption">内置 Huawei VRP Telnet 接入。先启动模拟设备并完成首次登录，看到 &lt;R1&gt; 等提示符，再填写模拟器显示的实际地址和端口。</p>
      <details className="device-advanced"><summary><Icon name="terminal"/>手动运行 / 已有 MCP 配置</summary>
        <p>在 LabAgent 项目目录准备配置并实际读取设备：</p>
        <code className="lab-command">python scripts/connect_lab.py --setup</code>
        <code className="lab-command">python scripts/connect_lab.py --probe</code>
        <p>随后运行连接程序，按提示输入连接码：</p><code className="lab-command">python scripts/connect_lab.py</code>
        <p>已有 MCP 配置可在命令后指定文件路径。<code>--check</code> 检查工具目录，<code>--probe</code> 实际读取设备输出。</p>
        <a href="https://github.com/Red-hj/labagent/blob/main/docs/接口说明.md#内置-huawei-vrp-控制台接入v0211" target="_blank" rel="noreferrer">查看完整配置说明 ↗</a>
      </details>
    </section>
    <section className="device-setup-card device-pair-card">
      <div className="device-card-heading"><span className="device-number">02</span><h3>连接这台电脑</h3></div>
      <p className="device-description">用连接码将实验电脑关联到个人账号。</p>
      <label htmlFor="lab-computer-name">实验电脑名称</label>
      <input id="lab-computer-name" maxLength={60} value={name} disabled={disabled} onChange={event => setName(event.target.value)}/>
      {state.signedIn === false ? <div className="device-signin"><p>登录后即可生成连接码，管理你的实验电脑。</p><button type="button" className="device-primary" onClick={controller.signIn}>登录并继续<Icon name="arrow"/></button></div> : <>
        <button id="lab-pair" type="button" className="device-primary device-pair-button" disabled={disabled} onClick={() => { void controller.pair(name); }}>
          {state.phase === "pairing" ? "正在生成…" : state.code ? "重新生成连接码" : "生成连接码"}<Icon name="arrow"/>
        </button>
        <div className="device-code-box" hidden={!state.code}><div className="device-code-label"><span>在本地程序输入</span><span>5 分钟内有效</span></div>
          <p id="lab-pair-code" className="lab-pair-code" aria-label={state.code ? "连接码：" + state.code : undefined}>{state.code?.match(/.{1,4}/g)?.join(" ")}</p>
          <button type="button" className="device-copy" onClick={() => { void copyCode(); }}><Icon name={copied ? "check" : "copy"}/>{copied ? "已复制连接码" : "复制连接码"}</button>
          <p className="device-copy-feedback" role="status">{copyError ? "复制未成功，请手动选择连接码。" : copied ? "已复制，可粘贴到实验电脑的连接程序。" : ""}</p>
        </div>
      </>}
      <p className="device-caption device-pair-caption">连接码只在自己的实验电脑输入。重新生成会断开旧连接。</p>
      <div className="device-pair-illustration" aria-hidden="true"><span><Icon name="monitor"/></span><i/><span className="device-cloud-mark">L<span>LabAgent</span></span></div>
    </section>
  </div>;
}

export function LabConnectionView({state, controller}: {state:LabState; controller:LabController}) {
  const disabled = state.busy || state.phase !== "idle", online = state.bridge?.state === "online";
  const waiting = state.bridge?.state === "waiting", ready = online || state.tools.length > 0;
  const offline = Boolean(state.bridge && !online && !waiting);
  useEffect(() => {
    if (!waiting || state.busy) return;
    const timer = window.setInterval(() => { void controller.loadTools(); }, 4000);
    return () => window.clearInterval(timer);
  }, [waiting, state.bridge?.id, state.busy, controller]);
  return <div className="device-module">
    <section className={"device-overview" + (online ? " is-connected" : "")} aria-label="电脑连接状态">
      <div className="device-overview-icon"><Icon name="monitor"/><span className="device-status-dot"/></div>
      <div className="device-overview-copy"><div className={"device-status-chip " + (online ? "online" : waiting ? "waiting" : "")}><span/>{online ? "已连接" : waiting ? "等待连接" : offline ? "已离线" : ready ? "本机检查可用" : "尚未连接"}</div>
        <h3>{online ? state.bridge?.name : waiting ? "输入连接码，即可继续" : offline ? "重新连接实验电脑" : "连接实验环境，接着排查。"}</h3>
        <p>{online ? "选择一项设备检查，把新的观测带回当前排查。" : waiting ? "在实验电脑的连接程序中输入连接码，状态将自动更新。" : offline ? "重新启动本地连接程序，再生成连接码配对。" : "从真实输出出发，让诊断和下一步操作更有依据。"}</p></div>
      <div className="device-overview-art" aria-hidden="true"><Icon name="plug"/></div>
    </section>
    {!ready && <ol className="device-stepper" aria-label="接入步骤">{["准备连接程序", "输入连接码", "采集并核对"].map((label, index) => <li key={label} className={index === (waiting ? 1 : 0) ? "active" : ""}><span>{index + 1}</span>{label}</li>)}</ol>}
    {online ? <details className="device-reconnect"><summary>连接新电脑或查看接入说明</summary><SetupCards state={state} controller={controller} disabled={disabled}/></details> : <SetupCards state={state} controller={controller} disabled={disabled}/>}
    <div className={"device-notice" + (state.error ? " is-error" : "")} hidden={Boolean(online && state.phase === "idle" && !state.error && !state.checkReady)} role={state.error ? "alert" : "status"}><span className={"device-notice-dot" + (state.phase !== "idle" ? " is-loading" : "")}/><p id="lab-note">{state.message}</p></div>
    {state.warnings.length > 0 && <p className="device-warning" role="status">{state.warnings.join("；")}</p>}
    <section className="device-checks" aria-label="设备检查">
      <div className="device-checks-heading"><div><h3>设备检查 <span>{state.tools.length} 项可用</span></h3><p>读取输出后，一键回到当前排查核对。</p></div>
        <button id="lab-load-tools" type="button" className="device-refresh" disabled={disabled} onClick={() => { void controller.loadTools(); }}><Icon name="refresh"/>{state.phase === "loading" ? "正在刷新…" : "刷新连接与检查"}</button></div>
      <button id="lab-check-step" type="button" className="device-primary device-check-ready" hidden={!state.checkReady} disabled={disabled} onClick={controller.check}>用刚采集的输出核对这一步<Icon name="arrow"/></button>
      <div id="lab-tools" className="device-tools-grid" aria-busy={state.phase === "collecting"}>
        {state.tools.map(tool => <ToolForm key={JSON.stringify([tool.bridge_id,tool.function.name,tool.function.parameters.properties])} tool={tool} disabled={disabled} onCollect={(args, verify) => { void controller.collect(tool,args,verify); }}/>) }
      </div>
      {!state.tools.length && <div className="device-empty"><span><Icon name="terminal"/></span><div><strong>{online ? "正在等待设备检查" : "接入后，这里会出现可用检查"}</strong><p>{online ? "请检查本地设备配置，或刷新工具目录。" : "选择实验设备和检查内容，再采集接口、邻居或路由等输出。"}</p></div></div>}
    </section>
    <footer className="device-footer"><p><Icon name="shield"/>本地配置与设备凭据保留在实验电脑。</p><button id="lab-disconnect" type="button" className="device-disconnect" hidden={!state.bridge} disabled={disabled} onClick={() => { void controller.disconnect(); }}>{state.phase === "disconnecting" ? "正在断开…" : "断开实验电脑"}</button></footer>
  </div>;
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
