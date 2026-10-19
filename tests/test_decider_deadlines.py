"""Real lock timing and lightweight worker processes; no model dependencies."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import decider_runtime as runtime_module

WORKER = r'''
import argparse, json, pathlib, sys, time
parser = argparse.ArgumentParser()
parser.add_argument('--config')
cfg = json.loads(pathlib.Path(parser.parse_args().config).read_text('utf-8'))
identity = {'model': 'pplx-decider-flash-v1.2', 'revision': '6d771ad7b5ecff5e93b8fc7b90d4c5ccc341954d', 'device': 'cpu'}
time.sleep(cfg.get('startup_delay', 0))
print(json.dumps({'event': 'ready', 'ok': True, **identity}), flush=True)
if cfg.get('block_stdin'):
    time.sleep(10)
for line in sys.stdin:
    envelope = json.loads(line)
    with pathlib.Path(cfg['received']).open('a') as output:
        output.write('received\n')
    time.sleep(cfg.get('inference_delay', 0))
    print(json.dumps({'id': envelope['id'], 'ok': True, **identity, 'result': {'answer': 'ok'}}), flush=True)
'''


class DeciderDeadlineTests(unittest.TestCase):
    def worker(self, *, startup_delay=0, inference_delay=0, block_stdin=False, startup_timeout=300, request_timeout=180):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        directory = Path(temporary)
        worker = directory / "fixture_worker.py"
        worker.write_text(WORKER, encoding="utf-8")
        received = directory / "received.txt"
        configuration = directory / "config.json"
        cfg = {"python": sys.executable, "device": "cpu", "cpu_dtype": "float32", "startup_timeout_seconds": startup_timeout,
               "request_timeout_seconds": request_timeout, "startup_delay": startup_delay, "inference_delay": inference_delay,
               "block_stdin": block_stdin, "received": str(received)}
        configuration.write_text(json.dumps(cfg), encoding="utf-8")
        self.enterContext(mock.patch.object(runtime_module, "WORKER_PATH", worker))
        self.enterContext(mock.patch.object(runtime_module, "_configuration", return_value=cfg))
        runtime = runtime_module.DeciderRuntime(configuration)
        self.addCleanup(runtime.stop)
        return runtime, received

    def test_lock_timeout_is_bounded_recoverable_and_cannot_touch_the_current_worker(self):
        runtime = runtime_module.DeciderRuntime(ROOT / "tests/unused-deadline-config.json")
        current = mock.Mock()
        runtime._process, runtime._ready = current, {"event": "ready"}
        runtime._phase = "busy"
        original_status = runtime._last_status
        held, release = threading.Event(), threading.Event()

        def occupy():
            with runtime._lock:
                held.set()
                release.wait(2)

        thread = threading.Thread(target=occupy, daemon=True)
        thread.start()
        self.assertTrue(held.wait(1))
        try:
            with mock.patch.object(runtime_module, "_configuration") as configuration, mock.patch.object(runtime, "_start") as start, \
                    mock.patch.object(runtime, "stop") as stop:
                begun = time.monotonic()
                with self.assertRaises(runtime_module.DeciderRuntimeError) as raised:
                    runtime.decide({"state": "queued"}, timeout=.06)
                elapsed = time.monotonic() - begun
                self.assertEqual(raised.exception.code, "timeout")
                self.assertTrue(raised.exception.recoverable)
                self.assertLess(elapsed, .5)
                configuration.assert_not_called()
                start.assert_not_called()
                stop.assert_not_called()
            self.assertIs(runtime._process, current)
            self.assertEqual(runtime._phase, "busy")
            self.assertIs(runtime._last_status, original_status)
            self.assertIsNone(runtime._last_error)
        finally:
            release.set()
            thread.join(1)

    def test_timeout_without_explicit_override_still_bounds_lock_waiting(self):
        runtime, _received = self.worker(startup_timeout=.03, request_timeout=.03)
        held, release = threading.Event(), threading.Event()

        def occupy():
            with runtime._lock:
                held.set()
                release.wait(1)

        thread = threading.Thread(target=occupy, daemon=True)
        thread.start()
        self.assertTrue(held.wait(1))
        try:
            begun = time.monotonic()
            with self.assertRaises(runtime_module.DeciderRuntimeError) as raised:
                runtime.decide({"state": "queued"})
            self.assertTrue(raised.exception.recoverable)
            self.assertLess(time.monotonic() - begun, .5)
        finally:
            release.set()
            thread.join(1)

    def test_slow_startup_obeys_total_deadline_and_never_receives_the_manuscript(self):
        runtime, received = self.worker(startup_delay=2)
        begun = time.monotonic()
        with self.assertRaises(runtime_module.DeciderRuntimeError) as raised:
            runtime.decide({"state": "must not be sent"}, timeout=.15)
        elapsed = time.monotonic() - begun
        self.assertEqual(raised.exception.code, "timeout")
        # Process-tree cleanup may exceed the inference budget, but startup's
        # configured 300-second wait must not be used after the .15s deadline.
        self.assertLess(elapsed, 1.5)
        self.assertFalse(received.exists())
        self.assertIsNone(runtime._process)

    def test_second_short_request_does_not_cancel_the_first_request_or_start_another_worker(self):
        runtime, received = self.worker(startup_delay=.15, inference_delay=.15)
        launched = threading.Event()
        popen = runtime_module.subprocess.Popen

        def launch(*args, **kwargs):
            process = popen(*args, **kwargs)
            launched.set()
            return process

        with mock.patch.object(runtime_module.subprocess, "Popen", side_effect=launch) as start, ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(runtime.decide, {"state": "first"}, timeout=2)
            self.assertTrue(launched.wait(1))
            begun = time.monotonic()
            with self.assertRaises(runtime_module.DeciderRuntimeError) as raised:
                runtime.decide({"state": "second"}, timeout=.05)
            self.assertTrue(raised.exception.recoverable)
            self.assertLess(time.monotonic() - begun, .5)
            self.assertTrue(first.result(timeout=2)["ok"])
            self.assertEqual(start.call_count, 1)
        self.assertEqual(received.read_text().splitlines(), ["received"])
        self.assertEqual(runtime._phase, "ready")

    def test_startup_and_inference_consume_one_shared_budget(self):
        runtime, received = self.worker(startup_delay=.1, inference_delay=2)
        begun = time.monotonic()
        with self.assertRaises(runtime_module.DeciderRuntimeError) as raised:
            runtime.decide({"state": "started inference"}, timeout=.4)
        self.assertEqual(raised.exception.code, "timeout")
        self.assertLess(time.monotonic() - begun, 1.5)
        self.assertTrue(received.exists())
        self.assertIsNone(runtime._process)

    def test_expired_configuration_read_cannot_start_or_send_new_work(self):
        runtime, _received = self.worker()
        cfg = runtime_module._configuration.return_value

        def slow_configuration(_path):
            time.sleep(.06)
            return cfg

        with mock.patch.object(runtime_module, "_configuration", side_effect=slow_configuration), \
                mock.patch.object(runtime, "_start") as start, mock.patch.object(runtime_module.subprocess, "Popen") as launch:
            with self.assertRaises(runtime_module.DeciderRuntimeError) as raised:
                runtime.decide({"state": "not launched"}, timeout=.02)
            self.assertEqual(raised.exception.code, "timeout")
            start.assert_not_called()
            launch.assert_not_called()

    def test_expired_dll_lock_setup_cannot_launch_a_new_worker(self):
        runtime, _received = self.worker()
        cfg = runtime_module._configuration.return_value

        @contextmanager
        def delayed_dll_setup():
            time.sleep(.05)
            yield

        with mock.patch.object(runtime_module, "_external_dll_search", delayed_dll_setup), \
                mock.patch.object(runtime_module.subprocess, "Popen") as launch:
            with self.assertRaises(runtime_module.DeciderRuntimeError) as raised:
                runtime._start(cfg, deadline=time.monotonic() + .02)
            self.assertEqual(raised.exception.code, "timeout")
            launch.assert_not_called()

    def test_writer_scheduled_after_deadline_cannot_send_new_inference(self):
        runtime = runtime_module.DeciderRuntime(ROOT / "tests/unused-writer-config.json")
        runtime._process = mock.Mock()

        def delayed_thread(*, target, **_kwargs):
            def start():
                time.sleep(.05)
                target()
            return SimpleNamespace(start=start)

        with mock.patch.object(runtime_module.threading, "Thread", side_effect=delayed_thread):
            with self.assertRaises(runtime_module.DeciderRuntimeError) as raised:
                runtime._send("must not be sent", time.monotonic() + .02)
            self.assertEqual(raised.exception.code, "timeout")
        runtime._process.stdin.write.assert_not_called()
        runtime._process.stdin.flush.assert_not_called()

    def test_blocked_worker_stdin_cannot_wait_forever_past_the_deadline(self):
        runtime, received = self.worker(block_stdin=True)
        begun = time.monotonic()
        with self.assertRaises(runtime_module.DeciderRuntimeError) as raised:
            runtime.decide({"state": "a" * 200_000}, timeout=.25)
        self.assertEqual(raised.exception.code, "timeout")
        self.assertLess(time.monotonic() - begun, 1.5)
        self.assertFalse(received.exists())
        self.assertIsNone(runtime._process)

    def test_a_normal_worker_reuses_one_process_for_consecutive_in_budget_requests(self):
        runtime, received = self.worker()
        self.assertTrue(runtime.decide({"state": "one"}, timeout=2)["ok"])
        process = runtime._process
        self.assertTrue(runtime.decide({"state": "two"}, timeout=2)["ok"])
        self.assertIs(runtime._process, process)
        self.assertEqual(received.read_text().splitlines(), ["received", "received"])


if __name__ == "__main__":
    unittest.main()
