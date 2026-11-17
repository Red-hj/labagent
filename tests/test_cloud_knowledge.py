from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from labagent import knowledge
from labagent.cloud_workflow import CloudWorkflow
from labagent.material_service import MaterialService
from labagent.store import Store
from test_course_knowledge import COURSE
from test_investigation import ENV, ROOT, completion, stages


class CloudKnowledgeTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "runtime/tests").mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=ROOT / "runtime/tests")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        for change in (patch.dict(os.environ, {**ENV, "LABAGENT_DATA_DIR": ""}),
                       patch("labagent.knowledge.NOTES_DIR", self.folder / "web-notes")):
            change.start()
            self.addCleanup(change.stop)
        self.store = Store(self.folder / "web.sqlite3")
        self.now = 1000
        self.requests, self.services = [], {}
        self.observations = []
        self.workflow = CloudWorkflow(self.store, self.client, clock=lambda: self.now)

    def model(self, messages):
        if messages[-1]["role"] != "tool":
            return {"tool_calls": [{"id": "reference", "function": {
                "name": "retrieve_reference", "arguments": json.dumps({"query": "蓝杉网关归属"})}}]}
        observation = json.loads(messages[-1]["content"])
        if "results" in observation:
            self.observations.append(observation)
            return completion([{"role": "user"}])
        return completion(messages)

    def client(self, platform_id):
        owner = self

        class Client:
            def invoke(self, request):
                owner.requests.append(deepcopy(request))
                service = owner.services.setdefault(platform_id,
                    MaterialService(owner.folder / (platform_id + ".sqlite3"), owner.model))
                # The runtime has no access to the web server's files.
                with patch("labagent.knowledge.NOTES_DIR", owner.folder / "empty-cloud-notes"):
                    return service.invoke(request)
        return Client()

    def run_round(self, **updates):
        payload = {**stages()[0], "knowledge_scope": COURSE["scope"], **updates}
        return self.store.save(payload, self.workflow.run(payload))

    def test_isolated_runtime_receives_current_scope_after_followup_and_idle_recovery(self):
        old = knowledge.import_document(COURSE)["document"]
        knowledge.import_document({**COURSE, "scope": "unrelated", "content": "不应传送的其他课程资料"})
        first = self.run_round()
        self.assertEqual(first["llm"]["status"], "ok")
        self.assertEqual([document["id"] for document in self.requests[0]["course_documents"]], [old["id"]])
        original = knowledge.get_document(old["id"], COURSE["scope"])
        cited = next(document for document in first["knowledge"] if document["id"] == old["id"])
        self.assertEqual(original["content"][cited["char_start"]:cited["char_end"]], cited["content"])
        new = knowledge.import_document({**COURSE, "version": "v2",
            "content": "蓝杉网关归属修订：先采集VRRP状态和路由输出。"})["document"]
        second = self.run_round(session_id=first["session_id"],
            interaction={"type": "ask", "message": "根据新资料继续检查。"})
        self.assertEqual(second["agentarts"]["platform_session_id"], first["agentarts"]["platform_session_id"])
        self.now += 301
        third = self.run_round(session_id=first["session_id"],
            interaction={"type": "ask", "message": "继续核对网关归属。"})
        self.assertTrue(third["agentarts"]["session_recovered"])
        self.assertEqual({document["id"] for document in third["knowledge"]}, {old["id"], new["id"]})
        self.assertEqual(len(self.store.history(first["session_id"])["attempts"]), 3)
        self.assertFalse((self.folder / "empty-cloud-notes").exists())
        self.assertEqual(self.store.history(first["session_id"])["attempts"][0]["result"]["knowledge"], first["knowledge"])

    def test_legacy_runtime_cannot_silently_ignore_imported_references(self):
        knowledge.import_document(COURSE)
        original_factory = self.workflow.client_factory

        def legacy(platform_id):
            client = original_factory(platform_id)
            original_invoke = client.invoke
            def invoke(request):
                response = original_invoke(request)
                response.pop("course_document_ids")
                return response
            client.invoke = invoke
            return client
        self.workflow.client_factory = legacy
        with self.assertRaisesRegex(ValueError, "接收本轮课程资料"):
            self.run_round()
        self.assertEqual(self.store.sessions(), [])

    def test_transferred_snapshot_overrides_stale_remote_notes_without_shared_writes(self):
        knowledge.import_document({**COURSE, "content": "蓝杉网关归属旧的云端副本"})
        current = knowledge.make_document(COURSE)
        result = knowledge.search("蓝杉网关归属", scope=COURSE["scope"], course_documents=[current])
        imported = [document for document in result["results"] if document["id"].startswith("course-")]
        self.assertEqual({document["id"] for document in imported}, {current["id"]})
        empty = knowledge.search("蓝杉网关归属", scope=COURSE["scope"], course_documents=[])
        self.assertFalse(any(document["id"].startswith("course-") for document in empty["results"]))

    def test_invalid_reference_scope_and_limits_fail_before_diagnosis(self):
        service = MaterialService(self.folder / "invalid.sqlite3", self.model)
        for documents in (None, {}, [COURSE, COURSE], [dict(COURSE, scope="other")],
                          [COURSE] * 101, [dict(COURSE, content="x" * 150001)]):
            with self.subTest(documents_type=type(documents).__name__), self.assertRaises(ValueError):
                service.invoke({"materials": stages()[0]["configs"],
                    "knowledge_scope": COURSE["scope"], "course_documents": documents})
        self.assertEqual(service.store.sessions(), [])
        huge = [dict(COURSE, title=str(index), content="x" * 150000) for index in range(7)]
        with self.assertRaisesRegex(ValueError, "100万"):
            knowledge.validate_course_documents(huge, COURSE["scope"])


if __name__ == "__main__":
    unittest.main()
