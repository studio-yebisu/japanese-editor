"""Candidate/length recovery and original-source mapping; no external calls."""
from contextlib import nullcontext
import json
import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine
import generators
import segmentation

IDENTITY = {"requested_provider": "sarashina", "reported_provider": "sarashina",
            "requested_model": "sarashina3-mini", "reported_model": "sarashina3-mini"}
STRENGTH = {"id": "natural"}


def result(edits=()):
    return {"proposal": {"edits": list(edits), "questions": [], "suggestions": []}, "identity": dict(IDENTITY)}


def run(source, maximum=2000):
    return engine._sarashina_generation(source, STRENGTH, "", "sarashina3-mini", "guidance fixture", {}, maximum)


class RecoveryTests(unittest.TestCase):
    def test_cutoff_splits_only_failed_range_and_maps_utf16_crlf_and_protection(self):
        source = ('👩🏽\u200d💻が見ていた。\r\n\r\n'
                  '「風車がからから回る。うち、捨てへんねん」\r\n\r\n'
                  '参照は https://example.org/a。\r\n\r\n'
                  '机をを片づけた。\r\n\r\n'
                  '合図をを待った。\r\n\r\n')
        seen = []
        def generate(payload, instruction, schema):
            part = payload["source"]
            seen.append(part)
            if len(seen) == 1:
                raise generators.GenerationError("generation_length", "cutoff fixture")
            edits = [{"before": before, "after": after, "reason": "重複", "rule": "typo"}
                     for before, after in (("机をを片づけた", "机を片づけた"), ("合図をを待った", "合図を待った")) if before in part]
            return result(edits)
        with patch.object(generators, "generate", side_effect=generate):
            output = run(source)
        edits, identity = output[0], output[5]
        expected = source.replace("机をを片づけた", "机を片づけた").replace("合図をを待った", "合図を待った")
        self.assertEqual(engine.apply_edits(source, edits), expected)
        self.assertEqual("".join(seen[1:]), source)
        self.assertTrue(all(not p.endswith("\r") and not p.startswith("\n") for p in seen[1:]))
        self.assertTrue(any('「風車がからから回る。うち、捨てへんねん」' in p for p in seen[1:]))
        self.assertTrue(any('https://example.org/a。' in p for p in seen[1:]))
        self.assertEqual(identity["segmentation"]["resplit_count"], 1)
        self.assertEqual(identity["segmentation"]["api_calls"], len(seen))
        self.assertTrue(any(e["start"] > source.index(e["before"]) for e in edits))

    def test_completed_part_is_not_resent_and_any_later_failure_discards_all(self):
        source = '初めをを直す。\n\n終わりをを直す。\n\n'
        seen = []
        def generate(payload, *_args):
            seen.append(payload['source'])
            if len(seen) == 1:
                return result([{"before": "初めをを直す", "after": "初めを直す", "reason": "重複", "rule": "typo"}])
            raise generators.GenerationError("http_error", "HTTP 429 fixture")
        with patch.object(generators, "generate", side_effect=generate), self.assertRaises(generators.GenerationError):
            run(source, maximum=14)
        self.assertEqual(len(seen), 2)
        self.assertEqual(''.join(seen), source)

    def test_incomplete_retries_are_bounded_and_never_become_empty_success(self):
        source = ''.join(f'第{i}段落には句点がある。\n\n' for i in range(30))
        for code in ("generation_length", "invalid_quote_balance"):
            with self.subTest(code=code), patch.object(generators, "generate", side_effect=generators.GenerationError(code, "fixture")) as call:
                with self.assertRaises(generators.GenerationError):
                    run(source)
                self.assertLessEqual(call.call_count, engine.SARASHINA_MAX_SPLIT_DEPTH + 1)

    def test_nonrecoverable_failures_never_resplit_or_fallback(self):
        for code in ("http_error", "connection_error", "identity_mismatch", "invalid_proposal", "generation_incomplete"):
            with self.subTest(code=code), patch.object(generators, "generate", side_effect=generators.GenerationError(code, "fixture")) as call:
                with self.assertRaises(generators.GenerationError):
                    run('最初の文。\n\n次の文。\n\n')
                self.assertEqual(call.call_count, 1)

    def test_oversized_atomic_quote_cannot_be_cut_for_recovery(self):
        source = '「' + '長い引用。' * 30 + '」'
        with patch.object(generators, "generate", side_effect=generators.GenerationError("generation_length", "fixture")) as call:
            with self.assertRaises(generators.GenerationError):
                run(source)
        self.assertEqual(call.call_count, 1)

    def test_newly_duplicated_closing_quote_is_not_applied_and_is_recovered(self):
        source = '最初の段落。\n\n「もう、帰る」\n\n最後の段落。\n\n'
        def generate(payload, *_args):
            if payload['source'] == source:
                return result([{"before": "「もう、帰る", "after": "「もう、帰る」", "reason": "closing quote", "rule": "fixture"}])
            return result()
        with patch.object(generators, 'generate', side_effect=generate):
            output = run(source)
        self.assertEqual(output[0], [])
        self.assertEqual(output[5]['requests'][0]['status'], 'invalid_quote_balance')
        self.assertEqual(output[5]['segmentation']['resplit_count'], 1)
        self.assertEqual(engine._quote_issues('「『入れ子』も保つ」'), 0)
        self.assertEqual(engine._quote_issues('`「` と `」」` はコード'), 0)

    def test_short_anchor_cannot_replace_itself_with_a_long_duplicate_of_the_story(self):
        source = '最初の段落。\n\n「もう、帰る」\n\n最後の段落。\n\n'
        for repeats in (10, 30):
            def generate(payload, *_args):
                if payload['source'] == source:
                    return result([{"before": "最初の段落。", "after": source * repeats, "reason": "fixture", "rule": "fixture"}])
                return result()
            with self.subTest(repeats=repeats), patch.object(generators, 'generate', side_effect=generate):
                output = run(source)
            self.assertEqual(output[0], [])
            self.assertEqual(output[5]['requests'][0]['status'], 'generation_oversized_patch')

    def test_request_and_time_budget_stop_before_new_call(self):
        source = '第一段落。\n\n第二段落。\n\n'
        with patch.object(engine, "SARASHINA_MAX_CALLS", 1), patch.object(generators, "generate", side_effect=generators.GenerationError("generation_length", "fixture")) as call:
            with self.assertRaises(generators.GenerationError):
                run(source)
            self.assertEqual(call.call_count, 1)
        with patch.object(engine, "SARASHINA_GENERATION_SECONDS", 0), patch.object(generators, "generate") as call:
            with self.assertRaises(engine.EditorError):
                run(source)
            call.assert_not_called()


