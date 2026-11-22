export interface LabTool {
  bridge_id?: string;
  function: {
    name: string;
    description: string;
    parameters: {properties: Record<string, {enum: string[]}>};
  };
}
interface Bridge {id: string; name: string; state: string}
interface ToolsResponse {bridge?: Bridge | null; tools?: LabTool[]; warnings?: string[]}
export interface LabOptions {
  api: <T>(path: string, body?: Record<string, unknown>) => Promise<T>;
  busy: () => boolean;
  setCollecting?: (value: boolean) => void;
  addMaterials: (materials: Record<string, string>) => void;
  ask: (text: string, type: string) => void;
}
export interface LabState {
  bridge: Bridge | null;
  code: string | null;
  tools: LabTool[];
  warnings: string[];
  phase: "idle" | "loading" | "pairing" | "disconnecting" | "collecting";
  busy: boolean;
  message: string;
  error: boolean;
  checkReady: boolean;
}
const initial = (): LabState => ({bridge:null, code:null, tools:[], warnings:[], phase:"idle",
  busy:false, message:"尚未连接实验电脑。首次使用请先完成上面的本地工具准备与自检，也可直接上传配置、截图或日志。", error:false, checkReady:false});

// The account/session owner calls reset; in-flight replies cannot add old materials.
export function createLabConnection(options: LabOptions) {
  let state = initial(), epoch = 0, contextEpoch = 0;
  const listeners = new Set<() => void>();
  const update = (changes: Partial<LabState>) => {
    state = {...state, ...changes, busy:options.busy()};
    for (const listener of listeners) listener();
  };
  function available() { return state.phase === "idle" && !options.busy(); }
  function failed(error: unknown) { update({message:error instanceof Error ? error.message : String(error), error:true}); }
  async function loadTools() {
    if (!available()) return;
    const owner = epoch;
    update({phase:"loading", error:false, message:"正在刷新连接与可用检查…"});
    try {
      const data = await options.api<ToolsResponse>("/api/lab/tools");
      if (owner !== epoch) return;
      const bridge = data.bridge || null;
      const message = bridge?.state === "online" ? `${bridge.name}已连接。选择下面的一项检查。` :
        bridge ? (bridge.state === "waiting" ? "等待实验电脑输入连接码，启动后点“刷新连接与检查”。" :
          "实验电脑已离线。请重新启动本地程序并生成连接码。") : initial().message;
      update({bridge, tools:data.tools || [], warnings:data.warnings || [], message,
        code:bridge?.state === "waiting" && bridge.id === state.bridge?.id ? state.code : null});
    } catch (error) { if (owner === epoch) failed(error); }
    finally { if (owner === epoch) update({phase:"idle"}); }
  }
  async function pair(name: string) {
    if (!available()) return;
    const owner = ++epoch;
    update({phase:"pairing", tools:[], code:null, warnings:[], error:false, message:"正在生成连接码…"});
    try {
      const data = await options.api<{id:string; code:string}>("/api/lab/bridge", {name});
      if (owner !== epoch) return;
      update({bridge:{id:data.id, name, state:"waiting"}, code:data.code,
        message:"在实验电脑的连接程序中输入连接码，再刷新可用检查。"});
    } catch (error) { if (owner === epoch) failed(error); }
    finally { if (owner === epoch) update({phase:"idle"}); }
  }
  async function disconnect() {
    if (!available() || !state.bridge) return;
    const owner = epoch;
    update({phase:"disconnecting", error:false, message:"正在断开实验电脑…"});
    try {
      await options.api("/api/lab/bridge/disconnect", {id:state.bridge.id});
      if (owner !== epoch) return;
      epoch++;
      update({bridge:null, code:null, tools:[], warnings:[], phase:"idle", message:"实验电脑已断开，已采集的材料仍保留。"});
    } catch (error) { if (owner === epoch) failed(error); }
    finally { if (owner === epoch) update({phase:"idle"}); }
  }
  async function collect(tool: LabTool, args: Record<string, string>, verify = false) {
    if (!available()) return;
    const owner = epoch, context = contextEpoch;
    let collected = false;
    update({phase:"collecting", error:false, message:"正在从已连接的实验设备读取输出…"});
    options.setCollecting?.(true);
    update({});
    try {
      const data = await options.api<{materials:Record<string,string>}>("/api/lab/collect", {
        tool:tool.function.name, ...(tool.bridge_id ? {bridge_id:tool.bridge_id} : {}), arguments:args});
      if (owner !== epoch || context !== contextEpoch) return;
      options.addMaterials(data.materials);
      collected = true;
      update({checkReady:true, message:"设备输出已加入当前材料。点“核对这一步”继续，会话保留。"});
    } catch (error) { if (owner === epoch && context === contextEpoch) failed(error); }
    finally {
      options.setCollecting?.(false);
      update(owner === epoch ? {phase:"idle"} : {});
    }
    if (collected && verify && owner === epoch && context === contextEpoch) check();
  }
  function check() {
    if (!available() || !state.checkReady) return;
    update({checkReady:false});
    options.ask("请比较刚采集的输出与上一轮材料，核对当前这一步，说明观察到的变化和下一步操作。", "verify");
  }
  return {
    getSnapshot:() => state,
    subscribe:(listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; },
    loadTools, pair, disconnect, collect,
    check,
    refreshBusy:() => { if (state.busy !== options.busy()) update({}); },
    resetContext:() => { contextEpoch++; update({checkReady:false}); },
    reset:() => { epoch++; contextEpoch++; state = initial(); update({}); }
  };
}
export type LabController = ReturnType<typeof createLabConnection>;
