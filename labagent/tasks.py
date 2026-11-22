"""Recoverable request status in the existing workspace database; never replay jobs."""
from contextlib import contextmanager
from copy import deepcopy
import json
import re
import secrets
import threading
import time
import uuid

from .conversations import dialogue

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


class Tasks:
    def __init__(self, store):
        self.store, self.lock, self.tokens = store, threading.Lock(), {}
        with store.connect() as db:
            create_task_table(db)
        self.recover()

    def recover(self):
        with self.store.connect() as db:
            db.execute("UPDATE diagnosis_tasks SET state='interrupted', message='服务重启，本轮已中断；材料保留，未自动重放检查。' WHERE state NOT IN ('completed','failed','cancelled','interrupted')")

    def get(self, task_id):
        with self.store.connect() as db:
            row = db.execute("SELECT owner,room,session_id,state,input,events,message FROM diagnosis_tasks WHERE id=?", (task_id,)).fetchone()
            result = db.execute("SELECT session_id,created_at FROM attempts WHERE json_extract(result,'$.execution.turn_id')=? ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
        if row is None:
            return None
        task = dict(zip(("owner", "room_id", "session_id", "state", "input", "events", "message"), row))
        task.update(id=task_id, input=json.loads(task["input"]), events=json.loads(task["events"]))
        if result:
            history = self.store.history(result[0])
            attempts = history["attempts"]
            index = next(i for i, item in enumerate(attempts) if item["result"].get("execution", {}).get("turn_id") == task_id)
            saved = deepcopy(attempts[index]["result"])
            saved.update(session_id=result[0], dialogue=dialogue(attempts[:index + 1]), learning_record=history["learning_record"])
            task.update(session_id=result[0], state=saved["execution"]["status"], result=saved, input=deepcopy(attempts[index]["input"]))
        return task

    def create(self, payload, *, owner=None, room=None, task_id=None):
        task_id = task_id or uuid.uuid4().hex
        clean = deepcopy(payload)
        clean.pop("personal_memory", None)
        if "learning" in clean:
            clean["learning"] = {"level": clean["learning"].get("level", 1)}
        with self.store.connect() as db:
            db.execute("INSERT INTO diagnosis_tasks VALUES (?,?,?,?,?,?,?,?,?)", (task_id, owner, room,
                payload.get("session_id"), "running", json.dumps(clean, ensure_ascii=False), "[]", "", time.time()))
        return task_id

    def record(self, task_id, event):
        with self.lock, self.store.connect() as db:
            row = db.execute("SELECT events FROM diagnosis_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return
            events = json.loads(row[0])
            # Completed results live once in attempts. Here retain only progress.
            if event["type"] == "progress":
                events.append(event)
            state = {"result": event.get("result", {}).get("execution", {}).get("status", "completed"),
                     "error": "failed", "cancelled": "cancelled"}.get(event["type"])
            if state:
                db.execute("UPDATE diagnosis_tasks SET state=?,session_id=COALESCE(?,session_id),message=?,events=?,updated=? WHERE id=?",
                           (state, event.get("session_id"), event.get("message", ""), json.dumps(events[-100:], ensure_ascii=False), time.time(), task_id))
                if event["type"] == "result":
                    db.execute("UPDATE diagnosis_tasks SET input='{}' WHERE id=?", (task_id,))
            else:
                db.execute("UPDATE diagnosis_tasks SET events=?,updated=? WHERE id=?", (json.dumps(events[-100:], ensure_ascii=False), time.time(), task_id))

    def cancel(self, task_id):
        with self.lock, self.store.connect() as db:
            db.execute("UPDATE diagnosis_tasks SET state='cancelling',message='正在停止，等待已发出的检查返回。',updated=? WHERE id=? AND state='running'", (time.time(), task_id))
        return self.get(task_id)

    def cancelled(self, task_id):
        with self.store.connect() as db:
            row = db.execute("SELECT state FROM diagnosis_tasks WHERE id=?", (task_id,)).fetchone()
        return row is None or row[0] in {"cancelling", "cancelled", "interrupted"}

    def finalizing(self, task_id):
        with self.lock, self.store.connect() as db:
            db.execute("UPDATE diagnosis_tasks SET state='finalizing' WHERE id=?", (task_id,))

    @contextmanager
    def runtime_connection(self, task_id, public_url):
        if not public_url:
            yield None
            return
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.tokens[token] = task_id
        try:
            yield {"url": public_url + "/api/tasks/runtime/status", "token": token}
        finally:
            with self.lock:
                self.tokens.pop(token, None)

    def runtime_status(self, token):
        with self.lock:
            task_id = self.tokens.get(token)
        if not task_id:
            raise ValueError("本轮任务控制凭据已失效")
        return {"cancelled": self.cancelled(task_id)}


def create_task_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS diagnosis_tasks
        (id TEXT PRIMARY KEY, owner TEXT, room TEXT, session_id TEXT, state TEXT NOT NULL,
         input TEXT NOT NULL, events TEXT NOT NULL, message TEXT NOT NULL, updated REAL NOT NULL)""")


def restore_task_tables(source, target):
    create_task_table(target)
    if not source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='diagnosis_tasks'").fetchone():
        return
    for row in source.execute("SELECT id,owner,room,session_id,state,input,events,message,updated FROM diagnosis_tasks"):
        task_id, owner, room, session, state, text, events, message, updated = row
        if not isinstance(task_id, str) or not re.fullmatch(r"[a-f0-9]{32}", task_id) or any(
                value is not None and (not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value)) for value in (owner, room, session)):
            raise ValueError("备份任务标识无效")
        payload, progress = json.loads(text), json.loads(events)
        if not isinstance(payload, dict) or not isinstance(progress, list) or not isinstance(message, str):
            raise ValueError("备份任务状态无效")
        payload.pop("personal_memory", None)
        if "learning" in payload:
            payload["learning"] = {"level": payload["learning"].get("level", 1)}
        if state not in TERMINAL:
            state, message = "interrupted", "服务重启，本轮已中断；材料保留，未自动重放检查。"
        target.execute("INSERT OR REPLACE INTO diagnosis_tasks VALUES (?,?,?,?,?,?,?,?,?)",
                       (task_id, owner, room, session, state, json.dumps(payload, ensure_ascii=False),
                        json.dumps(progress[-100:], ensure_ascii=False), message, float(updated)))
