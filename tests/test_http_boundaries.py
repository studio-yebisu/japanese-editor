"""Live loopback boundaries, using small synthetic requests and short deadlines."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app


class HttpBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(app.Handler, "timeout", .35))
        self.enterContext(patch.object(app.EditorServer, "max_connections", 4))
        self.server = app.create_server(0)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()

    def connect(self):
        client = socket.create_connection(("127.0.0.1", self.port), timeout=2)
        self.clients.append(client)
        return client

    def headers(self, method="POST", length=80):
        return (f"{method} /api/analyze HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {length}\r\n\r\n").encode()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def wait_slots(self, expected):
        deadline = time.monotonic() + 2
        while self.server._connection_slots._value != expected and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertEqual(self.server._connection_slots._value, expected)

    def test_loopback_bind_and_existing_http_rejection_boundaries(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        with patch.object(app, "analyze") as analyze:
            cases = [("GET", "/api/health", None, {"Host": "other.invalid"}, 403),
                     ("POST", "/api/analyze", "{}", {"Origin": "https://other.invalid", "Content-Type": "application/json"}, 403),
                     ("POST", "/api/analyze", "{}", {"Content-Type": "text/plain"}, 415),
                     ("POST", "/api/analyze", b"", {"Content-Type": "application/json", "Content-Length": "256001"}, 413)]
            for method, path, body, headers, expected in cases:
                with self.subTest(expected=expected):
                    status, _ = self.request(method, path, body, headers)
                    self.assertEqual(status, expected)
            analyze.assert_not_called()

    def test_mcp_readiness_probe_is_same_origin_empty_input_and_exclusive(self):
        headers = {"Content-Type": "application/json"}
        with patch.object(app.integrations, "check_connection", return_value={"status": "passed"}) as check:
            self.assertEqual(self.request("POST", "/api/integrations/check", "{}", {
                **headers, "Origin": "https://other.invalid"})[0], 403)
            self.assertEqual(self.request("POST", "/api/integrations/check", '{"text":"private"}', headers)[0], 400)
            check.assert_not_called()
            with app.Handler.semaphore:
                self.assertEqual(self.request("POST", "/api/integrations/check", "{}", headers)[0], 409)
            check.assert_not_called()
            self.assertEqual(self.request("POST", "/api/integrations/check", "{}", headers), (200, {"status": "passed"}))
            check.assert_called_once_with()

    def test_idle_and_incomplete_headers_expire_and_return_slots(self):
        for prefix in (b"", f"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n".encode()):
            with self.subTest(prefix=prefix):
                client = self.connect()
                if prefix:
                    client.sendall(prefix)
                self.assertEqual(client.recv(4096), b"")
                self.wait_slots(4)
        self.assertEqual(self.request("GET", "/api/health")[0], 200)

    def test_browser_decider_controls_preserve_raw_rpc_auth_boundary(self):
        headers = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"}
        with patch.object(app.decider_runtime, 'local_set_device', return_value={'status': 'saved', 'device': 'cuda:0'}) as device, \
             patch.object(app.decider_runtime, 'local_release', return_value={'status': 'stopped'}) as release:
            self.assertEqual(self.request('POST', '/api/decider/settings', '{"device":"cuda:0"}', headers)[0], 200)
            device.assert_called_once_with('cuda:0')
            self.assertEqual(self.request('POST', '/api/decider/release', '{}', headers)[0], 200)
            release.assert_called_once()
            self.assertEqual(self.request('POST', '/api/decider/stop', '{}', headers)[0], 401)
            self.assertEqual(self.request('POST', '/api/decider/settings', '{"device":"cpu","python":"other"}', headers)[0], 400)
            foreign = {**headers, 'Origin': 'https://other.invalid'}
            self.assertEqual(self.request('POST', '/api/decider/settings', '{"device":"cpu"}', foreign)[0], 403)
            device.assert_called_once()

    def test_decider_settings_refuse_during_analysis(self):
        self.assertTrue(app.Handler.semaphore.acquire(blocking=False))
        try:
            with patch.object(app.decider_runtime, 'local_set_device') as device:
                self.assertEqual(self.request('POST', '/api/decider/settings', '{"device":"cuda:0"}', {'Content-Type': 'application/json'})[0], 409)
                device.assert_not_called()
        finally:
            app.Handler.semaphore.release()

    def test_browser_backend_setting_accepts_only_a_prepared_name(self):
        headers = {'Content-Type': 'application/json'}
        with patch.object(app.decider_runtime, 'local_set_backend', return_value={'status': 'saved'}) as backend:
            self.assertEqual(self.request('POST', '/api/decider/settings', '{"backend":"llama_cpp"}', headers)[0], 200)
            backend.assert_called_once_with('llama_cpp')
            self.assertEqual(self.request('POST', '/api/decider/settings', '{"backend":"llama_cpp","cpp_executable":"other"}', headers)[0], 400)

    def test_body_limit_accepts_exact_maximum_and_rejects_empty_or_negative_lengths(self):
        prefix, suffix = b'{"text":"', b'"}'
        raw = prefix + b"x" * (256000 - len(prefix) - len(suffix)) + suffix
        self.assertEqual(len(raw), 256000)
        with patch.object(app, "analyze", return_value={"ok": True}) as analyze:
            self.assertEqual(self.request("POST", "/api/analyze", raw,
                {"Content-Type": "application/json; charset=utf-8"}), (200, {"ok": True}))
            analyze.assert_called_once()
            analyze.reset_mock()
            for length in ("0", "-1"):
                with self.subTest(length=length):
                    self.assertEqual(self.request("POST", "/api/analyze", b"",
                        {"Content-Type": "application/json", "Content-Length": length})[0], 413)
            analyze.assert_not_called()

    def test_incomplete_body_expires_without_invoking_engine(self):
        with patch.object(app, "analyze") as analyze:
            client = self.connect()
            client.sendall(self.headers() + b"{")
            response = http.client.HTTPResponse(client)
            response.begin()
            self.assertEqual(response.status, 408)
            self.assertNotIn("再実行", response.read().decode("utf-8"))
            analyze.assert_not_called()
            self.wait_slots(4)

    def test_dripping_header_cannot_extend_absolute_deadline(self):
        client = self.connect()
        started = time.monotonic()
        client.sendall(b"GET /")
        stopped, sender = self.drip(client, b"x")
        try:
            try:
                self.assertEqual(client.recv(4096), b"")
            except (ConnectionResetError, ConnectionAbortedError):
                pass  # Windows may reset an incomplete request when the deadline expires.
        finally:
            stopped.set()
            sender.join(1)
        self.assertLess(time.monotonic() - started, 1.5)
        self.wait_slots(4)

    def drip(self, client, chunk):
        stopped = threading.Event()
        def send():
            for _ in range(20):
                if stopped.wait(.10):
                    return
                try:
                    client.sendall(chunk)
                except ConnectionError:
                    return
        sender = threading.Thread(target=send, daemon=True)
        sender.start()
        return stopped, sender

    def test_dripping_body_cannot_extend_absolute_deadline(self):
        client = self.connect()
        started = time.monotonic()
        client.sendall(self.headers() + b"{")
        with patch.object(app, "analyze") as analyze:
            stopped, sender = self.drip(client, b" ")
            try:
                try:
                    response = http.client.HTTPResponse(client)
                    response.begin()
                    self.assertEqual(response.status, 408)
                    response.read()
                except (ConnectionResetError, ConnectionAbortedError, http.client.RemoteDisconnected):
                    pass  # A continued write can race the server's timeout response and close.
            finally:
                stopped.set()
                sender.join(1)
            self.assertLess(time.monotonic() - started, 1.5)
            analyze.assert_not_called()
        self.wait_slots(4)

    def test_connection_limit_denies_excess_without_leaking_slots(self):
        with patch.object(app.Handler, "timeout", 5):
            held = [self.connect() for _ in range(4)]
            self.wait_slots(0)
            extra = self.connect()
            self.assertEqual(extra.recv(1), b"")
            self.assertEqual(self.server._connection_slots._value, 0)
            for client in held:
                client.close()
            self.wait_slots(4)
            self.assertEqual(self.request("GET", "/api/health")[0], 200)

    def test_processing_can_outlive_input_deadline(self):
        def slow(_):
            time.sleep(.5)
            return {"ok": True}
        with patch.object(app, "analyze", side_effect=slow):
            status, value = self.request("POST", "/api/analyze", b'{"text":"sample"}', {"Content-Type": "application/json"})
        self.assertEqual((status, value), (200, {"ok": True}))

    def test_invalid_unicode_is_400_and_next_valid_request_still_works(self):
        for raw in (b'{"text":"private \\ud83d"}', b'{"text":"private \\ude42"}',
                    b'{"text":"\xff"}', b'{"text":"ok","context":"private \\ud800"}'):
            with self.subTest(raw=raw):
                with patch.object(app, "analyze", wraps=app.analyze):
                    status, value = self.request("POST", "/api/analyze", raw, {"Content-Type": "application/json"})
                self.assertEqual(status, 400)
                self.assertNotIn("private", value["error"])
                self.assertNotIn("再実行", value["error"])
        with patch.object(app, "analyze", return_value={"ok": True}):
            self.assertEqual(self.request("POST", "/api/analyze", b'{"text":"ok"}', {"Content-Type": "application/json"}), (200, {"ok": True}))

    def test_get_body_and_ambiguous_post_framing_close_connection(self):
        for request in (
            f"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nContent-Length: 4\r\n\r\nGET ",
            f"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
            f"POST /api/analyze HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nContent-Type: application/json\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{{}}",
            f"POST /api/analyze HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nContent-Type: application/json\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n{{}}",
        ):
            with self.subTest(request=request):
                client = self.connect()
                client.sendall(request.encode())
                response = http.client.HTTPResponse(client)
                response.begin()
                self.assertEqual(response.status, 400)
                self.assertEqual(response.getheader("Connection"), "close")
                response.read()


class StdioUnicodeBoundaryTests(unittest.TestCase):
    def environment(self):
        return {**os.environ, "YEBISU_EDITOR_NO_AUTOSTART": "1", "PYTHONIOENCODING": "utf-8"}

    def test_cli_review_surrogate_is_actionable_without_traceback(self):
        payload = json.dumps({"text": "private\ud83d", "edits": []}).encode()
        result = subprocess.run([sys.executable, "-B", "app.py", "review"], input=payload, capture_output=True,
                                cwd=ROOT, env=self.environment(), timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        error = result.stderr.decode("utf-8")
        self.assertIn("未対サロゲート", error)
        self.assertNotIn("private", error)
        self.assertNotIn("Traceback", error)

    def test_mcp_surrogate_is_tool_input_error_and_process_survives(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "japanese_editor_analyze", "arguments": {"text": "private\ud83d"}}},
            {"jsonrpc": "2.0", "id": "private\ud800", "method": "ping"},
            {"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {"protocolVersion": "private\ud800"}},
            {"jsonrpc": "2.0", "id": 4, "method": "ping"},
        ]
        result = subprocess.run([sys.executable, "-B", "app.py", "mcp"],
                                input=("\n".join(json.dumps(row) for row in requests) + "\n").encode(), capture_output=True,
                                cwd=ROOT, env=self.environment(), timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertTrue(rows[0]["result"]["isError"])
        self.assertIn("未対サロゲート", rows[0]["result"]["content"][0]["text"])
        self.assertNotIn("接続状態", rows[0]["result"]["content"][0]["text"])
        self.assertNotIn("private", result.stdout.decode())
        self.assertEqual(rows[1]["error"]["code"], -32602)
        self.assertEqual(rows[2]["error"]["code"], -32602)
        self.assertEqual(rows[3]["result"], {})


if __name__ == "__main__":
    unittest.main()
