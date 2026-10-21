"""Sarashina generation requires real Decider execution; no external calls."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import decider
import decider_runtime
import engine
import generators
import provider_settings

TEXT = "保存して下さい。"
IDENTITY = {"model": decider_runtime.MODEL, "revision": decider_runtime.REVISION, "device": "cpu", "backend": "local"}
AVAILABLE = {**IDENTITY, "available": True, "status": "ready", "loaded": True}


def native(choice="preserved"):
    return {"ok": True, **IDENTITY, "result": {"answers": {"meaning_shift": {"type": "choice", "choice": choice}}}}


class SarashinaRequirementTests(unittest.TestCase):
    def setUp(self):
        self.selected = self.enterContext(mock.patch.object(provider_settings, "read_settings", return_value={"provider": "sarashina", "model": "sarashina3-mini"}))
        self.key = self.enterContext(mock.patch.object(provider_settings, "api_key", side_effect=AssertionError("credentials must not be read")))
        self.post = self.enterContext(mock.patch.object(generators, "_post_json", side_effect=AssertionError("external API must not be called")))
        self.enterContext(mock.patch.object(engine, "lint", return_value=[]))
        self.edits, _ = engine.validate_edits(TEXT, [{"before": "下さい", "after": "ください", "reason": "表記を整える", "rule": "test"}])
        self.generated = self.enterContext(mock.patch.object(engine, "generate", return_value=(self.edits, [], [], ["sarashina3-mini"], [])))
        self.available = self.enterContext(mock.patch.object(decider, "availability", return_value=dict(AVAILABLE)))
        self.infer = self.enterContext(mock.patch.object(decider_runtime, "decide", return_value=native()))

    def analyze(self, **payload):
        return engine.analyze({"text": TEXT, "generate": True, **payload})

    def test_default_sarashina_checks_service_before_generation_and_judges_each_edit(self):
        events = []
        self.available.side_effect = lambda: events.append("available") or dict(AVAILABLE)
        self.generated.side_effect = lambda *_args, **_kwargs: events.append("generate") or (self.edits, [], [], ["sarashina3-mini"], [])
        self.infer.side_effect = lambda *_args, **_kwargs: events.append("judge") or native()
        result = self.analyze()
        self.assertEqual(result["requirements"], {"needs_decider": True})
        self.assertEqual(result["stages"]["decider"], "executed")
        self.assertEqual(result["edits"][0]["decision"]["status"], "executed")
        self.assertEqual(events, ["available", "generate", "available", "judge"])
        self.assertEqual(self.generated.call_args.kwargs, {"provider": "sarashina", "model": "sarashina3-mini"})
        self.key.assert_not_called()
        self.post.assert_not_called()

    def test_judge_false_is_rejected_before_generation_or_service_probe(self):
        with self.assertRaisesRegex(engine.EditorError, "Deciderの意味判定を有効"):
            self.analyze(judge=False)
        self.generated.assert_not_called()
        self.available.assert_not_called()
        self.infer.assert_not_called()
        self.key.assert_not_called()
        self.post.assert_not_called()

    def test_unavailable_or_unverifiable_decider_stops_before_external_generation(self):
        for state in ({"available": False, "status": status} for status in
                      ("not_configured", "disabled", "service_unavailable", "service_authentication_failed", "stop_failed")):
            with self.subTest(status=state["status"]):
                self.available.return_value = state
                with self.assertRaisesRegex(engine.EditorError, "共有サービスへ接続"):
                    self.analyze()
        for state in (None, {}, {"available": True, "status": "unavailable"}):
            self.available.return_value = state
            with self.assertRaisesRegex(engine.EditorError, "共有サービスへ接続"):
                self.analyze()
        self.available.side_effect = RuntimeError("PRIVATE_KEY /home/alice/private")
        with self.assertRaises(engine.EditorError) as raised:
            self.analyze()
        self.assertNotIn("PRIVATE", str(raised.exception))
        self.assertNotIn("alice", str(raised.exception))
        self.generated.assert_not_called()
        self.infer.assert_not_called()
        self.key.assert_not_called()
        self.post.assert_not_called()

    def test_configured_loading_and_busy_service_states_are_allowed(self):
        for status in ("configured", "loading", "busy", "ready"):
            with self.subTest(status=status):
                self.available.return_value = {**AVAILABLE, "status": status}
                result = self.analyze()
                self.assertTrue(result["requirements"]["needs_decider"])
                self.assertEqual(result["edits"][0]["decision"]["status"], "executed")

    def test_explicit_sarashina_override_cannot_bypass_required_judgment(self):
        self.selected.return_value = {"provider": "gemini", "model": "gemini-3.8-flash-high"}
        with self.assertRaisesRegex(engine.EditorError, "Deciderの意味判定を有効"):
            self.analyze(provider="sarashina", model="sarashina3-nano", judge=False)
        self.generated.assert_not_called()
        result = self.analyze(provider="sarashina", model="sarashina3-nano")
        self.assertTrue(result["requirements"]["needs_decider"])
        self.assertEqual(self.generated.call_args.kwargs, {"provider": "sarashina", "model": "sarashina3-nano"})

    def test_gemini_and_claude_keep_optional_judgment_without_decider_preflight(self):
        for provider, model in (("gemini", "gemini-3.8-flash-high"), ("claude", "claude-opus-5-5")):
            with self.subTest(provider=provider):
                result = self.analyze(provider=provider, model=model, judge=False)
                self.assertNotIn("requirements", result)
                self.assertEqual(result["stages"]["decider"], "not_run")
        self.available.assert_not_called()
        self.infer.assert_not_called()

    def test_rules_only_and_host_supplied_edits_do_not_require_sarashina_or_decider_settings(self):
        result = self.analyze(generate=False, provider="sarashina", judge=False)
        self.assertNotIn("requirements", result)
        self.assertEqual(result["stages"]["generation"], "not_requested")
        reviewed = engine.review_proposals({"text": TEXT, "generate": True, "provider": "sarashina", "judge": False,
                                           "edits": [{"before": "下さい", "after": "ください"}]})
        self.assertNotIn("requirements", reviewed)
        self.assertEqual(reviewed["stages"]["generation"], "host_supplied")
        self.selected.assert_not_called()
        self.available.assert_not_called()
        self.generated.assert_not_called()
        self.infer.assert_not_called()

    def test_service_failure_after_generation_preserves_edits_and_required_flag(self):
        self.available.side_effect = [dict(AVAILABLE), {**AVAILABLE, "available": False, "status": "service_unavailable"}]
        result = self.analyze()
        self.assertTrue(result["requirements"]["needs_decider"])
        self.assertEqual(result["stages"]["decider"], "unavailable")
        self.assertEqual(result["edits"][0]["before"], "下さい")
        self.assertEqual(result["edits"][0]["after"], "ください")
        self.assertEqual(result["edits"][0]["decision"]["status"], "not_run")
        self.assertTrue(any("コピー・保存できません" in note for note in result["notes"]))
        self.infer.assert_not_called()

    def test_runtime_failure_and_partial_judgment_are_not_mislabeled_as_complete(self):
        text = TEXT + "確認して頂く。"
        edits, _ = engine.validate_edits(text, [{"before": "下さい", "after": "ください"}, {"before": "頂く", "after": "いただく"}])
        self.generated.return_value = (edits, [], [], ["sarashina3-mini"], [])
        self.infer.side_effect = [native(), decider_runtime.DeciderRuntimeError("timeout", "private")]
        result = engine.analyze({"text": text, "generate": True})
        self.assertTrue(result["requirements"]["needs_decider"])
        self.assertEqual(result["stages"]["decider"], "partial")
        self.assertEqual([edit["decision"]["status"] for edit in result["edits"]], ["executed", "error"])
        self.assertTrue(any("コピー・保存できません" in note for note in result["notes"]))

    def test_all_executed_choices_remain_results_including_meaning_shift_warnings(self):
        for choice in ("preserved", "shifted", "needs_review"):
            with self.subTest(choice=choice):
                self.infer.return_value = native(choice)
                result = self.analyze()
                self.assertEqual(result["stages"]["decider"], "executed")
                self.assertEqual(result["edits"][0]["decision"]["choice"], choice)
                self.assertFalse(any("コピー・保存できません" in note for note in result["notes"]))

    def test_zero_generated_edits_need_no_inference_but_keep_workflow_requirement(self):
        self.generated.return_value = ([], [], [], ["sarashina3-mini"], [])
        result = self.analyze()
        self.assertTrue(result["requirements"]["needs_decider"])
        self.assertEqual(result["edits"], [])
        self.assertEqual(result["stages"]["decider"], "not_run")
        self.available.assert_called_once_with()
        self.infer.assert_not_called()
        self.assertFalse(any("コピー・保存できません" in note for note in result["notes"]))

    def test_chunk_limit_keeps_one_strict_schema_request_and_completion_guard(self):
        self.key.side_effect = None
        self.key.return_value = "fake-key"
        self.post.side_effect = None
        self.post.return_value = {"model": "sarashina3-mini", "choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"edits": [], "questions": [], "suggestions": []})}}]}
        result = generators.generate({"source": "あ" * 1200, "provider": "sarashina"}, "校正方針", engine.GENERATOR_SCHEMA)
        self.assertEqual(result["proposal"]["edits"], [])
        self.post.assert_called_once()
        body = self.post.call_args.args[1]
        self.assertEqual((body["max_completion_tokens"], body["n"], body["stream"]), (4000, 1, False))
        self.assertEqual(body["response_format"]["json_schema"], {"name": "japanese_editor", "schema": engine.GENERATOR_SCHEMA, "strict": True})
        self.assertEqual(len(json.loads(body["messages"][1]["content"].split("\n\n", 1)[1])["source"]), 1200)
        self.post.return_value["choices"][0]["finish_reason"] = "length"
        with self.assertRaises(generators.GenerationError) as raised:
            generators.generate({"source": "あ" * 1200, "provider": "sarashina"}, "校正方針", engine.GENERATOR_SCHEMA)
        self.assertEqual(raised.exception.code, "generation_length")
        self.post.reset_mock()
        self.key.reset_mock()
        with self.assertRaises(generators.GenerationError) as raised:
            generators.generate({"source": "あ" * 1201, "provider": "sarashina"}, "校正方針", engine.GENERATOR_SCHEMA)
        self.assertEqual(raised.exception.code, "input_too_long")
        self.key.assert_not_called()
        self.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
