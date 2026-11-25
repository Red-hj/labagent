import {useLayoutEffect, useRef, useState} from "react";
import {createRoot, type Root} from "react-dom/client";
import {flushSync} from "react-dom";
import {CopyButton} from "./formatted-text";

export interface Material {
  name: string;
  text: string;
  screenshot?: {name: string; imageUrl: string};
}
interface Location {name: string; line: number; quote: string; text: string}
export interface MaterialsProps {
  materials: Material[];
  scope?: number;
  busy: boolean;
  onRemove: (name: string) => void;
  onSave: (name: string, text: string) => void;
  onScreenshot: (name: string) => void;
}

function MaterialView({material, busy, location, onRemove, onSave, onScreenshot}:
  Omit<MaterialsProps, "materials"> & {material: Material; location: Location | null}) {
  const {name, text, screenshot} = material;
  const [savedText, setSavedText] = useState(text);
  const [draft, setDraft] = useState(text);
  const [error, setError] = useState("");
  // A new snapshot replaces its OCR draft; unrelated materials and busy updates keep it.
  if (savedText !== text) { setSavedText(text); setDraft(text); setError(""); }
  const preview = useRef<HTMLDetailsElement>(null);
  const mark = useRef<HTMLElement>(null);
  const selected = location?.name === name && location.text === text ? location : null;
  useLayoutEffect(() => {
    if (!selected || !mark.current || !preview.current) return;
    preview.current.open = true;
    mark.current.focus({preventScroll:true});
    mark.current.scrollIntoView({block:"center", behavior:"instant"});
  }, [selected]);
  const lines = text.split(/\r?\n/);
  const ext = name.split(".").pop()?.toLowerCase() || "";
  const kind = screenshot ? "img" : ["cfg", "conf"].includes(ext) ? "cfg" : ext === "log" ? "log" : "txt";
  const badge = screenshot ? "IMG" : kind === "cfg" ? "CFG" : kind === "log" ? "LOG" : "TXT";
  return <div className="material-item">
    {screenshot && <button type="button" className="screenshot-thumbnail" disabled={busy}
      aria-label={`查看截图 ${screenshot.name}`} onClick={() => onScreenshot(name)}>
      <img src={screenshot.imageUrl} alt={screenshot.name} loading="lazy"/>
      <span>查看原图 · 引用可定位画面</span>
    </button>}
    <div className="material-row">
      <span className="file-icon" data-kind={kind}>{badge}</span>
      <div className="file-info"><div className="file-name">{name}</div>
        <div className="file-size">{text.trimEnd().split(/\r?\n/).length} 行内容</div></div>
      <button type="button" className="remove-material" disabled={busy}
        aria-label={`移除 ${name}`} onClick={() => onRemove(name)}>移除</button>
    </div>
    <details ref={preview}>
      <summary>
        <span>{screenshot ? "查看识别文字 / 修正" : "查看内容"}</span>
      </summary>
      <div className="material-preview-toolbar">
        <CopyButton text={text} label="复制内容" copiedLabel="已复制"/>
      </div>
      <pre>{selected ? <>{lines.slice(0, selected.line - 1).join("\n")}{selected.line > 1 ? "\n" : ""}
        <mark ref={mark} tabIndex={-1} aria-label={`${name} 第${selected.line}行：${selected.quote}`}>{selected.quote}</mark>
        {"\n" + lines.slice(selected.line).join("\n")}</> : text}</pre>
      {screenshot && <details>
        <summary>修正识别文字</summary>
        <textarea rows={7} aria-label={`修正 ${screenshot.name} 的识别文字`} value={draft}
          disabled={busy} onChange={event => {setDraft(event.target.value); setError("");}}/>
        {error && <p role="alert">{error}</p>}
        <button type="button" className="secondary-button" disabled={busy} onClick={() => {
          if (!draft.trim() || draft.length > 200000) {setError("请保留1到200000字的识别内容。"); return;}
          onSave(name, draft);
        }}>保存识别修正</button>
      </details>}
    </details>
  </div>;
}

export function MaterialsView(props: MaterialsProps & {location?: Location | null}) {
  return <>{props.materials.map(material => <MaterialView key={`${props.scope || 0}:${material.name}`} {...props}
    material={material} location={props.location || null}/>)}</>;
}

interface MountedMaterials {root: Root; props: MaterialsProps; location: Location | null}
const mounted = new WeakMap<HTMLElement, MountedMaterials>();
function paint(state: MountedMaterials) {
  flushSync(() => state.root.render(<MaterialsView {...state.props} location={state.location}/>));
}
export function renderMaterials(container: HTMLElement, props: MaterialsProps) {
  let state = mounted.get(container);
  if (!state) {state = {root:createRoot(container), props, location:null}; mounted.set(container, state);}
  if (state.props.scope !== props.scope) state.location = null;
  state.props = props;
  paint(state);
}
export function configureMaterials(container: HTMLElement, changes: Pick<MaterialsProps, "busy">) {
  const state = mounted.get(container);
  if (state) {state.props = {...state.props, ...changes}; paint(state);}
}
export function locateMaterial(container: HTMLElement, name: string, line: number, quote: string) {
  const state = mounted.get(container);
  const material = state?.props.materials.find(item => item.name === name);
  if (!state || !material || material.text.split(/\r?\n/)[line - 1] !== quote) return false;
  state.location = {name, line, quote, text:material.text};
  paint(state);
  return true;
}
