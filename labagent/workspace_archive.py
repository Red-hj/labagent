"""Portable workspace snapshots: records, shared rooms, media and course notes."""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import zipfile

from . import __version__
from .store import Store
from .rooms import Rooms
from .recordings import IMAGE_SUFFIXES, VIDEO_SUFFIXES

MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
FORMAT = "labagent-workspace-v1"
ID = r"[a-f0-9]{32}"
MEDIA = re.compile(r"recordings/" + ID + r"/(?:status\.json|transcript\.log|preview\.jpg|image\.(?:png|jpg|jpeg|webp)|video\.(?:mp4|webm|mov|mkv)|frames/[0-9]+\.jpg)")


class WorkspaceBusy(ValueError):
    pass


class WorkspaceGate:
    """Keep snapshots exclusive while normal requests may run concurrently."""
    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.exclusive = False

    @contextmanager
    def activity(self, *, maintenance=False):
        with self.lock:
            if self.exclusive or (maintenance and self.active):
                raise WorkspaceBusy("工作区正在处理其他请求，请结束上传、识别或诊断后重试。")
            if maintenance:
                self.exclusive = True
            else:
                self.active += 1
        try:
            yield
        finally:
            with self.lock:
                if maintenance:
                    self.exclusive = False
                else:
                    self.active -= 1


