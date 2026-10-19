"""Lifecycle and release-boundary checks without GUI/model/HTTP requests."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import desktop

spec = importlib.util.spec_from_file_location("build_release", ROOT / "tools/build_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class DesktopLifecycleTests(unittest.TestCase):
    def setUp(self):
        record = patch.object(desktop.ServiceSession, "_record")
        marker = patch.object(desktop.ServiceSession, "read_stop_marker", return_value="initial-marker")
        record.start()
        marker.start()
        self.addCleanup(record.stop)
        self.addCleanup(marker.stop)

    def test_existing_service_is_never_stopped_by_nonowner(self):
        with patch.object(desktop, "service_identity", return_value={"application": desktop.APPLICATION}), patch("decider_runtime.local_stop") as stop:
            session = desktop.ServiceSession().start()
            self.assertFalse(session.owned)
            session.close()
            stop.assert_not_called()

    def test_owned_service_closes_server_after_stop_failure(self):
        session = desktop.ServiceSession()
        session.server, session.thread, session.owned = Mock(), Mock(), True
        with patch("decider_runtime.local_stop", side_effect=RuntimeError("owned worker failed")):
            with self.assertRaises(RuntimeError):
                session.close()
        session.server.shutdown.assert_called_once()
        session.server.server_close.assert_called_once()
        self.assertFalse(session.owned)

    def test_foreign_identity_is_rejected(self):
        response = Mock()
        response.read.return_value = b'{"application":"other","protocol_version":1}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(desktop.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(desktop.DesktopError):
                desktop.service_identity()

    def test_owned_close_keeps_http_until_existing_work_finishes(self):
        session = desktop.ServiceSession()
        session.server, session.thread, session.owned = Mock(), Mock(), True
        calls = []
        semaphore = session.server.RequestHandlerClass.semaphore
        semaphore.acquire.side_effect = lambda: calls.append("wait")
        semaphore.release.side_effect = lambda: calls.append("release")
        session.server.shutdown.side_effect = lambda: calls.append("shutdown")
        session.server.server_close.side_effect = lambda: calls.append("close")
        with patch("decider_runtime.local_stop", side_effect=lambda: calls.append("stop-model")):
            session.close()
        self.assertTrue(session.server.closing)
        self.assertEqual(calls, ["wait", "stop-model", "shutdown", "close", "release"])

    def test_window_settings_do_not_replay_paths_or_huge_sizes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "desktop.json"
            path.write_text(json.dumps({"width": 99999, "height": 900, "url": "https://other.example"}), "utf-8")
            self.assertEqual(desktop.read_window_settings(path), {"width": 1280, "height": 900})
            path.write_text('{"width":1000,"height":750,"url":"https://other.example"}', "utf-8")
            self.assertEqual(desktop.read_window_settings(path), {"width": 1000, "height": 750})

    def test_another_data_directory_is_never_reused(self):
        response = Mock()
        response.read.return_value = json.dumps({"application":desktop.APPLICATION, "protocol_version":1, "data_id":"different-root"}).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(desktop.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(desktop.DesktopError, "保存先"):
                desktop.service_identity()

    def test_missing_shared_service_recovers_as_owned(self):
        session = desktop.ServiceSession()
        def start_owned():
            session.owned = True
        with patch.object(desktop, "service_identity", return_value=None), patch.object(session, "_start_unlocked", side_effect=start_owned) as start:
            self.assertEqual(session.recover_once(), "recovered")
            self.assertTrue(session.owned)
            start.assert_called_once()

    def test_explicit_stop_marker_blocks_recovery(self):
        session = desktop.ServiceSession()
        with patch.object(session, "read_stop_marker", return_value="explicit-stop"), patch.object(session, "_start_unlocked") as start, patch.object(desktop, "service_identity") as probe:
            self.assertEqual(session.recover_once(), "intentional_stop")
            self.assertTrue(session.recovery_disabled)
            self.assertEqual(session.recover_once(), "disabled")
            start.assert_not_called()
            probe.assert_not_called()

    def test_foreign_listener_is_not_replaced_on_recovery(self):
        session = desktop.ServiceSession()
        with patch.object(desktop, "service_identity", side_effect=desktop.DesktopError("foreign")), patch.object(session, "_start_unlocked") as start:
            self.assertEqual(session.recover_once(), "unavailable")
            start.assert_not_called()

    def test_reconnection_does_not_navigate_a_live_draft(self):
        window = Mock()
        window.events.loaded.is_set.return_value = True
        window.evaluate_js.return_value = True
        desktop.reconnect_page(window)
        window.load_url.assert_not_called()
        self.assertIn("yebisu-service-reconnected", window.evaluate_js.call_args.args[0])

    def test_reconnection_reloads_only_an_error_or_blank_page(self):
        window = Mock()
        window.events.loaded.is_set.return_value = False
        desktop.reconnect_page(window)
        window.evaluate_js.assert_not_called()
        window.load_url.assert_called_once_with(desktop.URL)


class ReleaseBoundaryTests(unittest.TestCase):
    def test_private_and_model_paths_are_rejected(self):
        for path in (".local/config.json", "Models/checkpoint.bin", "tools/__pycache__/app.pyc", "draft.log", ".env.local", "model.safetensors"):
            self.assertFalse(release.permitted(Path(path)), path)
        self.assertTrue(release.permitted(Path("vendor/yomiyasu/LICENSE")))

    def test_binary_tree_refuses_a_private_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("YomiyasuStudio.exe", "YomiyasuStudioCLI.exe", ".env"):
                (root / name).write_bytes(b"synthetic")
            with self.assertRaises(ValueError):
                list(release.binary_files(root))

    def test_source_archive_never_collects_personal_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("# synthetic", "utf-8")
            (root / ".local").mkdir()
            (root / ".local/config.json").write_text('{"secret":"synthetic"}', "utf-8")
            (root / "docs").mkdir()
            (root / "docs/WINDOWS.md").write_text("# Public usage", "utf-8")
            (root / "docs/WINDOWS_OSS_SPEC.md").write_text("# Development consultation", "utf-8")
            with patch.object(release, "ROOT", root):
                entries = list(release.source_files())
            self.assertEqual([relative.as_posix() for _, relative in entries], ["app.py", "docs/WINDOWS.md"])


if __name__ == "__main__":
    unittest.main()
