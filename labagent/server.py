from functools import partial
from queue import Empty, Queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import base64
import hmac
import hashlib
from pathlib import Path
import re
import threading
import time
import shutil
import tempfile
from http.cookies import SimpleCookie, CookieError
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, parse_qs, unquote

from .engine import diagnose, fingerprint, validate
from .model import configured, public_config
from . import __version__
from .store import Store
from .workflow import run_workflow, available
from .workflow import run_open_workflow
from .workspace_archive import WorkspaceArchive, WorkspaceBusy, MAX_ARCHIVE_BYTES
from .report import generate_report
from .knowledge import get_document, import_document, list_documents, list_scopes, search
from .cases import demo_cases
from .demos import network_access_demo, ospf_goals_demo
from .progress import emit_progress, progress_to, execution_summary
from .recordings import Recordings, VIDEO_SUFFIXES, available as replay_available
from .operation_history import inspect_history
from .rooms import Rooms, RoomConflict
from .personal import Personal, SignInError, COOKIE, LIFETIME
from .lab_bridge import LabBridge
from .tasks import Tasks
from .task_control import task_control, check_cancelled, DiagnosisCancelled

ROOT = Path(__file__).resolve().parents[1]
MEASURED_DEMOS = {
    "/api/demos/repair_comparison": ("frr_repair_run.json", "FRR修复实验.json", "修复方案实际运行对照"),
    "/api/demos/ospf_area_measured": ("frr_lab02_area.json", "FRR区域复测.json", "OSPF 区域不匹配实测"),
    "/api/demos/ospf_shutdown_measured": ("frr_lab03_shutdown.json", "FRR接口复测.json", "OSPF 互联接口关闭实测"),
    "/api/demos/ospf_backup_measured": ("frr_lab02_backup.json", "FRR备用路由复测.json", "OSPF 失配与备用静态路由实测"),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "LabAgent/" + __version__

    def __init__(self, *args, store, **kwargs):
        self.store = store
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):
        # Avoid logging uploaded configurations and other request bodies.
        print(time.strftime("%H:%M:%S"), format % args, flush=True)
        self.server.write_log(format % args)

    def end_headers(self):
        # Native media requests do not consistently reuse browser Basic credentials.
        # Keep the same team access in an HttpOnly, signed, short-lived session.
        if getattr(self, "_team_authenticated", False):
            expires = str(int(time.time()) + 12 * 3600)
            signature = hmac.new(self.server.team_password.encode(), ("team-v1:" + expires).encode(), hashlib.sha256).hexdigest()
            self.send_header("Set-Cookie", "labagent_team=" + expires + "." + signature + "; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200" +
                ("; Secure" if self.server.public_url else ""))
        if getattr(self, "_clear_personal_cookie", False):
            self.send_header("Set-Cookie", COOKIE + "=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0" +
                ("; Secure" if self.server.public_url else ""))
        super().end_headers()

    def reply(self, value, status=200, download=False, cookie=None):
        content = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie is not None:
            self._clear_personal_cookie = False
            self.send_header("Set-Cookie", COOKIE + "=" + cookie + "; Path=/; HttpOnly; SameSite=Strict; Max-Age=" +
                str(LIFETIME if cookie else 0) + ("; Secure" if self.server.public_url else ""))
        if download:
            self.send_header("Content-Disposition", 'attachment; filename="labagent-record.json"')
        self.end_headers()
        self.wfile.write(content)

    def personal_token(self):
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            return cookies[COOKIE].value if COOKIE in cookies else None
        except CookieError:
            return "invalid"

    def personal_user(self, *, strict=True, required=False):
        token = self.personal_token()
        if not hasattr(self, "_personal_identity"):
            self._personal_identity = self.server.personal.user(token)
        user = self._personal_identity
        if token and not user:
            self._clear_personal_cookie = True
        if (token and not user and strict) or (required and not user):
            raise SignInError("请登录个人账号后继续", 401)
        return user

    def room_capability(self):
        value = self.headers.get("X-LabAgent-Room") or parse_qs(urlparse(self.path).query).get("room", [""])[0]
        return value if re.fullmatch(r"[a-f0-9]{32}", value) else None

    def can_read(self, kind, resource_id):
        user = self.personal_user(strict=False)
        return self.server.personal.owns(kind, resource_id, user["id"] if user else None, self.room_capability())

    def accessible_task(self, task_id):
        task = self.server.tasks.get(task_id)
        user = self.personal_user(strict=False)
        if task and ((task["owner"] and user and task["owner"] == user["id"]) or
                     (task["room_id"] and task["room_id"] == self.room_capability()) or
                     (not task["owner"] and not task["room_id"])):
            return {key: value for key, value in task.items() if key != "owner"}
        return None

    def safe_host(self):
        return self.headers.get("Host") in self.server.allowed_hosts

    def team_access(self):
        if not self.server.team_password:
            return True
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            session = cookies["labagent_team"].value if "labagent_team" in cookies else ""
            expires, signature = session.split(".", 1)
            expected = hmac.new(self.server.team_password.encode(), ("team-v1:" + expires).encode(), hashlib.sha256).hexdigest()
            if 0 < int(expires) - time.time() <= 12 * 3600 and hmac.compare_digest(signature, expected):
                return True
        except (ValueError, CookieError):
            pass
        try:
            scheme, value = self.headers.get("Authorization", "").split(" ", 1)
            credentials = base64.b64decode(value, validate=True).decode("utf-8")
            if scheme.lower() == "basic" and hmac.compare_digest(credentials.encode(), ("labagent:" + self.server.team_password).encode()):
                self._team_authenticated = True
                return True
        except (ValueError, UnicodeError):
            pass
        if urlparse(self.path).path.startswith("/api/"):
            return self.reply({"error": "网站访问已失效，请刷新页面重新输入团队访问口令。", "code": "team_sign_in_required"}, 401)
        content = "请输入团队访问口令".encode()
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="LabAgent", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)
        return False

    def do_GET(self):
        if urlparse(self.path).path == "/api/health":
            return self.handle_GET()
        try:
            with self.server.workspace.gate.activity():
                return self.handle_GET()
        except WorkspaceBusy as error:
            return self.reply({"error": str(error)}, 409)
        except SignInError as error:
            return self.reply({"error": str(error), "code": "sign_in_required"}, error.status)

    def handle_GET(self):
        if not self.safe_host():
            return self.reply({"error": "请使用当前网站地址访问"}, 403)
        path = urlparse(self.path).path
        if path == "/api/health":
            return self.reply({"app": "LabAgent", "version": __version__, "mode": "local_rules", "online": bool(self.server.public_url), "knowledge_method": "local_bm25_with_fault_tags", "workflow_backend": "langgraph" if available() else "local_rules", "diagnosis_backend": self.server.diagnosis_backend, "diagnosis_configured": self.server.cloud_configured if self.server.diagnosis_backend == "agentarts" else configured(), "llm_configured": configured(), "llm": public_config(), "replay_available": replay_available()})
        if not self.team_access():
            return
        if match := re.fullmatch(r"/api/tasks/([a-f0-9]{32})", path):
            task = self.accessible_task(match[1])
            return self.reply(task or {"error": "任务不存在"}, 200 if task else 404)
        if path == "/api/lab/connector":
            from .connector_package import build_connector_package
            content = build_connector_package()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="labagent-connector.zip"')
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            return self.wfile.write(content)
        if path == "/api/learning":
            from .learning import solution_allowed, COOKIE as LEARNING_COOKIE
            user = self.personal_user(strict=False)
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            token = cookies[LEARNING_COOKIE].value if LEARNING_COOKIE in cookies else ""
            return self.reply({"enabled": os.environ.get("LABAGENT_GUIDED_WORKFLOW") == "1",
                "solution_unlocked": bool(user) and solution_allowed(token, subject=user["id"]),
                "can_unlock": bool(os.environ.get("LABAGENT_SOLUTION_CODE") and os.environ.get("LABAGENT_LEARNING_KEY")),
                "bridge_available": bool(user),
                "mcp_configured": bool(os.environ.get("LABAGENT_MCP_CONFIG"))})
        if path == "/api/lab/bridge":
            user = self.personal_user(required=True)
            return self.reply({"bridge": self.server.lab_bridge.status(user["id"])})
        if path == "/api/lab/tools":
            from .mcp_client import LabMCPTools
            user = self.personal_user(strict=False)
            bridge = self.server.lab_bridge.status(user["id"]) if user else None
            tools = [{**tool, "bridge_id": bridge["id"]} for tool in bridge["tools"]] if bridge and bridge["state"] == "online" else []
            config = os.environ.get("LABAGENT_MCP_CONFIG")
            if not config:
                return self.reply({"tools": tools, "bridge": bridge, "warnings": [] if tools else ["请先连接实验电脑，也可继续上传材料"]})
            with LabMCPTools(config) as lab:
                return self.reply({"tools": tools + lab.tools, "bridge": bridge, "warnings": lab.warnings})
        if path == "/api/account":
            return self.reply({"user": self.personal_user(strict=False)}, cookie="" if self.personal_token() and not self.personal_user(strict=False) else None)
        if path == "/api/memories":
            user = self.personal_user(required=True)
            return self.reply({"enabled": user["memory_enabled"], "memories": self.server.personal.list_memories(user["id"])})
        if path == "/api/workspace":
            return self.reply({**self.server.workspace.summary(), "persistence": self.server.persistence.status() if self.server.persistence else {"enabled": False, "state": "local"}})
        if path == "/api/persistence":
            return self.reply(self.server.persistence.status() if self.server.persistence else {"enabled": False, "state": "local"})
        if match := re.fullmatch(r"/api/rooms/([a-f0-9]{32})", path):
            room = self.server.rooms.get(match[1])
            return self.reply(room if room else {"error": "协作实验不存在，请检查邀请链接"}, 200 if room else 404)
        if path == "/api/challenges/new":
            from .challenges import new_challenge
            return self.reply(new_challenge())
        if path == "/api/demos/network_access":
            return self.reply(network_access_demo())
        if path == "/api/demos/ospf_goals":
            return self.reply(ospf_goals_demo())
        if path == "/api/demos/operation_history":
            return self.reply({"title": "找回未生效的network命令", "source": "合成演示材料", "materials": {
                "终端操作.log": (ROOT / "data/examples/终端操作回溯.log").read_text(encoding="utf-8"),
                "实验要求.txt": "实验目标：R1通过OSPF访问R2的Loopback0地址10.2.2.2，Ping全部成功。\n分析操作记录中哪一步影响了配置，并说明怎样继续。"}})
        if path in MEASURED_DEMOS:
            filename, material, title = MEASURED_DEMOS[path]
            sample = ROOT / "data/examples" / filename
            if not sample.is_file():
                return self.reply({"error":"FRR实验运行记录尚未导入"}, 404)
            return self.reply({"title": title, "materials": {material: sample.read_text(encoding="utf-8")}})
        if path == "/api/demos/operation_history/video":
            return self.send_video(ROOT / "data/examples/实验操作回放.webm", "video/webm")
        if match := re.fullmatch(r"/api/recordings/([a-f0-9]{32})(/video|/image)?", path):
            if not self.can_read("recording", match[1]):
                return self.reply({"error": "图片或录屏不存在"}, 404)
            status = self.server.recordings.status(match[1])
            if status is None:
                return self.reply({"error": "图片或录屏不存在，请重新添加"}, 404)
            if match[2] == "/image":
                image = self.server.recordings.folder(match[1]) / "preview.jpg"
                if status.get("kind") != "image" or not image.is_file():
                    return self.reply({"error": "截图正在准备，请等待识别完成"}, 404)
                return self.send_video(image, "image/jpeg")
            if match[2] == "/video":
                if status.get("kind") == "image":
                    return self.reply({"error": "这份材料为截图，请使用image地址"}, 404)
                return self.send_recording(match[1], status)
            return self.reply(status)
        if path == "/api/knowledge/scopes":
            return self.reply({"scopes": list_scopes()})
        if path == "/api/knowledge/documents":
            try:
                scope = parse_qs(urlparse(self.path).query).get("scope", ["default"])[0]
                return self.reply(list_documents(scope))
            except ValueError as error:
                return self.reply({"error": str(error)}, 400)
        if match := re.fullmatch(r"/api/knowledge/documents/([A-Za-z0-9_-]{1,100})", path):
            try:
                scope = parse_qs(urlparse(self.path).query).get("scope", ["default"])[0]
                document = get_document(match[1], scope)
                return self.reply(document if document else {"error": "资料不存在或不在当前资料范围内。"}, 200 if document else 404)
            except ValueError as error:
                return self.reply({"error": str(error)}, 400)
        if path == "/api/cases":
            return self.reply([{"id": case_id, "title": data["title"], "description": data["description"]}
                               for case_id, data in demo_cases().items()])
        if path == "/api/scenarios":
            return self.reply([{"id": file.stem, "title": json.loads(file.read_text(encoding="utf-8"))["title"]}
                               for file in sorted((ROOT / "data" / "scenarios").glob("*.json"))])
        if match := re.fullmatch(r"/api/scenarios/([a-z0-9_-]+)", path):
            file = ROOT / "data" / "scenarios" / (match[1] + ".json")
            if file.is_file():
                return self.reply(json.loads(file.read_text(encoding="utf-8")))
        if match := re.fullmatch(r"/api/cases/([a-z0-9_-]+)", path):
            data = demo_cases().get(match[1])
            if data is not None:
                return self.reply(data)
        if path == "/api/sessions":
            try:
                query = parse_qs(urlparse(self.path).query)
                user = self.personal_user()
                return self.reply(self.store.sessions(limit=int(query.get("limit", ["30"])[0]),
                    before=int(query["before"][0]) if "before" in query else None, query=query.get("q", [""])[0],
                    personal=True, user_id=user["id"] if user else None, room_id=self.room_capability()))
            except SignInError:
                raise
            except ValueError as error:
                return self.reply({"error": str(error)}, 400)
        if match := re.fullmatch(r"/api/sessions/([a-f0-9]{32})(/export|/report)?", path):
            if not self.can_read("session", match[1]):
                return self.reply({"error": "会话不存在", "code": "session_not_found"}, 404)
            history = self.store.history(match[1])
            if history:
                if match[2] == "/report":
                    return self.reply({"markdown": generate_report(history)})
                return self.reply(history, download=bool(match[2]))
            return self.reply({"code": "session_not_found", "error": "这次会话已不存在，请开始新的排查并重新提供材料。"}, 404)
        assets = {"/": ("student.html", "text/html; charset=utf-8"), "/student.js": ("student.js", "text/javascript; charset=utf-8"), "/student.css": ("student.css", "text/css; charset=utf-8"),
                  "/react-conversation.js": ("react-conversation.js", "text/javascript; charset=utf-8"),
                  "/execution-state.js": ("execution-state.js", "text/javascript; charset=utf-8"),
                  "/task-control.js": ("task-control.js", "text/javascript; charset=utf-8"),
                  "/react-conversation.js.LEGAL.txt": ("react-conversation.js.LEGAL.txt", "text/plain; charset=utf-8"),
                  "/learning.js": ("learning.js", "text/javascript; charset=utf-8"),
                  "/devices.js": ("devices.js", "text/javascript; charset=utf-8"),
                  "/devices.css": ("devices.css", "text/css; charset=utf-8"),
                  "/network-room.js": ("network-room.js", "text/javascript; charset=utf-8"), "/network-room.css": ("network-room.css", "text/css; charset=utf-8"),
                  "/session-history.js": ("session-history.js", "text/javascript; charset=utf-8"), "/session-history.css": ("session-history.css", "text/css; charset=utf-8"),
                  "/course-knowledge.js": ("course-knowledge.js", "text/javascript; charset=utf-8"), "/course-knowledge.css": ("course-knowledge.css", "text/css; charset=utf-8"),
                  "/personal-memory.js": ("personal-memory.js", "text/javascript; charset=utf-8"), "/personal-memory.css": ("personal-memory.css", "text/css; charset=utf-8"),
                  "/ambient-network.js": ("ambient-network.js", "text/javascript; charset=utf-8"),
                  "/vendor/tsparticles.slim.bundle.min.js": ("vendor/tsparticles.slim.bundle.min.js", "text/javascript; charset=utf-8"),
                  "/workbench": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8"), "/knowledge.css": ("knowledge.css", "text/css; charset=utf-8")}
        if path in assets:
            name, mime = assets[path]
            content = (ROOT / "web" / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(content)
            return
        self.reply({"error": "未找到内容"}, 404)

    def do_DELETE(self):
        origin = self.headers.get("Origin")
        if not self.safe_host() or (origin and origin not in self.server.allowed_origins):
            return self.reply({"error": "请从当前网站提交请求"}, 403)
        if not self.team_access():
            return
        if urlparse(self.path).path == "/api/memories" or re.fullmatch(r"/api/memories/[a-f0-9]{32}", urlparse(self.path).path):
            try:
                with self.server.workspace.gate.activity():
                    user = self.personal_user(required=True)
                    memory_id = urlparse(self.path).path.rsplit("/", 1)[-1] if urlparse(self.path).path != "/api/memories" else None
                    return self.reply({"deleted": self.server.personal.delete_memory(user["id"], memory_id)})
            except SignInError as error:
                return self.reply({"error": str(error)}, error.status)
            except WorkspaceBusy as error:
                return self.reply({"error": str(error)}, 409)
        match = re.fullmatch(r"/api/sessions/([a-f0-9]{32})", urlparse(self.path).path)
        if not match:
            return self.reply({"error": "未找到内容"}, 404)
        try:
            # Diagnosis workers keep a lease until saving, so a deleted session
            # cannot reappear when an in-flight request completes.
            with self.server.workspace.gate.activity(maintenance=True):
                if not self.can_read("session", match[1]):
                    return self.reply({"error": "会话不存在", "code": "session_not_found"}, 404)
                result = self.store.delete_session(match[1], workflow_database=self.server.workflow_database)
                if result is None:
                    return self.reply({"code": "session_not_found", "error": "这次对话已不存在。"}, 404)
                return self.reply(result)
        except WorkspaceBusy as error:
            return self.reply({"error": str(error)}, 409)
        except SignInError as error:
            return self.reply({"error": str(error)}, error.status)
        except Exception:
            import traceback
            self.server.write_log(traceback.format_exc(), error=True)
            return self.reply({"error": "删除失败，请稍后重试。"}, 500)

    def do_POST(self):
        try:
            # Outbound workstation traffic carries a narrowly scoped bridge token,
            # never the website's team password or a student's account cookie.
            if urlparse(self.path).path in {"/api/lab/agent/claim", "/api/lab/agent/poll", "/api/lab/agent/result", "/api/lab/runtime/collect", "/api/tasks/runtime/status"}:
                return self.bridge_agent_request()
            if urlparse(self.path).path in ("/api/workspace/backup", "/api/workspace/restore"):
                return self.handle_POST()
            with self.server.workspace.gate.activity():
                return self.handle_POST()
        except WorkspaceBusy as error:
            return self.reply({"error": str(error)}, 409)
        except SignInError as error:
            return self.reply({"error": str(error)}, error.status)

    def bridge_agent_request(self):
        if not self.safe_host() or self.headers.get("Origin"):
            return self.reply({"error": "请使用本地实验连接程序"}, 403)
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.reply({"error": "请使用application/json"}, 415)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 500_000:
                raise ValueError("连接请求过大或为空")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("连接请求须为对象")
            path = urlparse(self.path).path
            if path.endswith("/claim"):
                return self.reply(self.server.lab_bridge.claim(payload.get("code"), payload.get("tools")))
            auth = self.headers.get("Authorization", "")
            token = auth[7:] if auth.startswith("Bearer ") else ""
            if path == "/api/tasks/runtime/status":
                return self.reply(self.server.tasks.runtime_status(token))
            if path == "/api/lab/runtime/collect":
                return self.reply(self.server.lab_bridge.runtime_collect(token, payload.get("tool"), payload.get("arguments", {})))
            if path.endswith("/poll"):
                return self.reply({"job": self.server.lab_bridge.poll(token)})
            self.server.lab_bridge.finish(token, payload.get("id"), payload.get("result"), payload.get("error"))
            return self.reply({"received": True})
        except PermissionError as error:
            return self.reply({"error": str(error)}, 401)
        except (ValueError, UnicodeError) as error:
            self.close_connection = True
            return self.reply({"error": str(error)}, 400)

    def handle_POST(self):
        origin = self.headers.get("Origin")
        if not self.safe_host() or (origin and origin not in self.server.allowed_origins):
            return self.reply({"error": "请从当前网站提交请求"}, 403)
        if not self.team_access():
            return
        if urlparse(self.path).path in ("/api/workspace/backup", "/api/workspace/restore"):
            return self.workspace_transfer()
        if urlparse(self.path).path == "/api/recordings":
            try:
                user = self.personal_user()
                length = int(self.headers.get("Content-Length", "0"))
                result = self.server.recordings.upload(self.rfile, length, unquote(self.headers.get("X-Recording-Name", "recording.mp4")))
                if user:
                    with self.store.connect() as db:
                        db.execute("INSERT INTO personal_resources VALUES ('recording',?,?)", (result["id"], user["id"]))
                return self.reply(result, 202)
            except ValueError as error:
                self.close_connection = True
                return self.reply({"error": str(error)}, error.status if isinstance(error, SignInError) else 400)
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self.reply({"error": "请使用 application/json"}, 415)
        path = urlparse(self.path).path
        if match := re.fullmatch(r"/api/tasks/([a-f0-9]{32})/cancel", path):
            if not self.accessible_task(match[1]):
                return self.reply({"error": "任务不存在"}, 404)
            # Consume the JSON body before keeping this HTTP connection alive.
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.server.tasks.cancel(match[1])
            return self.reply(self.accessible_task(match[1]))
        room_match = re.fullmatch(r"/api/rooms/([a-f0-9]{32})", path)
        if not room_match and path not in ("/api/lab/bridge", "/api/lab/bridge/disconnect", "/api/learning/unlock", "/api/lab/collect", "/api/account/register", "/api/account/login", "/api/account/logout", "/api/account/memory", "/api/memories", "/api/rooms", "/api/network/inspect", "/api/challenges/check", "/api/diagnose", "/api/diagnose/stream", "/api/fingerprint", "/api/export", "/api/knowledge/import", "/api/knowledge/search", "/api/operations/inspect", "/api/repair-labs/inspect"):
            return self.reply({"error": "未找到接口"}, 404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1_000_000:
                return self.reply({"error": "请求大小须在 1 字节至 1 MB 之间"}, 413)
            payload = json.loads(self.rfile.read(length).decode("utf-8-sig"))
            if not isinstance(payload, dict):
                raise ValueError("请求必须为 JSON 对象")
            if path in ("/api/account/register", "/api/account/login"):
                user, token = self.server.personal.authenticate(payload, register=path.endswith("/register"), client=self.client_address[0])
                return self.reply({"user": user}, cookie=token)
            if path == "/api/account/logout":
                self.server.personal.logout(self.personal_token())
                return self.reply({"signed_out": True}, cookie="")
            # These operations work for invited teammates and anonymous examples.
            # An expired personal identity must not disable shared operations.
            optional_identity = bool(room_match) or path in {
                "/api/rooms", "/api/network/inspect", "/api/challenges/check",
                "/api/operations/inspect", "/api/repair-labs/inspect", "/api/knowledge/search", "/api/lab/collect"
            }
            user = self.personal_user(strict=not optional_identity)
            if path in ("/api/lab/bridge", "/api/lab/bridge/disconnect"):
                if not user:
                    raise SignInError("请先登录个人账号，再连接自己的实验电脑")
                if path.endswith("/disconnect"):
                    self.server.lab_bridge.disconnect(user["id"], payload.get("id"))
                    return self.reply({"disconnected": True})
                return self.reply(self.server.lab_bridge.create(user["id"], payload.get("name", "我的实验电脑")), 201)
            if path == "/api/learning/unlock":
                from .learning import grant_solution, COOKIE as LEARNING_COOKIE
                if not user:
                    raise SignInError("请先登录个人账号，再输入老师提供的解答授权码")
                with self.server.learning_lock:
                    attempts = self.server.learning_attempts.get(user["id"], [])
                    attempts = [stamp for stamp in attempts if time.monotonic() - stamp < 60]
                    if len(attempts) >= 5:
                        return self.reply({"error": "授权码尝试过多，请一分钟后重试"}, 429)
                    self.server.learning_attempts[user["id"]] = attempts + [time.monotonic()]
                token = grant_solution(payload.get("code"), user["id"])
                self.send_response(200)
                content = b'{"solution_unlocked": true}'
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Set-Cookie", LEARNING_COOKIE + "=" + token + "; Path=/; HttpOnly; SameSite=Strict; Max-Age=7200" + ("; Secure" if self.server.public_url else ""))
                self.end_headers()
                self.wfile.write(content)
                return
            if path == "/api/lab/collect":
                from .mcp_client import LabMCPTools
                config = os.environ.get("LABAGENT_MCP_CONFIG")
                tool = payload.get("tool")
                if payload.get("bridge_id"):
                    if not user:
                        raise SignInError("请先登录个人账号，再采集自己的实验电脑")
                    observed = self.server.lab_bridge.collect(user["id"], payload["bridge_id"], tool, payload.get("arguments", {}))
                else:
                    if not config:
                        raise ValueError("尚未配置实验平台连接，请先连接实验电脑")
                    with LabMCPTools(config) as lab:
                        if not isinstance(tool, str) or tool not in lab.bindings:
                            raise ValueError("请选择当前实验已开放的检查")
                        observed = lab.execute(tool, payload.get("arguments", {}))
                from .investigation import safe_text
                command_slug = re.sub(r"[^\w-]+", "_", observed["command"])[:35]
                name = ("MCP-" + observed["device"] + "-" + tool + "-" + command_slug + ".log")[:120]
                text = "设备：" + observed["device"] + "\n命令：" + observed["command"] + "\n" + safe_text(observed["text"])
                return self.reply({"materials": {name: text}, "source": "mcp_device_observation",
                    "observed_at": datetime.now(timezone.utc).isoformat()})
            if path in ("/api/memories", "/api/account/memory"):
                if not user:
                    raise SignInError("请先登录个人账号")
                if path == "/api/account/memory":
                    self.server.personal.enable_memory(user["id"], payload.get("enabled"))
                    return self.reply({"enabled": payload["enabled"]})
                return self.reply({"memory": self.server.personal.save_memory(user["id"], payload)})
            if path == "/api/rooms":
                room = self.server.rooms.create(payload.get("name"), payload.get("actor"))
                self.server.personal.join_room(user["id"] if user else None, room["id"])
                return self.reply(room, 201)
            if room_match:
                for recording in payload.get("recordings", []):
                    if not self.can_read("recording", recording):
                        raise ValueError("无法共享这份图片或录屏")
                room = self.server.rooms.update(room_match[1], payload)
                self.server.personal.join_room(user["id"] if user else None, room["id"])
                return self.reply(room)
            if path == "/api/challenges/check":
                from .challenges import check_answer
                return self.reply(check_answer(payload.get("id"), payload.get("choice")))
            if path == "/api/network/inspect":
                from .investigation import validate_open, source_bundle
                from .network_map import build_map, explain_path
                material_input = {"configs": payload.get("materials"), "question": "重建实验网络"}
                validate_open(material_input)
                network = build_map(source_bundle(material_input)[0])
                if payload.get("source") and payload.get("target"):
                    network["explanation"] = explain_path(network, payload["source"], payload["target"])
                return self.reply(network)
            if path in {"/api/operations/inspect", "/api/repair-labs/inspect"}:
                from .investigation import validate_open, source_bundle
                material_input = {"configs": payload.get("materials"), "question": "回溯操作记录"}
                validate_open(material_input)
                if path == "/api/repair-labs/inspect":
                    from .repair_lab import discover_runs
                    return self.reply({"comparisons":discover_runs(source_bundle(material_input)[0])})
                history = inspect_history(source_bundle(material_input)[0])
                return self.reply(history)
            if path == "/api/knowledge/import":
                return self.reply(import_document(payload))
            if path == "/api/knowledge/search":
                return self.reply(search(payload.get("query", ""), scope=payload.get("scope", "default"), limit=payload.get("limit", 3)))
            session = payload.get("session_id")
            if session is not None and (not isinstance(session, str) or not re.fullmatch(r"[a-f0-9]{32}", session)):
                raise ValueError("session_id 格式错误")
            if session and not self.can_read("session", session):
                return self.reply({"error": "会话不存在", "code": "session_not_found"}, 404)
            if path == "/api/export":
                if not session:
                    raise ValueError("请选择已有会话")
                history = self.store.history(session)
                if not history:
                    return self.reply({"error": "会话不存在"}, 404)
                target = self.store.path.parent / "exports" / (session + ".json")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
                report_path = target.with_suffix(".md")
                report_path.write_text(generate_report(history), encoding="utf-8")
                result = {"path": str(target), "report_path": str(report_path)}
                if self.server.public_url:
                    result.update(markdown=report_path.read_text(encoding="utf-8"), filename="labagent-" + session[:8] + ".md")
                return self.reply(result)
            if payload.get("room_id"):
                payload = self.server.rooms.prepare_diagnosis(payload)
                session = payload.get("session_id")
                self.server.personal.join_room(user["id"] if user else None, payload["room_id"])
            for recording in payload.get("recordings", []):
                if not self.can_read("recording", recording):
                    raise ValueError("图片或录屏不属于本次排查")
            from .learning import solution_allowed, COOKIE as LEARNING_COOKIE
            if payload.get("diagnostic_mode") == "open_network" and (os.environ.get("LABAGENT_GUIDED_WORKFLOW") == "1" or "learning" in payload):
                learning = payload.get("learning", {"level": 1})
                if not isinstance(learning, dict):
                    raise ValueError("learning必须为对象")
                # Browser flags and a token copied from another account cannot grant access.
                learning = {"level": learning.get("level", 1)}
                if learning["level"] == 3:
                    cookies = SimpleCookie(self.headers.get("Cookie", ""))
                    token = cookies[LEARNING_COOKIE].value if LEARNING_COOKIE in cookies else ""
                    if shared_room := payload.get("room_id") or self.server.personal.session_room(session):
                        raise SignInError("共享实验继续使用逐步提示；请在个人会话查看授权解答", 403)
                    if not user or not solution_allowed(token, subject=user["id"]):
                        raise SignInError("完整解答需要老师授权，请先使用逐步提示", 403)
                    learning["access_token"] = token
                payload["learning"] = learning
            payload.pop("personal_memory", None)
            shared_room = payload.get("room_id") or self.server.personal.session_room(session)
            if payload.get("diagnostic_mode") == "open_network":
                payload["personal_memory"] = [] if shared_room else self.server.personal.context(user,
                    payload.get("question", "") + " " + payload.get("interaction", {}).get("message", ""))
            validate(payload)
            task_id = payload.pop("request_id", None)
            if task_id is not None and (not isinstance(task_id, str) or not re.fullmatch(r"[a-f0-9]{32}", task_id)):
                raise ValueError("request_id格式错误")
            if path == "/api/fingerprint":
                return self.reply({"config_fingerprint": fingerprint(payload)})
            if session:
                history = self.store.history(session)
                if not history:
                    raise ValueError("会话不存在，请开始新实验")
                if history["attempts"][-1]["input"].get("diagnostic_mode", "ospf_rules") != payload.get("diagnostic_mode", "ospf_rules"):
                    raise ValueError("切换排查模式时请开始新实验")
            if path == "/api/diagnose/stream" and task_id and self.server.tasks.get(task_id):
                if self.accessible_task(task_id):
                    return self.reply({"code": "task_exists", "error": "请求已存在，请恢复任务状态"}, 409)
                return self.reply({"error": "任务标识已使用"}, 409)
            if not self.server.diagnosis_slots.acquire(blocking=False):
                return self.reply({"error": "当前正在处理其他诊断，请稍后重试"}, 429)
            if path == "/api/diagnose/stream":
                return self.stream_diagnosis(payload, session, room_id=shared_room,
                    lab_owner=user["id"] if user and not shared_room and self.server.diagnosis_backend == "agentarts" else None,
                    owner=user["id"] if user else None, task_id=task_id)
            try:
                key = shared_room or session or "new"
                with self.server.session_locks[hash(key) % len(self.server.session_locks)], progress_to(None, session_id=session):
                    result = self.server.run_diagnosis(payload, **({"lab_owner": user["id"]} if user and not shared_room and self.server.diagnosis_backend == "agentarts" else {}))
                    self.reply(self.save_result(payload, result))
            finally:
                self.server.diagnosis_slots.release()
        except SignInError as error:
            self.reply({"error": str(error), "code": "sign_in_required"}, error.status)
        except RoomConflict as error:
            self.reply({"error": str(error), "code": "room_conflict"}, 409)
        except (ValueError, KeyError, TypeError, UnicodeError) as error:
            self.reply({"error": str(error)}, 400)
        except Exception:
            self.reply({"error": "本地服务处理失败，请查看项目内日志"}, 500)
            import traceback
            traceback.print_exc()
            self.server.write_log(traceback.format_exc(), error=True)

    def save_result(self, payload, result):
        execution = execution_summary(result)
        if execution is not None:
            result["execution"] = execution
        collected = result.get("collected_materials", {})
        if collected:
            combined = {**payload["configs"], **collected}
            if len(combined) > 20 or not all(isinstance(key, str) and 0 < len(key) <= 120 and
                    isinstance(value, str) and 0 < len(value) <= 200000 for key, value in combined.items()):
                raise ValueError("采集材料格式无效或超过当前材料上限")
            payload["configs"] = combined
        user = self.personal_user()
        owner = "room:" + payload["room_id"] if payload.get("room_id") else user["id"] if user else None
        result["memory_used"] = [{"kind": item["kind"], "title": item["title"]} for item in payload.get("personal_memory", [])]
        from .material_changes import compare_materials
        history = self.store.history(payload["session_id"]) if payload.get("session_id") else None
        result["material_changes"] = compare_materials(
            history["attempts"][-1]["input"].get("configs", {}) if history else None, payload["configs"])
        saved = self.store.save(payload, result, owner=owner)
        self.server.rooms.record_diagnosis(payload, saved)
        if user:
            self.server.personal.remember_issue(user["id"], payload, saved)
        return saved

    def workspace_transfer(self):
        try:
            maintenance = os.environ.get("LABAGENT_MAINTENANCE_PASSWORD", "")
            if maintenance or self.server.workspace.summary()["users"]:
                supplied = self.headers.get("X-LabAgent-Maintenance", "")
                if not maintenance or not hmac.compare_digest(supplied.encode(), maintenance.encode()):
                    return self.reply({"error": "全站备份与恢复需要独立的维护口令，请维护人员配置 LABAGENT_MAINTENANCE_PASSWORD 后输入。"}, 403)
            if urlparse(self.path).path.endswith("/backup"):
                with self.server.workspace.export() as archive, archive.open("rb") as content:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/zip")
                    self.send_header("Content-Length", str(archive.stat().st_size))
                    self.send_header("Content-Disposition", 'attachment; filename="labagent-workspace.zip"')
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    shutil.copyfileobj(content, self.wfile, 1024 * 1024)
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/zip":
                return self.reply({"error": "请选择下载的ZIP工作区备份"}, 415)
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_ARCHIVE_BYTES:
                self.close_connection = True
                return self.reply({"error": "工作区备份须在512MB以内"}, 413)
            with tempfile.TemporaryDirectory(dir=self.server.workspace.temporary) as directory:
                archive = Path(directory) / "upload.zip"
                with archive.open("wb") as output:
                    remaining = length
                    while remaining:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError("备份上传中断，请重新选择文件")
                        output.write(chunk)
                        remaining -= len(chunk)
                return self.reply(self.server.workspace.restore(archive))
        except WorkspaceBusy as error:
            return self.reply({"error": str(error)}, 409)
        except ValueError as error:
            self.close_connection = True
            return self.reply({"error": str(error)}, 400)

    def send_recording(self, recording_id, status):
        path = self.server.recordings.folder(recording_id) / ("video" + status["suffix"])
        return self.send_video(path, VIDEO_SUFFIXES[status["suffix"]])

    def send_video(self, path, mime):
        if not path.is_file():
            return self.reply({"error": "回放文件暂时不可用，请重新载入示例或材料。"}, 404)
        size = path.stat().st_size
        start, end = 0, size - 1
        requested = self.headers.get("Range")
        if requested:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
            if not match or not any(match.groups()):
                return self.reply({"error": "视频范围无效"}, 416)
            if match[1]:
                start = int(match[1]); end = min(int(match[2]), end) if match[2] else end
            else:
                start = max(0, size - int(match[2]))
            if not 0 <= start <= end < size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        self.send_response(206 if requested else 200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if requested:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with path.open("rb") as stream:
                stream.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = stream.read(min(65536, remaining))
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def stream_diagnosis(self, payload, session, *, room_id=None, lab_owner=None, owner=None, task_id=None):
        events = Queue()
        try:
            task_id = self.server.tasks.create(payload, owner=owner, room=room_id, task_id=task_id)
        except Exception:
            self.server.diagnosis_slots.release()
            raise

        def publish(event):
            self.server.tasks.record(task_id, event)
            events.put(event)

        def process():
            with progress_to(publish, session_id=session, turn_id=task_id) as progress, task_control(lambda: self.server.tasks.cancelled(task_id)):
                try:
                    with self.server.workspace.gate.activity():
                        emit_progress("received", "请求已收到，正在准备本轮诊断。", event="turn.started")
                        with self.server.session_locks[hash(room_id or payload.get("room_id") or session or "new") % len(self.server.session_locks)]:
                            check_cancelled()
                            kwargs = {"lab_owner": lab_owner} if lab_owner else {}
                            if self.server.diagnosis_backend == "agentarts":
                                kwargs["task_id"] = task_id
                            result = self.server.run_diagnosis(payload, **kwargs)
                            self.server.tasks.finalizing(task_id)
                            emit_progress("saving", "正在保存本轮结论与会话记录…")
                            saved = self.save_result(payload, result)
                        progress.session_id = saved["session_id"]
                        progress.send({"type": "result", "result": saved}, event="turn." + saved["execution"]["status"])
                except DiagnosisCancelled as error:
                    progress.send({"type": "cancelled", "message": str(error)}, event="turn.cancelled")
                except (ValueError, KeyError, TypeError) as error:
                    progress.send({"type": "error", "message": str(error)}, event="turn.failed")
                except Exception:
                    import traceback
                    self.server.write_log(traceback.format_exc(), error=True)
                    progress.send({"type": "error", "message": "本轮诊断处理失败，请重新提交。"}, event="turn.failed")
                finally:
                    self.server.diagnosis_slots.release()

        threading.Thread(target=process, daemon=True).start()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            while True:
                try:
                    event = events.get(timeout=10)
                except Empty:
                    event = {"type": "heartbeat"}
                self.wfile.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
                self.wfile.flush()
                if event["type"] in ("result", "error", "cancelled"):
                    return
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


class LabAgentServer(ThreadingHTTPServer):
    persistence = None

    def server_close(self):
        if self.persistence:
            self.persistence.close()
        super().server_close()


def create_server(port=8765, database=None, *, host="127.0.0.1", public_url=None, team_password="",
                  diagnosis_backend=None, cloud_client_factory=None):
    if public_url:
        parsed = urlparse(public_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("线上地址须为HTTPS网站根地址")
        public_url = parsed.scheme + "://" + parsed.netloc
        if not team_password:
            raise ValueError("请设置LABAGENT_TEAM_PASSWORD作为三人测试访问口令")
    store = Store(database)
    server = LabAgentServer((host, port), partial(Handler, store=store))
    server.rooms = Rooms(store)
    server.personal = Personal(store)
    server.lab_bridge = LabBridge()
    server.public_url, server.team_password = public_url, team_password
    server.allowed_hosts = {f"127.0.0.1:{server.server_port}", f"localhost:{server.server_port}"}
    server.allowed_origins = {"http://" + item for item in server.allowed_hosts}
    if public_url:
        server.allowed_hosts.add(urlparse(public_url).netloc)
        server.allowed_origins.add(public_url)
    server.learning_lock = threading.Lock()
    server.learning_attempts = {}
    server.diagnosis_slots = threading.BoundedSemaphore(4)
    server.session_locks = [threading.Lock() for _ in range(32)]
    server.workflow_database = store.path.with_name("workflow-" + store.path.name)
    server.diagnosis_backend = diagnosis_backend or os.environ.get("LABAGENT_DIAGNOSIS_BACKEND", "local")
    if server.diagnosis_backend not in ("local", "agentarts"):
        server.server_close()
        raise ValueError("LABAGENT_DIAGNOSIS_BACKEND应为local或agentarts")
    cloud_workflow = None
    server.cloud_configured = False
    if server.diagnosis_backend == "agentarts":
        from .cloud_workflow import CloudWorkflow
        from .model import load_env
        if cloud_client_factory is None:
            load_env(ROOT / "runtime/agentarts-client.env")
        cloud_workflow = CloudWorkflow(store, cloud_client_factory)
        if cloud_client_factory is not None:
            server.cloud_configured = True
        else:
            from .agentarts_client import AgentArtsClient
            try:
                AgentArtsClient.from_environment()
                server.cloud_configured = True
            except ValueError:
                pass

    def run_diagnosis(payload, *, lab_owner=None, task_id=None):
        if payload.get("room_id"):
            payload.update(server.rooms.prepare_diagnosis(payload))
        if cloud_workflow and payload.get("diagnostic_mode") == "open_network":
            with server.lab_bridge.runtime_connection(lab_owner if not payload.get("room_id") else None, server.public_url) as connection, server.tasks.runtime_connection(task_id, server.public_url if task_id else None) as control:
                return cloud_workflow.run(payload, lab_connection=connection, task_connection=control)
        if payload.get("session_id"):
            history = store.history(payload["session_id"])
            if history and history["attempts"][-1]["result"].get("agentarts"):
                raise ValueError("这个会话在华为云运行，请连接华为云服务后继续。")
        if payload.get("diagnostic_mode") == "open_network":
            history = store.history(payload["session_id"]) if payload.get("session_id") else None
            return run_open_workflow(payload, server.workflow_database,
                                     previous_attempt=history["attempts"][-1] if history else None)
        return run_workflow(payload, server.workflow_database)

    server.run_diagnosis = run_diagnosis
    server.recordings = Recordings(store.path.parent / "recordings")
    from .knowledge import notes_directory
    server.workspace = WorkspaceArchive(store, server.recordings, notes_directory(), server.workflow_database)
    log_lock = threading.Lock()
    def write_log(message, error=False):
        path = store.path.parent / ("server-error.log" if error else "server.log")
        stamp = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
        with log_lock, path.open("a", encoding="utf-8") as file:
            file.write(stamp + " " + message + "\n")
    server.write_log = write_log
    if os.environ.get("LABAGENT_CLOUD_PERSISTENCE") == "1":
        from .cloud_storage import CloudPersistence, SupabaseStorage
        try:
            server.persistence = CloudPersistence(server.workspace, SupabaseStorage.from_environment())
            server.persistence.bootstrap()
            server.persistence.start()
        except Exception:
            server.server_close()
            raise
    server.tasks = Tasks(store)
    write_log("LabAgent " + __version__ + " started on " + host + ":" + str(server.server_port))
    server.daemon_threads = True
    return server


def serve(port=8765, *, host="127.0.0.1", public_url=None, team_password=""):
    with create_server(port, host=host, public_url=public_url, team_password=team_password) as server:
        print(f"LabAgent: {public_url or ('http://' + host + ':' + str(server.server_port))}", flush=True)
        print("Local rules enabled. Ctrl+C to stop.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
