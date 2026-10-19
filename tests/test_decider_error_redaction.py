"""Untrusted worker and shared-service diagnostics never expose private text."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import decider_errors
import decider_runtime

PRIVATE = "PRIVATE_KEY_abc123 /home/alice/private-model/weights.safetensors C:\\Users\\alice\\secrets --api-key=private"
IDENTITY = {"model": decider_runtime.MODEL, "revision": decider_runtime.REVISION, "device": "cpu"}


class ErrorRedactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("redaction_fixture_worker", ROOT / "tools/decider_worker.py")
        cls.worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.worker)

    def assert_private_absent(self, value):
        text = json.dumps(value, ensure_ascii=False)
        for fragment in ("PRIVATE_KEY", "/home/alice", "alice", "--api-key", "weights.safetensors", "private-model"):
            self.assertNotIn(fragment, text)

    def test_exception_constructor_and_as_dict_allowlist_code_message_details_and_recoverability(self):
        error = decider_runtime.DeciderRuntimeError(PRIVATE, PRIVATE, {"exception_type": PRIVATE, "path": PRIVATE}, recoverable=True)
        self.assertEqual(error.code, "runtime_error")
        self.assertFalse(error.recoverable)
        self.assert_private_absent({"exception": str(error), "message": error.message, "body": error.as_dict()})
        # Defense at serialization still works if an upstream object was changed.
        error.code, error.message, error.details = PRIVATE, PRIVATE, {"exception_type": PRIVATE, "argv": PRIVATE}
        self.assertEqual(error.as_dict()["code"], "runtime_error")
        self.assert_private_absent(error.as_dict())

    def test_public_details_preserve_known_resource_numbers_but_no_arbitrary_values(self):
        safe = decider_errors.public_error({"code": "insufficient_memory", "message": PRIVATE, "details": {
            "ram_available_bytes": 123, "ram_required_bytes": 11 * 1024**3, "commit_available_bytes": 0,
            "cuda_available_bytes": True, "cuda_required_bytes": PRIVATE, "cuda_total_bytes": float("inf"),
            "max_length": 2048, "exception_type": "PermissionError", "path": PRIVATE, "argv": [PRIVATE],
            "ram_check": "vm_stat_free_inactive_speculative_estimate", "commit_check": "not_available_on_macos"}})
        self.assertEqual(safe["details"], {"ram_available_bytes": 123, "ram_required_bytes": 11 * 1024**3,
            "commit_available_bytes": 0, "max_length": 2048, "exception_type": "PermissionError",
            "ram_check": "vm_stat_free_inactive_speculative_estimate", "commit_check": "not_available_on_macos"})
        self.assert_private_absent(safe)

    def test_malformed_error_fields_and_extreme_numbers_cannot_escape_or_break_sanitization(self):
        for error in (None, [], PRIVATE, {"code": [PRIVATE]}, {"code": {"path": PRIVATE}},
                      {"code": "startup_error", "details": [PRIVATE]},
                      {"code": "startup_error", "details": {"ram_available_bytes": 10**10000,
                           "ram_required_bytes": -1, "max_length": True, "exception_type": PRIVATE, "return_code": 10**10000}}):
            with self.subTest(kind=type(error).__name__):
                safe = decider_errors.public_error(error)
                self.assertIn(safe["code"], decider_errors.MESSAGES)
                self.assertEqual(safe["details"], {})
                self.assert_private_absent(safe)

    def test_known_recoverable_errors_keep_their_recovery_contract(self):
        service = {"url": "http://127.0.0.1:1", "timeout": 1, "device": "cpu", "cpu_dtype": "float32"}
        for code in ("request_too_long", "invalid_request", "cuda_out_of_memory", "timeout"):
            with self.subTest(code=code):
                error = decider_runtime.DeciderRuntimeError(code, PRIVATE, {"exception_type": "LengthError"}, recoverable=True)
                self.assertTrue(error.recoverable)
                self.assertEqual(error.as_dict()["code"], code)
                self.assert_private_absent(error.as_dict())
            for recoverable in (True, False):
                for status in (503, 200):
                    output = {**IDENTITY, "ok": False, "error": {"code": code, "message": PRIVATE, "recoverable": recoverable}}
                    with self.subTest(code=code, recoverable=recoverable, status=status), \
                            mock.patch.object(decider_runtime, "_service_configuration", return_value=service), \
                            mock.patch.object(decider_runtime.rpc_auth, "authenticated_request", return_value=(status, json.dumps(output).encode("utf-8"))):
                        # Exercise _remote and the public decide entry point.
                        # A queued timeout remains retryable, while a timeout
                        # after inference started must remain nonrecoverable.
                        with self.assertRaises(decider_runtime.DeciderRuntimeError) as raised:
                            decider_runtime.decide({"state": "source"})
                        self.assertEqual(raised.exception.code, code)
                        self.assertIs(raised.exception.recoverable, recoverable)
                        self.assertIs(raised.exception.as_dict()["recoverable"], recoverable)
                        self.assert_private_absent(raised.exception.as_dict())

    def test_worker_startup_and_request_classification_never_use_exception_strings(self):
        for error in (OSError(PRIVATE), PermissionError(PRIVATE),
                      self.worker.WorkerError("insufficient_memory", PRIVATE, {"ram_available_bytes": 123, "path": PRIVATE}),
                      self.worker.WorkerError(PRIVATE, PRIVATE, {"exception_type": PRIVATE})):
            with self.subTest(kind=type(error).__name__):
                safe = self.worker.startup_error(error)
                self.assert_private_absent(safe)
        request_error, length_error = type("RequestError", (Exception,), {}), type("LengthError", (Exception,), {})
        for error in (request_error(PRIVATE), length_error(PRIVATE), RuntimeError(PRIVATE),
                      self.worker.WorkerError(PRIVATE, PRIVATE, {"path": PRIVATE})):
            safe = self.worker.classify_request_error(error, request_error, length_error, object(), {"device": "cpu", "max_length": 2048})
            self.assert_private_absent(safe)

    def test_actual_worker_protocol_redacts_missing_private_config_path_before_any_model_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "alice-private-model" / "PRIVATE_KEY_config.json"
            result = subprocess.run([sys.executable, "-B", str(ROOT / "tools/decider_worker.py"), "--config", str(path)],
                                    capture_output=True, text=True, encoding="utf-8", timeout=5)
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["event"], "startup_error")
        self.assertEqual(payload["error"]["code"], "startup_error")
        self.assertEqual(payload["error"]["details"], {"exception_type": "FileNotFoundError"})
        self.assert_private_absent(payload)

    def test_runtime_rejects_raw_startup_error_from_an_actual_untrusted_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake_worker = directory / "fake_worker.py"
            envelope = {"event": "startup_error", "ok": False, **IDENTITY,
                        "error": {"code": "startup_error", "message": PRIVATE,
                                  "details": {"exception_type": "OSError", "path": PRIVATE, "argv": [PRIVATE]}}}
            fake_worker.write_text("import time\nprint(" + repr(json.dumps(envelope)) + ", flush=True)\ntime.sleep(5)\n", encoding="utf-8")
            cfg = {"python": sys.executable, "device": "cpu", "cpu_dtype": "float32", "startup_timeout_seconds": 1, "request_timeout_seconds": 1}
            runtime = decider_runtime.DeciderRuntime(directory / "unused-config.json")
            try:
                with mock.patch.object(decider_runtime, "WORKER_PATH", fake_worker), mock.patch.object(decider_runtime, "_configuration", return_value=cfg):
                    with self.assertRaises(decider_runtime.DeciderRuntimeError) as raised:
                        runtime.decide({"state": "unpublished"}, timeout=1)
                self.assertEqual(raised.exception.code, "startup_error")
                self.assertEqual(raised.exception.details, {"exception_type": "OSError"})
                self.assert_private_absent(raised.exception.as_dict())
                self.assert_private_absent(runtime._last_error)
            finally:
                runtime.stop()

    def test_shared_client_redacts_both_signed_error_status_and_error_in_success_envelope(self):
        service = {"url": "http://127.0.0.1:1", "timeout": 1, "device": "cpu", "cpu_dtype": "float32"}
        for status, error in ((503, {"code": "startup_error", "message": PRIVATE, "details": {"exception_type": "OSError", "path": PRIVATE}}),
                              (200, {"code": PRIVATE, "message": PRIVATE, "details": {"exception_type": PRIVATE}}),
                              (200, [PRIVATE]), (200, None)):
            output = {**IDENTITY, "ok": False, "error": error}
            with self.subTest(status=status, error_type=type(error).__name__), \
                    mock.patch.object(decider_runtime, "_service_configuration", return_value=service), \
                    mock.patch.object(decider_runtime.rpc_auth, "authenticated_request", return_value=(status, json.dumps(output).encode("utf-8"))):
                with self.assertRaises(decider_runtime.DeciderRuntimeError) as raised:
                    decider_runtime.decide({"state": "source"})
                self.assert_private_absent(raised.exception.as_dict())


if __name__ == "__main__":
    unittest.main()