class WorkspaceArchive:
    def __init__(self, store, recordings, notes, workflow_database):
        self.store, self.recordings = store, recordings
        from .personal import Personal
        Personal(store)
        self.notes = Path(notes)
        self.workflow_database = Path(workflow_database)
        self.gate = WorkspaceGate()
        self.temporary = store.path.parent / "workspace-temp"
        self.temporary.mkdir(parents=True, exist_ok=True)

    def summary(self):
        with self.store.connect() as db:
            sessions = db.execute("SELECT COUNT(DISTINCT session_id) FROM attempts").fetchone()[0]
            rooms = db.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            users = db.execute("SELECT COUNT(*) FROM personal_users").fetchone()[0] if "personal_users" in tables else 0
            memories = db.execute("SELECT COUNT(*) FROM personal_memories").fetchone()[0] if "personal_memories" in tables else 0
        media = list(self.recordings.root.glob("*/status.json"))
        pending = any(json.loads(path.read_text(encoding="utf-8")).get("status") not in ("ready", "error") for path in media)
        documents = len(list(self.notes.glob("*.json")))
        return {"sessions": sessions, "rooms": rooms, "recordings": len(media), "documents": documents, "users": users, "memories": memories,
                "processing_media": pending, "can_restore": not (sessions or rooms or documents or users or
                    any(self.recordings.root.iterdir()) or self.workflow_database.exists())}

    @contextmanager
    def export(self):
        with tempfile.TemporaryDirectory(dir=self.temporary) as directory:
            folder = Path(directory)
            archive = folder / "labagent-workspace.zip"
            with self.gate.activity(maintenance=True):
                summary = self.summary()
                if summary["processing_media"]:
                    raise WorkspaceBusy("图片或录屏仍在识别，请识别完成后下载备份。")
                snapshot = folder / "sessions.sqlite3"
                with self.store.connect() as source, closing(sqlite3.connect(snapshot)) as target:
                    source.backup(target)
                    if target.execute("SELECT 1 FROM sqlite_master WHERE name='personal_tokens'").fetchone():
                        target.execute("DELETE FROM personal_tokens")
                        target.commit()
                manifest = {"format": FORMAT, "version": __version__,
                            "created_at": datetime.now(timezone.utc).isoformat(), **summary}
                with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as output:
                    output.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
                    output.write(snapshot, "sessions.sqlite3")
                    for status in self.recordings.root.glob("*/status.json"):
                        if status.parent.is_symlink():
                            raise ValueError("图片或录屏目录不能是链接")
                        for path in status.parent.rglob("*"):
                            if path.is_file() and not path.is_symlink():
                                name = "recordings/" + path.relative_to(self.recordings.root).as_posix()
                                if MEDIA.fullmatch(name):
                                    output.write(path, name)
                    for path in self.notes.glob("*.json"):
                        if re.fullmatch(r"[a-f0-9]{64}", path.stem) and path.is_file() and not path.is_symlink():
                            output.write(path, "course_notes/" + path.name)
                    if sum(entry.file_size for entry in output.infolist()) > MAX_EXPANDED_BYTES:
                        raise ValueError("工作区超过1GB，请先保存实验报告或迁移到持久存储。")
                if archive.stat().st_size > MAX_ARCHIVE_BYTES:
                    raise ValueError("备份超过512MB，请先保存实验报告或迁移到持久存储。")
            yield archive

    def restore(self, archive):
        with self.gate.activity(maintenance=True):
            if not self.summary()["can_restore"]:
                raise ValueError("当前工作区已有数据，请在空工作区恢复备份。")
            with tempfile.TemporaryDirectory(dir=self.temporary) as directory:
                folder = Path(directory)
                try:
                    with zipfile.ZipFile(archive) as source:
                        entries = source.infolist()
                        names = [entry.filename for entry in entries]
                        if len(names) > 20000 or len(names) != len(set(names)) or sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES:
                            raise ValueError("备份文件过大或包含重复文件")
                        for entry in entries:
                            name = entry.filename
                            allowed = name in ("manifest.json", "sessions.sqlite3") or MEDIA.fullmatch(name) or re.fullmatch(r"course_notes/[a-f0-9]{64}\.json", name)
                            if not allowed or stat.S_ISLNK(entry.external_attr >> 16):
                                raise ValueError("备份包含不支持的文件或路径")
                        if not {"manifest.json", "sessions.sqlite3"} <= set(names):
                            raise ValueError("备份缺少工作区说明或诊断记录")
                        if source.getinfo("manifest.json").file_size > 10000:
                            raise ValueError("备份说明过大")
                        manifest = json.loads(source.read("manifest.json"))
                        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
                            raise ValueError("不是支持的LabAgent工作区备份")
                        for entry in entries:
                            # Paths have been allowlisted; never extract arbitrary members.
                            target = folder / entry.filename
                            target.parent.mkdir(parents=True, exist_ok=True)
                            with source.open(entry) as content, target.open("wb") as output:
                                shutil.copyfileobj(content, output, 1024 * 1024)
                    clean = self.validate_snapshot(folder)
                    with clean.connect() as db:
                        counts = {"sessions": db.execute("SELECT COUNT(DISTINCT session_id) FROM attempts").fetchone()[0],
                                  "rooms": db.execute("SELECT COUNT(*) FROM rooms").fetchone()[0],
                                  "recordings": len(list((folder / "recordings").glob("*/status.json"))),
                                  "documents": len(list((folder / "course_notes").glob("*.json")))}
                        for key, table in (("users", "personal_users"), ("memories", "personal_memories")):
                            count = db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                            if count or key in manifest:
                                counts[key] = count
                    if any(manifest.get(key) != count for key, count in counts.items()):
                        raise ValueError("备份中的材料数量与记录不一致，文件可能不完整。")
                    moved = []
                    try:
                        for name, destination in (("recordings", self.recordings.root), ("course_notes", self.notes)):
                            destination.mkdir(parents=True, exist_ok=True)
                            for item in (folder / name).glob("*"):
                                target = destination / item.name
                                item.rename(target)
                                moved.append((target, item))
                        with clean.connect() as source, self.store.connect() as target:
                            target.executemany("INSERT INTO attempts(session_id,created_at,payload,result) VALUES (?,?,?,?)",
                                               source.execute("SELECT session_id,created_at,payload,result FROM attempts ORDER BY id"))
                            target.executemany("INSERT INTO rooms(id,data) VALUES (?,?)", source.execute("SELECT id,data FROM rooms"))
                            from .personal import restore_personal_tables
                            restore_personal_tables(source, target)
                            from .tasks import restore_task_tables
                            restore_task_tables(source, target)
                    except Exception:
                        for target, original in reversed(moved):
                            target.rename(original)
                        raise
                except (zipfile.BadZipFile, sqlite3.Error, KeyError, TypeError, UnicodeError, FileNotFoundError, json.JSONDecodeError) as error:
                    raise ValueError("备份文件不完整或数据格式无效，请使用下载的完整ZIP备份。") from error
        return self.summary()

    def validate_snapshot(self, folder):
        """Rebuild known tables rather than installing SQL schema from an upload."""
        clean = Store(folder / "validated.sqlite3")
        Rooms(clean)
        from .personal import Personal, restore_personal_tables
        Personal(clean)
        source_path = (folder / "sessions.sqlite3").resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(source_path, uri=True)) as source, clean.connect() as target:
            source.execute("PRAGMA trusted_schema=OFF")
            if source.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view')").fetchone():
                raise ValueError("备份诊断库包含不支持的数据结构")
            for session, created, payload_text, result_text in source.execute("SELECT session_id,created_at,payload,result FROM attempts ORDER BY id"):
                payload, result = json.loads(payload_text), json.loads(result_text)
                if not isinstance(session, str) or not re.fullmatch(ID, session) or not isinstance(created, str) or not isinstance(payload, dict) or not isinstance(result, dict):
                    raise ValueError("备份中的诊断记录无效")
                if not isinstance(payload.get("configs"), dict) or not all(key in result for key in ("created_at", "config_fingerprint", "hint_level", "findings", "status")):
                    raise ValueError("备份中的诊断记录不完整")
                target.execute("INSERT INTO attempts(session_id,created_at,payload,result) VALUES (?,?,?,?)", (session, created, payload_text, result_text))
            for room_id, room_text in source.execute("SELECT id,data FROM rooms"):
                room = json.loads(room_text)
                if not isinstance(room_id, str) or not re.fullmatch(ID, room_id) or not isinstance(room, dict) or room.get("id") != room_id:
                    raise ValueError("备份中的协作实验无效")
                for key in ("materials", "versions", "members"):
                    if not isinstance(room.get(key), dict):
                        raise ValueError("备份中的协作材料不完整")
                for key in ("tasks", "activity", "recordings"):
                    if not isinstance(room.get(key), list):
                        raise ValueError("备份中的协作记录不完整")
                if room.get("session") and not target.execute("SELECT 1 FROM attempts WHERE session_id=?", (room["session"]["session_id"],)).fetchone():
                    raise ValueError("备份缺少协作实验对应的诊断记录")
                target.execute("INSERT INTO rooms VALUES (?,?)", (room_id, room_text))
            restore_personal_tables(source, target)
            from .tasks import restore_task_tables
            restore_task_tables(source, target)
        for path in (folder / "recordings").glob("*"):
            status = json.loads((path / "status.json").read_text(encoding="utf-8"))
            if not isinstance(status, dict):
                raise ValueError("备份中的图片或录屏状态无效")
            suffix = status.get("suffix")
            image = status.get("kind") == "image"
            if status.get("id") != path.name or status.get("status") not in ("ready", "error") or suffix not in (IMAGE_SUFFIXES if image else VIDEO_SUFFIXES):
                raise ValueError("备份中的图片或录屏状态无效")
            if not (path / (("image" if image else "video") + suffix)).is_file():
                raise ValueError("备份缺少图片或录屏原文件")
            if status["status"] == "ready" and (not (path / "transcript.log").is_file() or (image and not (path / "preview.jpg").is_file())):
                raise ValueError("备份缺少识别结果或截图预览")
        for path in (folder / "course_notes").glob("*.json"):
            from .knowledge import make_document
            document = json.loads(path.read_text(encoding="utf-8"))
            normalized = make_document(document)
            if document.get("id") != normalized["id"] or path.stem != normalized["id"].removeprefix("course-") or document.get("content_hash") != normalized["content_hash"]:
                raise ValueError("备份中的课程资料不完整")
        return clean
