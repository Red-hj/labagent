from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import base64
import hmac
from pathlib import Path
import re
import threading
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, parse_qs

from .engine import diagnose, fingerprint, validate
from .model import configured, public_config
from . import __version__
from .store import Store
from .workflow import run_workflow, available
from .report import generate_report
from .knowledge import import_document, list_documents, list_scopes, search

ROOT = Path(__file__).resolve().parents[1]


class Handler(BaseHTTPRequestHandler):
    server_version = "LabAgent/" + __version__

    def __init__(self, *args, store, **kwargs):
        self.store = store
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):
        # Avoid logging uploaded configurations and other request bodies.
        print(time.strftime("%H:%M:%S"), format % args, flush=True)
        self.server.write_log(format % args)

    def reply(self, value, status=200, download=False):
        content = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if download:
            self.send_header("Content-Disposition", 'attachment; filename="labagent-record.json"')
        self.end_headers()
        self.wfile.write(content)

    def safe_host(self):
        return self.headers.get("Host") in self.server.allowed_hosts

    def team_access(self):
        if not self.server.team_password:
            return True
        try:
            scheme, value = self.headers.get("Authorization", "").split(" ", 1)
            credentials = base64.b64decode(value, validate=True).decode("utf-8")
            if scheme.lower() == "basic" and hmac.compare_digest(credentials.encode(), ("labagent:" + self.server.team_password).encode()):
                return True
        except (ValueError, UnicodeError):
            pass
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
        if not self.safe_host():
            return self.reply({"error": "请使用当前网站地址访问"}, 403)
        path = urlparse(self.path).path
        if path == "/api/health":
            return self.reply({"app": "LabAgent", "version": __version__, "mode": "local_rules", "online": bool(self.server.public_url), "knowledge_method": "local_bm25_with_fault_tags", "workflow_backend": "langgraph" if available() else "local_rules", "llm_configured": configured(), "llm": public_config()})
        if not self.team_access():
            return
        if path == "/api/knowledge/scopes":
            return self.reply({"scopes": list_scopes()})
        if path == "/api/knowledge/documents":
            try:
                scope = parse_qs(urlparse(self.path).query).get("scope", ["default"])[0]
                return self.reply(list_documents(scope))
            except ValueError as error:
                return self.reply({"error": str(error)}, 400)
        if path == "/api/cases":
            cases = []
            for file in sorted((ROOT / "data" / "cases").glob("*.json")):
                data = json.loads(file.read_text(encoding="utf-8"))
                cases.append({"id": file.stem, "title": data["title"], "description": data["description"]})
            return self.reply(cases)
        if path == "/api/scenarios":
            return self.reply([{"id": file.stem, "title": json.loads(file.read_text(encoding="utf-8"))["title"]}
                               for file in sorted((ROOT / "data" / "scenarios").glob("*.json"))])
        if match := re.fullmatch(r"/api/scenarios/([a-z0-9_-]+)", path):
            file = ROOT / "data" / "scenarios" / (match[1] + ".json")
            if file.is_file():
                return self.reply(json.loads(file.read_text(encoding="utf-8")))
        if match := re.fullmatch(r"/api/cases/([a-z0-9_-]+)", path):
            file = ROOT / "data" / "cases" / (match[1] + ".json")
            if file.is_file():
                data = json.loads(file.read_text(encoding="utf-8"))
                # Gold labels belong to offline evaluation, not engine input.
                return self.reply({k: v for k, v in data.items() if k != "expected"})
        if path == "/api/sessions":
            return self.reply(self.store.sessions())
        if match := re.fullmatch(r"/api/sessions/([a-f0-9]{32})(/export|/report)?", path):
            history = self.store.history(match[1])
            if history:
                if match[2] == "/report":
                    return self.reply({"markdown": generate_report(history)})
                return self.reply(history, download=bool(match[2]))
        assets = {"/": ("student.html", "text/html; charset=utf-8"), "/student.js": ("student.js", "text/javascript; charset=utf-8"), "/student.css": ("student.css", "text/css; charset=utf-8"),
                  "/ambient-network.js": ("ambient-network.js", "text/javascript; charset=utf-8"),
                  "/vendor/tsparticles.slim.bundle.min.js": ("vendor/tsparticles.slim.bundle.min.js", "text/javascript; charset=utf-8"),
                  "/workbench": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8"), "/knowledge.css": ("knowledge.css", "text/css; charset=utf-8")}
        if path in assets:
            name, mime = assets[path]
            content = (ROOT / "web" / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(content)
            return
        self.reply({"error": "未找到内容"}, 404)

    def do_POST(self):
        origin = self.headers.get("Origin")
        if not self.safe_host() or (origin and origin not in self.server.allowed_origins):
            return self.reply({"error": "请从当前网站提交请求"}, 403)
        if not self.team_access():
            return
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self.reply({"error": "请使用 application/json"}, 415)
        path = urlparse(self.path).path
        if path not in ("/api/diagnose", "/api/fingerprint", "/api/export", "/api/knowledge/import", "/api/knowledge/search"):
            return self.reply({"error": "未找到接口"}, 404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1_000_000:
                return self.reply({"error": "请求大小须在 1 字节至 1 MB 之间"}, 413)
            payload = json.loads(self.rfile.read(length).decode("utf-8-sig"))
            if not isinstance(payload, dict):
                raise ValueError("请求必须为 JSON 对象")
            if path == "/api/knowledge/import":
                return self.reply(import_document(payload))
            if path == "/api/knowledge/search":
                return self.reply(search(payload.get("query", ""), scope=payload.get("scope", "default"), limit=payload.get("limit", 3)))
            session = payload.get("session_id")
            if session is not None and (not isinstance(session, str) or not re.fullmatch(r"[a-f0-9]{32}", session)):
                raise ValueError("session_id 格式错误")
            if path == "/api/export":
                if not session:
                    raise ValueError("请选择已有会话")
                history = self.store.history(session)
                if not history:
                    return self.reply({"error": "会话不存在"}, 404)
                target = ROOT / "runtime" / "exports" / (session + ".json")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
                report_path = target.with_suffix(".md")
                report_path.write_text(generate_report(history), encoding="utf-8")
                result = {"path": str(target), "report_path": str(report_path)}
                if self.server.public_url:
                    result.update(markdown=report_path.read_text(encoding="utf-8"), filename="labagent-" + session[:8] + ".md")
                return self.reply(result)
            validate(payload)
            if path == "/api/fingerprint":
                return self.reply({"config_fingerprint": fingerprint(payload)})
            if session:
                history = self.store.history(session)
                if not history:
                    raise ValueError("会话不存在，请开始新实验")
                if history["attempts"][-1]["input"].get("diagnostic_mode", "ospf_rules") != payload.get("diagnostic_mode", "ospf_rules"):
                    raise ValueError("切换排查模式时请开始新实验")
            if not self.server.diagnosis_slots.acquire(blocking=False):
                return self.reply({"error": "当前正在处理其他诊断，请稍后重试"}, 429)
            try:
                key = session or "new"
                with self.server.session_locks[hash(key) % len(self.server.session_locks)]:
                    result = run_workflow(payload, self.server.workflow_database)
                    self.reply(self.store.save(payload, result))
            finally:
                self.server.diagnosis_slots.release()
        except (ValueError, KeyError, TypeError, UnicodeError) as error:
            self.reply({"error": str(error)}, 400)
        except Exception:
            self.reply({"error": "本地服务处理失败，请查看项目内日志"}, 500)
            import traceback
            traceback.print_exc()
            self.server.write_log(traceback.format_exc(), error=True)


def create_server(port=8765, database=None, *, host="127.0.0.1", public_url=None, team_password=""):
    if public_url:
        parsed = urlparse(public_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("线上地址须为HTTPS网站根地址")
        public_url = parsed.scheme + "://" + parsed.netloc
        if not team_password:
            raise ValueError("请设置LABAGENT_TEAM_PASSWORD作为三人测试访问口令")
    store = Store(database)
    server = ThreadingHTTPServer((host, port), partial(Handler, store=store))
    server.public_url, server.team_password = public_url, team_password
    server.allowed_hosts = {f"127.0.0.1:{server.server_port}", f"localhost:{server.server_port}"}
    server.allowed_origins = {"http://" + item for item in server.allowed_hosts}
    if public_url:
        server.allowed_hosts.add(urlparse(public_url).netloc)
        server.allowed_origins.add(public_url)
    server.diagnosis_slots = threading.BoundedSemaphore(4)
    server.session_locks = [threading.Lock() for _ in range(32)]
    server.workflow_database = store.path.with_name("workflow-" + store.path.name)
    log_lock = threading.Lock()
    def write_log(message, error=False):
        path = store.path.parent / ("server-error.log" if error else "server.log")
        stamp = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
        with log_lock, path.open("a", encoding="utf-8") as file:
            file.write(stamp + " " + message + "\n")
    server.write_log = write_log
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
