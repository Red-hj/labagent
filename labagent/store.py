"""Local attempt records, all under this project."""
from pathlib import Path
from contextlib import contextmanager
import json
import sqlite3
import uuid

ROOT = Path(__file__).resolve().parents[1]


class Store:
    def __init__(self, path=None):
        self.path = Path(path or ROOT / "runtime" / "sessions.sqlite3")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL, result TEXT NOT NULL)")

    @contextmanager
    def connect(self):
        database = sqlite3.connect(self.path, timeout=10)
        try:
            with database:
                yield database
        finally:
            database.close()

    def save(self, payload, result):
        session_id = payload.get("session_id") or result.get("workflow", {}).get("thread_id") or uuid.uuid4().hex
        with self.connect() as db:
            if payload.get("session_id") and not db.execute("SELECT 1 FROM attempts WHERE session_id=?", (session_id,)).fetchone():
                raise ValueError("会话不存在，请开始新实验")
            db.execute("INSERT INTO attempts(session_id, created_at, payload, result) VALUES (?, ?, ?, ?)",
                       (session_id, result["created_at"], json.dumps(payload, ensure_ascii=False), json.dumps(result, ensure_ascii=False)))
        history = self.history(session_id)
        result["session_id"] = session_id
        result["learning_record"] = history["learning_record"]
        return result

    def history(self, session_id):
        with self.connect() as db:
            rows = db.execute("SELECT created_at, payload, result FROM attempts WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
        attempts = [{"created_at": row[0], "input": json.loads(row[1]), "result": json.loads(row[2])} for row in rows]
        if not attempts:
            return None
        fingerprints = [attempt["result"]["config_fingerprint"] for attempt in attempts]
        return {"session_id": session_id, "attempts": attempts, "learning_record": {
            "requests": len(attempts),
            "config_revisions": sum(before != after for before, after in zip(fingerprints, fingerprints[1:])),
            "max_hint_level": max(attempt["result"]["hint_level"] for attempt in attempts),
            "encountered_faults": sorted({f["code"] for attempt in attempts for f in attempt["result"]["findings"]}),
            "latest_status": attempts[-1]["result"]["status"],
            "assessment": "仅记录本地提交、配置变化和提示等级，不推断独立完成程度，不自动给分。",
        }}

    def sessions(self):
        with self.connect() as db:
            rows = db.execute("SELECT session_id, MAX(created_at), COUNT(*) FROM attempts GROUP BY session_id ORDER BY MAX(id) DESC LIMIT 30").fetchall()
        return [{"session_id": row[0], "updated_at": row[1], "requests": row[2]} for row in rows]
