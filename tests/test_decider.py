"""No-model contract tests for local judgments and the shared HTTP boundary.

All inference/availability functions are patched. These tests use only the
standard library and never read a model configuration, start a worker, or make
network/model requests.
"""
from __future__ import annotations

import copy
from email.message import Message
import importlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import decider
import decider_runtime
import engine
import rpc_auth


IDENTITY = {
    "model": decider_runtime.MODEL,
    "revision": decider_runtime.REVISION,
    "device": "cpu",
    "backend": "local",
}
AVAILABLE = {**IDENTITY, "available": True, "status": "ready", "loaded": True}


def proposal(before, after):
    return {"before": before, "after": after,
            "reason": "呼出し元が作成した修正理由", "rule": "test:meaning"}


def native_response(choice="preserved"):
    # Matches the real worker envelope around the official Decider response.
    # The numbers are test fixtures, not model predictions.
    return {
        "id": "test-request", "ok": True, **IDENTITY, "latency_ms": 12.5,
        "result": {
            "answers": {"meaning_shift": {"type": "choice", "choice": choice,
                "probabilities": {"preserved": 0.8, "shifted": 0.1, "needs_review": 0.1},
                "confidence": 0.7}},
            "usage": {"input_tokens": 123, "output_tokens": 0, "sequences": 1},
        },
    }


def output_for(text, proposals):
    edits, notes = engine.validate_edits(text, proposals)
    return {
        "source_hash": engine.digest(text), "offset_encoding": "utf-16",
        "profile": "article", "strength": "natural", "edits": edits,
        "findings": [], "questions": [], "suggestions": [], "notes": notes,
        "stages": {"rules": "executed", "generation": "host_supplied", "decider": "not_run"},
    }


