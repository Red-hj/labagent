import base64
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

from labagent import knowledge
from labagent.engine import diagnose
from labagent.recordings import Recordings
from labagent.rooms import Rooms
from labagent.server import create_server
from labagent.storage import data_directory
from labagent.store import Store
from labagent.workflow import run_open_workflow
from labagent.workspace_archive import WorkspaceArchive, WorkspaceBusy
from test_engine import ROOT, case
from test_investigation import ENV, stages, completion
from test_knowledge import document
from test_session_recovery import compare_then_complete


def workspace(folder):
    store = Store(folder / "sessions.sqlite3")
    Rooms(store)
    return WorkspaceArchive(store, Recordings(folder / "recordings"), folder / "course_notes", folder / "workflow.sqlite3")


class WorkspaceArchiveTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / "runtime/tests"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = workspace(self.folder / "source")
        self.target = workspace(self.folder / "target")
        payload = case()
        self.session = self.source.store.save(payload, diagnose(payload))["session_id"]

    def saved_archive(self):
        path = self.folder / "backup.zip"
        with self.source.export() as archive:
            path.write_bytes(archive.read_bytes())
        return path

    def test_snapshot_preserves_sessions_and_ignores_credentials_logs_and_caches(self):
        for name in (".env", "agentarts-client.env", "server.log", "key.txt"):
            (self.source.store.path.parent / name).write_text("private-secret", encoding="utf-8")
        path = self.saved_archive()
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(set(archive.namelist()), {"manifest.json", "sessions.sqlite3"})
        result = self.target.restore(path)
        self.assertEqual(result["sessions"], 1)
        self.assertEqual(self.target.store.history(self.session), self.source.store.history(self.session))
        self.assertEqual(list(self.target.temporary.iterdir()), [])

    def test_existing_workspace_is_unchanged_when_restore_is_refused(self):
        payload = case("03_verified_demo")
        saved = self.target.store.save(payload, diagnose(payload))
        before = self.target.store.history(saved["session_id"])
        with self.assertRaisesRegex(ValueError, "空工作区"):
            self.target.restore(self.saved_archive())
        self.assertEqual(before, self.target.store.history(saved["session_id"]))
        self.assertIsNone(self.target.store.history(self.session))

    def test_busy_requests_and_media_prevent_snapshot_but_gate_recovers(self):
        with self.source.gate.activity():
            with self.assertRaises(WorkspaceBusy):
                with self.source.export():
                    pass
        media = self.source.recordings.folder("a" * 32)
        media.mkdir()
        (media / "status.json").write_text('{"status":"running"}', encoding="utf-8")
        with self.assertRaisesRegex(WorkspaceBusy, "识别完成"):
            with self.source.export():
                pass
        self.assertFalse(self.source.gate.exclusive)

    def test_malicious_paths_and_unknown_members_never_reach_destination(self):
        original = self.saved_archive()
        for member in ("../outside.txt", "recordings/" + "a" * 32 + "/../../.env", "/absolute", ".env"):
            path = self.folder / "bad.zip"
            with zipfile.ZipFile(original) as source, zipfile.ZipFile(path, "w") as target:
                for entry in source.infolist():
                    target.writestr(entry, source.read(entry))
                target.writestr(member, "secret")
            with self.subTest(member=member), self.assertRaisesRegex(ValueError, "不支持"):
                self.target.restore(path)
            self.assertEqual(self.target.store.sessions(), [])
        self.assertFalse((self.folder / "outside.txt").exists())

    def test_missing_manifest_or_media_cannot_partially_restore(self):
        original = self.saved_archive()
        for omit in ("manifest.json", "sessions.sqlite3"):
            path = self.folder / "missing.zip"
            with zipfile.ZipFile(original) as source, zipfile.ZipFile(path, "w") as target:
                for entry in source.infolist():
                    if entry.filename != omit:
                        target.writestr(entry, source.read(entry))
            with self.assertRaises(ValueError):
                self.target.restore(path)
            self.assertEqual(self.target.store.sessions(), [])

    def test_malformed_database_does_not_install_tables_or_triggers(self):
        original = self.saved_archive()
        database = self.folder / "malicious.sqlite3"
        with zipfile.ZipFile(original) as archive:
            database.write_bytes(archive.read("sessions.sqlite3"))
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("CREATE TRIGGER unwanted AFTER INSERT ON attempts BEGIN DELETE FROM attempts; END")
        path = self.folder / "malicious.zip"
        with zipfile.ZipFile(original) as source, zipfile.ZipFile(path, "w") as target:
            target.writestr("manifest.json", source.read("manifest.json"))
            target.write(database, "sessions.sqlite3")
        with self.assertRaisesRegex(ValueError, "数据结构"):
            self.target.restore(path)
        self.assertEqual(self.target.store.sessions(), [])

    def add_image(self):
        media = self.source.recordings.folder("a" * 32)
        media.mkdir()
        for name in ("image.png", "preview.jpg", "transcript.log"):
            (media / name).write_bytes(b"test-data")
        (media / "status.json").write_text(json.dumps({"id": "a" * 32, "kind": "image", "suffix": ".png", "status": "ready"}), encoding="utf-8")

    def test_missing_entire_media_folder_is_detected_before_restore(self):
        self.add_image()
        original = self.saved_archive()
        path = self.folder / "missing-media.zip"
        with zipfile.ZipFile(original) as source, zipfile.ZipFile(path, "w") as target:
            for entry in source.infolist():
                if not entry.filename.startswith("recordings/"):
                    target.writestr(entry, source.read(entry))
        with self.assertRaisesRegex(ValueError, "数量"):
            self.target.restore(path)
        self.assertEqual(self.target.store.sessions(), [])
        self.assertEqual(list(self.target.recordings.root.iterdir()), [])

    def test_database_write_failure_rolls_back_published_media(self):
        self.add_image()
        path = self.saved_archive()
        # Simulate a failure at the final database write, after moving the media.
        with self.target.store.connect() as db:
            db.execute("CREATE TRIGGER fail_write BEFORE INSERT ON attempts BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(ValueError):
            self.target.restore(path)
        self.assertEqual(self.target.store.sessions(), [])
        self.assertEqual(list(self.target.recordings.root.iterdir()), [])
        self.assertTrue(self.source.recordings.folder("a" * 32).exists())

    def test_project_local_data_directory_controls_both_engines_and_notes(self):
        location = self.folder / "persistent"
        with patch.dict(os.environ, {"LABAGENT_DATA_DIR": str(location)}):
            self.assertEqual(data_directory(), location)
            self.assertEqual(Store().path.parent, location)
            self.assertEqual(knowledge.notes_directory(), location / "course_notes")
            from labagent.material_service import MaterialService
            self.assertEqual(MaterialService().store.path.parent, location)
            knowledge.import_document(document())
            self.assertEqual(len(list((location / "course_notes").glob("*.json"))), 1)
        with patch.dict(os.environ, {"LABAGENT_DATA_DIR": "../outside"}):
            with self.assertRaises(ValueError):
                Store()
        with patch.dict(os.environ, {"LABAGENT_DATA_DIR": ""}):
            self.assertEqual(data_directory(), ROOT / "runtime")


class WorkspaceHttpTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / "runtime/tests"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.auth = "Basic " + base64.b64encode(b"labagent:workspace-test").decode()
        env = patch.dict(os.environ, ENV)
        env.start(); self.addCleanup(env.stop)
        self.complete = completion
        def run(payload, database, **kwargs):
            return run_open_workflow(payload, database, self.complete, **kwargs)
        workflow_patch = patch("labagent.server.run_open_workflow", side_effect=run)
        workflow_patch.start(); self.addCleanup(workflow_patch.stop)

    def start_server(self, name):
        with patch("labagent.knowledge.NOTES_DIR", self.folder / name / "course_notes"):
            server = create_server(0, self.folder / name / "sessions.sqlite3", team_password="workspace-test")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown(); server.server_close(); thread.join(5)
        self.addCleanup(stop)
        return server, f"http://127.0.0.1:{server.server_port}"

    def request(self, base, path, payload=None, *, binary=False, auth=True, headers=None):
        values = {"Authorization": self.auth} if auth else {}
        values.update(headers or {})
        if binary:
            data = payload
            values["Content-Type"] = "application/zip"
        elif payload is not None:
            data = json.dumps(payload).encode()
            values["Content-Type"] = "application/json"
        else:
            data = None
        with urlopen(Request(base + path, data=data, headers=values), timeout=15) as response:
            return response.read() if response.headers["Content-Type"] == "application/zip" else json.load(response)

    def test_full_roundtrip_keeps_invite_tasks_original_image_conversation_and_report(self):
        source, base = self.start_server("before-deploy")
        room = self.request(base, "/api/rooms", {"name": "备份恢复实验", "actor": "甲"})
        room_path = "/api/rooms/" + room["id"]
        payload = stages()[0]
        image_id = "a" * 32
        media = source.recordings.folder(image_id); media.mkdir()
        (media / "image.png").write_bytes(b"test-original-image")
        (media / "preview.jpg").write_bytes(b"test-preview-image")
        (media / "transcript.log").write_text("终端输出", encoding="utf-8")
        status = {"id": image_id, "status": "ready", "kind": "image", "suffix": ".png", "regions": []}
        (media / "status.json").write_text(json.dumps(status), encoding="utf-8")
        room = self.request(base, room_path, {"actor": "甲", "member_id": "a", "action": "materials", "recordings": [image_id],
            "patches": [{"name": k, "content": v, "base_revision": 0} for k, v in payload["configs"].items()]})
        room = self.request(base, room_path, {"actor": "甲", "member_id": "a", "action": "task", "title": "检查网关", "assignee": "乙"})
        first = self.request(base, "/api/diagnose", {**payload, "room_id": room["id"], "room_actor": "甲", "room_revision": room["revision"]})
        note = knowledge.make_document(document())
        source.workspace.notes.mkdir()
        (source.workspace.notes / (note["id"].removeprefix("course-") + ".json")).write_text(json.dumps(note), encoding="utf-8")
        backup = self.request(base, "/api/workspace/backup", {})
        restored, new_base = self.start_server("after-deploy")
        self.assertTrue(self.request(new_base, "/api/workspace")["can_restore"])
        summary = self.request(new_base, "/api/workspace/restore", backup, binary=True)
        self.assertEqual([summary[k] for k in ("sessions", "rooms", "recordings", "documents")], [1, 1, 1, 1])
        returned = self.request(new_base, room_path)
        self.assertEqual(returned["session"]["session_id"], first["session_id"])
        self.assertEqual(returned["tasks"][0]["title"], "检查网关")
        self.assertEqual(returned["materials"], room["materials"])
        self.assertEqual(self.request(new_base, "/api/recordings/" + image_id)["transcript"], "终端输出")
        with urlopen(Request(new_base + "/api/recordings/" + image_id + "/image", headers={"Authorization": self.auth})) as response:
            self.assertEqual(response.read(), b"test-preview-image")
        with patch("labagent.knowledge.NOTES_DIR", restored.workspace.notes):
            self.assertIn("course-a", self.request(new_base, "/api/knowledge/scopes")["scopes"])
        self.complete = compare_then_complete
        second = self.request(new_base, "/api/diagnose", {**stages()[1], "session_id": first["session_id"],
            "interaction": {"type": "update", "message": "恢复后已改正网关。"}}, headers={"X-LabAgent-Room": room["id"]})
        self.assertTrue(second["workflow"]["context_restored"])
        self.assertEqual(len(second["conversation"]), 2)
        self.assertTrue(second["interaction_checks"]["compare_materials"]["changed"])
        self.assertEqual(len(self.request(new_base, "/api/sessions/" + first["session_id"], headers={"X-LabAgent-Room": room["id"]})["attempts"]), 2)
        report = self.request(new_base, "/api/sessions/" + first["session_id"] + "/report", headers={"X-LabAgent-Room": room["id"]})["markdown"]
        self.assertIn("恢复后已改正网关", report)
        exported = self.request(new_base, "/api/export", {"session_id": first["session_id"]}, headers={"X-LabAgent-Room": room["id"]})
        self.assertTrue(Path(exported["report_path"]).is_relative_to(restored.workspace.store.path.parent))

    def test_backup_and_restore_require_team_login_and_same_site(self):
        server, base = self.start_server("access")
        for path in ("/api/workspace/backup", "/api/workspace/restore"):
            with self.subTest(path=path), self.assertRaises(HTTPError) as denied:
                self.request(base, path, b"bad", binary=True, auth=False)
            self.assertEqual(denied.exception.code, 401)
            with self.assertRaises(HTTPError) as denied:
                self.request(base, path, b"bad", binary=True, headers={"Origin": "https://other.invalid"})
            self.assertEqual(denied.exception.code, 403)

    def test_invalid_upload_fails_without_changing_empty_workspace(self):
        server, base = self.start_server("invalid")
        with self.assertRaises(HTTPError) as failed:
            self.request(base, "/api/workspace/restore", b"not-a-zip", binary=True)
        self.assertEqual(failed.exception.code, 400)
        self.assertTrue(self.request(base, "/api/workspace")["can_restore"])
        self.assertEqual(list(server.workspace.temporary.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
