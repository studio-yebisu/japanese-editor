import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine import EditorError, analyze, apply_edits, check_final, digest, finalize_proposals, validate_edits
from decider import prepare


def patch(before, after):
    return {"before": before, "after": after, "reason": "test", "rule": "test"}


class PreservationTests(unittest.TestCase):
    def test_utf16_multi_edit_and_revert(self):
        text = "🙂保存して下さい。確認して頂く。"
        edits, _ = validate_edits(text, [patch("して下さい", "してください"), patch("して頂く", "していただく")])
        self.assertEqual(apply_edits(text, edits), "🙂保存してください。確認していただく。")
        self.assertEqual(apply_edits(text, []), text)
        self.assertEqual(edits[0]["start"], 4)

    def test_ambiguous_or_overlapping_anchors_rejected(self):
        with self.assertRaises(EditorError):
            validate_edits("保存。保存。", [patch("保存", "記録")])
        with self.assertRaises(EditorError):
            validate_edits("aaaaa", [patch("aaa", "b")])
        with self.assertRaises(EditorError):
            validate_edits("保存して下さい。", [patch("保存して下さい", "保存してください"), patch("下さい", "ください")])

    def test_stale_patch_rejected(self):
        edits, _ = validate_edits("保存して下さい。", [patch("下さい", "ください")])
        with self.assertRaises(EditorError):
            apply_edits("保存してくれ。", edits)

    def test_valid_markdown_code_and_urls_are_protected(self):
        examples = [
            ("  ```python\nsecret = 3\n  ```\n", "secret = 3"),
            ("``code ` nested``", "nested"),
            ("```\nunclosed = 3\n", "unclosed = 3"),
            ("    indented = 3\n", "indented = 3"),
            ("> ```\n> quoted = 3\n> ```\n", "quoted = 3"),
            ("[参照](https://example.com/a)", "example.com"),
        ]
        for text, before in examples:
            with self.subTest(text=text):
                edits, notes = validate_edits(text, [patch(before, "CHANGED")])
                self.assertEqual(edits, [])
                self.assertTrue(notes)

    def test_risk_flags_are_candidates_not_pass_scores(self):
        edits, _ = validate_edits("3件のみ保存しません。", [patch("3件のみ保存しません", "4件保存します")])
        self.assertTrue(edits[0]["review_required"])
        self.assertIn("数値の変化", edits[0]["risk_flags"])
        self.assertNotIn("confidence", edits[0])

    def test_rules_only_uses_actual_upstream_without_generation(self):
        result = analyze({"text": "いかがでしたでしょうか。", "profile": "article"})
        self.assertTrue(result["findings"])
        self.assertEqual(result["edits"], [])
        self.assertEqual(result["stages"]["generation"], "not_requested")
        self.assertEqual(result["stages"]["decider"], "not_run")

    def test_export_checks_exact_bytes_for_lf_and_crlf(self):
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=repr(newline)):
                text = f"保存して下さい。{newline}上限は3件。"
                final = text.replace("下さい", "ください")
                result = check_final(text, final)
                self.assertEqual(result["final_hash"], digest(final))
                self.assertEqual(result["verification"]["final"]["sha256"], digest(final))
                self.assertEqual(result["verification"]["status"], "executed")

    def test_decider_preparation_does_not_claim_inference(self):
        result = prepare({"text": "保存して下さい。", "edits": [patch("下さい", "ください")]})
        self.assertEqual(result["status"], "prepared_not_executed")
        self.assertEqual(len(result["requests"]), 1)
        self.assertEqual(len(result["requests"][0]["questions"]), 1)
        self.assertNotIn("answers", result)

    def test_host_final_is_assembled_from_only_the_accepted_edits(self):
        result = finalize_proposals({"text": "保存して下さい。上限は3件です。",
                                    "edits": [patch("して下さい", "してください")]})
        self.assertEqual(result["text"], "保存してください。上限は3件です。")
        self.assertEqual(result["verification"]["final"]["sha256"], result["final_hash"])

    def test_stdio_mcp_actual_child_process(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "japanese_editor_review_proposals", "arguments": {
                    "text": "保存して下さい。", "edits": [patch("下さい", "ください")], "judge": False}}},
        ]
        proc = subprocess.run([sys.executable, "-B", str(ROOT / "app.py"), "mcp"],
                              input="\n".join(json.dumps(x, ensure_ascii=False) for x in requests)+"\n",
                              env={**os.environ, "YEBISU_EDITOR_NO_AUTOSTART": "1"},
                              encoding="utf-8", capture_output=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")
        responses = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual([r["id"] for r in responses], [1, 2, 3])
        self.assertEqual(len(responses[1]["result"]["tools"]), 6)
        result = json.loads(responses[2]["result"]["content"][0]["text"])
        self.assertEqual(result["stages"]["generation"], "host_supplied")
        self.assertEqual(result["edits"][0]["before"], "下さい")


if __name__ == "__main__":
    unittest.main()
