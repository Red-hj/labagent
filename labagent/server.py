from functools import partial
from queue import Empty, Queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import base64
import hmac
from pathlib import Path
import re
import threading
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, parse_qs, unquote

from .engine import diagnose, fingerprint, validate
from .model import configured, public_config
from . import __version__
from .store import Store
from .workflow import run_workflow, available
from .report import generate_report
from .knowledge import import_document, list_documents, list_scopes, search
from .cases import demo_cases
from .demos import network_access_demo, ospf_goals_demo
from .progress import emit_progress, progress_to
from .recordings import Recordings, VIDEO_SUFFIXES, available as replay_available
from .operation_history import inspect_history

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
            return self.reply({"app": "LabAgent", "version": __version__, "mode": "local_rules", "online": bool(self.server.public_url), "knowledge_method": "local_bm25_with_fault_tags", "workflow_backend": "langgraph" if available() else "local_rules", "diagnosis_backend": self.server.diagnosis_backend, "diagnosis_configured": self.server.cloud_configured if self.server.diagnosis_backend == "agentarts" else configured(), "llm_configured": configured(), "llm": public_config(), "replay_available": replay_available()})
        if not self.team_access():
            return
        if path == "/api/demos/network_access":
            return self.reply(network_access_demo())
        if path == "/api/demos/ospf_goals":
            return self.reply(ospf_goals_demo())
        if path == "/api/demos/operation_history":
            return self.reply({"title": "找回未生效的network命令", "source": "合成演示材料", "materials": {
                "终端操作.log": (ROOT / "data/examples/终端操作回溯.log").read_text(encoding="utf-8"),
                "实验要求.txt": "实验目标：R1通过OSPF访问R2的Loopback0地址10.2.2.2，Ping全部成功。\n分析操作记录中哪一步影响了配置，并说明怎样继续。"}})
        if path == "/api/demos/repair_comparison":
            sample = ROOT / "data/examples/frr_repair_run.json"
            if not sample.is_file():
                return self.reply({"error":"FRR实验运行记录尚未导入"}, 404)
            return self.reply({"title":"修复方案实际运行对照", "materials":{"FRR修复实验.json":sample.read_text(encoding="utf-8")}})
        if path == "/api/demos/operation_history/video":
            return self.send_video(ROOT / "data/examples/实验操作回放.webm", "video/webm")
        if match := re.fullmatch(r"/api/recordings/([a-f0-9]{32})(/video|/image)?", path):
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
            return self.reply(self.store.sessions())
        if match := re.fullmatch(r"/api/sessions/([a-f0-9]{32})(/export|/report)?", path):
            history = self.store.history(match[1])
            if history:
                if match[2] == "/report":
                    return self.reply({"markdown": generate_report(history)})
                return self.reply(history, download=bool(match[2]))
            return self.reply({"code": "session_not_found", "error": "这次会话已不存在，请开始新的排查并重新提供材料。"}, 404)
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
        if urlparse(self.path).path == "/api/recordings":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                result = self.server.recordings.upload(self.rfile, length, unquote(self.headers.get("X-Recording-Name", "recording.mp4")))
                return self.reply(result, 202)
            except ValueError as error:
                self.close_connection = True
                return self.reply({"error": str(error)}, 400)
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self.reply({"error": "请使用 application/json"}, 415)
        path = urlparse(self.path).path
        if path not in ("/api/diagnose", "/api/diagnose/stream", "/api/fingerprint", "/api/export", "/api/knowledge/import", "/api/knowledge/search", "/api/operations/inspect", "/api/repair-labs/inspect"):
            return self.reply({"error": "未找到接口"}, 404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1_000_000:
                return self.reply({"error": "请求大小须在 1 字节至 1 MB 之间"}, 413)
            payload = json.loads(self.rfile.read(length).decode("utf-8-sig"))
            if not isinstance(payload, dict):
                raise ValueError("请求必须为 JSON 对象")
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
            if path == "/api/diagnose/stream":
                return self.stream_diagnosis(payload, session)
            try:
                key = session or "new"
                with self.server.session_locks[hash(key) % len(self.server.session_locks)]:
                    result = self.server.run_diagnosis(payload)
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

    def send_recording(self, recording_id, status):
        path = self.server.recordings.folder(recording_id) / ("video" + status["suffix"])
        return self.send_video(path, VIDEO_SUFFIXES[status["suffix"]])

    def send_video(self, path, mime):
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

    def stream_diagnosis(self, payload, session):
        events = Queue()

        def process():
            try:
                with progress_to(events.put):
                    emit_progress("received", "请求已收到，正在准备本轮诊断。")
                    with self.server.session_locks[hash(session or "new") % len(self.server.session_locks)]:
                        result = self.server.run_diagnosis(payload)
                        emit_progress("saving", "正在保存本轮结论与会话记录…")
                        saved = self.store.save(payload, result)
                    events.put({"type": "result", "result": saved})
            except (ValueError, KeyError, TypeError) as error:
                events.put({"type": "error", "message": str(error)})
            except Exception:
                import traceback
                self.server.write_log(traceback.format_exc(), error=True)
                events.put({"type": "error", "message": "本轮诊断处理失败，请重新提交。"})
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
                if event["type"] in ("result", "error"):
                    return
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


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

    def run_diagnosis(payload):
        if cloud_workflow and payload.get("diagnostic_mode") == "open_network":
            return cloud_workflow.run(payload)
        if payload.get("session_id"):
            history = store.history(payload["session_id"])
            if history and history["attempts"][-1]["result"].get("agentarts"):
                raise ValueError("这个会话在华为云运行，请连接华为云服务后继续。")
        return run_workflow(payload, server.workflow_database)

    server.run_diagnosis = run_diagnosis
    server.recordings = Recordings(store.path.parent / "recordings")
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
