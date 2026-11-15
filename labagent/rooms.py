"""Shared lab materials, attributed changes and hand-off tasks in project SQLite."""
from datetime import datetime, timezone
import json
import uuid


class RoomConflict(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Rooms:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS rooms (id TEXT PRIMARY KEY, data TEXT NOT NULL)")

    def get(self, room_id):
        with self.store.connect() as db:
            row = db.execute("SELECT data FROM rooms WHERE id=?", (room_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def create(self, name, actor):
        actor = self.text(actor, "请填写你的姓名", 40)
        name = self.text(name, "请填写实验名称", 80)
        room = {"id": uuid.uuid4().hex, "name": name, "revision": 0, "materials": {}, "versions": {}, "members": {},
                "tasks": [], "activity": [], "session": None, "recordings": [], "question": ""}
        self.event(room, actor, "created", "创建协作实验")
        with self.store.connect() as db:
            db.execute("INSERT INTO rooms VALUES (?,?)", (room["id"], json.dumps(room, ensure_ascii=False)))
        return room

    @staticmethod
    def text(value, message, limit):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(message)
        return value.strip()

    @staticmethod
    def event(room, actor, kind, text):
        room["activity"].append({"actor": actor, "kind": kind, "text": text, "at": now()})
        room["activity"] = room["activity"][-100:]

    def update(self, room_id, payload):
        actor = self.text(payload.get("actor"), "请填写你的姓名", 40)
        member_id = self.text(payload.get("member_id"), "缺少协作成员编号", 64)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM rooms WHERE id=?", (room_id,)).fetchone()
            if not row:
                raise ValueError("协作实验不存在，请检查邀请链接")
            room = json.loads(row[0])
            room.setdefault("versions", {k: v["revision"] for k, v in room["materials"].items()})
            member = room["members"].get(member_id)
            if not member:
                self.event(room, actor, "joined", "加入协作实验")
            room["members"][member_id] = {"name": actor, "seen_at": now()}
            action = payload.get("action", "join")
            if action == "materials":
                patches = payload.get("patches", [])
                if not isinstance(patches, list) or len(patches) > 40:
                    raise ValueError("一次最多更新20份材料")
                for patch in patches:
                    if not isinstance(patch, dict):
                        raise ValueError("材料更新格式错误")
                    name = self.text(patch.get("name"), "材料名称不超过120字", 120)
                    old = room["materials"].get(name)
                    version = room["versions"].get(name, 0)
                    if patch.get("base_revision") != version:
                        raise RoomConflict(name + "已被队友更新，请先同步后再修改")
                    room["versions"][name] = version + 1
                    text = patch.get("content")
                    if text is None:
                        room["materials"].pop(name, None)
                    else:
                        if not isinstance(text, str) or not 0 < len(text) <= 200000:
                            raise ValueError("每份材料需要1到200000字")
                        room["materials"][name] = {"content": text, "revision": version + 1,
                                                   "author": actor, "updated_at": now()}
                    self.event(room, actor, "material", ("移除 " if text is None else "更新 " if old else "补充 ") + name)
                if len(room["materials"]) > 20:
                    raise ValueError("一个实验最多保存20份材料")
                if "question" in payload:
                    question = payload["question"]
                    if not isinstance(question, str) or len(question) > 10000:
                        raise ValueError("实验问题不超过10000字")
                    if question != room["question"]:
                        if payload.get("base_question") != room["question"]:
                            raise RoomConflict("实验问题已被队友更新，请先同步")
                        room["question"] = question
                recordings = payload.get("recordings", [])
                if not isinstance(recordings, list) or not all(isinstance(i, str) and len(i) == 32 and all(c in "abcdef0123456789" for c in i) for i in recordings):
                    raise ValueError("截图或录屏编号错误")
                room["recordings"] = list(dict.fromkeys(room["recordings"] + recordings))[-15:]
                if patches or room["question"] != json.loads(row[0])["question"]:
                    room["revision"] += 1
            elif action == "task":
                title = self.text(payload.get("title"), "请填写检查内容，最多200字", 200)
                assignee = self.text(payload.get("assignee"), "请填写负责队友的姓名", 40)
                room["tasks"].append({"id": uuid.uuid4().hex, "title": title, "assignee": assignee, "author": actor, "done": False})
                self.event(room, actor, "task", "交给" + assignee + "：" + title)
            elif action == "complete":
                task = next((t for t in room["tasks"] if t["id"] == payload.get("task_id")), None)
                if not task:
                    raise ValueError("这项检查不存在")
                task["done"] = not task["done"]
                self.event(room, actor, "task", ("完成 " if task["done"] else "重新打开 ") + task["title"])
            elif action != "join":
                raise ValueError("未知协作操作")
            db.execute("UPDATE rooms SET data=? WHERE id=?", (json.dumps(room, ensure_ascii=False), room_id))
        return room

    def prepare_diagnosis(self, payload):
        room = self.get(payload.get("room_id"))
        if not room:
            raise ValueError("协作实验不存在")
        if payload.get("room_revision") != room["revision"]:
            raise RoomConflict("队友刚补充了材料，请同步后再开始诊断")
        actor = self.text(payload.get("room_actor"), "请填写你的姓名", 40)
        prepared = dict(payload)
        prepared["configs"] = {k: v["content"] for k, v in room["materials"].items()}
        prepared["question"] = room["question"] or payload.get("question")
        prepared["recordings"] = room["recordings"]
        session = room.get("session")
        if session:
            prepared["session_id"] = session["session_id"]
        else:
            prepared.pop("session_id", None)
        prepared["room_actor"] = actor
        return prepared

    def record_diagnosis(self, payload, result):
        if not payload.get("room_id"):
            return
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM rooms WHERE id=?", (payload["room_id"],)).fetchone()
            room = json.loads(row[0])
            if room["revision"] == payload["room_revision"]:
                room["session"] = {"session_id": result["session_id"], "revision": room["revision"], "attempts": result.get("learning_record", {}).get("requests", 1)}
                text = "完成第" + str(room["revision"]) + "版材料诊断"
            else:
                text = "完成旧版材料诊断；队友已补充新材料"
            self.event(room, payload["room_actor"], "diagnosis", text)
            db.execute("UPDATE rooms SET data=? WHERE id=?", (json.dumps(room, ensure_ascii=False), room["id"]))
