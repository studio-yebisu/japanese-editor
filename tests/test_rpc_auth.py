"""Real loopback sockets: do not disclose manuscript bodies to fake services."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import importlib
import json
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import decider_runtime
import rpc_auth

KEY = bytes(range(32))
MANUSCRIPT = "未公開の原稿。認証前には送らない。"


class ExclusiveTestServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class AuthHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def send_json(self, status, data, *, context=None, close=False):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        signed = raw
        if self.server.mode == "tamper" and context is not None:
            signed = b"a response that was replaced"
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Content-Type", "application/json")
        if context is not None:
            for key, value in rpc_auth.response_headers(context, status, signed).items():
                self.send_header(key, value)
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urlsplit(self.path)
        if path.path == rpc_auth.CHALLENGE_PATH:
            self.server.challenges.append(self.path)
            nonce = parse_qs(path.query)["nonce"][0]
            if self.server.mode == "old":
                return self.send_json(404, {"application": "studio-yebisu-japanese-editor", "protocol_version": 1})
            if self.server.mode == "redirect":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.relay_port}{self.path}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            port = self.connection.getsockname()[1]
            if self.server.mode in ("relay", "relay_rewrite"):
                # The attacker forwards a fresh real challenge. It never has
                # access to the legitimate listener's private key.
                upstream = http.client.HTTPConnection("127.0.0.1", self.server.relay_port, timeout=1)
                try:
                    upstream.request("GET", self.path)
                    challenge = json.loads(upstream.getresponse().read())
                finally:
                    upstream.close()
            else:
                challenge, self.session = rpc_auth.issue_challenge(nonce, port, secret=KEY)
            if self.server.mode == "fake":
                challenge["proof"] = "0" * 64
            elif self.server.mode == "relay_rewrite":
                challenge["port"] = self.connection.getsockname()[1]
            return self.send_json(200, challenge, close=self.server.mode == "drop")
        context = rpc_auth.verify_request(self.session, "GET", path.path, b"", self.headers)
        self.server.requests.append((path.path, b""))
        return self.send_json(200, self.server.result, context=context, close=True)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.requests.append((self.path, raw))
        context = rpc_auth.verify_request(self.session, "POST", self.path, raw, self.headers)
        return self.send_json(self.server.status_code, self.server.result, context=context, close=True)


@contextmanager
def listener(mode="valid", *, result=None, status=200, relay_port=None):
    server = ExclusiveTestServer(("127.0.0.1", 0), AuthHandler)
    server.mode, server.status_code = mode, status
    server.result = {"ok": True, "model": decider_runtime.MODEL, "revision": decider_runtime.REVISION} if result is None else result
    server.requests, server.challenges = [], []
    server.relay_port = relay_port
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class RpcSocketTests(unittest.TestCase):
    def body(self):
        return json.dumps({"request": {"state": MANUSCRIPT}}, ensure_ascii=False).encode("utf-8")

    def test_public_constants_cannot_forge_proof_and_manuscript_is_never_sent(self):
        for mode in ("fake", "old", "drop"):
            with self.subTest(mode=mode), listener(mode) as (server, url):
                with self.assertRaises(rpc_auth.AuthenticationError):
                    rpc_auth.authenticated_request(url, "/api/decider/decide", self.body(), secret=KEY)
                self.assertEqual(server.requests, [])
                self.assertEqual(len(server.challenges), 1)
                self.assertNotIn(MANUSCRIPT, server.challenges[0])
                self.assertNotIn(KEY.hex(), server.challenges[0])

    def test_relay_from_another_legitimate_port_is_rejected_even_if_port_is_rewritten(self):
        with listener() as (legitimate, _legitimate_url):
            for mode in ("relay", "relay_rewrite"):
                with self.subTest(mode=mode), listener(mode, relay_port=legitimate.server_port) as (fake, url):
                    with self.assertRaises(rpc_auth.AuthenticationError):
                        rpc_auth.authenticated_request(url, "/api/decider/decide", self.body(), secret=KEY)
                    self.assertEqual(fake.requests, [])
                    self.assertEqual(legitimate.requests, [])
            self.assertEqual(len(legitimate.challenges), 2)

    def test_challenge_redirect_is_never_followed(self):
        with listener() as (legitimate, _legitimate_url), listener("redirect", relay_port=legitimate.server_port) as (fake, url):
            with self.assertRaises(rpc_auth.AuthenticationError):
                rpc_auth.authenticated_request(url, "/api/decider/decide", self.body(), secret=KEY)
            self.assertEqual(fake.requests, [])
            self.assertEqual(legitimate.challenges, [])
            self.assertEqual(legitimate.requests, [])

    def test_authenticated_service_receives_exact_payload_and_response_is_verified(self):
        with listener() as (server, url):
            status, raw = rpc_auth.authenticated_request(url, "/api/decider/decide", self.body(), secret=KEY)
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(raw)["ok"])
            self.assertEqual(server.requests, [("/api/decider/decide", self.body())])
            self.assertEqual(len(server.challenges), 1)

    def test_status_and_stop_use_authentication_too_without_disclosing_the_secret(self):
        for path, body in (("/api/decider/status", None), ("/api/decider/stop", b"{}")):
            with self.subTest(path=path), listener() as (server, url):
                rpc_auth.authenticated_request(url, path, body, secret=KEY)
                self.assertEqual(server.requests, [(path, b"" if body is None else body)])
                self.assertNotIn(KEY.hex(), repr(server.requests) + repr(server.challenges))

    def test_modified_response_is_not_accepted(self):
        with listener("tamper") as (_server, url):
            with self.assertRaisesRegex(rpc_auth.AuthenticationError, "応答を認証"):
                rpc_auth.authenticated_request(url, "/api/decider/decide", self.body(), secret=KEY)

    def test_runtime_maps_authentication_failure_without_unsigned_fallback(self):
        with listener("fake") as (server, url), mock.patch.object(rpc_auth, "load_secret", return_value=KEY):
            with mock.patch.object(decider_runtime, "local_decide") as local:
                with self.assertRaises(decider_runtime.DeciderRuntimeError) as raised:
                    decider_runtime._remote({"url": url}, "/api/decider/decide", {"request": {"state": MANUSCRIPT}})
            self.assertEqual(raised.exception.code, "service_authentication_failed")
            self.assertNotIn(MANUSCRIPT, str(raised.exception))
            self.assertEqual(server.requests, [])
            local.assert_not_called()

    def test_runtime_accepts_only_signed_success_and_structured_errors(self):
        error = {"error": {"code": "request_too_long", "message": "短くしてください。", "recoverable": True}}
        with listener(result=error, status=422) as (server, url), mock.patch.object(rpc_auth, "load_secret", return_value=KEY):
            with self.assertRaises(decider_runtime.DeciderRuntimeError) as raised:
                decider_runtime._remote({"url": url}, "/api/decider/decide", {"request": {"state": MANUSCRIPT}})
            self.assertEqual(raised.exception.code, "request_too_long")
            self.assertTrue(raised.exception.recoverable)
            self.assertEqual(len(server.requests), 1)

    def test_non_loopback_and_credential_urls_are_rejected_before_opening_socket(self):
        for url in ("https://127.0.0.1", "http://example.com", "http://user@localhost", "http://127.0.0.1/other", "http://127.0.0.1?x=1",
                    "http://127.0.0.1:0", "http://127.0.0.1:70000", "http://127.0.0.1:noport", "http://[broken", None):
            with self.subTest(url=url), mock.patch.object(rpc_auth.http.client, "HTTPConnection") as connect:
                with self.assertRaises(rpc_auth.AuthenticationError):
                    rpc_auth.authenticated_request(url, "/api/decider/decide", self.body(), secret=KEY)
                connect.assert_not_called()

    def test_request_size_guard_runs_before_connecting(self):
        with mock.patch.object(rpc_auth, "authenticated_request") as connect:
            with self.assertRaises(decider_runtime.DeciderRuntimeError) as raised:
                decider_runtime._remote({"url": "http://127.0.0.1:1"}, "/api/decider/decide", {"state": "a" * decider_runtime.MAX_REQUEST_BYTES})
            self.assertEqual(raised.exception.code, "invalid_request")
            connect.assert_not_called()


class RpcBindingTests(unittest.TestCase):
    def session(self):
        return rpc_auth.issue_challenge("a" * 64, 8896, secret=KEY)

    def test_request_signature_binds_method_path_payload_and_connection_nonce(self):
        challenge, server = self.session()
        client = rpc_auth.verify_challenge(challenge, "a" * 64, 8896, secret=KEY)
        headers = rpc_auth.request_headers(client, "POST", "/api/decider/decide", b"original")
        for method, path, body in (("GET", "/api/decider/decide", b"original"),
                                   ("POST", "/api/decider/stop", b"original"),
                                   ("POST", "/api/decider/decide", b"changed")):
            with self.subTest(method=method, path=path, body=body):
                fresh = rpc_auth.Session(KEY, server.client_nonce, server.server_nonce, server.port)
                with self.assertRaises(rpc_auth.AuthenticationError):
                    rpc_auth.verify_request(fresh, method, path, body, headers)
        _, other = self.session()
        with self.assertRaises(rpc_auth.AuthenticationError):
            rpc_auth.verify_request(other, "POST", "/api/decider/decide", b"original", headers)
        context = rpc_auth.verify_request(server, "POST", "/api/decider/decide", b"original", headers)
        with self.assertRaises(rpc_auth.AuthenticationError):
            rpc_auth.verify_request(server, "POST", "/api/decider/decide", b"original", headers)
        self.assertEqual(context.request_mac, headers[rpc_auth.REQUEST_HEADER])

    def test_expired_challenge_cannot_authorize_request(self):
        _challenge, session = self.session()
        headers = rpc_auth.request_headers(session, "POST", "/api/decider/stop", b"{}")
        session.created -= rpc_auth.SESSION_SECONDS + 1
        with self.assertRaises(rpc_auth.AuthenticationError):
            rpc_auth.verify_request(session, "POST", "/api/decider/stop", b"{}", headers)

    def test_response_signature_binds_request_status_and_body(self):
        _challenge, session = self.session()
        context = rpc_auth.RequestContext(session, "a" * 64)
        headers = rpc_auth.response_headers(context, 200, b"original")
        rpc_auth.verify_response(context, 200, b"original", headers)
        for status, body, request_mac in ((503, b"original", "a" * 64), (200, b"changed", "a" * 64), (200, b"original", "b" * 64)):
            with self.subTest(status=status, body=body, request_mac=request_mac):
                with self.assertRaises(rpc_auth.AuthenticationError):
                    rpc_auth.verify_response(rpc_auth.RequestContext(session, request_mac), status, body, headers)


class EditorServerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with mock.patch.object(decider_runtime, "status", return_value={"status": "not_configured", "loaded": False}):
            cls.app = importlib.import_module("app")

    @contextmanager
    def service(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(rpc_auth, "SECRET_PATH", Path(directory) / "rpc" / "secret-v1"):
            server = self.app.create_server(0)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            thread.start()
            try:
                yield server, {"url": f"http://127.0.0.1:{server.server_port}"}
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_real_editor_service_authenticates_with_persistent_key_and_dispatches_all_raw_routes(self):
        result = {"ok": True, "model": decider_runtime.MODEL, "revision": decider_runtime.REVISION}
        state = {"status": "configured", "loaded": False}
        with self.service() as (_server, service), \
                mock.patch.object(decider_runtime, "local_decide", return_value=result) as decide, \
                mock.patch.object(decider_runtime, "local_status", return_value=state) as status, \
                mock.patch.object(decider_runtime, "local_stop", return_value={"status": "stopped"}) as stop:
            payload = {"state": MANUSCRIPT, "questions": {}}
            self.assertEqual(decider_runtime._remote(service, "/api/decider/decide", {"request": payload, "timeout": 1.25}), result)
            decide.assert_called_once_with(payload, timeout=1.25)
            self.assertEqual(decider_runtime._remote(service, "/api/decider/status"), state)
            status.assert_called_once_with()
            self.assertEqual(decider_runtime._remote(service, "/api/decider/stop", {}), {"status": "stopped"})
            stop.assert_called_once_with()

    def test_editor_listen_port_cannot_be_shared_by_a_second_listener(self):
        with self.service() as (server, _service):
            competitor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                competitor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if hasattr(socket, "SO_REUSEPORT"):
                    competitor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                with self.assertRaises(OSError):
                    competitor.bind(("127.0.0.1", server.server_port))
                    competitor.listen()
            finally:
                competitor.close()

    def test_unsigned_request_to_real_editor_never_calls_worker(self):
        with self.service() as (server, _service), mock.patch.object(decider_runtime, "local_decide") as decide:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=1)
            try:
                connection.request("POST", "/api/decider/decide", self.body(), headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 401)
                response.read()
            finally:
                connection.close()
            decide.assert_not_called()

    def health_connection(self, server):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=1)
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        response.read()
        self.assertIsNotNone(connection.sock)
        return connection

    def test_shutdown_disables_challenges_on_an_already_accepted_keepalive_socket(self):
        with self.service() as (server, _service):
            connection = self.health_connection(server)
            try:
                server.shutdown()
                with mock.patch.object(rpc_auth, "issue_challenge") as issue:
                    connection.request("GET", rpc_auth.CHALLENGE_PATH + "?nonce=" + "a" * 64)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 503)
                    self.assertNotIn("proof", json.loads(response.read()))
                    self.assertTrue(response.will_close)
                    issue.assert_not_called()
            finally:
                connection.close()

    def test_server_close_closes_old_accepted_sockets_before_they_can_act_as_proof_oracles(self):
        with self.service() as (server, _service):
            connection = self.health_connection(server)
            try:
                server.shutdown()
                server.server_close()
                connection.auto_open = 0
                with mock.patch.object(rpc_auth, "issue_challenge") as issue:
                    with self.assertRaises((OSError, http.client.HTTPException)):
                        connection.request("GET", rpc_auth.CHALLENGE_PATH + "?nonce=" + "b" * 64)
                        connection.getresponse()
                    issue.assert_not_called()
            finally:
                connection.close()

    def test_shutdown_preserves_the_response_of_an_already_running_authenticated_rpc(self):
        started, finish = threading.Event(), threading.Event()
        result = {"ok": True, "model": decider_runtime.MODEL, "revision": decider_runtime.REVISION}

        def inference(_request, timeout=None):
            started.set()
            self.assertTrue(finish.wait(2))
            return result

        with self.service() as (server, service), mock.patch.object(decider_runtime, "local_decide", side_effect=inference), \
                ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(decider_runtime._remote, service, "/api/decider/decide", {"request": {"state": MANUSCRIPT}})
            try:
                self.assertTrue(started.wait(2))
                server.shutdown()
            finally:
                finish.set()
            self.assertEqual(future.result(timeout=2), result)

    def test_listener_can_restart_on_the_same_port_after_a_completed_rpc(self):
        state = {"status": "configured", "loaded": False}
        with self.service() as (server, service), mock.patch.object(decider_runtime, "local_status", return_value=state):
            self.assertEqual(decider_runtime._remote(service, "/api/decider/status"), state)
            server.shutdown()
            server.server_close()
            replacement = self.app.create_server(server.server_port)
            thread = threading.Thread(target=replacement.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            thread.start()
            try:
                self.assertEqual(decider_runtime._remote(service, "/api/decider/status"), state)
            finally:
                replacement.shutdown()
                replacement.server_close()
                thread.join(timeout=2)

    @staticmethod
    def body():
        return json.dumps({"request": {"state": MANUSCRIPT}}, ensure_ascii=False).encode("utf-8")


class PrivateSecretTests(unittest.TestCase):
    def test_atomic_concurrent_creation_returns_one_persistent_private_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rpc" / "secret-v1"
            with ThreadPoolExecutor(max_workers=8) as pool:
                values = list(pool.map(lambda _index: rpc_auth.load_secret(path), range(16)))
            self.assertEqual(len(set(values)), 1)
            self.assertEqual(len(values[0]), 32)
            self.assertEqual(rpc_auth.load_secret(path), values[0])
            self.assertEqual(list(path.parent.iterdir()), [path])
            if os.name == "nt":
                self.assertNotIn(values[0], path.read_bytes())
            else:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    @unittest.skipIf(os.name == "nt", "POSIX ownership/mode checks")
    def test_shared_permissions_and_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret-v1"
            path.write_bytes(KEY)
            path.chmod(0o644)
            with self.assertRaises(rpc_auth.AuthenticationError):
                rpc_auth.load_secret(path)
            path.chmod(0o600)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(rpc_auth.AuthenticationError):
                rpc_auth.load_secret(link)

    def test_corrupted_secret_fails_closed_without_replacing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret-v1"
            path.write_bytes(b"not a valid protected secret")
            path.chmod(0o600)
            with self.assertRaises(rpc_auth.AuthenticationError):
                rpc_auth.load_secret(path)
            self.assertEqual(path.read_bytes(), b"not a valid protected secret")


if __name__ == "__main__":
    unittest.main()
