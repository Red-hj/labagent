"""Account isolation, memory lifecycle and real HTTP handlers without TCP access."""
from copy import deepcopy
from email.parser import BytesParser
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid
import zipfile

from labagent.cloud_workflow import CloudWorkflow
from labagent.engine import diagnose
from labagent.investigation import investigate, source_bundle, validate_open
from labagent.material_service import MaterialService
from labagent.personal import Personal, SignInError, LIFETIME
from labagent.rooms import Rooms
from labagent.server import Handler, create_server
from labagent.store import Store
from test_engine import case
from test_investigation import ENV, completion, stages
from test_workspace_archive import workspace

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "local-test-password"


class PersonalTests(unittest.TestCase):
    def setUp(self):
        folder = ROOT / "runtime/tests"
        folder.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=folder)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.store = Store(self.folder / "sessions.sqlite3")
        self.rooms = Rooms(self.store)
        self.now = 1_000
        self.personal = Personal(self.store, clock=lambda: self.now)
        self.user, self.token = self.register("alice")

    def register(self, username):
        return self.personal.authenticate({"username": username, "password": PASSWORD}, register=True)

    def memory(self, **changes):
        return self.personal.save_memory(self.user["id"], {"kind": "preference", "title": "排查方式",
            "content": "先解释原因，再给检查命令。", **changes})

    def remember(self, session=None, **changes):
        payload = {"question": "OSPF邻居无法建立", **changes}
        result = {"session_id": session or uuid.uuid4().hex, "llm": {"status": "ok"}}
        self.personal.remember_issue(self.user["id"], payload, result)
        return result["session_id"]

    def test_authentication_hashes_tokens_and_passwords_and_reuses_account(self):
        user, token = self.personal.authenticate({"username": "ALICE", "password": PASSWORD})
        self.assertEqual(user, self.user)
        with self.store.connect() as db:
            salt, digest = db.execute("SELECT salt,password_hash FROM personal_users").fetchone()
            stored_tokens = db.execute("SELECT token_hash FROM personal_tokens").fetchall()
        self.assertEqual(len(salt), 32)
        self.assertTrue(digest.startswith("pbkdf2_sha256$600000$"))
        self.assertNotIn(PASSWORD, digest)
        self.assertNotIn(token, str(stored_tokens))
        self.assertEqual(set(user), {"id", "username", "display_name", "memory_enabled"})
        self.personal.logout(token)
        self.assertIsNone(self.personal.user(token))
        self.now += LIFETIME + 1
        self.assertIsNone(self.personal.user(self.token))

    def test_wrong_password_is_throttled_and_duplicate_account_rejected(self):
        for _ in range(5):
            with self.assertRaises(SignInError):
                self.personal.authenticate({"username": "alice", "password": "incorrect-long-password"})
        with self.assertRaises(SignInError) as caught:
            self.personal.authenticate({"username": "alice", "password": PASSWORD})
        self.assertEqual(caught.exception.status, 429)
        self.now += 601
        self.assertEqual(self.personal.authenticate({"username": "alice", "password": PASSWORD})[0], self.user)
        with self.assertRaisesRegex(ValueError, "已存在"):
            self.register("ALICE")

    def test_memory_crud_and_account_isolation(self):
        bob, _ = self.register("bob")
        saved = self.memory()
        self.assertEqual(self.personal.list_memories(bob["id"]), [])
        self.assertEqual(self.personal.delete_memory(bob["id"], saved["id"]), 0)
        with self.assertRaisesRegex(ValueError, "不存在"):
            self.personal.save_memory(bob["id"], {**saved, "content": "改成我的内容"})
        edited = self.memory(id=saved["id"], content="先给检查命令，再解释原因。")
        self.assertEqual(edited["id"], saved["id"])
        self.assertEqual(len(self.personal.list_memories(self.user["id"])), 1)
        self.assertEqual(self.personal.delete_memory(self.user["id"]), 1)

    def test_counts_independent_sessions_and_does_not_recreate_deleted_memory(self):
        session = self.remember()
        self.remember(session)
        self.remember()
        self.assertEqual(self.personal.list_memories(self.user["id"])[0]["occurrences"], 2)
        self.personal.delete_memory(self.user["id"])
        self.remember(session)
        self.assertEqual(self.personal.list_memories(self.user["id"]), [])
        self.remember()
        self.assertEqual(self.personal.list_memories(self.user["id"])[0]["occurrences"], 1)

    def test_disabled_demo_shared_failed_and_sensitive_questions_are_not_recorded(self):
        self.personal.enable_memory(self.user["id"], False)
        self.remember()
        self.assertEqual(self.personal.context(self.personal.user(self.token), "OSPF"), [])
        self.personal.enable_memory(self.user["id"], True)
        self.remember(room_id="a" * 32)
        self.remember(evidence_kind="demo")
        self.remember(question="我的密码是一个测试字符串")
        self.personal.remember_issue(self.user["id"], {"question": "OSPF邻居无法建立"},
            {"session_id": "a" * 32, "llm": {"status": "error"}})
        self.assertEqual(self.personal.list_memories(self.user["id"]), [])

    def test_context_uses_only_own_relevant_topics_and_keeps_preferences(self):
        self.memory()
        self.memory(kind="issue", title="OSPF邻居排查", content="曾经排查邻居建立问题。")
        self.memory(kind="issue", title="DHCP地址池", content="核对地址池。")
        bob, _ = self.register("bob")
        self.personal.save_memory(bob["id"], {"kind": "environment", "title": "其他人的环境", "content": "其他人的配置"})
        context = self.personal.context(self.user, "OSPF邻居")
        self.assertEqual({item["title"] for item in context}, {"排查方式", "OSPF邻居排查"})
        self.assertTrue(all(set(item) == {"kind", "title", "content", "occurrences"} for item in context))
        with self.assertRaisesRegex(ValueError, "密钥"):
            self.memory(content="api_key=test-value")

    def test_history_ownership_filters_before_pagination_and_preserves_old_team_records(self):
        payload = case()
        old = self.store.save(payload, diagnose(payload))
        own = self.store.save(payload, diagnose(payload), owner=self.user["id"])
        bob, _ = self.register("bob")
        other = self.store.save(payload, diagnose(payload), owner=bob["id"])
        self.assertEqual(self.store.sessions(personal=True, user_id=self.user["id"], limit=1)[0]["session_id"], own["session_id"])
        self.assertFalse(self.personal.owns("session", other["session_id"], self.user["id"]))
        self.store.save({**payload, "session_id": old["session_id"]}, diagnose(payload), owner=self.user["id"])
        self.assertTrue(self.personal.owns("session", old["session_id"], bob["id"]))
        self.assertEqual(len(self.store.sessions(personal=True)), 1)
        self.store.delete_session(own["session_id"])
        with self.store.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM personal_resources WHERE resource_id=?", (own["session_id"],)).fetchone())

    def test_invitation_shares_room_history_and_recordings_but_not_personal_memories(self):
        room = self.rooms.create("协作实验", "Alice")
        payload = case()
        shared = self.store.save(payload, diagnose(payload), owner="room:" + room["id"])
        self.assertFalse(self.personal.owns("session", shared["session_id"]))
        self.assertTrue(self.personal.owns("session", shared["session_id"], room_id=room["id"]))
        bob, _ = self.register("bob")
        self.personal.join_room(bob["id"], room["id"])
        self.assertTrue(self.personal.owns("session", shared["session_id"], bob["id"]))
        rid = "a" * 32
        with self.store.connect() as db:
            db.execute("INSERT INTO personal_resources VALUES ('recording',?,?)", (rid, self.user["id"]))
            room["recordings"] = [rid]
            db.execute("UPDATE rooms SET data=? WHERE id=?", (json.dumps(room), room["id"]))
        self.assertTrue(self.personal.owns("recording", rid, bob["id"]))
        self.assertTrue(self.personal.owns("recording", rid, room_id=room["id"]))
        self.assertFalse(self.personal.owns("recording", rid))

    def test_memory_is_model_context_and_cannot_be_used_as_current_source(self):
        payload = {**stages()[0], "personal_memory": [self.memory()]}
        payload["personal_memory"] = self.personal.context(self.user, "OSPF")
        validate_open(payload)
        seen = []
        def respond(messages):
            seen.append(deepcopy(messages))
            return completion(messages)
        with patch.dict(os.environ, ENV):
            result = investigate(payload, completion=respond)
        self.assertEqual(result["llm"]["status"], "ok")
        self.assertIn("不作为当前设备观测", seen[0][0]["content"])
        self.assertEqual(json.loads(seen[0][1]["content"])["personal_memory"], payload["personal_memory"])
        self.assertFalse(any("memory" in key for key in source_bundle(payload)[0]))

    def test_backup_restores_accounts_memories_and_ownership_but_never_login_tokens(self):
        source = workspace(self.folder / "source")
        personal = Personal(source.store)
        user, token = personal.authenticate({"username": "alice", "password": PASSWORD}, register=True)
        personal.save_memory(user["id"], {"kind": "environment", "title": "实验设备", "content": "使用FRRouting。"})
        saved = source.store.save(case(), diagnose(case()), owner=user["id"])
        with source.export() as archive:
            target = workspace(self.folder / "restored")
            summary = target.restore(archive)
        self.assertEqual((summary["users"], summary["memories"]), (1, 1))
        restored = Personal(target.store)
        self.assertIsNone(restored.user(token))
        restored_user, _ = restored.authenticate({"username": "alice", "password": PASSWORD})
        self.assertEqual(restored_user["id"], user["id"])
        self.assertEqual(restored.list_memories(user["id"])[0]["title"], "实验设备")
        self.assertFalse(restored.owns("session", saved["session_id"]))
        self.assertTrue(restored.owns("session", saved["session_id"], user["id"]))

    def test_cloud_roundtrip_applies_current_memory_and_clears_it_on_followup(self):
        service = MaterialService(self.folder / "cloud.sqlite3", completion)
        requests = []
        class Client:
            def invoke(self, request):
                requests.append(deepcopy(request))
                return service.invoke(request)
        workflow = CloudWorkflow(self.store, lambda _: Client())
        self.memory()
        payload = {**stages()[0], "personal_memory": self.personal.context(self.user, "OSPF")}
        with patch.dict(os.environ, ENV):
            first = workflow.run(payload)
            saved = self.store.save(payload, first, owner=self.user["id"])
            second = workflow.run({**payload, "session_id": saved["session_id"], "personal_memory": [],
                "interaction": {"type": "ask", "message": "再解释一下"}})
        self.assertEqual(requests[0]["personal_memory"], payload["personal_memory"])
        self.assertEqual(requests[1]["personal_memory"], [])
        current = service.store.history(second["agentarts"]["session_id"])["attempts"][-1]["input"]
        self.assertEqual(current["personal_memory"], [])
        class OldClient:
            def invoke(self, request):
                return {"session_id": "a" * 32, "result": first}
        with self.assertRaisesRegex(ValueError, "尚不支持个人记忆"):
            CloudWorkflow(self.store, lambda _: OldClient()).run(payload)