class CandidateContractTests(unittest.TestCase):
    def call(self, proposal, usage=None):
        response = {"model": "sarashina3-mini", "usage": usage,
                    "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(proposal, ensure_ascii=False)}}]}
        with patch.object(generators.provider_settings, "read_settings", return_value={"provider": "sarashina", "model": "sarashina3-mini"}), \
             patch.object(generators.provider_settings, "api_key", return_value="offline-fixture"), \
             patch.object(generators, "_post_json", return_value=response):
            return generators.generate({"source": "風車がからから回る。", "provider": "sarashina"}, "full guidance fixture", engine.GENERATOR_SCHEMA)

    def test_unchanged_proposals_are_reported_without_forcing_more_generation(self):
        proposal = {"edits": [{"before": "風車がからから回る。", "after": "風車がからから回る。", "reason": "変更なし", "rule": "style"}], "questions": [], "suggestions": []}
        output = self.call(proposal)
        self.assertEqual(output['diagnostics']["unchanged_edit_count"], 1)
        with patch.object(generators, 'generate', return_value=output) as call:
            reviewed = run('風車がからから回る。')
        self.assertEqual(reviewed[0], [])
        self.assertEqual(call.call_count, 1)
        self.assertEqual(reviewed[5]['result'], 'unchanged_proposals_only')
        self.assertTrue(any('見逃し' in note for note in reviewed[2]))
        self.assertEqual(self.call({"edits": [], "questions": [], "suggestions": []})["proposal"]["edits"], [])

    def test_diagnostics_do_not_retain_provider_text_or_extra_usage(self):
        output = self.call({"edits": [], "questions": [], "suggestions": []},
                           {"prompt_tokens": 15, "completion_tokens": 8, "total_tokens": True, "private": "private text"})
        self.assertEqual(output["diagnostics"]["usage"], {"prompt_tokens": 15, "completion_tokens": 8})
        self.assertNotIn("private", json.dumps(output["diagnostics"]))
        self.assertNotIn("風車", json.dumps(output["diagnostics"], ensure_ascii=False))

    def test_cooldown_and_lock_wait_share_deadline_and_never_send_after_expiry(self):
        clock = Mock(return_value=10)
        with patch.object(generators, "_SARASHINA_NEXT_REQUEST_AT", 100), \
             patch.object(generators.time, "monotonic", clock), \
             patch.object(generators.time, "sleep") as sleep:
            sleep.side_effect = lambda _seconds: setattr(clock, "return_value", 12)
            with generators.generation_budget(11), self.assertRaises(generators.GenerationError) as error:
                with generators._sarashina_slot():
                    self.fail("expired request was sent")
            self.assertEqual(error.exception.code, "generation_deadline")
            sleep.assert_called_once_with(1)


if __name__ == '__main__':
    unittest.main()
