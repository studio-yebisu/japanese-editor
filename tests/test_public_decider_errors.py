"""A worker failure must remain private through HTTP and host-tool results."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import decider
import decider_runtime
import rpc_auth

PRIVATE = "RAW_STARTUP /home/private-user/models/weights.safetensors key=private-key"
PAYLOAD = {"text": "保存して下さい。", "edits": [{"before": "して下さい", "after": "してください",
           "reason": "補助動詞の表記", "rule": "yomiyasu"}]}


class PublicDeciderErrorTests(unittest.TestCase):
    def failure(self):
        return decider_runtime.DeciderRuntimeError("startup_error", PRIVATE,
            {"exception_type": "FileNotFoundError", "path": PRIVATE, "nested": {"message": PRIVATE}})

    def assert_private(self, value):
        rendered = json.dumps(value, ensure_ascii=False)
        for marker in ("RAW_STARTUP", "private-user", "weights.safetensors", "private-key"):
            self.assertNotIn(marker, rendered)

    def available(self):
        return {"available": True, "status": "configured", "device": "cpu", "model": decider_runtime.MODEL,
                "revision": decider_runtime.REVISION, "max_length": 2048}

    def test_authenticated_raw_http_error_is_redacted_before_signing(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(rpc_auth, "SECRET_PATH", Path(directory) / "rpc/secret-v1"), \
             patch.object(decider_runtime, "local_decide", side_effect=self.failure()):
            server = app.create_server(0)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            thread.start()
            try:
                status, raw = rpc_auth.authenticated_request(f"http://127.0.0.1:{server.server_port}",
                    "/api/decider/decide", json.dumps({"request": {}, "timeout": 1}).encode())
                self.assertEqual(status, 503)
                result = json.loads(raw)
                self.assertEqual(result["error"]["code"], "startup_error")
                self.assert_private(result)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)

    def test_review_proposals_keeps_candidate_without_exposing_worker_exception(self):
        with patch.object(decider, "availability", return_value=self.available()), \
             patch.object(decider_runtime, "decide", side_effect=self.failure()), \
             patch.dict(os.environ, {"YEBISU_EDITOR_NO_AUTOSTART": "1"}):
            result = app.execute_tool("japanese_editor_review_proposals", PAYLOAD)
        self.assertEqual(len(result["edits"]), 1)
        self.assertEqual(result["edits"][0]["decision"]["status"], "error")
        self.assertEqual(result["edits"][0]["after"], "してください")
        self.assert_private(result)

    def test_mcp_tool_result_does_not_echo_raw_worker_failure(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "japanese_editor_review_proposals", "arguments": PAYLOAD}}
        source = io.TextIOWrapper(io.BytesIO((json.dumps(request) + "\n").encode()), encoding="utf-8")
        output = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        with patch.object(decider, "availability", return_value=self.available()), \
             patch.object(decider_runtime, "decide", side_effect=self.failure()), \
             patch.dict(os.environ, {"YEBISU_EDITOR_NO_AUTOSTART": "1"}), \
             patch.object(app.sys, "stdin", source), patch.object(app.sys, "stdout", output):
            app.mcp()
        output.flush()
        response = json.loads(output.buffer.getvalue())
        result = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(result["edits"][0]["decision"]["status"], "error")
        self.assert_private(response)


if __name__ == "__main__":
    unittest.main()