def nested_keys(value):
    if isinstance(value, dict):
        return set(value).union(*(nested_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(nested_keys(item) for item in value))
    return set()


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.available = self.enterContext(mock.patch.object(decider, "availability", return_value=dict(AVAILABLE)))
        self.infer = self.enterContext(mock.patch.object(decider_runtime, "decide", side_effect=AssertionError("unexpected inference")))

    def test_native_envelope_attaches_identity_without_probabilities_or_replacing_reason(self):
        for choice in ("preserved", "shifted", "needs_review"):
            with self.subTest(choice=choice):
                text = "🙂保存して下さい。"
                output = output_for(text, [proposal("して下さい", "してください")])
                original_edit = copy.deepcopy(output["edits"][0])
                self.infer.reset_mock()
                self.infer.side_effect = None
                self.infer.return_value = native_response(choice)
                result = decider.attach(output, text)
                self.assertIs(result, output)
                self.assertEqual(result["stages"]["decider"], "executed")
                decision = result["edits"][0]["decision"]
                self.assertEqual(decision["status"], "executed")
                self.assertEqual(decision["choice"], choice)
                self.assertEqual(decision["scope"], "document")
                for key in ("model", "revision", "device", "backend"):
                    self.assertEqual(decision[key], IDENTITY[key])
                for key, value in original_edit.items():
                    self.assertEqual(result["edits"][0][key], value)
                self.assertTrue(result["edits"][0]["review_required"])
                self.assertFalse({"probabilities", "confidence", "answers", "logits"} & nested_keys(result))
                request = self.infer.call_args.args[0]
                self.assertEqual(request["state"]["original"], text)
                self.assertEqual(request["state"]["candidate"], engine.apply_edits(text, [original_edit]))
                self.assertFalse({"reason", "rule"} & nested_keys(request["state"]))
                self.assertEqual(list(request["questions"]), ["meaning_shift"])
                self.infer.assert_called_once()

    def test_unavailable_keeps_all_proposals_without_inference(self):
        self.available.return_value = {**AVAILABLE, "available": False, "status": "missing_runtime"}
        output = output_for("保存して下さい。", [proposal("下さい", "ください")])
        original = copy.deepcopy(output["edits"])
        decider.attach(output, "保存して下さい。")
        self.assertEqual(output["stages"]["decider"], "unavailable")
        self.assertEqual(output["edits"][0]["before"], original[0]["before"])
        self.assertEqual(output["edits"][0]["after"], original[0]["after"])
        self.assertEqual(output["edits"][0]["decision"]["status"], "not_run")
        self.assertNotIn("choice", output["edits"][0]["decision"])
        self.infer.assert_not_called()

    def test_unknown_choice_marks_failure_and_does_not_retry_remaining_candidates(self):
        text = "保存して下さい。確認して頂く。"
        output = output_for(text, [proposal("下さい", "ください"), proposal("頂く", "いただく")])
        self.infer.side_effect = None
        self.infer.return_value = native_response("unexpected_label")
        decider.attach(output, text)
        self.assertEqual(output["stages"]["decider"], "unavailable")
        self.assertEqual([item["decision"]["status"] for item in output["edits"]], ["error", "not_run"])
        self.assertEqual(output["edits"][0]["decision"]["error"]["code"], "invalid_answer")
        self.assertEqual(len(output["edits"]), 2)
        self.infer.assert_called_once()

    def test_failure_after_one_success_is_partial_and_stops_following_inference(self):
        text = "保存して下さい。確認して頂く。結果を表示出来る。"
        proposals = [proposal("下さい", "ください"), proposal("頂く", "いただく"), proposal("出来る", "できる")]
        output = output_for(text, proposals)
        failure = decider_runtime.DeciderRuntimeError("timeout", "test timeout")
        self.infer.side_effect = [native_response("shifted"), failure]
        decider.attach(output, text)
        self.assertEqual(output["stages"]["decider"], "partial")
        self.assertEqual([item["decision"]["status"] for item in output["edits"]], ["executed", "error", "not_run"])
        self.assertEqual(output["decider"]["executed"], 1)
        self.assertEqual(output["decider"]["total"], 3)
        self.assertEqual(self.infer.call_count, 2)
        for actual, expected in zip(output["edits"], proposals):
            self.assertEqual(actual["before"], expected["before"])
            self.assertEqual(actual["after"], expected["after"])
            self.assertEqual(actual["reason"], expected["reason"])

    def test_no_edits_is_not_run_and_does_not_even_query_availability(self):
        output = output_for("このままで読みやすい文です。", [])
        decider.attach(output, "このままで読みやすい文です。")
        self.assertEqual(output["stages"]["decider"], "not_run")
        self.available.assert_not_called()
        self.infer.assert_not_called()

    def test_malformed_answer_envelopes_preserve_proposal_as_error(self):
        for malformed in (None, {"answers": []}, {"answers": {"meaning_shift": []}}):
            with self.subTest(envelope=malformed):
                output = output_for("保存して下さい。", [proposal("下さい", "ください")])
                self.infer.side_effect = None
                self.infer.return_value = {**native_response(), "result": malformed}
                decider.attach(output, "保存して下さい。")
                self.assertEqual(output["edits"][0]["after"], "ください")
                self.assertEqual(output["edits"][0]["decision"]["status"], "error")
                self.assertEqual(output["stages"]["decider"], "unavailable")

    def test_judgment_length_limit_does_not_discard_a_valid_proposal(self):
        # An individual edit larger than the judgment context budget cannot be
        # evaluated by trimming surrounding paragraphs. Keep it as a proposal.
        text = "あ" * 2299 + "。"
        output = output_for(text, [proposal(text, "表現を変えた候補です。")])
        decider.attach(output, text)
        self.assertEqual(output["edits"][0]["before"], text)
        self.assertEqual(output["edits"][0]["after"], "表現を変えた候補です。")
        self.assertIn(output["edits"][0]["decision"]["status"], ("error", "not_run"))
        self.assertEqual(output["stages"]["decider"], "deferred")
        self.infer.assert_not_called()

    def test_long_source_uses_bounded_excerpt_without_changing_source_or_proposal(self):
        sentence = "保存して下さい。"
        prefix = "🙂" + "前" * 5899
        text = prefix + sentence + "後" * (12000 - len(prefix) - len(sentence))
        output = output_for(text, [proposal("下さい", "ください")])
        original_edit = copy.deepcopy(output["edits"][0])
        original_hash = output["source_hash"]
        self.infer.side_effect = None
        self.infer.return_value = native_response()
        decider.attach(output, text)
        request = self.infer.call_args.args[0]
        bounded_original = request["state"]["original"]
        bounded_candidate = request["state"]["candidate"]
        self.assertEqual(len(text), 12000)
        self.assertLessEqual(len(bounded_original), 2200)
        self.assertLessEqual(len(bounded_candidate), 3000)
        self.assertIn(sentence, bounded_original)
        self.assertEqual(bounded_candidate, bounded_original.replace("下さい", "ください", 1))
        self.assertEqual(output["source_hash"], original_hash)
        self.assertEqual(output["source_hash"], engine.digest(text))
        for key, value in original_edit.items():
            self.assertEqual(output["edits"][0][key], value)
        self.assertEqual(output["edits"][0]["decision"]["status"], "executed")
        self.assertEqual(output["edits"][0]["decision"]["scope"], "excerpt")
        self.assertFalse({"reason", "rule"} & nested_keys(request["state"]))
        self.assertTrue(any("抜粋" in note or "周辺" in note for note in output["notes"]))
        self.infer.assert_called_once()

    def test_expanding_an_excerpt_past_its_budget_preserves_proposal_without_inference(self):
        text = "前" * 5900 + "。" + "後" * 6099
        replacement = "新" * 2200
        output = output_for(text, [proposal("。", replacement)])
        decider.attach(output, text)
        self.assertEqual(output["edits"][0]["after"], replacement)
        self.assertEqual(output["edits"][0]["decision"]["status"], "error")
        self.assertEqual(output["stages"]["decider"], "deferred")
        self.infer.assert_not_called()

    def test_native_length_error_does_not_stop_later_candidate(self):
        text = "保存して下さい。確認して頂く。"
        output = output_for(text, [proposal("下さい", "ください"), proposal("頂く", "いただく")])
        length_error = decider_runtime.DeciderRuntimeError(
            "request_too_long", "入力が判定の上限を超えました。", recoverable=True)
        self.infer.side_effect = [length_error, native_response("needs_review")]
        decider.attach(output, text)
        self.assertEqual(output["stages"]["decider"], "partial")
        self.assertEqual([item["decision"]["status"] for item in output["edits"]], ["error", "executed"])
        self.assertEqual(output["edits"][1]["decision"]["choice"], "needs_review")
        self.assertEqual(output["decider"]["executed"], 1)
        self.assertEqual(self.infer.call_count, 2)

    def test_explicit_precision_metadata_is_retained_on_public_judgment(self):
        precision = {"precision": "bfloat16", "precision_override": True,
                     "reference_path": decider_runtime.CPU_BFLOAT16_MODE}
        self.available.return_value = {**AVAILABLE, **precision}
        self.infer.side_effect = None
        self.infer.return_value = {**native_response(), **precision}
        output = output_for("保存して下さい。", [proposal("下さい", "ください")])
        decider.attach(output, "保存して下さい。")
        for key, value in precision.items():
            self.assertEqual(output["edits"][0]["decision"][key], value)

    def test_overall_deadline_preserves_earlier_result_and_defers_remaining_edits(self):
        text = "保存して下さい。確認して頂く。結果を表示出来る。"
        proposals = [proposal("下さい", "ください"), proposal("頂く", "いただく"), proposal("出来る", "できる")]
        output = output_for(text, proposals)
        self.infer.side_effect = None
        self.infer.return_value = native_response("preserved")
        with mock.patch.object(decider.time, "monotonic", side_effect=[1000.0, 1000.5, 1480.25, 1480.5]):
            decider.attach(output, text)
        self.infer.assert_called_once()
        self.assertEqual(self.infer.call_args.kwargs["timeout"], 479.5)
        self.assertEqual([item["decision"]["status"] for item in output["edits"]], ["executed", "not_run", "not_run"])
        self.assertEqual(output["edits"][0]["decision"]["choice"], "preserved")
        self.assertEqual(output["stages"]["decider"], "partial")
        self.assertEqual(output["decider"]["executed"], 1)
        self.assertEqual(output["decider"]["total"], 3)
        for item in output["edits"][1:]:
            self.assertEqual(item["decision"]["error"]["code"], "deadline")
            self.assertNotIn("choice", item["decision"])
        for actual, expected in zip(output["edits"], proposals):
            for key in ("before", "after", "reason", "rule"):
                self.assertEqual(actual[key], expected[key])

    def test_exhausted_budget_before_first_edit_is_deferred_without_inference(self):
        text = "保存して下さい。確認して頂く。"
        output = output_for(text, [proposal("下さい", "ください"), proposal("頂く", "いただく")])
        with mock.patch.object(decider.time, "monotonic", side_effect=[1000.0, 1480.0, 1480.5]):
            decider.attach(output, text)
        self.infer.assert_not_called()
        self.assertEqual(output["stages"]["decider"], "deferred")
        self.assertTrue(all(item["decision"]["status"] == "not_run" for item in output["edits"]))
        self.assertTrue(all(item["decision"]["error"]["code"] == "deadline" for item in output["edits"]))


class PrecisionIdentityTests(unittest.TestCase):
    def test_explicit_cpu_bfloat16_rejects_missing_or_mismatched_precision_metadata(self):
        service = {"url": "http://127.0.0.1:1", "timeout": 1, "device": "cpu", "cpu_dtype": "bfloat16"}
        correct = {"precision": "bfloat16", "precision_override": True,
                   "reference_path": decider_runtime.CPU_BFLOAT16_MODE}
        for metadata in ({}, {**correct, "precision": "float32"},
                         {**correct, "precision_override": False}, {**correct, "reference_path": True}):
            with self.subTest(metadata=metadata):
                with mock.patch.object(decider_runtime, "_service_configuration", return_value=service):
                    with mock.patch.object(decider_runtime, "_remote", return_value={**native_response(), **metadata}):
                        with mock.patch.object(decider_runtime, "local_decide", side_effect=AssertionError("no local fallback")):
                            with self.assertRaises(decider_runtime.DeciderRuntimeError) as raised:
                                decider_runtime.decide({"state": "test", "questions": {}})
                self.assertEqual(raised.exception.code, "identity_mismatch")

    def test_any_present_precision_must_match_the_configured_mode(self):
        cases = (("cpu", "bfloat16", True), ("cpu", "float16", False),
                 ("cpu", None, False), ("cuda:0", "float32", False))
        for device, precision, override in cases:
            with self.subTest(device=device, response_precision=precision):
                service = {"url": "http://127.0.0.1:1", "timeout": 1, "device": device, "cpu_dtype": "float32"}
                response = {**native_response(), "device": device, "precision": precision,
                            "precision_override": override,
                            "reference_path": decider_runtime.CPU_BFLOAT16_MODE if override else True}
                with mock.patch.object(decider_runtime, "_service_configuration", return_value=service):
                    with mock.patch.object(decider_runtime, "_remote", return_value=response):
                        with self.assertRaises(decider_runtime.DeciderRuntimeError) as raised:
                            decider_runtime.decide({"state": "test", "questions": {}})
                self.assertEqual(raised.exception.code, "identity_mismatch")

    def test_matching_cpu_bfloat16_identity_returns_the_actual_envelope(self):
        service = {"url": "http://127.0.0.1:1", "timeout": 1, "device": "cpu", "cpu_dtype": "bfloat16"}
        response = {**native_response(), "precision": "bfloat16", "precision_override": True,
                    "reference_path": decider_runtime.CPU_BFLOAT16_MODE}
        with mock.patch.object(decider_runtime, "_service_configuration", return_value=service):
            with mock.patch.object(decider_runtime, "_remote", return_value=response):
                self.assertIs(decider_runtime.decide({"state": "test", "questions": {}}), response)

    def test_runtime_and_app_availability_surface_precision_identity_without_internal_paths(self):
        precision = {"precision": "bfloat16", "precision_override": True,
                     "reference_path": decider_runtime.CPU_BFLOAT16_MODE}
        state = {**IDENTITY, **precision, "status": "ready", "loaded": True,
                 "model_path": "private-local-path", "runtime": {**precision, "source_path": "private-source-path"}}
        clean = decider_runtime._public_status(state)
        self.assertNotIn("model_path", clean)
        self.assertNotIn("source_path", clean["runtime"])
        with mock.patch.object(decider_runtime, "status", return_value=state):
            available = decider.availability()
        for key, value in precision.items():
            self.assertEqual(clean[key], value)
            self.assertEqual(clean["runtime"][key], value)
            self.assertEqual(available[key], value)


class ScopedPrecisionLoaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Loading the worker module is safe: torch and model imports live only
        # inside main(). A unique name avoids collisions with a generic tools
        # package that might exist in another test environment.
        spec = importlib.util.spec_from_file_location(
            "japanese_editor_decider_worker_test", ROOT / "tools" / "decider_worker.py")
        cls.worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.worker)

    def fake_loader(self, *, fail=False, unexpected_dtype=False):
        torch = SimpleNamespace(float32=object(), bfloat16=object())
        backbone_loader = mock.Mock(return_value=object())
        backbone = type("FakeBackbone", (), {"from_pretrained": staticmethod(backbone_loader)})
        instance = SimpleNamespace(readout=SimpleNamespace(to=mock.Mock()))

        def construct(path, **_kwargs):
            dtype = torch.bfloat16 if unexpected_dtype else torch.float32
            backbone.from_pretrained(path, dtype=dtype)
            if fail:
                raise RuntimeError("fixture load failure")
            return instance

        native = SimpleNamespace(from_pretrained=mock.Mock(side_effect=construct))
        config = {"device": "cpu", "cpu_dtype": "bfloat16", "max_length": 1024, "cpu_threads": 2}
        return torch, backbone_loader, backbone, native, instance, config

    def test_cpu_bfloat16_override_is_scoped_and_restored_after_success_and_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                torch, loader, backbone, native, instance, config = self.fake_loader(fail=fail)
                original_loader, original_float32 = backbone.from_pretrained, torch.float32
                if fail:
                    with self.assertRaisesRegex(RuntimeError, "fixture load failure"):
                        self.worker.load_native_decider(native, torch, "fixture-model", config, backbone_class=backbone)
                    instance.readout.to.assert_not_called()
                else:
                    result = self.worker.load_native_decider(native, torch, "fixture-model", config, backbone_class=backbone)
                    self.assertIs(result, instance)
                    instance.readout.to.assert_called_once_with(dtype=torch.bfloat16)
                self.assertIs(backbone.from_pretrained, original_loader)
                self.assertIs(torch.float32, original_float32)
                loader.assert_called_once_with("fixture-model", dtype=torch.bfloat16)
                self.assertFalse(native.from_pretrained.call_args.kwargs["fast"])

    def test_unexpected_official_loader_dtype_stops_and_restores_without_fallback(self):
        torch, loader, backbone, native, instance, config = self.fake_loader(unexpected_dtype=True)
        original_loader = backbone.from_pretrained
        with self.assertRaises(self.worker.WorkerError) as raised:
            self.worker.load_native_decider(native, torch, "fixture-model", config, backbone_class=backbone)
        self.assertEqual(raised.exception.code, "precision_override_error")
        self.assertIs(backbone.from_pretrained, original_loader)
        loader.assert_not_called()
        instance.readout.to.assert_not_called()


