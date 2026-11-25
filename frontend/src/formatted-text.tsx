import {useEffect, useState, type ReactNode} from "react";

const CLI_LINE_RE = /^\s*(?:<[A-Za-z0-9_.-]{1,24}>|\[[A-Za-z0-9_.-]{1,24}(?:-[^\]]+)?\]|[A-Za-z0-9_.-]{1,24}[>#]|[$#]\s+\S|(?:display|system-view|interface|ospf|area|network|ip\s+(?:address|route|pool)|ping|tracert|traceroute|undo|quit|return|save|shutdown|no\s+shutdown|router\s+ospf|show\s+|vtysh|configure\s+terminal|ip\s+link|ip\s+addr)\b)/i;

export function CopyButton({text, label = "复制", copiedLabel = "已复制", className = "copy-btn"}:
  {text: string; label?: string; copiedLabel?: string; className?: string}) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 1800);
    return () => clearTimeout(timer);
  }, [copied]);
  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }
  return <button type="button" className={className + (copied ? " is-copied" : "")}
    aria-label={copied ? copiedLabel : label} onClick={() => { void handleCopy(); }}>
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {copied ? <path d="m3.5 8.5 3 3 6-6.5"/> : <><rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M10.5 5.5v-2a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2"/></>}
    </svg>
    <span>{copied ? copiedLabel : label}</span>
  </button>;
}

export function extractCliCommand(text: string): string | null {
  const backtick = text.match(/`([^`]{3,160})`/);
  if (backtick) return backtick[1].trim();
  const lines = text.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  const cli = lines.filter(line => CLI_LINE_RE.test(line));
  return cli.length === 1 ? cli[0] : null;
}

export function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const tokenRe = /(`[^`]+`|\*\*[^*]+\*\*)/g;
  let last = 0, index = 0, match: RegExpExecArray | null;
  while ((match = tokenRe.exec(text)) !== null) {
    if (match.index > last) nodes.push(text.slice(last, match.index));
    const token = match[0];
    if (token.startsWith("`")) {
      nodes.push(<code key={`${keyPrefix}-c-${index++}`} className="inline-code">{token.slice(1, -1)}</code>);
    } else {
      nodes.push(<strong key={`${keyPrefix}-b-${index++}`}>{token.slice(2, -2)}</strong>);
    }
    last = tokenRe.lastIndex;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

type Block =
  | {kind: "code"; lang: string; content: string}
  | {kind: "cli"; content: string}
  | {kind: "heading"; level: number; text: string}
  | {kind: "list"; ordered: boolean; items: string[]}
  | {kind: "paragraph"; text: string};

function isCliLine(line: string): boolean {
  const trimmed = line.trim();
  if (!trimmed || trimmed.length > 180) return false;
  if (/^[-*•]\s+/.test(trimmed) || /^\d+[.)]\s+/.test(trimmed)) return false;
  return CLI_LINE_RE.test(trimmed);
}

function parseBlocks(source: string): Block[] {
  const blocks: Block[] = [];
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }

    const fence = line.match(/^\s*```([A-Za-z0-9_-]*)\s*$/);
    if (fence) {
      const lang = fence[1] || "CLI / 终端输出";
      const body: string[] = [];
      i++;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) {
        body.push(lines[i]);
        i++;
      }
      if (i < lines.length) i++;
      blocks.push({kind:"code", lang, content:body.join("\n")});
      continue;
    }

    const heading = line.match(/^\s*(#{1,4})\s+(.+)$/);
    if (heading) {
      blocks.push({kind:"heading", level:heading[1].length, text:heading[2].trim()});
      i++;
      continue;
    }

    if (isCliLine(line)) {
      const cliLines: string[] = [];
      while (i < lines.length && isCliLine(lines[i])) {
        cliLines.push(lines[i].trimEnd());
        i++;
      }
      blocks.push({kind:"cli", content:cliLines.join("\n")});
      continue;
    }

    const bullet = line.match(/^\s*[-*•]\s+(.+)$/);
    if (bullet) {
      const items: string[] = [];
      while (i < lines.length) {
        const next = lines[i].match(/^\s*[-*•]\s+(.+)$/);
        if (!next) break;
        items.push(next[1]);
        i++;
      }
      blocks.push({kind:"list", ordered:false, items});
      continue;
    }

    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (ordered) {
      const items: string[] = [];
      while (i < lines.length) {
        const next = lines[i].match(/^\s*\d+[.)]\s+(.+)$/);
        if (!next) break;
        items.push(next[1]);
        i++;
      }
      blocks.push({kind:"list", ordered:true, items});
      continue;
    }

    const para: string[] = [];
    while (i < lines.length && lines[i].trim() &&
      !/^\s*```/.test(lines[i]) && !/^\s*#{1,4}\s+/.test(lines[i]) &&
      !/^\s*(?:[-*•]|\d+[.)])\s+/.test(lines[i]) && !isCliLine(lines[i])) {
      para.push(lines[i]);
      i++;
    }
    blocks.push({kind:"paragraph", text:para.join("\n")});
  }
  return blocks;
}

export function FormattedContent({text}: {text: string}) {
  const blocks = parseBlocks(text || "");
  if (!blocks.length) return <div className="chat-body">{text}</div>;
  return <div className="chat-body formatted-body">
    {blocks.map((block, idx) => {
      if (block.kind === "code") {
        return <div key={idx} className="code-card">
          <div className="code-card-head">
            <span className="code-card-lang">{block.lang}</span>
            <CopyButton text={block.content} label="复制" copiedLabel="已复制"/>
          </div>
          <pre><code>{block.content}</code></pre>
        </div>;
      }
      if (block.kind === "cli") {
        return <div key={idx} className="cli-snippet">
          <div className="cli-snippet-body">
            <span className="cli-prompt" aria-hidden="true">&gt;_</span>
            <pre><code>{block.content}</code></pre>
          </div>
          <CopyButton text={block.content} label="复制命令" copiedLabel="已复制"/>
        </div>;
      }
      if (block.kind === "heading") {
        return <p key={idx} className={"formatted-heading level-" + block.level}>{renderInline(block.text, `h-${idx}`)}</p>;
      }
      if (block.kind === "list") {
        const ListTag = block.ordered ? "ol" : "ul";
        return <ListTag key={idx} className="formatted-list">
          {block.items.map((item, itemIdx) => <li key={itemIdx}>{renderInline(item, `l-${idx}-${itemIdx}`)}</li>)}
        </ListTag>;
      }
      return <p key={idx} className="formatted-paragraph">{renderInline(block.text, `p-${idx}`)}</p>;
    })}
  </div>;
}
