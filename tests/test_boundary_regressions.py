"""Offline regressions for protected text, export limits and advisory flags."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine
import decider


def proposal(before, after):
    return {"before": before, "after": after, "reason": "regression", "rule": "regression"}


class BoundaryRegressions(unittest.TestCase):
    def test_balanced_urls_and_case_insensitive_schemes_are_held(self):
        for text, before in [
            ("https://example.org/a(b)TAIL", "TAIL"),
            ("https://example.org/a[1]TAIL", "TAIL"),
            ("HTTPS://EXAMPLE.ORG/PATH", "PATH"),
            ("詳細はhttps://example.org/PATH", "PATH"),
            ("https://[::1]/a(b(c))TAIL", "TAIL"),
            ("[link](https://example.org/a(b)TAIL)", "TAIL"),
            ("[link](relative/a(b)TAIL)", "TAIL"),
            (r"[link](relative/a\)TAIL)", "TAIL"),
        ]:
            with self.subTest(text=text):
                edits, notes = engine.validate_edits(text, [proposal(before, "changed")])
                self.assertEqual(edits, [])
                self.assertTrue(notes)

    def test_text_after_url_wrapper_remains_editable_and_iri_is_preserved(self):
        for text in ("（https://example.org/a(b)）本文", "(https://example.org/a(b))本文",
                     "[link](relative/a(b))本文", "<https://example.org/a(b)>本文"):
            edits, notes = engine.validate_edits(text, [proposal("本文", "文章")])
            self.assertEqual(engine.apply_edits(text, edits), text.replace("本文", "文章"))
            self.assertFalse(notes)
        edits, _ = engine.validate_edits("https://example.org/日本語", [proposal("日本語", "別名")])
        self.assertFalse(edits)

    def test_code_delimiters_never_pair_with_fenced_or_indented_code(self):
        for prefix in ("~~~\na`b\n~~~\n", "```\na`b\n```\n", "    a`b\n"):
            text = prefix + "文 `rm` 語"
            edits, notes = engine.validate_edits(text, [proposal("rm", "RM")])
            self.assertEqual(edits, [])
            self.assertTrue(notes)
            edits, _ = engine.validate_edits(text, [proposal("語", "言葉")])
            self.assertEqual(engine.apply_edits(text, edits), text.replace("語", "言葉"))

    def test_inline_code_is_paragraph_scoped_and_crlf_offsets_are_exact(self):
        for newline in ("\n", "\r\n", "\r"):
            text = f"ここで`を使う。{newline}{newline}`foo` を呼ぶ"
            edits, _ = engine.validate_edits(text, [proposal("foo", "bar")])
            self.assertFalse(edits)
            # A single line ending inside inline code is valid, including CRLF.
            text = f"🙂 `foo{newline}bar` 本文"
            edits, _ = engine.validate_edits(text, [proposal("bar", "baz")])
            self.assertFalse(edits)
            edits, _ = engine.validate_edits(text, [proposal("本文", "文章")])
            self.assertEqual(engine.apply_edits(text, edits), text.replace("本文", "文章"))

    def test_escaped_backtick_does_not_swallow_later_code(self):
        text = r"文字 \` と `foo` 本文"
        edits, _ = engine.validate_edits(text, [proposal("foo", "bar")])
        self.assertFalse(edits)

    def test_inline_code_cannot_pair_across_heading_list_or_quote_boundaries(self):
        for text in ("# 見出し`\n- `foo` を呼ぶ", "- 項目`\n- `foo` を呼ぶ",
                     "> 引用`\n`foo` を呼ぶ", "本文`\n---\n`foo` を呼ぶ"):
            edits, _ = engine.validate_edits(text, [proposal("foo", "bar")])
            self.assertFalse(edits, text)

    def test_individual_overflow_is_held_before_judgment_and_can_export_original(self):
        text = "あ" * 11999 + "語"
        edits, notes = engine.validate_edits(text, [proposal("語", "単語")])
        self.assertFalse(edits)
        self.assertTrue(any("12,000" in note for note in notes))
        prepared = decider.prepare({"text": text, "edits": [proposal("語", "単語")]})
        self.assertEqual(prepared["requests"], [])
        final = engine.check_final(text, engine.apply_edits(text, edits))
        self.assertEqual(final["text"], text)
        self.assertEqual(final["verification"]["status"], "executed")

    def test_combined_overflow_retains_non_growing_edits(self):
        text = "あ" * 11995 + "甲乙丙丁"
        edits, notes = engine.validate_edits(text, [proposal("甲", "甲一"), proposal("乙", "乙二"), proposal("丁", "戊")])
        self.assertEqual([e["before"] for e in edits], ["丁"])
        self.assertTrue(any("合わせる" in note for note in notes))
        self.assertEqual(len(engine.apply_edits(text, edits)), 11999)

    def test_exact_limit_counts_codepoints_and_does_not_truncate(self):
        text = "🙂" * 11998 + "語"
        edits, notes = engine.validate_edits(text, [proposal("語", "単語")])
        self.assertEqual(len(engine.apply_edits(text, edits)), 12000)
        self.assertEqual(edits[0]["start"], 23996)
        self.assertFalse(notes)

    def test_host_finalize_rejects_held_overflow_instead_of_silently_dropping_it(self):
        text = "あ" * 11999 + "語"
        with self.assertRaises(engine.EditorError):
            engine.finalize_proposals({"text": text, "edits": [proposal("語", "単語")]})

    def test_held_count_is_structured_and_excludes_noop_proposals(self):
        result = engine.review_proposals({"text": "`code` と本文", "edits": [proposal("code", "CODE"),
                                          proposal("本文", "本文")], "judge": False})
        self.assertEqual(result["held_edit_count"], 1)
        self.assertEqual(result["edits"], [])

    def test_common_numeral_spellings_do_not_raise_false_change_flags(self):
        for before, after in (("十四日", "14日"), ("１，２８０円", "1280円"),
                              ("三万円", "3万円"), ("三・五", "三・五"), ("一郎", "二郎")):
            self.assertEqual(engine._numeric_values(before), engine._numeric_values(after))
        edits, _ = engine.validate_edits("上限は三件です。", [proposal("三件", "四件")])
        self.assertIn("数値の変化", edits[0]["risk_flags"])

    def test_dotted_identifiers_never_crash_numeric_comparison(self):
        edits, _ = engine.validate_edits("版は1.2.3です。", [proposal("1.2.3", "1.2.4")])
        self.assertIn("数値の変化", edits[0]["risk_flags"])
        self.assertNotEqual(engine._numeric_values("1" * 40 + "2"), engine._numeric_values("1" * 40 + "3"))

    def test_negative_inflections_and_certainty_are_advisory_flags(self):
        for before, after, expected in (("保存しなかった", "保存した", "否定表現の変化"),
                                       ("保存せず", "保存して", "否定表現の変化"),
                                       ("保存しなくても", "保存しても", "否定表現の変化"),
                                       ("実施するでしょう", "実施します", "確度・時制の変化")):
            edits, _ = engine.validate_edits(before, [proposal(before, after)])
            self.assertIn(expected, edits[0]["risk_flags"])
            self.assertTrue(edits[0]["review_required"])
        edits, _ = engine.validate_edits("保存しない", [proposal("しない", "しません")])
        self.assertNotIn("否定表現の変化", edits[0]["risk_flags"])
        edits, _ = engine.validate_edits("承認は必要です。", [proposal("必要", "不要")])
        self.assertIn("否定表現の変化", edits[0]["risk_flags"])
        edits, _ = engine.validate_edits("いかがでしたでしょうか。", [proposal("でしょうか", "ですか")])
        self.assertNotIn("確度・時制の変化", edits[0]["risk_flags"])
        self.assertNotEqual(engine._numeric_values("三名"), engine._numeric_values("四名"))


if __name__ == "__main__":
    unittest.main()
