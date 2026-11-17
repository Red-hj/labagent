"""Personal sign-in and account-scoped, inspectable troubleshooting memories."""
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import uuid

from .knowledge import tokens

ROUNDS = 600_000
COOKIE = "labagent_personal"
LIFETIME = 30 * 24 * 3600
KINDS = {"issue", "preference", "environment"}
BACKUP_TABLES = {
    "personal_users": "id,username,display_name,salt,password_hash,memory_enabled,created_at",
    "personal_memories": "id,user_id,kind,title,topic,content,occurrences,created_at,updated_at",
    "personal_memory_seen": "user_id,session_id",
    "personal_room_members": "user_id,room_id",
    "personal_resources": "kind,resource_id,owner_id",
}
SENSITIVE = re.compile(r"password|passwd|secret|api[_-]?key|authorization|bearer|private.key|密码|密钥|口令|\btoken\b|sk-[a-z0-9]{12,}|BEGIN.*PRIVATE KEY", re.I)


class SignInError(ValueError):
    def __init__(self, message, status=401):
        super().__init__(message)
        self.status = status


def clean_text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"请填写1～{limit}字的内容")
    if SENSITIVE.search(value):
        raise ValueError("记忆中请勿填写密码、访问令牌或密钥")
    return value.strip()


def validate_memory_context(value):
    if not isinstance(value, list) or len(value) > 6:
        raise ValueError("本轮记忆最多6条")
    for item in value:
        if not isinstance(item, dict) or set(item) != {"kind", "title", "content", "occurrences"}:
            raise ValueError("记忆上下文格式无效")
        if item["kind"] not in KINDS or type(item["occurrences"]) is not int or not 1 <= item["occurrences"] <= 1_000_000:
            raise ValueError("记忆类型或次数无效")
        clean_text(item["title"], 120)
        clean_text(item["content"], 600)
    return value