class OwnedWorkerLifecycleTests(unittest.TestCase):
    def runtime_with_process(self):
        # Constructor only resolves this fixture path; status/configuration and
        # model-start paths are never called by these lifecycle tests.
        runtime = decider_runtime.DeciderRuntime(ROOT / "tests" / "unused-runtime-config.json")
        process = mock.Mock(pid=24680)
        process.poll.return_value = None
        process.wait.return_value = 0
        runtime._process = process
        runtime._ready = {"event": "ready", "ok": True, **IDENTITY}
        runtime._signature = "owned-fixture"
        runtime._phase = "ready"
        return runtime, process

    def test_windows_stops_only_its_live_owned_process_tree_before_closing_pipes(self):
        runtime, process = self.runtime_with_process()
        with mock.patch.object(decider_runtime.os, "name", "nt"):
            with mock.patch.object(decider_runtime.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as taskkill:
                runtime.stop()
        taskkill.assert_called_once_with(
            ["taskkill", "/PID", "24680", "/T", "/F"],
            stdin=decider_runtime.subprocess.DEVNULL, capture_output=True, timeout=10,
            creationflags=getattr(decider_runtime.subprocess, "CREATE_NO_WINDOW", 0),
        )
        process.wait.assert_called_once_with(timeout=5)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        self.assertIsNone(runtime._process)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close.assert_called_once_with()

    def test_failed_windows_tree_stop_retains_ownership_and_blocks_replacement_after_wrapper_exit(self):
        runtime, process = self.runtime_with_process()
        with mock.patch.object(decider_runtime.os, "name", "nt"):
            with mock.patch.object(decider_runtime.subprocess, "run", return_value=SimpleNamespace(returncode=1)) as taskkill:
                with self.assertRaises(decider_runtime.DeciderRuntimeError) as raised:
                    runtime.stop()
                self.assertEqual(raised.exception.code, "worker_stop_failed")
                self.assertIs(runtime._process, process)
                self.assertIsNone(runtime._ready)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close.assert_not_called()
                # The wrapper can exit while its heavy child remains alive.
                # Never taskkill its now-recyclable PID or launch a replacement.
                process.poll.return_value = 0
                with mock.patch.object(decider_runtime.subprocess, "Popen") as launch:
                    with self.assertRaises(decider_runtime.DeciderRuntimeError) as repeated:
                        runtime._start({"device": "cpu"})
                    self.assertEqual(repeated.exception.code, "worker_stop_failed")
                    launch.assert_not_called()
                taskkill.assert_called_once()

    def test_windows_failure_can_retry_only_while_the_owned_wrapper_remains_live(self):
        runtime, process = self.runtime_with_process()
        results = [SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)]
        with mock.patch.object(decider_runtime.os, "name", "nt"):
            with mock.patch.object(decider_runtime.subprocess, "run", side_effect=results) as taskkill:
                with self.assertRaises(decider_runtime.DeciderRuntimeError):
                    runtime.stop()
                runtime.stop()
        self.assertEqual(taskkill.call_count, 2)
        self.assertIsNone(runtime._process)
        self.assertIsNone(runtime._stop_failure)
        process.wait.assert_called_once_with(timeout=5)

    def test_already_exited_worker_is_not_targeted_by_pid(self):
        runtime, process = self.runtime_with_process()
        process.poll.return_value = 0
        with mock.patch.object(decider_runtime.os, "name", "nt"):
            with mock.patch.object(decider_runtime.subprocess, "run") as taskkill:
                runtime.stop()
        taskkill.assert_not_called()
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        self.assertIsNone(runtime._process)

    def test_non_windows_keeps_terminate_then_kill_without_taskkill(self):
        for force in (False, True):
            with self.subTest(force=force):
                runtime, process = self.runtime_with_process()
                if force:
                    process.wait.side_effect = [decider_runtime.subprocess.TimeoutExpired("fixture-worker", 5), 0]
                with mock.patch.object(decider_runtime.os, "name", "posix"):
                    with mock.patch.object(decider_runtime.subprocess, "run") as taskkill:
                        runtime.stop()
                taskkill.assert_not_called()
                process.terminate.assert_called_once_with()
                if force:
                    process.kill.assert_called_once_with()
                    self.assertEqual(process.wait.call_count, 2)
                else:
                    process.kill.assert_not_called()
                    process.wait.assert_called_once_with(timeout=5)
                self.assertIsNone(runtime._process)


