"""Bound MCP reads before allocation, then recover at the next JSON line."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app


PRIVATE = b"private-manuscript-must-not-be-echoed"


def ping(identifier=2, newline=b"\n"):
    return json.dumps({"jsonrpc": "2.0", "id": identifier, "method": "ping"}, ensure_ascii=False).encode("utf-8") + newline


class BoundedReader:
    """Fail if the transport falls back to iteration or an unbounded read."""
    def __init__(self, data):
        self.source = io.BytesIO(data)
        self.sizes = []

    def __iter__(self):
        raise AssertionError("MCP must not iterate full, unbounded input lines")

    def read(self, *_args):
        raise AssertionError("MCP must not use unbounded read")

    def readline(self, size=-1):
        if size != app.MCP_MAX_LINE_BYTES + 1:
            raise AssertionError("Every normal and discard read must be bounded")
        self.sizes.append(size)
        if len(self.sizes) > 100:
            raise AssertionError("Transport did not stop at EOF")
        return self.source.readline(size)


class MCPInputLimitTests(unittest.TestCase):
    def fake_transport(self, data):
        reader, output = BoundedReader(data), io.BytesIO()
        with patch.dict(os.environ, {"YEBISU_EDITOR_NO_AUTOSTART": "1"}), \
             patch.object(app.sys, "stdin", types.SimpleNamespace(buffer=reader)), \
             patch.object(app.sys, "stdout", types.SimpleNamespace(buffer=output)):
            app.mcp()
        raw = output.getvalue()
        self.assertNotIn(PRIVATE, raw)
        return reader, [json.loads(line) for line in raw.splitlines()]

    def assert_size_error(self, response):
        self.assertIsNone(response["id"])
        self.assertEqual(response["error"]["code"], -32602)
        self.assertEqual(response["error"]["message"], "入力サイズの上限（512,000バイト）を超えています。")

    def test_every_read_is_bounded_including_discard_and_next_request(self):
        oversized = PRIVATE + b"x" * (app.MCP_MAX_LINE_BYTES * 2) + b"\n"
        reader, responses = self.fake_transport(oversized + ping())
        self.assertGreaterEqual(len(reader.sizes), 4)
        self.assertEqual(set(reader.sizes), {app.MCP_MAX_LINE_BYTES + 1})
        self.assertEqual(len(responses), 2)
        self.assert_size_error(responses[0])
        self.assertEqual(responses[1], {"jsonrpc": "2.0", "id": 2, "result": {}})

    def test_oversized_eof_without_newline_returns_once_and_stops(self):
        _reader, responses = self.fake_transport(PRIVATE + b"x" * (app.MCP_MAX_LINE_BYTES + 12))
        self.assertEqual(len(responses), 1)
        self.assert_size_error(responses[0])

    def test_crlf_split_at_read_limit_does_not_leave_a_phantom_request(self):
        # The first bounded read ends with CR; the discard read consumes LF.
        oversized = b"x" * app.MCP_MAX_LINE_BYTES + b"\r\n"
        _reader, responses = self.fake_transport(oversized + ping(3, b"\r\n"))
        self.assertEqual(len(responses), 2)
        self.assert_size_error(responses[0])
        self.assertEqual(responses[1]["id"], 3)
        self.assertEqual(responses[1]["result"], {})

    def test_multiple_oversized_lines_each_have_one_error(self):
        oversized = b"x" * (app.MCP_MAX_LINE_BYTES + 1)
        _reader, responses = self.fake_transport(oversized + b"\n" + ping() + oversized)
        self.assertEqual(len(responses), 3)
        self.assert_size_error(responses[0])
        self.assertEqual(responses[1]["result"], {})
        self.assert_size_error(responses[2])

    def test_exact_byte_limit_accepts_utf8_id_lf_crlf_and_final_eof(self):
        for newline in (b"\n", b"\r\n", b""):
            with self.subTest(newline=newline):
                base = ping("🙂日本語", b"")
                line = base + b" " * (app.MCP_MAX_LINE_BYTES - len(base) - len(newline)) + newline
                self.assertEqual(len(line), app.MCP_MAX_LINE_BYTES)
                _reader, responses = self.fake_transport(line)
                self.assertEqual(responses, [{"jsonrpc": "2.0", "id": "🙂日本語", "result": {}}])

    def test_one_byte_over_limit_is_rejected_without_executing_tool(self):
        base = json.dumps({"jsonrpc": "2.0", "id": PRIVATE.decode(), "method": "tools/call",
                           "params": {"name": "japanese_editor_profiles"}}).encode()
        line = base + b" " * (app.MCP_MAX_LINE_BYTES - len(base)) + b"\n"
        self.assertEqual(len(line), app.MCP_MAX_LINE_BYTES + 1)
        with patch.object(app, "execute_tool") as execute:
            _reader, responses = self.fake_transport(line + ping())
        execute.assert_not_called()
        self.assertEqual(len(responses), 2)
        self.assert_size_error(responses[0])
        self.assertEqual(responses[1]["result"], {})

    def test_surrogate_error_is_still_recoverable_after_discard(self):
        invalid = json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "japanese_editor_analyze", "arguments": {"text": "private-manuscript-must-not-be-echoed\ud800"}}}).encode()
        _reader, responses = self.fake_transport(b"x" * (app.MCP_MAX_LINE_BYTES + 1) + b"\n" + invalid + b"\n" + ping())
        self.assertEqual(len(responses), 3)
        self.assert_size_error(responses[0])
        self.assertTrue(responses[1]["result"]["isError"])
        self.assertIn("不正なUnicode文字", responses[1]["result"]["content"][0]["text"])
        self.assertEqual(responses[2]["result"], {})

    def test_empty_input_produces_no_response(self):
        reader, responses = self.fake_transport(b"")
        self.assertEqual(responses, [])
        self.assertEqual(len(reader.sizes), 1)

    def subprocess_stream(self, *, terminated):
        # Stream an 8 MiB line using one reusable 64 KiB chunk. No giant input
        # fixture, platform-specific RSS measurement, or model process is used.
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.Popen(
                [sys.executable, "-B", str(ROOT / "app.py"), "mcp"], cwd=ROOT,
                env={**os.environ, "YEBISU_EDITOR_NO_AUTOSTART": "1", "YEBISU_EDITOR_DATA_DIR": directory},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            try:
                process.stdin.write(PRIVATE)
                chunk = b"x" * 65_536
                for _ in range(128):
                    process.stdin.write(chunk)
                if terminated:
                    process.stdin.write(b"\n" + ping("🙂after-large-line"))
                process.stdin.close()
                process.stdin = None
                output, errors = process.communicate(timeout=20)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, errors.decode("utf-8", errors="replace"))
            self.assertEqual(errors, b"")
            self.assertNotIn(PRIVATE, output)
            return [json.loads(line) for line in output.splitlines()]

    def test_real_subprocess_streamed_oversized_line_then_ping(self):
        responses = self.subprocess_stream(terminated=True)
        self.assertEqual(len(responses), 2)
        self.assert_size_error(responses[0])
        self.assertEqual(responses[1], {"jsonrpc": "2.0", "id": "🙂after-large-line", "result": {}})

    def test_real_subprocess_streamed_oversized_final_line_without_newline(self):
        responses = self.subprocess_stream(terminated=False)
        self.assertEqual(len(responses), 1)
        self.assert_size_error(responses[0])


if __name__ == "__main__":
    unittest.main()
