import json
import os
from contextlib import closing
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from labagent.conversations import DEFAULT_QUESTIONS
from labagent.server import create_server
from labagent.store import Store
from test_investigation import ROOT, ENV, stages, completion


def saved_result(number=1, answer="检查网关归属，并提供路由表。"):
    return {"mode": "open_network", "created_at": f"2026-11-16T08:{number % 60:02d}:00+00:00",
            "config_fingerprint": str(number), "hint_level": 2, "findings": [], "status": "needs_information",
            "llm": {"status": "ok"}, "investigation": {"summary": "网关需要核对。", "reply": answer,
                "experiment_goal": "PC1访问服务器10.30.0.10", "hypotheses": []}, "conversation": []}


class ConversationStoreTests(unittest.TestCase):
    def setUp(self):
        folder = ROOT / "runtime/tests"
        folder.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=folder)
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "sessions.sqlite3")

    def test_full_dialogue_survives_bounded_model_context_without_duplicate_storage(self):
        session = None
        conversation = []
        for number in range(12):
            payload = stages()[0]
            payload["question"] = "PC1为何无法访问服务器？"
            if session:
                payload["session_id"] = session
                payload["interaction"] = {"type": "ask", "message": f"第{number}次追问：下一步怎么检查？"}
            result = saved_result(number, f"第{number}轮回复")
            conversation.append({"intent": "ask", "user": str(number), "assistant": str(number), "created_at": result["created_at"]})
            result["conversation"] = conversation[-8:]
            returned = self.store.save(payload,result)
            session = returned["session_id"]
        self.assertEqual(len(returned["conversation"]), 8)
        self.assertEqual(len(returned["dialogue"]), 12)
        self.assertEqual(returned["dialogue"][0]["user"], "PC1为何无法访问服务器？")
        self.assertEqual(returned["dialogue"][0]["assistant"], "第0轮回复")
        self.assertEqual(returned["dialogue"][-1]["assistant"], "第11轮回复")
        reopened = Store(self.store.path).history(session)
        self.assertEqual(reopened["dialogue"], returned["dialogue"])
        with self.store.connect() as db:
            for row in db.execute("SELECT result FROM attempts"):
                self.assertNotIn("dialogue", json.loads(row[0]))

    def test_old_evidence_keeps_its_original_material_version(self):
        original = stages()[0]
        original["configs"] = {"R1.cfg": "gateway-list 10.20.0.254"}
        result = saved_result()
        result["investigation"]["hypotheses"] = [{"title": "网关疑点", "evidence": [
            {"source_id": "config:1", "device": "R1.cfg", "line": 1, "quote": "gateway-list 10.20.0.254"}]}]
        first = self.store.save(original,result)
        updated = {**original,"configs":{"R1.cfg":"gateway-list 10.20.0.1"},"session_id":first["session_id"],
                   "interaction":{"type":"update","message":"已修改网关"}}
        self.store.save(updated,saved_result(2))
        history = self.store.history(first["session_id"])
        citation = history["dialogue"][0]["evidence"][0]
        self.assertEqual(citation["quote"], history["attempts"][0]["input"]["configs"][citation["device"]])
        self.assertNotEqual(citation["quote"], history["attempts"][-1]["input"]["configs"][citation["device"]])

    def test_list_search_and_cursor_reach_more_than_thirty_sessions(self):
        ids = []
        for number in range(35):
            payload = {**stages()[0], "question": f"路由实验 {number:02d}"}
            ids.append(self.store.save(payload,saved_result(number))["session_id"])
        first = self.store.sessions()
        second = self.store.sessions(before=first[-1]["cursor"])
        self.assertEqual([row["session_id"] for row in first + second], list(reversed(ids)))
        self.assertEqual(self.store.sessions(query="实验 03")[0]["session_id"],ids[3])
        self.assertEqual(self.store.sessions(query="' OR 1=1 --"),[])
        self.assertEqual(self.store.sessions(query="%"),[])
        self.assertEqual(self.store.sessions(query="服务器")[0]["session_id"],ids[-1])

    def test_updated_session_moves_to_top_without_leaking_material_bodies(self):
        payload = {**stages()[0],"question":"DHCP 网关"}
        payload["configs"]["private.cfg"] = "private-material-content"
        first = self.store.save(payload,saved_result())
        self.store.save({**stages()[0],"question":"OSPF路由"},saved_result(2))
        result = saved_result(0)
        result["created_at"] = "2026-11-15T00:00:00+00:00"
        self.store.save({**payload,"session_id":first["session_id"]},result)
        rows = self.store.sessions()
        self.assertEqual(rows[0]["session_id"],first["session_id"])
        self.assertEqual(rows[0]["updated_at"],result["created_at"])
        self.assertEqual(rows[0]["requests"],2)
        self.assertNotIn("private-material-content",json.dumps(rows))
        self.assertEqual(self.store.sessions(query="private.cfg")[0]["session_id"],first["session_id"])

    def test_default_question_uses_goal_title_and_recorded_failure_has_own_turn(self):
        payload = {**stages()[0],"question":DEFAULT_QUESTIONS[0]}
        result = saved_result()
        first = self.store.save(payload,result)
        failed = saved_result(2)
        failed["llm"] = {"status":"error","message":"模型暂时无法连接"}
        self.store.save({**payload,"session_id":first["session_id"],"interaction":{"type":"ask","message":"继续检查"}},failed)
        history = self.store.history(first["session_id"])
        self.assertEqual(history["dialogue"][0]["user"],"已提交材料，开始诊断。")
        self.assertEqual(history["dialogue"][1]["assistant"],"模型暂时无法连接")
        self.assertEqual(self.store.sessions()[0]["title"],"PC1访问服务器10.30.0.10")
        self.assertEqual(self.store.sessions()[0]["preview"],"模型暂时无法连接")


class ConversationApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "runtime/tests")
        self.addCleanup(self.temp.cleanup)
        self.server = create_server(0,Path(self.temp.name)/"sessions.sqlite3")
        thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        thread.start()
        def stop():
            self.server.shutdown(); self.server.server_close(); thread.join(5)
        self.addCleanup(stop)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def request(self,path,payload=None,*,method=None,headers=None):
        data = json.dumps(payload).encode() if payload is not None else None
        with urlopen(Request(self.base+path,data=data,headers={"Content-Type":"application/json", **(headers or {})},method=method),timeout=15) as response:
            return json.load(response)

    def test_delete_removes_all_turns_checkpoints_exports_and_only_target_room_link(self):
        from labagent.rooms import Rooms
        store = Store(Path(self.temp.name)/"sessions.sqlite3")
        with patch.dict(os.environ,ENV), patch("labagent.investigation.chat_completion",side_effect=lambda messages,**kwargs: completion(messages)):
            first = self.request("/api/diagnose",stages()[0])
            session = first["session_id"]
            self.request("/api/diagnose",{**stages()[1],"session_id":session,"interaction":{"type":"ask","message":"继续检查"}})
        other = store.save(stages()[0],saved_result(3))["session_id"]
        rooms = Rooms(store)
        room = rooms.create("共享实验", "成员")
        room = rooms.update(room["id"], {"actor":"成员", "member_id":"member", "action":"materials",
            "patches":[{"name":"R1.cfg", "content":"interface test", "base_revision":0}]})
        rooms.record_diagnosis({"room_id":room["id"],"room_revision":room["revision"],"room_actor":"成员"}, first)
        unrelated = rooms.create("其他实验", "成员")
        rooms.record_diagnosis({"room_id":unrelated["id"],"room_revision":0,"room_actor":"成员"}, {"session_id":other})
        before_room = rooms.get(room["id"])
        self.request("/api/export",{"session_id":session})
        self.request("/api/export",{"session_id":other})
        import sqlite3
        with closing(sqlite3.connect(self.server.workflow_database)) as db:
            self.assertGreater(db.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id=?",("open:"+session,)).fetchone()[0],0)
        result = self.request("/api/sessions/"+session,method="DELETE")
        self.assertEqual(result,{"session_id":session,"deleted":True,"deleted_requests":2})
        self.assertEqual([row["session_id"] for row in self.request("/api/sessions")],[other])
        for suffix in ("", "/report", "/export"):
            with self.subTest(suffix=suffix), self.assertRaises(HTTPError) as error:
                self.request("/api/sessions/"+session+suffix)
            self.assertEqual(error.exception.code,404)
        with closing(sqlite3.connect(self.server.workflow_database)) as db:
            for table in ("checkpoints", "writes"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table} WHERE thread_id IN (?,?)",(session,"open:"+session)).fetchone()[0],0)
        for suffix in (".json", ".md"):
            self.assertFalse((store.path.parent/"exports"/(session+suffix)).exists())
            self.assertTrue((store.path.parent/"exports"/(other+suffix)).exists())
        after_room = rooms.get(room["id"])
        self.assertIsNone(after_room["session"])
        self.assertEqual({k:v for k,v in before_room.items() if k != "session"},{k:v for k,v in after_room.items() if k != "session"})
        self.assertEqual(rooms.get(unrelated["id"])["session"]["session_id"],other)
        prepared = rooms.prepare_diagnosis({"room_id":room["id"],"room_revision":room["revision"],"room_actor":"成员","session_id":session})
        self.assertNotIn("session_id",prepared)
        with self.assertRaises(HTTPError) as error:
            self.request("/api/diagnose",{**stages()[0],"session_id":session})
        self.assertEqual(error.exception.code,400)

    def test_delete_requires_team_access_and_same_origin(self):
        session = Store(Path(self.temp.name)/"sessions.sqlite3").save(stages()[0],saved_result())["session_id"]
        path = "/api/sessions/"+session
        self.server.team_password = "test-team-secret"
        with self.assertRaises(HTTPError) as error:
            self.request(path,method="DELETE")
        self.assertEqual(error.exception.code,401)
        import base64
        authorization = "Basic "+base64.b64encode(b"labagent:test-team-secret").decode()
        with self.assertRaises(HTTPError) as error:
            self.request(path,method="DELETE",headers={"Authorization":authorization,"Origin":"https://unrelated.example"})
        self.assertEqual(error.exception.code,403)
        self.assertTrue(self.request(path,headers={"Authorization":authorization}))
        self.assertTrue(self.request(path,method="DELETE",headers={"Authorization":authorization})["deleted"])

    def test_delete_while_work_in_flight_returns_conflict_without_changes(self):
        store = Store(Path(self.temp.name)/"sessions.sqlite3")
        session = store.save(stages()[0],saved_result())["session_id"]
        with self.server.workspace.gate.activity(), self.assertRaises(HTTPError) as error:
            self.request("/api/sessions/"+session,method="DELETE")
        self.assertEqual(error.exception.code,409)
        self.assertIsNotNone(store.history(session))
        self.assertTrue(self.request("/api/sessions/"+session,method="DELETE")["deleted"])

    def test_missing_or_invalid_delete_does_not_touch_other_sessions(self):
        store = Store(Path(self.temp.name)/"sessions.sqlite3")
        session = store.save(stages()[0],saved_result())["session_id"]
        for target in ("f"*32,"bad-id",session+"/report"):
            with self.subTest(target=target), self.assertRaises(HTTPError) as error:
                self.request("/api/sessions/"+target,method="DELETE")
            self.assertEqual(error.exception.code,404)
        self.assertIsNotNone(store.history(session))

    def test_read_history_does_not_call_model_and_followup_appends_same_session(self):
        with patch.dict(os.environ,ENV), patch("labagent.investigation.chat_completion",side_effect=lambda messages,**kwargs: completion(messages)) as model:
            first = self.request("/api/diagnose",stages()[0])
            calls = model.call_count
            rows = self.request("/api/sessions?"+urlencode({"q":"SW1"}))
            self.assertEqual(rows[0]["session_id"],first["session_id"])
            history = self.request("/api/sessions/"+first["session_id"])
            self.assertEqual(len(history["dialogue"]),1)
            self.assertEqual(model.call_count,calls)
            second = self.request("/api/diagnose",{**stages()[1],"session_id":first["session_id"],
                "interaction":{"type":"ask","message":"接着原来的排查解释"}})
            self.assertEqual(second["session_id"],first["session_id"])
            self.assertEqual(len(second["dialogue"]),2)
            self.assertEqual(second["dialogue"][-1]["user"],"接着原来的排查解释")
            self.assertEqual(len(self.request("/api/sessions/"+first["session_id"])["attempts"]),2)

    def test_invalid_pagination_is_a_readable_400(self):
        for query in ("limit=0","limit=1000","before=0","before=999999999999999999999999","before=bad","q="+"a"*201):
            with self.subTest(query=query), self.assertRaises(HTTPError) as error:
                self.request("/api/sessions?"+query)
            self.assertEqual(error.exception.code,400)


if __name__ == "__main__":
    unittest.main()