class Personal:
    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock
        self.lock = threading.Lock()
        self.failures = {}
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS personal_users (
                    id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL,
                    salt TEXT NOT NULL, password_hash TEXT NOT NULL, memory_enabled INTEGER NOT NULL,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS personal_tokens (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS personal_memories (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL,
                    title TEXT NOT NULL, topic TEXT NOT NULL, content TEXT NOT NULL,
                    occurrences INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(user_id,kind,topic));
                CREATE TABLE IF NOT EXISTS personal_memory_seen (
                    user_id TEXT NOT NULL, session_id TEXT NOT NULL, PRIMARY KEY(user_id,session_id));
                CREATE TABLE IF NOT EXISTS personal_room_members (
                    user_id TEXT NOT NULL, room_id TEXT NOT NULL, PRIMARY KEY(user_id,room_id));
            """)

    @staticmethod
    def public(row):
        return {"id": row[0], "username": row[1], "display_name": row[2], "memory_enabled": bool(row[3])}

    def user(self, token):
        if not token or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            return None
        with self.store.connect() as db:
            row = db.execute("SELECT u.id,u.username,u.display_name,u.memory_enabled FROM personal_users u "
                "JOIN personal_tokens t ON t.user_id=u.id WHERE t.token_hash=? AND t.expires>?",
                (hashlib.sha256(token.encode()).hexdigest(), self.clock())).fetchone()
        return self.public(row) if row else None

    def _token(self, user_id):
        token = secrets.token_urlsafe(32)
        with self.store.connect() as db:
            db.execute("DELETE FROM personal_tokens WHERE expires<=?", (self.clock(),))
            db.execute("INSERT INTO personal_tokens VALUES (?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), user_id, self.clock() + LIFETIME))
            db.execute("DELETE FROM personal_tokens WHERE user_id=? AND token_hash NOT IN "
                "(SELECT token_hash FROM personal_tokens WHERE user_id=? ORDER BY expires DESC LIMIT 6)", (user_id, user_id))
        return token

    def authenticate(self, payload, *, register=False, client="local"):
        username, password = payload.get("username"), payload.get("password")
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{2,31}", username):
            raise ValueError("用户名须为3～32位英文字母、数字、点、下划线或短横线，以字母开头")
        if not isinstance(password, str) or not 10 <= len(password) <= 128:
            raise ValueError("密码须为10～128个字符")
        username = username.lower()
        with self.lock:
            key = (client, username)
            count, until = self.failures.get(key, (0, 0))
            if until > self.clock() and count >= 5:
                raise SignInError("尝试次数较多，请10分钟后再试", 429)
            if until <= self.clock():
                count = 0
            with self.store.connect() as db:
                row = db.execute("SELECT id,salt,password_hash FROM personal_users WHERE username=?", (username,)).fetchone()
                salt = secrets.token_hex(16) if register else (row[1] if row else "0" * 32)
                digest = "pbkdf2_sha256$600000$" + hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ROUNDS).hex()
                if register:
                    if row:
                        raise ValueError("这个用户名已存在，请登录或换一个用户名")
                    display = payload.get("display_name", username)
                    if not isinstance(display, str) or not 1 <= len(display.strip()) <= 40:
                        raise ValueError("昵称须为1～40字")
                    enabled = payload.get("memory_enabled", True)
                    if type(enabled) is not bool:
                        raise ValueError("记忆开关须为布尔值")
                    user_id = uuid.uuid4().hex
                    db.execute("INSERT INTO personal_users VALUES (?,?,?,?,?,?,?)", (user_id, username,
                        display.strip(), salt, digest, int(enabled), datetime.now(timezone.utc).isoformat()))
                else:
                    if not row or not hmac.compare_digest(digest, row[2]):
                        self.failures[key] = (count + 1, self.clock() + 600)
                        raise SignInError("用户名或密码不正确")
                    user_id = row[0]
            self.failures.pop(key, None)
        token = self._token(user_id)
        return self.user(token), token

    def logout(self, token):
        with self.store.connect() as db:
            db.execute("DELETE FROM personal_tokens WHERE token_hash=?", (hashlib.sha256((token or "").encode()).hexdigest(),))

    def enable_memory(self, user_id, enabled):
        if type(enabled) is not bool:
            raise ValueError("记忆开关须为布尔值")
        with self.store.connect() as db:
            db.execute("UPDATE personal_users SET memory_enabled=? WHERE id=?", (int(enabled), user_id))

    def list_memories(self, user_id):
        with self.store.connect() as db:
            rows = db.execute("SELECT id,kind,title,content,occurrences,created_at,updated_at FROM personal_memories "
                "WHERE user_id=? ORDER BY occurrences DESC,updated_at DESC", (user_id,)).fetchall()
        return [dict(zip(("id", "kind", "title", "content", "occurrences", "created_at", "updated_at"), row)) for row in rows]

    def save_memory(self, user_id, payload):
        kind = payload.get("kind", "preference")
        if kind not in KINDS:
            raise ValueError("请选择常见问题、排查偏好或实验环境")
        title, content = clean_text(payload.get("title"), 120), clean_text(payload.get("content"), 600)
        memory_id = payload.get("id")
        topic = re.sub(r"\s+", "", title.casefold())
        now = datetime.now(timezone.utc).isoformat()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if memory_id:
                row = db.execute("SELECT id FROM personal_memories WHERE id=? AND user_id=?", (memory_id, user_id)).fetchone()
                if not row:
                    raise ValueError("这条记忆不存在")
                duplicate = db.execute("SELECT id FROM personal_memories WHERE user_id=? AND kind=? AND topic=? AND id!=?",
                    (user_id, kind, topic, memory_id)).fetchone()
                if duplicate:
                    raise ValueError("已有同名记忆，请直接编辑原记录")
                db.execute("UPDATE personal_memories SET kind=?,title=?,topic=?,content=?,updated_at=? WHERE id=? AND user_id=?",
                    (kind, title, topic, content, now, memory_id, user_id))
            else:
                row = db.execute("SELECT id FROM personal_memories WHERE user_id=? AND kind=? AND topic=?", (user_id, kind, topic)).fetchone()
                if row:
                    memory_id = row[0]
                    db.execute("UPDATE personal_memories SET content=?,updated_at=? WHERE id=?", (content, now, memory_id))
                else:
                    if db.execute("SELECT COUNT(*) FROM personal_memories WHERE user_id=?", (user_id,)).fetchone()[0] >= 100:
                        raise ValueError("最多保存100条记忆，请先整理旧记录")
                    memory_id = uuid.uuid4().hex
                    db.execute("INSERT INTO personal_memories VALUES (?,?,?,?,?,?,?,?,?)",
                        (memory_id, user_id, kind, title, topic, content, 1, now, now))
            row = db.execute("SELECT id,kind,title,content,occurrences,created_at,updated_at FROM personal_memories WHERE id=?", (memory_id,)).fetchone()
        return dict(zip(("id", "kind", "title", "content", "occurrences", "created_at", "updated_at"), row))

    def delete_memory(self, user_id, memory_id=None):
        with self.store.connect() as db:
            if memory_id:
                return db.execute("DELETE FROM personal_memories WHERE user_id=? AND id=?", (user_id, memory_id)).rowcount
            return db.execute("DELETE FROM personal_memories WHERE user_id=?", (user_id,)).rowcount

    def context(self, user, query):
        if not user or not user["memory_enabled"]:
            return []
        words = set(tokens(query))
        ranked = []
        for memory in self.list_memories(user["id"]):
            overlap = len(words & set(tokens(memory["title"] + " " + memory["content"])))
            if memory["kind"] != "issue" or overlap:
                ranked.append((memory["kind"] != "issue", overlap, memory["occurrences"], memory))
        ranked.sort(key=lambda item: item[:3], reverse=True)
        return [{key: memory[key] for key in ("kind", "title", "content", "occurrences")}
                for *_, memory in ranked[:6]]

    def remember_issue(self, user_id, payload, result):
        if (payload.get("room_id") or self.session_room(result.get("session_id")) or
            payload.get("evidence_kind") == "demo" or result.get("llm", {}).get("status") != "ok"):
            return
        title = payload.get("question", "").strip()
        if not title or ("上传" in title and title.startswith(("请根据", "请分析"))):
            title = result.get("investigation", {}).get("experiment_goal", "").strip()
        if len(title) < 4 or SENSITIVE.search(title):
            return
        title = title[:120]
        content = "曾排查：" + title + "。历史经验仅供参考，下次仍需核对当前材料。"
        topic = re.sub(r"\s+", "", title.casefold())
        now = datetime.now(timezone.utc).isoformat()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            enabled = db.execute("SELECT memory_enabled FROM personal_users WHERE id=?", (user_id,)).fetchone()
            if not enabled or not enabled[0]:
                return
            if db.execute("SELECT 1 FROM personal_memory_seen WHERE user_id=? AND session_id=?", (user_id, result["session_id"])).fetchone():
                return
            db.execute("INSERT INTO personal_memory_seen VALUES (?,?)", (user_id, result["session_id"]))
            existing = db.execute("SELECT id FROM personal_memories WHERE user_id=? AND kind='issue' AND topic=?", (user_id, topic)).fetchone()
            if existing:
                db.execute("UPDATE personal_memories SET occurrences=MIN(occurrences+1,1000000),updated_at=? WHERE id=?", (now, existing[0]))
            elif db.execute("SELECT COUNT(*) FROM personal_memories WHERE user_id=?", (user_id,)).fetchone()[0] < 100:
                db.execute("INSERT INTO personal_memories VALUES (?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, user_id, "issue", title, topic, content, 1, now, now))

    def join_room(self, user_id, room_id):
        if user_id:
            with self.store.connect() as db:
                db.execute("INSERT OR IGNORE INTO personal_room_members VALUES (?,?)", (user_id, room_id))

    def session_room(self, session_id):
        if not session_id:
            return None
        with self.store.connect() as db:
            row = db.execute("SELECT substr(owner_id,6) FROM personal_resources WHERE kind='session' AND resource_id=? AND owner_id LIKE 'room:%'",
                (session_id,)).fetchone() or db.execute("SELECT json_extract(payload,'$.room_id') FROM attempts WHERE session_id=? AND json_extract(payload,'$.room_id') IS NOT NULL ORDER BY id LIMIT 1",
                (session_id,)).fetchone()
        return row[0] if row and isinstance(row[0], str) and re.fullmatch(r"[a-f0-9]{32}", row[0]) else None

    def owns(self, kind, resource_id, user_id=None, room_id=None):
        with self.store.connect() as db:
            row = db.execute("SELECT owner_id FROM personal_resources WHERE kind=? AND resource_id=?", (kind, resource_id)).fetchone()
            if not row:
                return True  # Existing team records retain their original sharing scope.
            owner = row[0]
            if owner == user_id:
                return True
            if owner.startswith("room:"):
                room = owner[5:]
                return room == room_id or bool(user_id and db.execute("SELECT 1 FROM personal_room_members WHERE user_id=? AND room_id=?", (user_id, room)).fetchone())
            if kind == "recording" and room_id:
                room = db.execute("SELECT data FROM rooms WHERE id=?", (room_id,)).fetchone()
                return bool(room and resource_id in json.loads(room[0]).get("recordings", []))
            if kind == "recording" and user_id:
                return bool(db.execute("SELECT 1 FROM personal_room_members m JOIN rooms r ON r.id=m.room_id "
                    "JOIN json_each(r.data,'$.recordings') media WHERE m.user_id=? AND media.value=?", (user_id, resource_id)).fetchone())
            return False


def restore_personal_tables(source, target):
    """Copy only the known, validated personal schema; never restore login tokens."""
    tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    users = set()
    if "personal_users" in tables:
        for row in source.execute("SELECT " + BACKUP_TABLES["personal_users"] + " FROM personal_users"):
            user, username, display, salt, digest, enabled, created = row
            if (not isinstance(user, str) or not re.fullmatch(r"[a-f0-9]{32}", user) or
                not isinstance(username, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{2,31}", username) or
                not isinstance(display, str) or not 1 <= len(display) <= 40 or
                not isinstance(salt, str) or not re.fullmatch(r"[a-f0-9]{32}", salt) or
                not isinstance(digest, str) or not re.fullmatch(r"pbkdf2_sha256\$600000\$[a-f0-9]{64}", digest) or
                enabled not in (0, 1) or not isinstance(created, str) or len(created) > 80):
                raise ValueError("备份中的个人账号格式无效")
            target.execute("INSERT INTO personal_users VALUES (?,?,?,?,?,?,?)", row)
            users.add(user)
    for table in ("personal_memories", "personal_memory_seen", "personal_room_members", "personal_resources"):
        if table not in tables:
            continue
        for row in source.execute("SELECT " + BACKUP_TABLES[table] + " FROM " + table):
            if table == "personal_memories":
                mid, uid, kind, title, topic, content, count, created, updated = row
                validate_memory_context([{"kind": kind, "title": title, "content": content, "occurrences": count}])
                if (uid not in users or not re.fullmatch(r"[a-f0-9]{32}", mid) or
                    topic != re.sub(r"\s+", "", title.casefold()) or
                    any(not isinstance(stamp, str) or len(stamp) > 80 for stamp in (created, updated))):
                    raise ValueError("备份中的个人记忆格式无效")
            elif table == "personal_resources":
                kind, rid, owner = row
                if kind not in ("session", "recording") or not isinstance(rid, str) or not re.fullmatch(r"[a-f0-9]{32}", rid):
                    raise ValueError("备份中的个人资料归属无效")
                room_owner = isinstance(owner, str) and owner.startswith("room:") and target.execute("SELECT 1 FROM rooms WHERE id=?", (owner[5:],)).fetchone()
                if owner not in users and not room_owner:
                    raise ValueError("备份缺少资料所属的账号或共享实验")
                if kind == "session" and not target.execute("SELECT 1 FROM attempts WHERE session_id=?", (rid,)).fetchone():
                    raise ValueError("备份缺少个人对话")
            else:
                uid, rid = row
                if uid not in users or not isinstance(rid, str) or not re.fullmatch(r"[a-f0-9]{32}", rid):
                    raise ValueError("备份中的账号关联无效")
                if table == "personal_room_members" and not target.execute("SELECT 1 FROM rooms WHERE id=?", (rid,)).fetchone():
                    raise ValueError("备份缺少共享实验")
            target.execute("INSERT INTO " + table + " VALUES (" + ",".join("?" for _ in row) + ")", row)
