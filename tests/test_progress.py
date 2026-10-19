"""Actual progress events without models, credentials, or external network."""
from contextlib import ExitStack
from email.message import Message
import io
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import decider
import decider_runtime
import engine
import progress


IDENTITY = {"model": "pplx-decider-flash-v1.2", "revision": "fixture", "device": "cpu"}
INFO = {**IDENTITY, "available": True, "max_length": 2048}


def native_response():
    return {**IDENTITY, "result": {"answers": {"meaning_shift": {"type": "choice", "choice": "preserved"}}}}


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = progress.Registry()
        self.enterContext(patch.object(progress, "REGISTRY", self.registry))

    def test_only_bounded_ascii_ids_are_accepted(self):
        for value in ("short", "a" * 101, "日本語の進捗ですよ", "abcdefgh/secret", "abcdefgh\n", "abcdefgh?token=x", True, 12):
            with self.subTest(value=type(value).__name__), self.assertRaises(progress.ProgressError):
                progress.validate_id(value)
        self.assertEqual(progress.validate_id("req_1234-XYZ"), "req_1234-XYZ")
        self.assertIsNone(progress.validate_id(None))

    def test_duplicate_running_id_is_rejected_and_completed_id_can_be_reused(self):
        self.registry.begin("request-123")
        with self.assertRaises(progress.ProgressError) as error:
            self.registry.begin("request-123")
        self.assertEqual(error.exception.status, 409)
        self.registry.finish("request-123")
        self.registry.begin("request-123", "checking")
        self.assertEqual(self.registry.get("request-123")["status"], "running")

    def test_lru_is_bounded_and_polling_does_not_extend_ttl(self):
        tick = [10.0]
        registry = progress.Registry(maximum=2, ttl=5, clock=lambda: 100 + tick[0], monotonic=lambda: tick[0])
        registry.begin("request-a")
        registry.finish("request-a")
        registry.begin("request-b")
        registry.finish("request-b")
        registry.get("request-a")
        registry.begin("request-c")
        self.assertIsNone(registry.get("request-b"))
        self.assertEqual(len(registry._entries), 2)
        tick[0] = 14
        self.assertIsNotNone(registry.get("request-a"))
        tick[0] = 15.1
        self.assertIsNone(registry.get("request-a"))
        self.assertIsNone(registry.get("request-c"))

    def test_default_capacity_cannot_grow_without_bound(self):
        for i in range(140):
            self.registry.begin(f"request-{i:04d}")
        self.assertEqual(len(self.registry._entries), 128)
        self.assertIsNone(self.registry.get("request-0000"))

    def test_completion_preserves_actual_counter_and_unfinished_message(self):
        with progress.operation("request-partial"):
            progress.report("decider", completed=1, total=3, message="decider_unfinished")
        result = self.registry.get("request-partial")
        self.assertEqual((result["status"], result["stage"], result["completed"], result["total"]), ("completed", "completed", 1, 3))
        self.assertEqual(result["message"], progress.MESSAGES["decider_unfinished"])

    def test_failure_keeps_stage_but_never_exception_text(self):
        with self.assertRaisesRegex(RuntimeError, "private"):
            with progress.operation("request-failed"):
                progress.report("generation")
                raise RuntimeError("private manuscript and token")
        result = self.registry.get("request-failed")
        self.assertEqual((result["status"], result["stage"]), ("failed", "generation"))
        self.assertNotIn("private", json.dumps(result))
        progress.report("checking")
        self.assertEqual(self.registry.get("request-failed"), result)

    def test_report_without_http_context_does_nothing(self):
        with patch.object(self.registry, "update") as update:
            progress.report("generation")
        update.assert_not_called()

    def test_retry_attempt_is_real_generation_metadata_not_a_completion_percentage(self):
        with progress.operation("request-retrying"):
            progress.report("generation", message="generation_retry_wait", attempt=2, max_attempts=3)
            state = self.registry.get("request-retrying")
            self.assertEqual((state["attempt"], state["max_attempts"]), (2, 3))
            self.assertNotIn("completed", state)
            self.assertNotIn("total", state)
            progress.report("decider", completed=0, total=4)
            state = self.registry.get("request-retrying")
            self.assertNotIn("attempt", state)
            self.assertNotIn("max_attempts", state)

    def test_invalid_retry_attempt_metadata_is_not_stored(self):
        self.registry.begin("request-bad-retry")
        for fields in ({"attempt": True, "max_attempts": 3}, {"attempt": 4, "max_attempts": 3},
                       {"attempt": 1}, {"max_attempts": 3}, {"attempt": 1, "max_attempts": 4}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.registry.update("request-bad-retry", "generation", **fields)
        with self.assertRaises(ValueError):
            self.registry.update("request-bad-retry", "decider", attempt=2, max_attempts=3)

    def test_arbitrary_messages_and_bad_counts_are_not_stored(self):
        self.registry.begin("request-bounds")
        for fields in ({"message": "private manuscript"}, {"completed": True}, {"completed": 2, "total": 1}, {"total": 1000001}):
            with self.subTest(fields=list(fields)), self.assertRaises(ValueError):
                self.registry.update("request-bounds", "decider", **fields)
        snapshot = self.registry.get("request-bounds")
        snapshot["message"] = "private"
        self.assertNotIn("private", json.dumps(self.registry.get("request-bounds")))

    def test_contexts_are_isolated_between_http_threads(self):
        barrier = threading.Barrier(2)
        errors = []
        def worker(request_id, stage):
            try:
                with progress.operation(request_id):
                    progress.report(stage)
                    barrier.wait(timeout=3)
                    self.assertEqual(self.registry.get(request_id)["stage"], stage)
            except BaseException as error:
                errors.append(error)
        threads = [threading.Thread(target=worker, args=("request-thread-a", "generation")),
                   threading.Thread(target=worker, args=("request-thread-b", "checking"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(4)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(self.registry.get("request-thread-a")["status"], "completed")
        self.assertEqual(self.registry.get("request-thread-b")["status"], "completed")


class EngineProgressTests(unittest.TestCase):
    def setUp(self):
        self.registry = progress.Registry()
        self.enterContext(patch.object(progress, "REGISTRY", self.registry))
        self.enterContext(patch.object(engine, "lint", return_value=[]))

    def proposals(self):
        text = "保存して下さい。上限は二件です。"
        edits, _ = engine.validate_edits(text, [
            {"before": "下さい", "after": "ください", "reason": "表記", "rule": "表記"},
            {"before": "二件", "after": "2件", "reason": "表記", "rule": "表記"},
        ])
        return text, edits

    def test_stage_changes_follow_actual_rules_generation_and_runtime_attempts(self):
        text, edits = self.proposals()
        observed = []
        def record(stage):
            state = self.registry.get("request-stages")
            self.assertEqual(state["stage"], stage)
            observed.append((stage, state.get("completed")))
        def lint(*_):
            record("rules")
            return []
        def generate(*_, **__):
            record("generation")
            return edits, [], [], ["fixture"], []
        def decide(*_, **__):
            record("decider")
            return native_response()
        with patch.object(engine, "lint", side_effect=lint), patch.object(engine, "generate", side_effect=generate), \
             patch.object(decider, "availability", return_value=INFO), patch.object(decider_runtime, "decide", side_effect=decide):
            with progress.operation("request-stages"):
                result = engine.analyze({"text": text, "provider": "gemini", "model": "gemini-3.8-flash-high", "generate": True, "judge": True})
        self.assertEqual(observed, [("rules", None), ("generation", None), ("decider", 0), ("decider", 1)])
        self.assertEqual(result["decider"]["executed"], 2)
        final = self.registry.get("request-stages")
        self.assertEqual((final["completed"], final["total"], final["status"]), (2, 2, "completed"))

    def test_generation_off_and_judge_off_do_not_create_unused_model_stages(self):
        with patch.object(engine, "generate") as generate, patch.object(decider, "availability") as available:
            with progress.operation("request-rules-only"):
                engine.analyze({"text": "原文です。", "generate": False, "judge": True})
                self.assertEqual(self.registry.get("request-rules-only")["stage"], "rules")
            generate.assert_not_called()
            available.assert_not_called()
        text, edits = self.proposals()
        with patch.object(engine, "generate", return_value=(edits, [], [], ["fixture"], [])), \
             patch.object(decider, "availability") as available:
            with progress.operation("request-generation-only"):
                engine.analyze({"text": text, "provider": "gemini", "generate": True, "judge": False})
                self.assertEqual(self.registry.get("request-generation-only")["stage"], "generation")
            available.assert_not_called()

    def test_one_runtime_error_does_not_count_skipped_candidates_as_checked(self):
        text, edits = self.proposals()
        output = {"edits": edits, "profile": "article", "stages": {}, "notes": []}
        with patch.object(decider, "availability", return_value=INFO), \
             patch.object(decider_runtime, "decide", side_effect=decider_runtime.DeciderRuntimeError("fixture", "private text")) as decide:
            with progress.operation("request-partial-model"):
                decider.attach(output, text)
        decide.assert_called_once()
        final = self.registry.get("request-partial-model")
        self.assertEqual((final["completed"], final["total"]), (1, 2))
        self.assertEqual(final["message"], progress.MESSAGES["decider_unfinished"])
        self.assertEqual(output["edits"][1]["decision"]["status"], "not_run")
        self.assertNotIn("private", json.dumps(final))

    def test_deadline_before_first_attempt_keeps_zero_actual_attempts(self):
        text, edits = self.proposals()
        output = {"edits": edits, "profile": "article", "stages": {}, "notes": []}
        with patch.object(decider, "availability", return_value=INFO), \
             patch.object(decider.time, "monotonic", side_effect=[0, 481, 482]), patch.object(decider_runtime, "decide") as decide:
            with progress.operation("request-deadline"):
                decider.attach(output, text)
        decide.assert_not_called()
        final = self.registry.get("request-deadline")
        self.assertEqual((final["completed"], final["total"]), (0, 2))

    def test_export_check_and_verify_each_advance_only_after_success(self):
        states = []
        final = "原文です。"
        def checked(*_, **__):
            state = self.registry.get("request-export")
            states.append((state["stage"], state["completed"], state["total"]))
            payload = {"format": "yomiyasu-check-result", "status": "executed", "run_id": "fixture"}
            if len(states) == 2:
                payload["final"] = {"sha256": engine.digest(final)}
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload))
        with patch.object(engine.subprocess, "run", side_effect=checked):
            with progress.operation("request-export", "checking"):
                engine.check_final(final, final)
        self.assertEqual(states, [("checking", 0, 2), ("checking", 1, 2)])
        self.assertEqual(self.registry.get("request-export")["completed"], 2)


class HttpProgressTests(unittest.TestCase):
    def setUp(self):
        self.registry = progress.Registry()
        self.enterContext(patch.object(progress, "REGISTRY", self.registry))

    def handler(self, route="/api/analyze", body=None, request_id="request-http", origin="http://127.0.0.1:8896"):
        handler = object.__new__(app.Handler)
        handler.path = route
        handler.headers = Message()
        raw = body if isinstance(body, bytes) else json.dumps(body or {"text": "原文です。"}).encode()
        for key, value in {"Host": "127.0.0.1:8896", "Content-Type": "application/json", "Content-Length": str(len(raw)), "Origin": origin}.items():
            if value is not None:
                handler.headers[key] = value
        if request_id is not None:
            handler.headers["X-Yebisu-Request-Id"] = request_id
        handler.rfile = io.BytesIO(raw)
        handler.server = SimpleNamespace(server_port=8896, closing=False)
        handler.semaphore = threading.BoundedSemaphore(1)
        handler.send = Mock()
        return handler

    def test_tracked_http_result_finishes_before_response_and_get_returns_snapshot(self):
        handler = self.handler()
        def analyze(_):
            self.assertEqual(self.registry.get("request-http")["status"], "running")
            progress.report("generation")
            return {"ok": True}
        with patch.object(app, "analyze", side_effect=analyze):
            handler.do_POST()
        self.assertEqual(handler.send.call_args.args, (200, {"ok": True}))
        self.assertEqual(self.registry.get("request-http")["status"], "completed")
        poll = self.handler("/api/progress/request-http")
        del poll.headers["Content-Length"]
        poll.do_GET()
        self.assertEqual(poll.send.call_args.args, (200, self.registry.get("request-http")))

    def test_invalid_id_duplicate_and_foreign_origin_never_execute_engine(self):
        self.registry.begin("request-duplicate")
        cases = [(self.handler(request_id="bad"), 400), (self.handler(request_id="request-duplicate"), 409),
                 (self.handler(request_id="request-foreign", origin="https://other.invalid"), 403)]
        with patch.object(app, "analyze") as analyze:
            for handler, status in cases:
                handler.do_POST()
                self.assertEqual(handler.send.call_args.args[0], status)
        analyze.assert_not_called()
        self.assertIsNone(self.registry.get("request-foreign"))

    def test_failed_operation_and_bad_json_are_recorded_without_error_details(self):
        handler = self.handler(request_id="request-private-failure")
        with patch.object(app, "analyze", side_effect=RuntimeError("private account and token")):
            handler.do_POST()
        self.assertEqual(handler.send.call_args.args[0], 500)
        failed = self.registry.get("request-private-failure")
        self.assertEqual(failed["status"], "failed")
        self.assertNotIn("private", json.dumps(failed))
        malformed = self.handler(body=b"{", request_id="request-json-error")
        malformed.do_POST()
        self.assertEqual(malformed.send.call_args.args[0], 400)
        self.assertEqual(self.registry.get("request-json-error")["status"], "failed")

    def test_get_invalid_unknown_and_nonloopback_host_are_rejected(self):
        for route, status in (("/api/progress/invalid", 400), ("/api/progress/request-missing", 404)):
            handler = self.handler(route)
            del handler.headers["Content-Length"]
            handler.do_GET()
            self.assertEqual(handler.send.call_args.args[0], status)
        handler = self.handler("/api/progress/request-http")
        handler.headers.replace_header("Host", "other.invalid")
        handler.do_GET()
        self.assertEqual(handler.send.call_args.args[0], 403)


if __name__ == "__main__":
    unittest.main()
