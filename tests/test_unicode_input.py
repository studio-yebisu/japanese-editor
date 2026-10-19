"""Malformed JSON strings fail before I/O; valid Unicode remains byte-exact."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine


SOURCE = "🙂保存して下さい。\r\n上限は3件です。"
EDIT = {"before": "して下さい", "after": "してください", "reason": "表記を整える", "rule": "表記"}
PRIVATE = "private-input-never-echoed"
BAD_STRINGS = (PRIVATE + "\ud800", "\udfff" + PRIVATE)


class UnicodeInputTests(unittest.TestCase):
    def assert_input_error(self, call):
        with self.assertRaises(engine.EditorError) as caught:
            call()
        message = str(caught.exception)
        self.assertIn("不正なUnicode文字", message)
        self.assertIn("入力し直してください", message)
        self.assertNotIn(PRIVATE, message)
        self.assertNotIn("接続", message)
        self.assertNotIn("もう一度実行", message)
        message.encode("utf-8")

    def test_analyze_rejects_all_input_strings_before_any_processing(self):
        for field in ("text", "context", "profile", "strength", "model", "provider"):
            for bad in BAD_STRINGS:
                with self.subTest(field=field, bad=ascii(bad)), \
                     patch.object(engine, "lint") as lint, \
                     patch.object(engine, "generate") as generate, \
                     patch.object(engine.progress, "report") as report:
                    payload = {"text": SOURCE, "context": "作風を保つ", "generate": True, field: bad}
                    self.assert_input_error(lambda: engine.analyze(payload))
                    lint.assert_not_called()
                    generate.assert_not_called()
                    report.assert_not_called()

    def test_host_edits_are_rejected_before_rules_or_decider(self):
        for field in ("before", "after", "reason", "rule"):
            for bad in BAD_STRINGS:
                with self.subTest(field=field, bad=ascii(bad)), \
                     patch.object(engine, "lint") as lint, \
                     patch("decider.attach") as attach:
                    edits = [{**EDIT, field: bad}]
                    self.assert_input_error(lambda: engine.review_proposals({"text": SOURCE, "edits": edits}))
                    lint.assert_not_called()
                    attach.assert_not_called()

    def test_no_change_or_protected_edits_do_not_hide_invalid_metadata(self):
        examples = [
            ("原文", {"before": "原文", "after": "原文", "reason": BAD_STRINGS[0]}),
            ("`code`", {"before": "code", "after": "other", "rule": BAD_STRINGS[1]}),
        ]
        for text, edit in examples:
            with self.subTest(text=text):
                self.assert_input_error(lambda: engine.validate_edits(text, [edit]))

    def test_finalize_rejects_invalid_patch_before_checks_or_file_creation(self):
        with patch.object(engine, "lint") as lint, \
             patch.object(engine, "check_final") as check:
            self.assert_input_error(lambda: engine.finalize_proposals({
                "text": SOURCE, "edits": [{**EDIT, "after": BAD_STRINGS[0]}]}))
            lint.assert_not_called()
            check.assert_not_called()

    def test_final_check_rejects_source_final_and_profile_before_io(self):
        for field in ("source", "final", "profile_id"):
            for bad in BAD_STRINGS:
                with self.subTest(field=field, bad=ascii(bad)), \
                     patch.object(engine.tempfile, "TemporaryDirectory") as temporary, \
                     patch.object(engine.subprocess, "run") as run, \
                     patch.object(engine.progress, "report") as report:
                    arguments = {"source": SOURCE, "final": SOURCE, "profile_id": "article", field: bad}
                    self.assert_input_error(lambda: engine.check_final(**arguments))
                    temporary.assert_not_called()
                    run.assert_not_called()
                    report.assert_not_called()

    def test_direct_encoding_and_edit_helpers_return_input_errors(self):
        for bad in BAD_STRINGS:
            calls = [
                lambda: engine.digest(bad),
                lambda: engine._utf16(bad),
                lambda: engine.validate_edits(bad, []),
                lambda: engine.apply_edits(bad, []),
                lambda: engine.apply_edits(SOURCE, [{"start": 4, "end": 9, "before": "して下さい", "after": bad}]),
                lambda: engine.lint(bad, {"doc_type": None}),
            ]
            for index, call in enumerate(calls):
                with self.subTest(bad=ascii(bad), helper=index), \
                     patch.object(engine.subprocess, "run") as run:
                    self.assert_input_error(call)
                    run.assert_not_called()

    def test_generator_rejects_input_before_settings_or_external_call(self):
        for field in ("text", "context", "provider", "model"):
            with self.subTest(field=field), \
                 patch("provider_settings.read_settings") as settings, \
                 patch("generators.generate") as generate:
                arguments = {"text": SOURCE, "context": "作風を保つ", "provider": "gemini",
                             "model": "gemini-3.8-flash-high", "profile": {}, "strength": {}, field: BAD_STRINGS[0]}
                self.assert_input_error(lambda: engine.generate(**arguments))
                settings.assert_not_called()
                generate.assert_not_called()

    def test_generated_questions_suggestions_and_identity_cannot_break_serialization(self):
        baseline = {"proposal": {"edits": [], "questions": [], "suggestions": []},
                    "identity": {"reported_model": "gemini-3.8-flash-high"}}
        for field in ("question", "suggestion", "reported_model"):
            generated = copy.deepcopy(baseline)
            if field == "reported_model":
                generated["identity"][field] = BAD_STRINGS[0]
            else:
                collection = "questions" if field == "question" else "suggestions"
                generated["proposal"][collection] = [{"quote": SOURCE, field: BAD_STRINGS[0]}]
            with self.subTest(field=field), \
                 patch("provider_settings.read_settings", return_value={"provider": "gemini", "model": "gemini-3.8-flash-high"}), \
                 patch.object(engine, "_generation_bundle", return_value=("校正方針", {})), \
                 patch("generators.generate", return_value=generated), \
                 patch.object(engine, "validate_edits") as validate:
                self.assert_input_error(lambda: engine.generate(SOURCE, {}, {"id": "structure"}, ""))
                validate.assert_not_called()

    def test_recursive_validation_checks_keys_without_echoing_them(self):
        values = [{BAD_STRINGS[0]: "値"}, {"unused": [{"nested": BAD_STRINGS[1]}]}]
        for value in values:
            with self.subTest(value=ascii(value)):
                self.assert_input_error(lambda: engine.validate_unicode_input(value))

    def test_decider_preparation_rejects_invalid_text_and_edits_without_inference(self):
        import decider
        cases = [
            {"text": BAD_STRINGS[0], "edits": []},
            {"text": SOURCE, "edits": [{**EDIT, "after": BAD_STRINGS[1]}]},
        ]
        for payload in cases:
            with self.subTest(payload=ascii(payload)), \
                 patch.object(decider.decider_runtime, "decide") as decide:
                self.assert_input_error(lambda: decider.prepare(payload))
                decide.assert_not_called()

    def test_escaped_emoji_crlf_and_utf16_anchors_remain_exact(self):
        payload = json.loads(json.dumps({"text": SOURCE, "edits": [EDIT], "judge": False}))
        reviewed = engine.review_proposals(payload)
        self.assertEqual(reviewed["source_hash"], hashlib.sha256(SOURCE.encode("utf-8")).hexdigest())
        self.assertEqual((reviewed["edits"][0]["start"], reviewed["edits"][0]["end"]), (4, 9))
        final = engine.finalize_proposals(payload)
        expected = SOURCE.replace("して下さい", "してください")
        self.assertEqual(final["text"], expected)
        self.assertEqual(final["final_hash"], hashlib.sha256(expected.encode("utf-8")).hexdigest())
        self.assertEqual(final["verification"]["status"], "executed")
        self.assertIn("\r\n", final["text"])
        self.assertEqual(engine.apply_edits(SOURCE, []), SOURCE)
        json.dumps(final, ensure_ascii=False).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