class FrozenExternalLaunchTests(unittest.TestCase):
    def fake_api(self, original="fixture-bundled-dlls"):
        api = mock.Mock()

        def get_directory(size, buffer):
            if original is None:
                return 0
            if size == 0:
                return len(original) + 1
            buffer.value = original
            return len(original)

        api.GetDllDirectoryW.side_effect = get_directory
        api.SetDllDirectoryW.return_value = 1
        return api

    def patch_api(self, api):
        self.enterContext(mock.patch.object(decider_runtime, "_frozen_windows", return_value=True))
        self.enterContext(mock.patch.object(decider_runtime, "_windows_dll_api", return_value=api))
        self.enterContext(mock.patch.object(decider_runtime.ctypes, "set_last_error", create=True))
        self.enterContext(mock.patch.object(decider_runtime.ctypes, "get_last_error", return_value=0, create=True))

    def test_dll_directory_is_reset_only_for_launch_and_restored_on_success_and_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                api = self.fake_api()
                with mock.patch.object(decider_runtime, "_frozen_windows", return_value=True), \
                     mock.patch.object(decider_runtime, "_windows_dll_api", return_value=api), \
                     mock.patch.object(decider_runtime.ctypes, "set_last_error", create=True), \
                     mock.patch.object(decider_runtime.ctypes, "get_last_error", return_value=0, create=True):
                    try:
                        with decider_runtime._external_dll_search():
                            self.assertEqual(api.SetDllDirectoryW.call_args_list, [mock.call(None)])
                            if fail:
                                raise OSError("fixture launch failure")
                    except OSError:
                        self.assertTrue(fail)
                self.assertEqual(api.SetDllDirectoryW.call_args_list, [mock.call(None), mock.call("fixture-bundled-dlls")])

    def test_source_launch_does_not_modify_dll_search(self):
        with mock.patch.object(decider_runtime, "_frozen_windows", return_value=False), \
             mock.patch.object(decider_runtime, "_windows_dll_api") as api:
            with decider_runtime._external_dll_search():
                pass
        api.assert_not_called()

    def test_child_path_removes_only_bundle_anchored_entries_without_mutating_parent(self):
        bundle = ROOT / "tests/fixture-bundle"
        outside = ROOT / "tests/fixture-system"
        environment = {"PATH": decider_runtime.os.pathsep.join([str(bundle), str(bundle / "dlls"), str(outside)]), "FIXTURE": "keep"}
        before = dict(environment)
        with mock.patch.object(decider_runtime, "_frozen_windows", return_value=True), \
             mock.patch.object(decider_runtime.sys, "_MEIPASS", str(bundle), create=True):
            clean = decider_runtime._external_environment(environment)
        self.assertEqual(clean, {"PATH": str(outside), "FIXTURE": "keep"})
        self.assertEqual(environment, before)

    def test_failed_dll_reset_does_not_enter_launch_body(self):
        api = self.fake_api()
        api.SetDllDirectoryW.return_value = 0
        self.patch_api(api)
        entered = False
        with self.assertRaises(OSError):
            with decider_runtime._external_dll_search():
                entered = True
        self.assertFalse(entered)
        api.SetDllDirectoryW.assert_called_once_with(None)

    def test_frozen_tree_stop_uses_sanitized_path_and_restores_dll_search(self):
        runtime, process = OwnedWorkerLifecycleTests().runtime_with_process()
        api = self.fake_api()
        self.patch_api(api)
        bundle, outside = ROOT / "tests/fixture-bundle", ROOT / "tests/fixture-system"
        # Mock the Windows launch branch without changing the test host's
        # concrete filesystem path type on Linux/macOS.
        host_path = type(bundle)

        def kill(arguments, **kwargs):
            self.assertEqual(arguments, ["taskkill", "/PID", "24680", "/T", "/F"])
            self.assertEqual(kwargs["env"]["PATH"], str(outside))
            self.assertEqual(api.SetDllDirectoryW.call_args_list, [mock.call(None)])
            return SimpleNamespace(returncode=0)

        with mock.patch.object(decider_runtime, "Path", host_path), \
             mock.patch.object(decider_runtime.os, "name", "nt"), \
             mock.patch.dict(decider_runtime.os.environ, {"PATH": decider_runtime.os.pathsep.join([str(bundle), str(outside)])}, clear=True), \
             mock.patch.object(decider_runtime.sys, "_MEIPASS", str(bundle), create=True), \
             mock.patch.object(decider_runtime.subprocess, "run", side_effect=kill):
            runtime.stop()
        self.assertEqual(api.SetDllDirectoryW.call_args_list, [mock.call(None), mock.call("fixture-bundled-dlls")])
        self.assertIsNone(runtime._process)


