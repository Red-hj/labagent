"""Local attempt records, all under this project."""
from pathlib import Path
from contextlib import contextmanager
import json
import sqlite3
import uuid
import re
from .storage import data_directory
from .conversations import conversation_title, dialogue

ROOT = Path(__file__).resolve().parents[1]


class Store:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else data_directory() / "sessions.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL, result TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS attempts_session ON attempts(session_id, id)")
            db.execute("CREATE TABLE IF NOT EXISTS personal_resources (kind TEXT NOT NULL, resource_id TEXT NOT NULL, owner_id TEXT NOT NULL, PRIMARY KEY(kind,resource_id))")

    @contextmanager
    def connect(self):
        database = sqlite3.connect(self.path, timeout=10)
        try:
            with database:
                yield database
        finally:
            database.close()

    def save(self, payload, result, *, owner=None):
        if "learning" in payload:
            # A solution grant must never enter history, exports or shared rooms.
            payload = {**payload, "learning": {"level": payload["learning"].get("level", 1)}}
        session_id = payload.get("session_id") or result.get("workflow", {}).get("thread_id") or uuid.uuid4().hex
        with self.connect() as db:
            if payload.get("session_id") and not db.execute("SELECT 1 FROM attempts WHERE session_id=?", (session_id,)).fetchone():
                raise ValueError("会话不存在，请开始新实验")
            db.execute("INSERT INTO attempts(session_id, created_at, payload, result) VALUES (?, ?, ?, ?)",
                       (session_id, result["created_at"], json.dumps(payload, ensure_ascii=False),
                        json.dumps({key: value for key, value in result.items() if key != "dialogue"}, ensure_ascii=False)))
            if owner and not payload.get("session_id"):
                db.execute("INSERT OR IGNORE INTO personal_resources VALUES ('session',?,?)", (session_id, owner))
        history = self.history(session_id)
        result["session_id"] = session_id
        result["learning_record"] = history["learning_record"]
        result["dialogue"] = history["dialogue"]
        return result

    def history(self, session_id):
        with self.connect() as db:
            rows = db.execute("SELECT created_at, payload, result FROM attempts WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
        attempts = [{"created_at": row[0], "input": json.loads(row[1]), "result": json.loads(row[2])} for row in rows]
        if not attempts:
            return None
        fingerprints = [attempt["result"]["config_fingerprint"] for attempt in attempts]
        return {"session_id": session_id, "attempts": attempts, "dialogue": dialogue(attempts), "learning_record": {
            "requests": len(attempts),
            "config_revisions": sum(before != after for before, after in zip(fingerprints, fingerprints[1:])),
            "max_hint_level": max(attempt["result"]["hint_level"] for attempt in attempts),
            "encountered_faults": sorted({f["code"] for attempt in attempts for f in attempt["result"]["findings"]}),
            "latest_status": attempts[-1]["result"]["status"],
            "assessment": "仅记录本地提交、配置变化和提示等级，不推断独立完成程度，不自动给分。",
        }}

    def sessions(self, *, limit=30, before=None, query="", personal=False, user_id=None, room_id=None):
        if type(limit) is not int or not 1 <= limit <= 100 or (before is not None and (type(before) is not int or not 1 <= before <= 2**63 - 1)):
            raise ValueError("历史列表每页须为1～100条，游标须为正整数")
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError("历史搜索不超过200字")
        fields = """SELECT g.session_id, last.created_at, g.requests, last.id,
            json_extract(first.payload,'$.question') AS question,
            json_extract(first.result,'$.investigation.experiment_goal') AS goal,
            (SELECT key FROM json_each(first.payload,'$.configs') LIMIT 1) AS material,
            CASE WHEN json_extract(last.result,'$.llm.status') IS NOT NULL AND json_extract(last.result,'$.llm.status')!='ok'
                 THEN COALESCE(json_extract(last.result,'$.llm.message'),json_extract(last.result,'$.hint'),'')
                 ELSE COALESCE(json_extract(last.result,'$.investigation.reply'),json_extract(last.result,'$.investigation.summary'),
                               json_extract(last.result,'$.hint'),'') END AS preview,
            json_extract(last.result,'$.status') AS status,
            COALESCE(json_extract(first.payload,'$.diagnostic_mode'),'ospf_rules') AS mode,
            COALESCE((SELECT substr(p.owner_id,6) FROM personal_resources p WHERE p.kind='session' AND p.resource_id=g.session_id AND p.owner_id LIKE 'room:%'),
                     json_extract(last.payload,'$.room_id'),json_extract(first.payload,'$.room_id')) AS room_id,
            (SELECT group_concat(key,' ') FROM json_each(first.payload,'$.configs')) AS material_names
            FROM grouped g JOIN attempts first ON first.id=g.first_id JOIN attempts last ON last.id=g.last_id"""
        with self.connect() as db:
            access, access_values = "", ()
            if personal:
                access = """ AND (NOT EXISTS(SELECT 1 FROM personal_resources p WHERE p.kind='session' AND p.resource_id=summaries.session_id)
                    OR EXISTS(SELECT 1 FROM personal_resources p WHERE p.kind='session' AND p.resource_id=summaries.session_id
                        AND (p.owner_id=? OR p.owner_id=? OR p.owner_id IN
                            (SELECT 'room:'||room_id FROM personal_room_members WHERE user_id=?))))"""
                access_values = (user_id, 'room:' + room_id if room_id else None, user_id)
            rows = db.execute("""WITH grouped AS (SELECT session_id, MIN(id) AS first_id, MAX(id) AS last_id,
                              COUNT(*) AS requests FROM attempts GROUP BY session_id), summaries AS (""" + fields + """)
                              SELECT * FROM summaries WHERE (? IS NULL OR id<?) AND
                              (?='' OR instr(lower(COALESCE(question,'') || ' ' || COALESCE(goal,'') || ' ' ||
                                                  COALESCE(material_names,'') || ' ' || preview), lower(?))>0)
                              """ + access + " ORDER BY id DESC LIMIT ?", (before, before, query.strip(), query.strip(), *access_values, limit)).fetchall()
        return [{"session_id": row[0], "updated_at": row[1], "requests": row[2], "cursor": row[3],
                 "title": conversation_title(row[4], row[5], row[6]), "preview": " ".join(row[7].split())[:160],
                 "status": row[8], "mode": row[9], "room_id": row[10]} for row in rows]

    def delete_session(self, session_id, *, workflow_database=None):
        if not isinstance(session_id, str) or not re.fullmatch(r"[a-f0-9]{32}", session_id):
            raise ValueError("session_id 格式错误")
        with self.connect() as db:
            if workflow_database is not None and Path(workflow_database).is_file():
                db.execute("ATTACH DATABASE ? AS workflow", (str(workflow_database),))
                workflow_tables = {row[0] for row in db.execute("SELECT name FROM workflow.sqlite_master WHERE type='table'")}
            else:
                workflow_tables = set()
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT COUNT(*) FROM attempts WHERE session_id=?", (session_id,)).fetchone()[0]
            if not count:
                return None
            for table in ("checkpoints", "writes"):
                if table in workflow_tables:
                    db.execute(f"DELETE FROM workflow.{table} WHERE thread_id IN (?,?)", (session_id, "open:" + session_id))
            if db.execute("SELECT 1 FROM main.sqlite_master WHERE type='table' AND name='rooms'").fetchone():
                db.execute("UPDATE rooms SET data=json_set(data,'$.session',NULL) WHERE json_extract(data,'$.session.session_id')=?", (session_id,))
            db.execute("DELETE FROM attempts WHERE session_id=?", (session_id,))
            db.execute("DELETE FROM personal_resources WHERE kind='session' AND resource_id=?", (session_id,))
            for suffix in (".json", ".md"):
                (self.path.parent / "exports" / (session_id + suffix)).unlink(missing_ok=True)
        return {"session_id": session_id, "deleted": True, "deleted_requests": count}
