export interface PendingRequest {
  text: string;
  type: string;
  materialCount: number;
  started: number;
  message: string;
  delivered: boolean;
  elapsed: number;
  hint: string | null;
  error?: string;
  onRetry?: () => void;
}

export interface ProgressEntry {
  key: string | null;
  message: string;
  status: string;
}

export function PendingTurn({request}: {request: PendingRequest | null}) {
  if (!request) return null;
  const failed = Boolean(request.error);
  const delivery = failed ? (request.delivered ? "回复未完成" : "发送未完成") : (request.delivered ? "已发送" : "发送中…");
  return <div className="chat-turn chat-transient">
    <div className="chat-message chat-user">
      <p className="chat-label">你<time className="chat-time" dateTime={new Date(request.started).toISOString()}>
        {new Date(request.started).toLocaleTimeString("zh-CN", {hour:"2-digit", minute:"2-digit"})}
      </time></p>
      <div className="chat-body">{request.text}</div>
      {request.type === "analyze" && request.materialCount > 0 && <div className="chat-materials">{request.materialCount} 份实验材料</div>}
    </div>
    <span className={"chat-delivery " + (failed ? "failed" : request.delivered ? "delivered" : "sending")}>{delivery}</span>
    <div className={"chat-message chat-assistant " + (failed ? "chat-failed" : "chat-pending")}>
      <p className="chat-label">LabAgent</p>
      {failed ? <>
        <p className="chat-error-text">{request.error}</p>
        <button type="button" className="text-button chat-retry" onClick={request.onRetry}>重新发送</button>
      </> : <>
        <div className="chat-activity">
          <span className="typing-dots" aria-hidden="true"><i/><i/><i/></span>
          <span className="pending-text">{request.message}</span>
          <span className="chat-elapsed" aria-hidden="true">{request.elapsed >= 5 ? `${request.elapsed}秒` : ""}</span>
        </div>
        <p className="pending-hint" hidden={!request.hint}>{request.hint}</p>
      </>}
    </div>
  </div>;
}

export function ProgressList({entries}: {entries: ProgressEntry[]}) {
  return <>{entries.map((entry, index) => <li key={entry.key || `progress:${index}`} data-status={entry.status}>{entry.message}</li>)}</>;
}
