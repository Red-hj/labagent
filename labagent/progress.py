"""Public execution progress for the active web request."""
from contextlib import contextmanager
from contextvars import ContextVar
import threading
import uuid

_sink = ContextVar("labagent_progress", default=None)


class ProgressStream:
    """One request's public events; arguments and observations stay in results."""
    def __init__(self, callback, session_id=None):
        self.callback, self.session_id = callback, session_id
        self.turn_id = uuid.uuid4().hex
        self.sequence = 0
        self.items = {}
        self.lock = threading.Lock()

    def send(self, value, *, event="turn.progress"):
        with self.lock:
            self.sequence += 1
            value = {**value, "event": event, "turn_id": self.turn_id,
                     "session_id": self.session_id, "sequence": self.sequence}
            if "item" in value:
                self.items[value["item"]["id"]] = dict(value["item"])
            if self.callback:
                self.callback(value)
            return value

    def summary(self, result):
        failed = result.get("mode") == "open_network" and result.get("llm", {}).get("status") != "ok"
        return {"turn_id": self.turn_id, "status": "failed" if failed else "completed",
                "items": [dict(item) for item in self.items.values()]}


@contextmanager
def progress_to(callback, *, session_id=None):
    stream = ProgressStream(callback, session_id)
    token = _sink.set(stream)
    try:
        yield stream
    finally:
        _sink.reset(token)


def emit_progress(stage, message, *, event="turn.progress", item=None):
    stream = _sink.get()
    if stream:
        value = {"type": "progress", "stage": stage, "message": message}
        if item is not None:
            value["item"] = item
        stream.send(value, event=event)


def execution_summary(result):
    stream = _sink.get()
    return stream.summary(result) if stream else None


class ToolProgress:
    """A tool becomes successful only after any required result review."""
    def __init__(self, tool, label, round):
        self.id, self.tool, self.label, self.round = uuid.uuid4().hex, tool, label, round
        self.publish("running", "tool", label, "item.started")

    def publish(self, status, stage, message, event):
        emit_progress(stage, message, event=event,
                      item={"id": self.id, "tool": self.tool, "round": self.round, "status": status})

    def review(self):
        self.publish("pending_review", "learning_review", "正在核对当前提示的级别与依据…", "item.updated")

    def complete(self):
        self.publish("completed", "tool_completed", self.label.replace("正在", "已完成：", 1), "item.completed")

    def fail(self):
        self.publish("failed", "tool_failed", self.label.replace("正在", "未通过：", 1) + "，将根据返回信息继续处理。", "item.completed")
