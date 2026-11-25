import {createRef, forwardRef, useImperativeHandle, useRef, useState} from "react";
import {createRoot, type Root} from "react-dom/client";
import {flushSync} from "react-dom";

export interface ComposerOptions {
  disabled: boolean;
  canSend: boolean;
  sending: boolean;
  placeholder: string;
  onSubmit: (text: string) => void;
}
interface ComposerHandle {
  getDraft: () => string;
  setDraft: (text: string) => void;
}

export const Composer = forwardRef<ComposerHandle, ComposerOptions>(function Composer(options, ref) {
  const [draft, setDraft] = useState("");
  const input = useRef<HTMLTextAreaElement>(null);
  useImperativeHandle(ref, () => ({getDraft:() => draft, setDraft}), [draft]);
  return <form id="followup-form" onSubmit={event => {
    event.preventDefault();
    if (!options.canSend) return;
    const text = draft.trim();
    if (text) options.onSubmit(text); else input.current?.focus();
  }}>
    <label htmlFor="followup-message" className="sr-only">继续追问</label>
    <textarea id="followup-message" ref={input} rows={Math.min(6, Math.max(2, draft.split("\n").length))} aria-describedby="followup-shortcuts"
      value={draft} disabled={options.disabled} placeholder={options.placeholder}
      onChange={event => setDraft(event.target.value)} onKeyDown={event => {
        if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing || event.keyCode === 229) return;
        event.preventDefault();
        if (options.canSend && !event.repeat) event.currentTarget.form?.requestSubmit();
      }}/>
    <div className="composer-footer">
      <span id="followup-shortcuts">Enter 发送 · Shift+Enter 换行 · Ctrl+V 粘贴截图</span>
      <button id="send-followup" className="send-button" type="submit" disabled={!options.canSend}>
        {options.sending ? "等待回复" : "发送 ↗"}
      </button>
    </div>
  </form>;
});

interface MountedComposer {
  root: Root;
  options: ComposerOptions;
  handle: ReturnType<typeof createRef<ComposerHandle>>;
}
const mounted = new WeakMap<HTMLElement, MountedComposer>();

function paint(state: MountedComposer) {
  flushSync(() => state.root.render(<Composer {...state.options} ref={state.handle}/>));
}

export function mountComposer(container: HTMLElement, onSubmit: ComposerOptions["onSubmit"]) {
  const state: MountedComposer = {root:createRoot(container), handle:createRef<ComposerHandle>(),
    options:{disabled:false, canSend:true, sending:false, placeholder:"继续追问，或粘贴新的复测输出…", onSubmit}};
  mounted.set(container, state);
  paint(state);
}

export function configureComposer(container: HTMLElement, changes: Partial<ComposerOptions>) {
  const state = mounted.get(container)!;
  state.options = {...state.options, ...changes};
  paint(state);
}

export function getDraft(container: HTMLElement) {
  return mounted.get(container)!.handle.current!.getDraft();
}

export function setDraft(container: HTMLElement, text: string) {
  flushSync(() => mounted.get(container)!.handle.current!.setDraft(text));
}
