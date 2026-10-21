"""Full-passage responses must preserve exact source positions and text."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine
import generators


class PassageDiffTests(unittest.TestCase):
    def edits(self, source, corrected):
        raw, positions = engine._passage_patches(source, corrected)
        return engine._validate_edits(source, raw, positions)

    def test_repeated_anchor_utf16_crlf_and_insertions_reconstruct_exact_text(self):
        for source, corrected in [
            ('👩🏽\u200d💻名前をを呼んだ。\r\n名前をを呼んだ。\r\n', '👩🏽\u200d💻名前を呼んだ。\n名前を呼んだ。\n'),
            ('はじめ。おわり。', '「はじめ。おわり。」'),
            ('か\u3099く。🇯🇵を見る。', 'がく。🇺🇸を見る。'),
            ('出来なった。傘を開いだ。', '出来なかった。傘を開いた。'),
        ]:
            with self.subTest(source=source):
                edits, notes = self.edits(source, corrected)
                expected = corrected.replace('\n', '\r\n') if '\r\n' in source else corrected
                self.assertEqual(engine.apply_edits(source, edits), expected)
                self.assertFalse(notes)
                for edit in edits:
                    start = len(source.encode('utf-16-le')[:edit['start'] * 2].decode('utf-16-le'))
                    end = len(source.encode('utf-16-le')[:edit['end'] * 2].decode('utf-16-le'))
                    self.assertTrue(engine.segmentation._safe_boundary(source, start))
                    self.assertTrue(engine.segmentation._safe_boundary(source, end))

    def test_same_sentence_changes_are_judged_together(self):
        source = '名前をを呼び、少し待ってから傘を開いだ。次の文。'
        corrected = source.replace('をを', 'を').replace('開いだ', '開いた')
        edits, _ = self.edits(source, corrected)
        self.assertEqual(len(edits), 1)
        self.assertEqual(engine.apply_edits(source, edits), corrected)

    def test_external_patch_contract_still_rejects_ambiguous_anchors(self):
        with self.assertRaises(engine.EditorError):
            engine.validate_edits('名前をを呼ぶ。名前をを呼ぶ。', [{'before': 'を', 'after': ''}])

    def test_protected_code_and_urls_are_held_without_losing_neighboring_changes(self):
        source = '名前をを呼ぶ。\n`code` と https://example.test を残す。'
        edits, notes = self.edits(source, source.replace('をを', 'を').replace('code', 'CODE'))
        self.assertEqual(engine.apply_edits(source, edits), source.replace('をを', 'を'))
        self.assertTrue(any('保留' in note for note in notes))

    def test_truncation_and_lost_paragraphs_cannot_become_deletion_patches(self):
        source = '全文を保持する必要がある。' * 20 + '\n\n' + '末尾の段落。'
        for corrected in (source[:40], source.replace('\n\n', '')):
            with self.subTest(corrected=corrected), self.assertRaises(generators.GenerationError):
                self.edits(source, corrected)

    def test_sarashina_uses_full_passage_schema_and_exact_local_diff(self):
        source = '名前をを呼ぶ。\n\n名前をを呼ぶ。'
        def generate(payload, _instruction, schema):
            self.assertIn('corrected_text', schema['properties'])
            return {'proposal': {'corrected_text': payload['source'].replace('をを', 'を'), 'questions': [], 'suggestions': []},
                    'identity': {'requested_provider': 'sarashina', 'reported_provider': 'sarashina',
                                 'requested_model': 'sarashina3-mini', 'reported_model': 'sarashina3-mini'}}
        with patch.object(generators, 'generate', side_effect=generate):
            result = engine._sarashina_generation(source, {'id': 'natural'}, '', 'sarashina3-mini', 'guide', {}, 1200)
        self.assertEqual(engine.apply_edits(source, result[0]), source.replace('をを', 'を'))
        self.assertEqual(len(result[0]), 2)


if __name__ == '__main__':
    unittest.main()
