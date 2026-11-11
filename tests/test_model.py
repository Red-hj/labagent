import io
import json
import os
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from labagent.model import chat_completion, ModelError, NoRedirect, public_config

ENV = {"LABAGENT_LLM_URL": "https://api.deepseek.com/chat/completions", "LABAGENT_LLM_MODEL": "deepseek-flash", "LABAGENT_LLM_KEY": "private-test-value"}


def response(**changes):
    return {"choices": [{"message": {"role": "assistant", "content": "测试回复", "reasoning_content": "private-reasoning"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}, **changes}


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.transport = patch("labagent.model.build_opener")
        self.opener = self.transport.start()
        self.addCleanup(self.transport.stop)

    def reply(self, body):
        self.opener.return_value.open.return_value = io.BytesIO(json.dumps(body).encode())

    def test_deepseek_request_and_metadata_without_reasoning_or_key(self):
        self.reply(response())
        result = chat_completion([{"role": "user", "content": "测试"}])
        request = self.opener.return_value.open.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["max_tokens"], 800)
        self.assertEqual(result["_meta"]["usage"]["total_tokens"], 20)
        self.assertNotIn("private-reasoning", json.dumps(result))
        self.assertNotIn(ENV["LABAGENT_LLM_KEY"], json.dumps(result))

    def test_compatible_provider_does_not_receive_deepseek_parameters(self):
        os.environ["LABAGENT_LLM_URL"] = "https://model.invalid/chat"
        self.reply(response())
        chat_completion([{"role": "user", "content": "test"}])
        self.assertNotIn("thinking", json.loads(self.opener.return_value.open.call_args.args[0].data))

    def test_provider_errors_have_safe_actionable_codes(self):
        for status, expected in ((401, "authentication_failed"), (402, "insufficient_balance"), (429, "rate_limited"), (500, "provider_unavailable")):
            self.opener.return_value.open.side_effect = HTTPError(ENV["LABAGENT_LLM_URL"], status, ENV["LABAGENT_LLM_KEY"], {}, io.BytesIO(b'private-body'))
            with self.subTest(status=status), self.assertRaises(ModelError) as caught:
                chat_completion([])
            self.assertEqual(caught.exception.code, expected)
            self.assertNotIn(ENV["LABAGENT_LLM_KEY"], json.dumps(caught.exception.as_dict()))

    def test_timeout_is_safe_and_has_no_automatic_retry(self):
        self.opener.return_value.open.side_effect = TimeoutError(ENV["LABAGENT_LLM_KEY"])
        with self.assertRaises(ModelError) as caught:
            chat_completion([])
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(self.opener.return_value.open.call_count, 1)
        self.assertNotIn(ENV["LABAGENT_LLM_KEY"], str(caught.exception))

    def test_truncation_or_missing_choices_is_not_success(self):
        for body in (response(choices=[]), response(choices=[{"message": {"role": "assistant", "content": "partial"}, "finish_reason": "length"}])):
            self.reply(body)
            with self.subTest(body=body), self.assertRaises(ModelError):
                chat_completion([])

    def test_configuration_rejected_before_network(self):
        for key, value in (("LABAGENT_LLM_TIMEOUT", "999"), ("LABAGENT_LLM_MAX_TOKENS", "invalid"), ("LABAGENT_LLM_THINKING", "enabled"), ("LABAGENT_LLM_URL", "http://external.invalid/chat")):
            with patch.dict(os.environ, {key: value}), self.subTest(key=key), self.assertRaises(ModelError):
                chat_completion([])
        self.opener.return_value.open.assert_not_called()

    def test_redirect_does_not_forward_credentials(self):
        with self.assertRaises(ModelError) as caught:
            NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://other.invalid")
        self.assertEqual(caught.exception.code, "redirect_rejected")

    def test_health_config_does_not_expose_key_or_endpoint(self):
        config = public_config()
        self.assertEqual(config["model"], "deepseek-flash")
        self.assertNotIn(ENV["LABAGENT_LLM_KEY"], json.dumps(config))
        self.assertNotIn(ENV["LABAGENT_LLM_URL"], json.dumps(config))

    def test_open_output_budget_remains_bounded_and_truncation_usage_is_kept(self):
        self.reply(response(choices=[{"message": {"role": "assistant", "content": "partial"}, "finish_reason": "length"}]))
        with self.assertRaises(ModelError) as caught:
            chat_completion([], max_tokens=1600)
        self.assertEqual(json.loads(self.opener.return_value.open.call_args.args[0].data)["max_tokens"], 1600)
        self.assertEqual(caught.exception.call["usage"]["total_tokens"], 20)
        with self.assertRaises(ModelError):
            chat_completion([], max_tokens=999999)


if __name__ == "__main__":
    unittest.main()
