"""Engine/provider contract checks without settings, credentials, or network."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import engine
import generators
import provider_settings

REAL_GENERATE = generators.generate
REAL_CLAUDE_PROPOSAL = engine._claude_proposal

TEXT = "保存して下さい。上限は3件です。"
PROPOSAL = {"edits": [{"before": "して下さい", "after": "してください", "reason": "補助動詞をひらがなにする", "rule": "表記"}],
            "questions": [], "suggestions": []}


def identity(provider="gemini", model="gemini-3.8-flash-high", reported=None):
    return {"requested_provider": provider, "requested_model": model,
            "reported_provider": provider, "reported_model": reported or model,
            "transport": "antigravity-cli" if provider == "gemini" else provider + "-api",
            "identity_basis": "cli_stream_init_model" if provider == "gemini" else "api_response_model",
            "exact_checkpoint_verified": False, "model_version_differs": bool(reported and reported != model)}


class ProviderIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.settings = self.stack.enter_context(patch.object(provider_settings, "read_settings", return_value={
            "version": 1, "provider": "gemini", "model": "gemini-3.8-flash-high"}))
        self.keys = self.stack.enter_context(patch.object(provider_settings, "api_key", side_effect=AssertionError("must not read keys")))
        self.network = self.stack.enter_context(patch.object(generators, "_post_json", side_effect=AssertionError("must not use network")))
        self.cloud = self.stack.enter_context(patch.object(generators, "generate", return_value={"proposal": PROPOSAL, "identity": identity()}))
        self.claude = self.stack.enter_context(patch.object(engine, "_claude_proposal", return_value=(PROPOSAL, [engine.MODEL])))
        self.stack.enter_context(patch.object(engine, "lint", return_value=[]))
        self.local_judge = self.stack.enter_context(patch("decider.attach", side_effect=AssertionError("must not start local model")))

    def analyze(self, **fields):
        return engine.analyze({"text": TEXT, "profile": "article", "strength": "natural",
                               "context": "語り口を残す", "generate": True, "judge": False, **fields})

    def provider_analyze(self, provider, **fields):
        """Guidance/routing fixtures satisfy Sarashina's mandatory judge only.

        These tests inspect generation inputs, not model quality. Keep all real
        credentials, transports and inference disabled while returning explicit
        fixture decisions on this scoped branch.
        """
        if provider != "sarashina":
            return self.analyze(provider=provider, **fields)

        def judged(output, _text):
            for edit in output["edits"]:
                edit["decision"] = {"status": "executed", "choice": "preserved"}
            output["stages"]["decider"] = "executed"
            return output

        with patch("decider.availability", return_value={"available": True, "status": "configured"}) as available, \
                patch("decider.attach", side_effect=judged) as judge:
            output = self.analyze(provider=provider, judge=True, **fields)
            available.assert_called_once_with()
            judge.assert_called_once()
        self.assertEqual(output["requirements"], {"needs_decider": True})
        return output

    def test_default_routes_gemini_and_retains_cli_identity_and_source_hash(self):
        output = self.analyze()
        payload, instruction, schema = self.cloud.call_args.args
        self.assertEqual(payload, {"source": TEXT, "style_notes": "語り口を残す", "provider": "gemini", "model": "gemini-3.8-flash-high"})
        self.assertEqual(instruction, engine.generation_instructions(engine.CONFIG["profiles"][0], engine.CONFIG["strengths"][1]))
        self.assertIs(schema, engine.GENERATOR_SCHEMA)
        self.assertEqual(output["source_hash"], engine.digest(TEXT))
        self.assertEqual(output["generator_model"], "gemini-3.8-flash-high")
        self.assertEqual(output["generator"]["requested_model"], "gemini-3.8-flash-high")
        self.assertFalse(output["generator"]["model_version_differs"])
        self.assertEqual(output["generator"]["transport"], "antigravity-cli")
        self.assertEqual(output["generator"]["identity_basis"], "cli_stream_init_model")
        self.assertFalse(output["generator"]["exact_checkpoint_verified"])
        self.assertEqual(output["stages"]["generation"], "executed")
        self.assertEqual(output["edits"][0]["before"], "して下さい")
        self.assertEqual(engine.apply_edits(TEXT, output["edits"]), "保存してください。上限は3件です。")
        self.claude.assert_not_called()
        self.keys.assert_not_called()
        self.network.assert_not_called()
        self.local_judge.assert_not_called()

    def test_explicit_sarashina_model_overrides_saved_gemini_without_fallback(self):
        self.cloud.return_value["identity"] = identity("sarashina", "sarashina3-nano")
        output = self.provider_analyze("sarashina", model="sarashina3-nano")
        payload = self.cloud.call_args.args[0]
        self.assertEqual((payload["provider"], payload["model"]), ("sarashina", "sarashina3-nano"))
        self.assertEqual(output["generator"]["reported_provider"], "sarashina")
        self.assertEqual(output["generator_model"], "sarashina3-nano")
        self.claude.assert_not_called()

    def test_explicit_claude_keeps_existing_adapter_and_model_receipt(self):
        output = self.analyze(provider="claude")
        self.cloud.assert_not_called()
        self.claude.assert_called_once_with(TEXT, "語り口を残す", engine.generation_instructions(engine.CONFIG["profiles"][0], engine.CONFIG["strengths"][1]))
        self.assertEqual(output["generator_model"], engine.MODEL)
        self.assertEqual(output["generator"]["requested_provider"], "claude")
        self.assertEqual(output["generator"]["reported_model"], engine.MODEL)
        self.assertEqual(output["generator"]["transport"], "claude-cli")

    def test_cloud_failure_is_error_not_success_with_empty_edits(self):
        payload = {"text": TEXT, "generate": True, "judge": False}
        for code in ("not_configured", "http_error", "identity_mismatch", "invalid_proposal", "input_too_long"):
            self.cloud.side_effect = generators.GenerationError(code, "修正案を生成できませんでした。")
            with self.subTest(code=code), self.assertRaises(engine.EditorError):
                engine.analyze(payload)
            self.assertEqual(payload["text"], TEXT)
        self.claude.assert_not_called()
        self.local_judge.assert_not_called()
        self.network.assert_not_called()

    def test_bad_selection_is_error_before_generation_and_no_fallback(self):
        for fields in ({"provider": "missing"}, {"provider": "sarashina", "model": "gemini-3.8-flash-high"}):
            with self.subTest(fields=fields), self.assertRaises(engine.EditorError):
                self.analyze(**fields)
        self.cloud.assert_not_called()
        self.claude.assert_not_called()

    def test_rules_only_never_reads_generation_settings_or_secrets(self):
        with patch.object(engine, "_generation_bundle", side_effect=AssertionError("must not prepare generation guidance")):
            output = self.analyze(generate=False)
        self.assertEqual(output["edits"], [])
        self.assertEqual(output["stages"]["generation"], "not_requested")
        self.assertNotIn("generator", output)
        self.assertEqual(output["yomiyasu"]["rules"], "executed")
        self.assertEqual(output["yomiyasu"]["version"], "2.0.0")
        self.assertEqual(output["yomiyasu"]["generation_guidance"], {"status": "not_sent", "sources": []})
        self.settings.assert_not_called()
        self.cloud.assert_not_called()
        self.claude.assert_not_called()
        self.keys.assert_not_called()

    def test_full_upstream_guide_and_required_references_reach_every_provider_and_profile(self):
        guide_bytes = (engine.VENDOR / "SKILL.md").read_bytes()
        self.assertEqual(len(guide_bytes), 110419)
        self.assertEqual(hashlib.sha256(guide_bytes).hexdigest(), "b3ca0bd8d1a479eb9789dca2f8e4cf22ad008f0fc8df0e922614ea9ec10aa7ae")
        routes = {
            "article": ["references/doc-types/article.md", "references/domains/tech.md"],
            "blog": ["references/doc-types/essay.md", "references/domains/essay.md"],
            "fiction": ["references/doc-types/essay.md", "references/domains/essay.md"],
            "dialogue": ["references/doc-types/essay.md", "references/domains/essay.md"],
            "business": ["references/domains/business.md"], "social": [],
        }
        for profile in engine.CONFIG["profiles"]:
            expected = ["SKILL.md", "references/rephrase-test.md", "references/gemini-syntax.md", *routes[profile["id"]]]
            for strength in engine.CONFIG["strengths"]:
                for provider, model in (("gemini", "gemini-3.8-flash-high"), ("sarashina", "sarashina3-nano"), ("claude", engine.MODEL)):
                    with self.subTest(profile=profile["id"], strength=strength["id"], provider=provider):
                        self.cloud.reset_mock(); self.claude.reset_mock()
                        self.cloud.return_value = {"proposal": PROPOSAL, "identity": identity(provider, model)}
                        output = self.provider_analyze(provider, profile=profile["id"], strength=strength["id"], model=model)
                        instruction = self.claude.call_args.args[2] if provider == "claude" else self.cloud.call_args.args[1]
                        self.assertEqual(instruction.count(guide_bytes.decode("utf-8")), 1)
                        self.assertIn(profile["guidance"], instruction)
                        self.assertIn(strength["guidance"], instruction)
                        provenance = output["yomiyasu"]
                        self.assertEqual(provenance["rules"], "executed")
                        self.assertEqual(provenance["version"], "2.0.0")
                        self.assertEqual(provenance["guide_bytes"], len(guide_bytes))
                        self.assertEqual(provenance["guide_sha256"], hashlib.sha256(guide_bytes).hexdigest())
                        guidance = provenance["generation_guidance"]
                        self.assertEqual(guidance["status"], "included")
                        self.assertEqual(guidance["instruction_sha256"], engine.digest(instruction))
                        self.assertEqual([item["path"] for item in guidance["sources"]], expected)
                        for item in guidance["sources"]:
                            content = (engine.VENDOR / item["path"]).read_bytes()
                            self.assertIn(content.decode("utf-8"), instruction)
                            self.assertEqual(item["sha256"], hashlib.sha256(content).hexdigest())
                            self.assertEqual(item["bytes"], len(content))
                        self.assertEqual(self.cloud.call_count, int(provider != "claude"))
                        self.assertEqual(self.claude.call_count, int(provider == "claude"))
        self.keys.assert_not_called(); self.network.assert_not_called(); self.local_judge.assert_not_called()

    def test_missing_required_reference_fails_before_any_provider_call(self):
        original = Path.read_bytes
        for missing in ("references/gemini-syntax.md", "references/doc-types/article.md", "references/domains/tech.md"):
            missing_path = engine.VENDOR / missing
            def read_bytes(path):
                if path == missing_path:
                    raise FileNotFoundError(path)
                return original(path)
            with self.subTest(missing=missing), patch.object(Path, "read_bytes", read_bytes), self.assertRaisesRegex(engine.EditorError, "Yomiyasu校正方針"):
                self.analyze()
        self.cloud.assert_not_called(); self.claude.assert_not_called()

    def test_complete_guidance_survives_real_adapter_serialization_without_external_calls(self):
        guide = (engine.VENDOR / "SKILL.md").read_bytes().decode("utf-8")
        for profile in engine.CONFIG["profiles"]:
            captured = {}
            def gemini_boundary(payload, instruction, schema):
                wire = generators.gemini_cli._input(payload, instruction, schema)
                captured["gemini"] = json.loads(json.loads(wire)["message"]["content"])["instructions"]
                return {"proposal": PROPOSAL, "identity": identity()}
            def sarashina_boundary(endpoint, body, key, provider):
                self.assertEqual(key, "offline-fixture-key")
                captured["sarashina"] = json.loads(json.dumps(body, ensure_ascii=False))["messages"][0]["content"]
                return {"model": "sarashina3-nano", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(PROPOSAL)}}]}
            def claude_boundary(command, **kwargs):
                captured["claude"] = json.loads(kwargs["input"])["editor_instructions"]
                self.assertNotIn(guide, " ".join(command))
                return SimpleNamespace(returncode=0, stdout=json.dumps({"modelUsage": {engine.MODEL: {}}, "structured_output": PROPOSAL}))
            with self.subTest(profile=profile["id"]), patch.object(generators, "generate", side_effect=REAL_GENERATE), \
                 patch.object(generators.gemini_cli, "generate", side_effect=gemini_boundary), \
                 patch.object(generators, "_post_json", side_effect=sarashina_boundary), \
                 patch.object(provider_settings, "api_key", return_value="offline-fixture-key"), \
                 patch.object(engine, "_claude_proposal", side_effect=REAL_CLAUDE_PROPOSAL), \
                 patch.object(engine.shutil, "which", return_value="offline-claude-fixture"), \
                 patch.object(engine.subprocess, "run", side_effect=claude_boundary):
                for provider, model in (("gemini", "gemini-3.8-flash-high"), ("sarashina", "sarashina3-nano"), ("claude", engine.MODEL)):
                    output = self.provider_analyze(provider, profile=profile["id"], model=model)
                    instruction = captured[provider]
                    self.assertIn(guide, instruction)
                    self.assertEqual(engine.digest(instruction), output["yomiyasu"]["generation_guidance"]["instruction_sha256"])
                    for source in output["yomiyasu"]["generation_guidance"]["sources"]:
                        self.assertIn((engine.VENDOR / source["path"]).read_bytes().decode("utf-8"), instruction)

    def test_host_supplied_edits_do_not_claim_app_generated_guidance(self):
        output = engine.review_proposals({"text": TEXT, "edits": PROPOSAL["edits"], "judge": False})
        self.assertEqual(output["stages"]["generation"], "host_supplied")
        self.assertEqual(output["yomiyasu"]["generation_guidance"], {"status": "not_sent", "sources": []})
        self.cloud.assert_not_called(); self.claude.assert_not_called(); self.settings.assert_not_called()

    def test_structure_suggestions_remain_separate_from_applicable_edits(self):
        self.cloud.return_value = {"proposal": {**PROPOSAL, "suggestions": [{"quote": "上限は3件です。", "suggestion": "利用条件を先に示す案があります。"}]}, "identity": identity()}
        output = self.analyze(strength="structure")
        self.assertEqual(len(output["suggestions"]), 1)
        self.assertEqual(len(output["edits"]), 1)
        self.assertEqual(engine.apply_edits(TEXT, output["edits"]), "保存してください。上限は3件です。")


if __name__ == "__main__":
    unittest.main()
