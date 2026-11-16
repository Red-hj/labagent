import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from labagent import knowledge
from labagent.server import create_server
from test_investigation import ENV, ROOT, completion, stages


COURSE = {"title":"课程网络实验笔记", "scope":"course-rag", "version":"v1", "source_name":"自编测试资料",
          "content":"# 课程笔记 📘\r\n\r\n## 蓝杉网关归属\r\n蓝杉课程需核对网关归属与 VRRP。\r\n```text\r\n## 这不是章节\r\n```\r\n\r\n## 青柠租约更新\r\n青柠课程需核对 DHCP 租约更新及续租结果。\r\n"}


class CourseKnowledgeFixture(unittest.TestCase):
    def setUp(self):
        (ROOT / "runtime/tests").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "runtime/tests")
        self.addCleanup(self.temp.cleanup)
        self.notes = patch("labagent.knowledge.NOTES_DIR", Path(self.temp.name)/"notes")
        self.notes.start(); self.addCleanup(self.notes.stop)
        self.environment = patch.dict(os.environ,{"LABAGENT_DATA_DIR":""})
        self.environment.start(); self.addCleanup(self.environment.stop)


class CourseKnowledgeTests(CourseKnowledgeFixture):
    def test_markdown_sections_keep_fenced_headers_and_exact_original_offsets(self):
        created = knowledge.import_document(COURSE)["document"]
        original = knowledge.get_document(created["id"],COURSE["scope"])
        full = next(doc for doc in knowledge.corpus(COURSE["scope"])[0] if doc["id"] == created["id"])
        parts = list(knowledge.chunks(full))
        self.assertEqual(len(parts),2)
        self.assertIn("## 这不是章节",parts[0]["content"])
        for part in parts:
            self.assertEqual(original["content"][part["char_start"]:part["char_end"]],part["content"])
            self.assertEqual(original["content"].splitlines()[part["line_start"]-1:part["line_end"]],part["content"].splitlines())
        self.assertNotIn("\r",original["content"])

    def test_different_course_questions_retrieve_different_sections(self):
        knowledge.import_document(COURSE)
        gateway = knowledge.search("蓝杉网关归属",scope=COURSE["scope"])["results"][0]
        lease = knowledge.search("青柠租约更新",scope=COURSE["scope"])["results"][0]
        self.assertNotEqual(gateway["chunk_id"],lease["chunk_id"])
        self.assertIn("VRRP",gateway["content"])
        self.assertIn("DHCP",lease["content"])

    def test_source_lookup_respects_scope_and_keeps_old_version(self):
        old = knowledge.import_document(COURSE)["document"]
        knowledge.import_document({**COURSE,"version":"v2","content":"修订后的资料"})
        self.assertIsNone(knowledge.get_document(old["id"],"another-course"))
        self.assertIn("蓝杉网关",knowledge.get_document(old["id"],COURSE["scope"])["content"])
        self.assertIsNone(knowledge.get_document("missing",COURSE["scope"]))


class CourseKnowledgeApiTests(CourseKnowledgeFixture):
    def setUp(self):
        super().setUp()
        self.server = create_server(0,Path(self.temp.name)/"sessions.sqlite3")
        thread = threading.Thread(target=self.server.serve_forever,daemon=True); thread.start()
        def stop():
            self.server.shutdown(); self.server.server_close(); thread.join(5)
        self.addCleanup(stop)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def request(self,path,payload=None):
        with urlopen(Request(self.base+path,data=json.dumps(payload).encode() if payload is not None else None,
                             headers={"Content-Type":"application/json"}),timeout=15) as response:
            return json.load(response)

    def test_import_search_original_and_agent_retrieval_share_same_scope(self):
        imported = self.request("/api/knowledge/import",COURSE)["document"]
        found = self.request("/api/knowledge/search",{"scope":COURSE["scope"],"query":"蓝杉网关归属"})["results"][0]
        original = self.request("/api/knowledge/documents/"+found["id"]+"?"+urlencode({"scope":COURSE["scope"]}))
        self.assertEqual(original["content"][found["char_start"]:found["char_end"]],found["content"])
        def model(messages,**kwargs):
            if messages[-1]["role"] != "tool":
                return {"tool_calls":[{"id":"retrieve","function":{"name":"retrieve_reference","arguments":json.dumps({"query":"蓝杉网关归属"})}}]}
            observation = json.loads(messages[-1]["content"])
            if "results" in observation:
                self.assertIn(imported["id"],{doc["id"] for doc in observation["results"]})
                return {"tool_calls":[{"id":"read","function":{"name":"read_source","arguments":json.dumps({"source_id":"config:1","start_line":1,"end_line":10})}}]}
            return completion(messages)
        with patch.dict(os.environ,ENV), patch("labagent.investigation.chat_completion",side_effect=model):
            result = self.request("/api/diagnose",{**stages()[0],"knowledge_scope":COURSE["scope"]})
        self.assertEqual(result["llm"]["status"],"ok")
        self.assertIn(imported["id"],{doc["id"] for doc in result["knowledge"]})
        self.assertEqual(result["knowledge_scope"],COURSE["scope"])
        history = self.request("/api/sessions/"+result["session_id"])
        self.assertEqual(history["attempts"][0]["input"]["knowledge_scope"],COURSE["scope"])

    def test_original_requires_team_access_and_does_not_cross_scope(self):
        created = knowledge.import_document(COURSE)["document"]
        path = "/api/knowledge/documents/"+created["id"]
        with self.assertRaises(HTTPError) as error:
            self.request(path+"?scope=another-course")
        self.assertEqual(error.exception.code,404)
        self.server.team_password = "test-password"
        with self.assertRaises(HTTPError) as error:
            self.request(path+"?scope=course-rag")
        self.assertEqual(error.exception.code,401)


if __name__ == "__main__":
    unittest.main()
