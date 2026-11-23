"""Public feedback must distinguish failed model guidance from response contracts."""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from labagent.cloud_workflow import CloudWorkflow


class CloudGuidanceFeedbackTests(unittest.TestCase):
    def invoke(self, status, *, guided=False, replaced=True):
        result = {"created_at": "2026-11-21T00:00:00+00:00", "config_fingerprint": "test",
                  "hint_level": 1, "findings": [], "status": "needs_information",
                  "llm": {"status": status, "message": "private-provider-response"},
                  "investigation": {"learning": {"current_step": "inspect"}} if guided else {}}
        self.client = Mock()
        self.client.invoke.return_value = {"status": status, "session_id": "a" * 32, "result": result, "materials_replaced": replaced}
        self.store = Mock()
        self.store.history.return_value = None
        workflow = CloudWorkflow(self.store, client_factory=lambda platform: self.client)
        with patch("labagent.cloud_workflow.export_course_documents", return_value=[]):
            return workflow.run({"configs": {}, "question": "inspect interfaces", "learning": {"level": 1}})

    def test_failed_model_results_do_not_claim_the_runtime_is_outdated(self):
        for status in ("unavailable", "step_limit", "no_evidence", "not_configured"):
            with self.subTest(status=status):
                with self.assertRaises(ValueError) as caught:
                    self.invoke(status)
                message = str(caught.exception)
                self.assertNotIn("版本", message)
                self.assertNotIn("private-provider-response", message)
                self.assertIn("保留", message)
                self.client.invoke.assert_called_once()
                self.store.save.assert_not_called()

    def test_successful_plain_result_reports_a_missing_guidance_contract(self):
        with self.assertRaisesRegex(ValueError, "未返回约定"):
            self.invoke("ok")

    def test_valid_guidance_is_returned_unchanged(self):
        result = self.invoke("ok", guided=True)
        self.assertEqual(result["investigation"]["learning"]["current_step"], "inspect")
        self.assertIn("agentarts", result)

    def test_old_runtime_cannot_silently_keep_removed_materials(self):
        with self.assertRaisesRegex(ValueError, "当前材料列表"):
            self.invoke("ok", guided=True, replaced=False)
        self.client.invoke.assert_called_once()
        self.assertEqual(self.client.invoke.call_args.args[0]["material_mode"], "replace")
        self.store.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