class StartupRejectionExitTests(unittest.TestCase):
    def fixture(self):
        runtime = decider_runtime.DeciderRuntime(ROOT / "tests/unused-resource-guard-config.json")
        process = mock.Mock(pid=13579)
        process.poll.return_value = None
        process.wait.return_value = 1
        config = {"python": "fixture-dedicated-python", "device": "cpu", "cpu_dtype": "float32", "startup_timeout_seconds": 1,
                  "request_timeout_seconds": 1}
        self.enterContext(mock.patch.object(decider_runtime, "_configuration", return_value=config))
        self.enterContext(mock.patch.object(decider_runtime.subprocess, "Popen", return_value=process))
        self.enterContext(mock.patch.object(decider_runtime.threading, "Thread"))
        self.taskkill = self.enterContext(mock.patch.object(decider_runtime.subprocess, "run", return_value=SimpleNamespace(returncode=1)))
        return runtime, process

    def test_resource_guard_exit_requires_owned_handle_and_protocol_eof_and_preserves_error(self):
        runtime, process = self.fixture()

        def rejected(_timeout):
            runtime._stdout_closed.set()
            return {"event": "startup_error", "ok": False, **IDENTITY,
                    "error": {"code": "insufficient_memory", "message": "fixture commit guard"}}

        self.enterContext(mock.patch.object(runtime, "_receive", side_effect=rejected))
        with self.assertRaises(decider_runtime.DeciderRuntimeError) as error:
            runtime.decide({"state": "fixture", "questions": {}})
        self.assertEqual(error.exception.code, "insufficient_memory")
        self.assertEqual(str(error.exception), "空きメモリが不足しているため、モデルを読み込んでいません。")
        self.assertNotIn("fixture", json.dumps(error.exception.as_dict()))
        self.taskkill.assert_not_called()
        process.wait.assert_called_once_with(timeout=2)
        process.stdin.write.assert_not_called()
        self.assertIsNone(runtime._process)
        self.assertEqual(runtime._last_error["code"], "insufficient_memory")
        self.assertIsNone(getattr(runtime, "_stop_failure", None))

    def test_exited_wrapper_without_protocol_eof_remains_blocked(self):
        runtime, process = self.fixture()
        process.poll.return_value = 1
        closed = mock.Mock()
        closed.wait.return_value = False
        runtime._process, runtime._startup_rejection = process, (process, closed)
        with self.assertRaises(decider_runtime.DeciderRuntimeError) as error:
            runtime.stop()
        self.assertEqual(error.exception.code, "worker_stop_failed")
        self.assertEqual(error.exception.details["operation"], "startup_exit_without_protocol_eof")
        self.assertIs(runtime._process, process)
        self.taskkill.assert_not_called()

    def test_protocol_eof_from_another_process_does_not_release_current_ownership(self):
        runtime, process = self.fixture()
        other = mock.Mock()
        closed = mock.Mock()
        closed.wait.return_value = True
        runtime._process, runtime._startup_rejection = process, (other, closed)
        with mock.patch.object(decider_runtime.os, "name", "nt"):
            with self.assertRaises(decider_runtime.DeciderRuntimeError):
                runtime.stop()
        closed.wait.assert_not_called()
        process.wait.assert_not_called()
        self.assertIs(runtime._process, process)

    def test_cleanup_failure_keeps_original_code_only_in_private_diagnostic(self):
        runtime, _process = self.fixture()
        trigger = decider_runtime.DeciderRuntimeError("insufficient_memory", "fixture-private-path manuscript")
        failure = decider_runtime.DeciderRuntimeError("worker_stop_failed", "fixture cleanup")
        with mock.patch.object(runtime, "stop", side_effect=failure), mock.patch.object(decider_runtime.sys, "stderr", new_callable=io.StringIO) as private:
            with self.assertRaises(decider_runtime.DeciderRuntimeError):
                runtime._cleanup_after_error(trigger)
        self.assertEqual(runtime._cleanup_diagnostic, {"trigger_code": "insufficient_memory", "cleanup_code": "worker_stop_failed"})
        self.assertIn("insufficient_memory", private.getvalue())
        self.assertNotIn("manuscript", private.getvalue())
        self.assertNotIn("fixture-private-path", private.getvalue())

    def test_reader_exception_is_not_eof_proof(self):
        closed = threading.Event()
        stream = mock.Mock()
        stream.__iter__ = mock.Mock(side_effect=OSError("fixture pipe error"))
        messages = decider_runtime.queue.Queue()
        decider_runtime.DeciderRuntime._read_stdout(SimpleNamespace(stdout=stream), messages, closed)
        self.assertFalse(closed.is_set())
        self.assertIsNone(messages.get_nowait())


class EngineJudgmentDispatchTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(engine, "lint", return_value=[]))
        self.available = self.enterContext(mock.patch.object(decider, "availability", side_effect=AssertionError("availability must not be queried")))
        self.infer = self.enterContext(mock.patch.object(decider_runtime, "decide", side_effect=AssertionError("inference must not run")))

    def test_host_judge_false_never_calls_runtime(self):
        payload = {"text": "保存して下さい。", "edits": [proposal("下さい", "ください")], "judge": False}
        result = engine.review_proposals(payload)
        self.assertEqual(result["edits"][0]["after"], "ください")
        self.assertNotIn("decision", result["edits"][0])
        self.available.assert_not_called()
        self.infer.assert_not_called()

    def test_generator_judge_false_preserves_generated_proposals_without_runtime(self):
        text = "保存して下さい。"
        edits, _ = engine.validate_edits(text, [proposal("下さい", "ください")])
        with mock.patch.object(engine, "generate", return_value=(edits, [], [], [engine.MODEL], [])) as generate:
            result = engine.analyze({"text": text, "provider": "gemini", "generate": True, "judge": False})
        generate.assert_called_once()
        self.assertEqual(result["stages"]["generation"], "executed")
        self.assertNotIn("decision", result["edits"][0])
        self.available.assert_not_called()
        self.infer.assert_not_called()

    def test_rules_only_reports_not_run_instead_of_a_connection_claim(self):
        result = engine.analyze({"text": "原文を確認します。", "generate": False})
        self.assertEqual(result["edits"], [])
        self.assertEqual(result["stages"]["decider"], "not_run")
        self.available.assert_not_called()
        self.infer.assert_not_called()

    def test_finalization_assembles_accepted_edits_without_reinference(self):
        payload = {"text": "保存して下さい。", "edits": [proposal("下さい", "ください")], "judge": True}
        original_payload = copy.deepcopy(payload)
        with mock.patch.object(engine, "check_final", return_value={"verified": True}) as check:
            result = engine.finalize_proposals(payload)
        check.assert_called_once_with("保存して下さい。", "保存してください。", "article")
        self.assertTrue(result["verified"])
        self.assertEqual(payload, original_payload)
        self.available.assert_not_called()
        self.infer.assert_not_called()


class HttpBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # app builds tool schemas from profiles at import. Patch status here so
        # import never reads local runtime configuration or queries a service.
        with mock.patch.object(decider_runtime, "status", return_value={**IDENTITY, "status": "ready", "loaded": True}):
            cls.app = importlib.import_module("app")

    def handler(self, path, payload, origin=None):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler = object.__new__(self.app.Handler)
        handler.path = path
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:8896"
        handler.headers["Content-Type"] = "application/json"
        handler.headers["Content-Length"] = str(len(raw))
        if origin is not None:
            handler.headers["Origin"] = origin
        handler.rfile = io.BytesIO(raw)
        handler.server = SimpleNamespace(server_port=8896)
        handler.connection = mock.Mock()
        handler.connection.getsockname.return_value = ("127.0.0.1", 8896)
        if path in ("/api/decider/decide", "/api/decider/stop"):
            _challenge, handler._rpc_session = rpc_auth.issue_challenge("a" * 64, 8896, secret=bytes(range(32)))
            for key, value in rpc_auth.request_headers(handler._rpc_session, "POST", path, raw).items():
                handler.headers[key] = value
        handler.semaphore = threading.BoundedSemaphore(1)
        handler.send = mock.Mock()
        return handler

    def test_raw_decide_uses_local_runtime_even_when_editor_semaphore_is_busy(self):
        request = {"state": "原文", "questions": {"q": {"type": "noul", "instructions": "確認する"}}}
        handler = self.handler("/api/decider/decide", {"request": request})
        handler.semaphore.acquire()
        with mock.patch.object(decider_runtime, "local_decide", return_value=native_response()) as local:
            with mock.patch.object(decider_runtime, "decide", side_effect=AssertionError("recursive service call")) as shared:
                handler.do_POST()
        local.assert_called_once_with(request, timeout=None)
        shared.assert_not_called()
        self.assertEqual(handler.send.call_args.args[0], 200)

    def test_raw_decide_preserves_exact_positive_floating_timeout(self):
        request = {"state": "原文", "questions": {"q": {"type": "noul", "instructions": "確認する"}}}
        handler = self.handler("/api/decider/decide", {"request": request, "timeout": 37.125})
        with mock.patch.object(decider_runtime, "local_decide", return_value=native_response()) as local:
            handler.do_POST()
        local.assert_called_once_with(request, timeout=37.125)
        self.assertEqual(handler.send.call_args.args[0], 200)

    def test_raw_status_uses_local_runtime_without_shared_service_recursion(self):
        handler = self.handler("/api/decider/status", {})
        del handler.headers["Content-Length"]
        with mock.patch.object(decider_runtime, "local_status", return_value={"status": "busy"}) as local:
            with mock.patch.object(decider_runtime, "status", side_effect=AssertionError("recursive status call")) as shared:
                handler.do_GET()
        local.assert_called_once_with()
        shared.assert_not_called()
        handler.send.assert_called_once_with(200, {"status": "busy"})

    def test_raw_malformed_json_and_nonobject_body_do_not_call_runtime(self):
        for payload in (b"{", []):
            with self.subTest(payload=payload):
                handler = self.handler("/api/decider/decide", payload)
                with mock.patch.object(decider_runtime, "local_decide") as local:
                    handler.do_POST()
                self.assertEqual(handler.send.call_args.args[0], 400)
                local.assert_not_called()

    def test_foreign_origin_is_rejected_before_raw_inference(self):
        handler = self.handler("/api/decider/decide", {"request": {}}, "https://example.invalid")
        with mock.patch.object(decider_runtime, "local_decide") as local:
            handler.do_POST()
        self.assertEqual(handler.send.call_args.args[0], 403)
        local.assert_not_called()

    def test_raw_runtime_failure_returns_structured_unavailable_response(self):
        handler = self.handler("/api/decider/decide", {"request": {}})
        error = decider_runtime.DeciderRuntimeError("timeout", "test timeout")
        with mock.patch.object(decider_runtime, "local_decide", side_effect=error):
            handler.do_POST()
        handler.send.assert_called_once_with(503, {"error": error.as_dict()})

    def test_judge_endpoint_forces_judgment_on_host_proposals(self):
        payload = {"text": "保存して下さい。", "edits": [proposal("下さい", "ください")], "judge": False}
        handler = self.handler("/api/judge", payload)
        with mock.patch.object(self.app, "review_proposals", return_value={"stages": {"decider": "executed"}}) as review:
            handler.do_POST()
        review.assert_called_once_with({**payload, "judge": True})
        self.assertEqual(handler.send.call_args.args[0], 200)
        self.assertTrue(handler.semaphore.acquire(blocking=False))


if __name__ == "__main__":
    unittest.main()
