"""Setup/preflight contracts with simulated hosts; no installs or model calls."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("setup_decider_under_test", Path(__file__).resolve().parents[1] / "tools/setup_decider.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class SetupDeciderTests(unittest.TestCase):
    def host(self, private, *, total=24, available=11, system="darwin", machine="arm64", macos="14.7"):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(setup, "PERSISTENT_ROOT", private))
        stack.enter_context(patch.object(setup.sys, "platform", system))
        stack.enter_context(patch.object(setup.sys, "version_info", (3, 12, 10)))
        stack.enter_context(patch.object(setup.platform, "machine", return_value=machine))
        stack.enter_context(patch.object(setup.platform, "mac_ver", return_value=(macos, (), "")))
        stack.enter_context(patch.object(setup, "macos_total_ram", return_value=total * setup.worker.GIB))
        stack.enter_context(patch.object(setup.worker, "available_ram", return_value=available * setup.worker.GIB))
        stack.enter_context(patch.object(setup.worker, "available_commit", return_value=40 * setup.worker.GIB))
        stack.enter_context(patch.object(setup.shutil, "which", return_value="/test/uv"))
        return stack

    def report(self, private, *arguments):
        return setup.preflight(setup.parse_arguments(["--preset", "mac24", "--check-only", *arguments]), private / "decider")

    def test_preset_selects_bounded_cpu_settings(self):
        args = setup.parse_arguments(["--preset", "mac24"])
        self.assertEqual((args.device, args.cpu_dtype, args.max_length), ("cpu", "bfloat16", 2048))
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            self.host(private)
            self.assertEqual(self.report(private)["selected"]["cpu_threads"], 8)

    def test_conflicting_preset_flags_rejected_before_preflight(self):
        for flags in (("--device", "cuda:0"), ("--cpu-dtype", "float32"), ("--max-length", "8192")):
            with self.subTest(flags=flags), patch.object(setup, "preflight") as preflight, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    setup.main(["--preset", "mac24", *flags])
                self.assertEqual(caught.exception.code, 2)
                preflight.assert_not_called()

    def test_matching_explicit_flags_are_unambiguous(self):
        args = setup.parse_arguments(["--preset", "mac24", "--device", "cpu", "--cpu-dtype", "bfloat16", "--max-length", "2048"])
        self.assertEqual(args.max_length, 2048)

    def test_generic_cpu_and_cuda_arguments_remain_available(self):
        for flags, expected in (([], ("cpu", "bfloat16", 2048)),
                                (["--device", "cuda:0", "--cpu-dtype", "float32", "--max-length", "8192"], ("cuda:0", "float32", 8192))):
            args = setup.parse_arguments(flags)
            self.assertEqual((args.device, args.cpu_dtype, args.max_length), expected)
            self.assertIsNone(args.preset)

    def test_mac24_boundary_is_setup_readiness_not_runtime_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            self.host(private)
            report = self.report(private)
            self.assertEqual(report["status"], "ready_to_setup")
            self.assertTrue(report["experimental"])
            self.assertEqual(report["resources"]["ram_total_bytes"], 24 * setup.worker.GIB)
            self.assertEqual(report["resources"]["ram_required_bytes"], 11 * setup.worker.GIB)
            self.assertEqual(report["runtime_checks"]["cpu_bfloat16_ops"], "not_run")
            self.assertEqual(report["runtime_checks"]["inference"], "not_run")
            self.assertEqual(report["runtime_checks"]["native_macos"], "unverified")
            self.assertFalse(report["inference_verified"])
            self.assertFalse(report["native_mac_verified"])
            self.assertFalse(report["model_files_present"])

    def test_total_24_does_not_substitute_for_available_11(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            self.host(private, available=10)
            report = self.report(private)
            self.assertEqual(report["status"], "not_ready")
            self.assertEqual(report["resources"]["ram_total_bytes"], 24 * setup.worker.GIB)
            self.assertEqual(report["resources"]["ram_available_bytes"], 10 * setup.worker.GIB)

    def test_below_total_target_is_not_mac24_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            self.host(private, total=16, available=12)
            self.assertEqual(self.report(private)["status"], "not_ready")

    def test_wrong_os_does_not_run_mac_resource_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            self.host(private, system="win32", machine="AMD64", macos="")
            with patch.object(setup, "macos_total_ram") as total, patch.object(setup.worker, "preflight") as resource:
                self.assertEqual(self.report(private)["status"], "not_ready")
                total.assert_not_called()
                resource.assert_not_called()

    def test_rosetta_python_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            self.host(private, machine="x86_64")
            report = self.report(private)
            self.assertEqual(report["status"], "not_ready")
            self.assertTrue(any("arm64 Python" in item for item in report["issues"]))

    def test_mac13_or_unknown_version_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            self.host(private)
            for version in ("13.7", "", "unknown"):
                with self.subTest(version=version), patch.object(setup.platform, "mac_ver", return_value=(version, (), "")):
                    self.assertEqual(self.report(private)["status"], "not_ready")

    def test_missing_prerequisites_not_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            self.host(private)
            with patch.object(setup.shutil, "which", return_value=None), patch.object(setup.sys, "version_info", (3, 11, 9)):
                report = self.report(private)
            self.assertEqual(report["status"], "not_ready")
            self.assertEqual(report["prerequisites"], {"python_3_12_or_later": False, "uv_available": False})

    def test_unknown_total_or_available_ram_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            self.host(private)
            with patch.object(setup, "macos_total_ram", side_effect=ValueError("bad output")):
                self.assertEqual(self.report(private)["status"], "not_ready")
            with patch.object(setup.worker, "available_ram", side_effect=setup.worker.WorkerError("resource_check_failed", "取得失敗")):
                self.assertEqual(self.report(private)["status"], "not_ready")

    def test_unavailable_or_excluded_pinned_python_wheel_is_not_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            self.host(private)
            for version in ((3, 15, 0), (3, 14, 1)):
                with self.subTest(version=version), patch.object(setup.sys, "version_info", version):
                    self.assertEqual(self.report(private)["status"], "not_ready")

    def test_check_only_never_writes_downloads_or_launches_python(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / "nonexistent"
            self.host(private)
            output = io.StringIO()
            with patch.object(setup.subprocess, "run") as run, patch.object(Path, "mkdir") as mkdir, \
                    patch.object(Path, "write_bytes") as write, redirect_stdout(output):
                result = setup.main(["--preset", "mac24", "--check-only"])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "ready_to_setup")
            run.assert_not_called()
            mkdir.assert_not_called()
            write.assert_not_called()
            self.assertFalse(private.exists())

    def test_failed_preset_never_creates_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / "nonexistent"
            self.host(private, available=10)
            with patch.object(setup.subprocess, "run") as run, redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    setup.main(["--preset", "mac24"])
            run.assert_not_called()
            self.assertFalse(private.exists())

    def test_check_only_failure_exit_code_and_json(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            self.host(private, system="win32")
            output = io.StringIO()
            with redirect_stdout(output):
                result = setup.main(["--preset", "mac24", "--check-only"])
            self.assertEqual(result, 2)
            self.assertEqual(json.loads(output.getvalue())["status"], "not_ready")

    def test_dependency_metadata_is_read_without_importing_or_executing_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            packages = private / ".venv/lib/python3.12/site-packages"
            for name, version in setup.PINNED_DEPENDENCIES.items():
                metadata = packages / f"{name.replace('-', '_')}-{version}.dist-info/METADATA"
                metadata.parent.mkdir(parents=True)
                metadata.write_text(f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n\n", encoding="utf-8")
            with patch.object(setup.subprocess, "run") as run:
                snapshot = setup.dependency_snapshot(private)
            self.assertTrue(snapshot["versions_match"])
            self.assertEqual(snapshot["installed"], setup.PINNED_DEPENDENCIES)
            self.assertEqual(snapshot["package_imports"], "not_run")
            run.assert_not_called()

    def test_existing_configuration_bytes_are_preserved_in_setup_and_check_only(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            self.host(private)
            config = private / "decider/config.json"
            config.parent.mkdir()
            original = b'{"device":"cuda:0","custom":"keep"}\r\n'
            config.write_bytes(original)
            with patch.object(setup.subprocess, "run") as run, redirect_stdout(io.StringIO()):
                setup.main(["--preset", "mac24"])
            self.assertEqual(config.read_bytes(), original)
            self.assertEqual(run.call_count, 3)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(commands[0][1], "venv")
            self.assertTrue(commands[1][-1].endswith("download_decider.py"))
            self.assertEqual(commands[2][1:3], ["pip", "install"])
            self.assertEqual(self.report(private)["existing_config"], "preserved_not_reconfigured")
            self.assertEqual(config.read_bytes(), original)

    def test_fresh_preset_configuration_is_explicit_and_cpu_only(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            self.host(private)
            with patch.object(setup.subprocess, "run"), redirect_stdout(io.StringIO()):
                setup.main(["--preset", "mac24", "--port", "8910"])
            config = json.loads((private / "decider/config.json").read_text("utf-8"))
            self.assertEqual((config["device"], config["cpu_dtype"], config["max_length"], config["cpu_threads"]), ("cpu", "bfloat16", 2048, 8))
            self.assertEqual(config["service_url"], "http://127.0.0.1:8910")
            self.assertEqual(config["revision"], setup.REVISION)

    def test_sysctl_has_fixed_read_only_command_and_timeout(self):
        with patch.object(setup.subprocess, "run", return_value=Mock(stdout=str(24 * setup.worker.GIB) + "\n")) as run:
            self.assertEqual(setup.macos_total_ram(), 24 * setup.worker.GIB)
            self.assertEqual(run.call_args.args[0], ["/usr/sbin/sysctl", "-n", "hw.memsize"])
            self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_sysctl_malformed_output_rejected(self):
        for text in ("", "-1", "24GB", "1" * 33):
            with self.subTest(text=text), patch.object(setup.subprocess, "run", return_value=Mock(stdout=text)):
                with self.assertRaises(ValueError):
                    setup.macos_total_ram()


if __name__ == "__main__":
    unittest.main()
