"""Mocked macOS resource parsing, not a macOS/model runtime proof."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("macos_resource_worker", Path(__file__).resolve().parents[1] / "tools/decider_worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


def sample(page_size=16384):
    return f"""Mach Virtual Memory Statistics: (page size of {page_size} bytes)
Pages free:                              100.
Pages active:                            900.
Pages inactive:                          200.
Pages speculative:                        50.
Pages purgeable:                         120.
"""


class MacOSResourceTests(unittest.TestCase):
    def test_uses_header_page_size_and_three_nonoverlapping_counts(self):
        with patch.object(worker.subprocess, "run", return_value=Mock(stdout=sample())) as run:
            self.assertEqual(worker._macos_available_ram(), 350*16384)
            self.assertEqual(run.call_args.args[0], ["/usr/bin/vm_stat"])
            self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_intel_4k_page_size(self):
        with patch.object(worker.subprocess, "run", return_value=Mock(stdout=sample(4096))):
            self.assertEqual(worker._macos_available_ram(), 350*4096)

    def test_missing_counter_fails_closed(self):
        with patch.object(worker.subprocess, "run", return_value=Mock(stdout=sample().replace("Pages speculative:", "Other counter:"))):
            with self.assertRaises(worker.WorkerError) as caught:
                worker._macos_available_ram()
            self.assertEqual(caught.exception.code, "resource_check_failed")

    def test_vm_stat_timeout_fails_closed(self):
        with patch.object(worker.subprocess, "run", side_effect=subprocess.TimeoutExpired("vm_stat", 5)):
            with self.assertRaises(worker.WorkerError):
                worker._macos_available_ram()

    def test_macos_ram_guard_remains_and_commit_is_not_invented(self):
        config = {"device":"cpu", "cpu_dtype":"bfloat16", "max_length":2048}
        with patch.object(worker.sys, "platform", "darwin"), patch.object(worker, "available_ram", return_value=12*worker.GIB), patch.object(worker, "available_commit") as commit:
            resource = worker.preflight(config)
            commit.assert_not_called()
            self.assertEqual(resource["ram_required_bytes"], 11*worker.GIB)
            self.assertEqual(resource["commit_check"], "not_available_on_macos")
            self.assertNotIn("commit_available_bytes", resource)
        with patch.object(worker.sys, "platform", "darwin"), patch.object(worker, "available_ram", return_value=10*worker.GIB):
            with self.assertRaises(worker.WorkerError) as caught:
                worker.preflight(config)
            self.assertEqual(caught.exception.code, "insufficient_memory")

    def test_other_platform_commit_guard_is_preserved(self):
        config = {"device":"cpu", "cpu_dtype":"bfloat16", "max_length":2048}
        with patch.object(worker.sys, "platform", "win32"), patch.object(worker, "available_ram", return_value=12*worker.GIB), patch.object(worker, "available_commit", return_value=10*worker.GIB):
            with self.assertRaises(worker.WorkerError) as caught:
                worker.preflight(config)
            self.assertEqual(caught.exception.code, "insufficient_memory")


if __name__ == "__main__":
    unittest.main()
