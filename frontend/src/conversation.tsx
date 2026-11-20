import {createRoot, type Root} from "react-dom/client";
import {flushSync} from "react-dom";
import {memo} from "react";
import {PendingTurn, ProgressList, type PendingRequest, type ProgressEntry} from "./request-view";
export {mountComposer, configureComposer, getDraft, setDraft, Composer} from "./composer";
export {PendingTurn, ProgressList} from "./request-view";
export {mountLabConnection, LabConnectionView, createLabConnection} from "./lab-connection";
export {renderMaterials, configureMaterials, locateMaterial, MaterialsView} from "./materials";

export interface Citation {
  device?: string;
  line: number;
  quote: string;
}

export interface Turn {
  attempt?: number;
  user: string;
  assistant: string;
  actor?: string;
  created_at?: string;
  model_status?: string | null;
  summary?: string;
  steps?: string[];
  material_names?: string[];
  evidence?: Citation[];
}

type OpenMaterials = (turn: Turn, citation?: Citation) => void;
interface ConversationProps {
  turns: Turn[];
  onMaterials: OpenMaterials;
}

function Evidence({turn, onMaterials}: {turn: Turn; onMaterials: OpenMaterials}) {
  if (!turn.attempt) return null;
  return <details className="chat-evidence">
    <summary>本轮材料与依据</summary>
    {turn.summary && turn.summary !== turn.assistant && <p>{turn.summary}</p>}
    <button type="button" className="text-button" onClick={() => onMaterials(turn)}>查看当时提交的材料</button>
    {(turn.evidence || []).map((citation, index) => <div key={index}>
      <button type="button" className="text-button" onClick={() => onMaterials(turn, citation)}>
        {citation.device || "原文"} · 第 {citation.line} 行：{citation.quote}
      </button>
    </div>)}
  </details>;
}

function TurnView({turn, onMaterials}: {turn: Turn; onMaterials: OpenMaterials}) {
  const failed = Boolean(turn.model_status && turn.model_status !== "ok");
  const names = turn.material_names || [];
  return <div className={"chat-turn" + (failed ? " chat-saved-error" : "")} data-request-text={failed ? turn.user : undefined}>
    <div className="chat-message chat-user">
      <p className="chat-label">{turn.actor || "你"}
        {turn.created_at && <time className="chat-time" dateTime={turn.created_at}>
          {new Date(turn.created_at).toLocaleString("zh-CN", {month:"numeric", day:"numeric", hour:"2-digit", minute:"2-digit"})}
        </time>}
      </p>
      <div className="chat-body">{turn.user}</div>
      {names.length > 0 && <div className="chat-materials">材料：{names.slice(0, 2).join("、")}{names.length > 2 ? ` 等 ${names.length} 份` : ""}</div>}
    </div>
    <div className="chat-message chat-assistant">
      <p className="chat-label">LabAgent</p>
      <div className={"chat-body" + (failed ? " pending-text" : "")}>{turn.assistant}</div>
      {Boolean(turn.steps?.length) && <ol className="chat-steps">{turn.steps!.map((step, index) => <li key={index}>{step}</li>)}</ol>}
      <Evidence turn={turn} onMaterials={onMaterials}/>
    </div>
  </div>;
}

export const SavedConversation = memo(function SavedConversation({turns, onMaterials}: ConversationProps) {
  return <>{turns.map((turn, index) => <TurnView key={turn.attempt ?? index} turn={turn} onMaterials={onMaterials}/>)}</>;
});

interface MountedConversation extends ConversationProps {
  host: HTMLDivElement;
  root: Root;
  pending: PendingRequest | null;
}
const mounted = new WeakMap<HTMLElement, MountedConversation>();
const progressRoots = new WeakMap<HTMLElement, Root>();

function paint(state: MountedConversation) {
  // The existing request UI reads the DOM immediately after rendering a result.
  flushSync(() => state.root.render(<><SavedConversation turns={state.turns} onMaterials={state.onMaterials}/><PendingTurn request={state.pending}/></>));
}

function ensureMounted(container: HTMLElement) {
  let state = mounted.get(container);
  if (!state) {
    const host = document.createElement("div");
    host.className = "chat-history";
    container.prepend(host);
    state = {host, root:createRoot(host), turns:[], pending:null, onMaterials:() => {}};
    mounted.set(container, state);
  }
  return state;
}

export function renderTurns(container: HTMLElement, turns: Turn[], onMaterials: OpenMaterials) {
  const state = ensureMounted(container);
  state.pending = null;
  state.turns = turns;
  state.onMaterials = onMaterials;
  paint(state);
}

export function beginPending(container: HTMLElement, input: Pick<PendingRequest, "text" | "type" | "materialCount" | "started">) {
  const state = ensureMounted(container);
  state.pending = {...input, message:"正在发送请求…", delivered:false, elapsed:0, hint:null};
  paint(state);
}

export function updatePending(container: HTMLElement, changes: Partial<PendingRequest>) {
  const state = mounted.get(container);
  if (!state?.pending) return;
  state.pending = {...state.pending, ...changes};
  paint(state);
}

export function failPending(container: HTMLElement, error: string, onRetry: () => void) {
  updatePending(container, {error, onRetry});
}

export function renderProgress(container: HTMLElement, entries: ProgressEntry[]) {
  let root = progressRoots.get(container);
  if (!root) { root = createRoot(container); progressRoots.set(container, root); }
  flushSync(() => root.render(<ProgressList entries={entries}/>));
}

export function removeLastFailure(container: HTMLElement, requestText: string) {
  const state = mounted.get(container);
  const last = state?.turns[state.turns.length - 1];
  if (!state || !last || !last.model_status || last.model_status === "ok" || last.user !== requestText) return;
  state.turns = state.turns.slice(0, -1);
  paint(state);
}

export function clear(container: HTMLElement) {
  const state = mounted.get(container);
  if (state) {
    state.root.unmount();
    mounted.delete(container);
  }
  container.replaceChildren();
}
