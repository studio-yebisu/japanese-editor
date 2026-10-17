import io
import json
import os
from pathlib import Path
import sys
import types
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


class MCPRecoveryTests(unittest.TestCase):
    def test_failed_tool_does_not_end_transport_or_leak_exception(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "japanese_editor_profiles"}},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        ]
        incoming = io.BytesIO(("\n".join(json.dumps(item) for item in requests) + "\n").encode())
        outgoing = io.BytesIO()
        with patch.dict(os.environ, {"YEBISU_EDITOR_NO_AUTOSTART": "1"}), \
             patch.object(app.sys, "stdin", types.SimpleNamespace(buffer=incoming)), \
             patch.object(app.sys, "stdout", types.SimpleNamespace(buffer=outgoing)), \
             patch.object(app, "execute_tool", side_effect=OSError("private-token-and-path")):
            app.mcp()
        responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        self.assertTrue(responses[0]["result"]["isError"])
        self.assertNotIn(b"private-token", outgoing.getvalue())
        self.assertEqual(responses[1], {"jsonrpc": "2.0", "id": 2, "result": {}})

    def test_tool_rechecks_service_after_shutdown(self):
        with patch.dict(os.environ, {"YEBISU_EDITOR_NO_AUTOSTART": "0"}), \
             patch("service_runtime.ensure_service") as ensure, \
             patch.object(app, "config", return_value={"version": "test"}):
            self.assertEqual(app.execute_tool("japanese_editor_profiles", {}), {"version": "test"})
            self.assertEqual(app.execute_tool("japanese_editor_profiles", {}), {"version": "test"})
        self.assertEqual(ensure.call_count, 2)

    def test_stop_does_not_post_to_unrecognized_service(self):
        import service_runtime
        with patch.object(service_runtime, "_probe", return_value=False), \
             patch.object(service_runtime, "build_opener") as opener:
            with self.assertRaises(ValueError):
                service_runtime.stop_service()
        opener.assert_not_called()

    def test_explicit_stop_replaces_nonce_atomically(self):
        import service_runtime
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(service_runtime, "PERSISTENT_ROOT", root), \
                 patch.object(service_runtime, "STOP_PATH", root / "service-stop.json"):
                service_runtime.record_service_stop()
                before = json.loads((root / "service-stop.json").read_text("utf-8"))
                service_runtime.record_service_stop()
                after = json.loads((root / "service-stop.json").read_text("utf-8"))
            self.assertNotEqual(before["nonce"], after["nonce"])
            self.assertEqual(len(list(root.iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