class RequestSocket:
    """Feed wire-format requests to the actual handler without opening a client socket."""
    def __init__(self, request):
        self.input = BytesIO(request)
        self.output = BytesIO()

    def makefile(self, *args, **kwargs):
        return self.input

    def sendall(self, data):
        self.output.write(data)


class PersonalHttpTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / "runtime/tests"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.env = patch.dict(os.environ, {**ENV, "LABAGENT_MAINTENANCE_PASSWORD": "", "LABAGENT_DATA_DIR": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        notes = patch("labagent.knowledge.NOTES_DIR", self.folder / "notes")
        notes.start()
        self.addCleanup(notes.stop)
        self.server = create_server(0, self.folder / "web.sqlite3")
        self.addCleanup(self.server.server_close)
        self.store = self.server.personal.store
        self.user, token = self.server.personal.authenticate({"username": "alice", "password": PASSWORD}, register=True)
        self.cookie = "labagent_personal=" + token
        self.server.run_diagnosis = lambda payload: investigate(payload, completion=completion)

    def request(self, path, payload=None, *, method=None, cookie=None, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else b""
        values = {"Host": f"127.0.0.1:{self.server.server_port}", "Content-Type": "application/json",
            "Content-Length": str(len(body)), **(headers or {})}
        if cookie:
            values["Cookie"] = cookie
        raw = (method or ("POST" if payload is not None else "GET")) + " " + path + " HTTP/1.0\r\n"
        raw += "".join(key + ": " + value + "\r\n" for key, value in values.items())
        sock = RequestSocket(raw.encode() + b"\r\n" + body)
        with patch.object(Handler, "log_message"):
            self.server.RequestHandlerClass(sock, ("127.0.0.1", 12345), self.server)
        head, content = sock.output.getvalue().split(b"\r\n\r\n", 1)
        status, fields = head.split(b"\r\n", 1)
        fields = BytesParser().parsebytes(fields + b"\r\n\r\n")
        data = json.loads(content) if "application/json" in fields.get("Content-Type", "") else content
        return int(status.split()[1]), fields, data

    def test_cookie_login_logout_registration_and_expired_identity(self):
        status, headers, result = self.request("/api/account/login", {"username": "alice", "password": PASSWORD})
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertNotIn("token", result)
        self.assertEqual(self.request("/api/account", cookie=cookie.split(";")[0])[2]["user"], self.user)
        self.assertEqual(self.request("/api/account/logout", {}, cookie=cookie.split(";")[0])[0], 200)
        self.assertEqual(self.request("/api/memories", cookie=cookie.split(";")[0])[0], 401)
        self.assertEqual(self.request("/api/memories")[0], 401)
        self.assertEqual(self.request("/api/account/register", {"username": "bob", "password": PASSWORD})[0], 200)
        self.server.public_url = "https://example.test"
        self.assertIn("Secure", self.request("/api/account/login", {"username": "alice", "password": PASSWORD})[1]["Set-Cookie"])

    def test_routes_isolate_memory_history_reports_delete_and_followup(self):
        memory = {"kind": "preference", "title": "命令风格", "content": "先给检查命令。", "user_id": "spoof"}
        self.assertEqual(self.request("/api/memories", memory, cookie=self.cookie)[0], 200)
        saved = self.request("/api/diagnose", stages()[0], cookie=self.cookie)[2]
        session = saved["session_id"]
        self.assertEqual(saved["memory_used"][0]["title"], "命令风格")
        self.assertEqual(len(self.request("/api/sessions", cookie=self.cookie)[2]), 1)
        self.assertEqual(self.request("/api/sessions")[2], [])
        for suffix in ("", "/report"):
            self.assertEqual(self.request("/api/sessions/" + session + suffix)[0], 404)
        self.assertEqual(self.request("/api/sessions/" + session, method="DELETE")[0], 404)
        self.assertEqual(self.request("/api/export", {"session_id": session})[0], 404)
        self.assertEqual(self.request("/api/diagnose", {**stages()[0], "session_id": session})[0], 404)
        self.assertEqual(self.request("/api/account/memory", {"enabled": False}, cookie=self.cookie)[0], 200)
        updated = self.request("/api/diagnose", {**stages()[0], "session_id": session}, cookie=self.cookie)[2]
        self.assertEqual(updated["memory_used"], [])
        self.assertEqual(self.request("/api/sessions/" + session, method="DELETE", cookie=self.cookie)[0], 200)
        self.assertEqual(self.request("/api/memories", method="DELETE", cookie=self.cookie)[0], 200)
        self.assertEqual(self.request("/api/memories", cookie=self.cookie)[2]["memories"], [])

    def test_client_cannot_inject_memory_or_mutate_from_another_origin(self):
        payload = {**stages()[0], "personal_memory": [{"kind": "preference", "title": "伪造", "content": "伪造内容", "occurrences": 1}]}
        result = self.request("/api/diagnose", payload)[2]
        self.assertEqual(result["memory_used"], [])
        self.assertEqual(self.request("/api/memories", {"title": "test", "content": "test"}, cookie=self.cookie,
            headers={"Origin": "https://other.test"})[0], 403)
        self.assertEqual(self.request("/api/memories", {}, cookie="labagent_personal=invalid")[0], 401)

    def test_personal_accounts_require_independent_maintenance_password_for_global_backup(self):
        self.assertEqual(self.request("/api/workspace/backup", {})[0], 403)
        with patch.dict(os.environ, {"LABAGENT_MAINTENANCE_PASSWORD": "test-maintenance-password"}):
            self.assertEqual(self.request("/api/workspace/backup", {}, cookie=self.cookie)[0], 403)
            status, _, archive = self.request("/api/workspace/backup", {}, headers={"X-LabAgent-Maintenance": "test-maintenance-password"})
        self.assertEqual(status, 200)
        with zipfile.ZipFile(BytesIO(archive)) as saved:
            self.assertEqual(json.loads(saved.read("manifest.json"))["users"], 1)

    def test_uploaded_media_is_private_and_room_invitation_allows_shared_diagnosis_without_memory(self):
        rid = "a" * 32
        with patch.object(self.server.recordings, "upload", return_value={"id": rid, "status": "queued"}):
            self.assertEqual(self.request("/api/recordings", {}, cookie=self.cookie)[0], 202)
        with patch.object(self.server.recordings, "status", return_value={"id": rid, "status": "ready"}):
            self.assertEqual(self.request("/api/recordings/" + rid)[0], 404)
            self.assertEqual(self.request("/api/recordings/" + rid, cookie=self.cookie)[0], 200)
        room = self.request("/api/rooms", {"name": "协作排查", "actor": "Alice"}, cookie=self.cookie)[2]
        room = self.request("/api/rooms/" + room["id"], {"actor": "Alice", "member_id": "test-alice", "action": "materials",
            "patches": [{"name": key, "content": value, "base_revision": 0} for key, value in stages()[0]["configs"].items()],
            "question": "协作检查OSPF", "base_question": "", "recordings": [rid]}, cookie=self.cookie)[2]
        self.assertIn("id", room)
        with patch.object(self.server.recordings, "status", return_value={"id": rid, "status": "ready"}):
            self.assertEqual(self.request("/api/recordings/" + rid + "?room=" + room["id"])[0], 200)
        self.server.personal.save_memory(self.user["id"], {"kind": "preference", "title": "个人偏好", "content": "我的私有说明"})
        saved = self.request("/api/diagnose", {**stages()[0], "room_id": room["id"], "room_revision": room["revision"], "room_actor": "Alice"},
            cookie=self.cookie, headers={"X-LabAgent-Room": room["id"]})[2]
        self.assertEqual(saved["memory_used"], [])
        self.assertEqual(len(self.server.personal.list_memories(self.user["id"])), 1)
        self.assertEqual(self.request("/api/sessions/" + saved["session_id"])[0], 404)
        self.assertEqual(self.request("/api/sessions/" + saved["session_id"], headers={"X-LabAgent-Room": room["id"]})[0], 200)
        followup = self.request("/api/diagnose", {**stages()[0], "session_id": saved["session_id"],
            "interaction": {"type": "ask", "message": "继续共享排查"}}, cookie=self.cookie)[2]
        self.assertEqual(followup["memory_used"], [])
        self.assertEqual(len(self.server.personal.list_memories(self.user["id"])), 1)
        row = next(row for row in self.request("/api/sessions", cookie=self.cookie)[2] if row["session_id"] == saved["session_id"])
        self.assertEqual(row["room_id"], room["id"])
        self.assertEqual(self.server.personal.session_room(saved["session_id"]), room["id"])


if __name__ == "__main__":
    unittest.main()
