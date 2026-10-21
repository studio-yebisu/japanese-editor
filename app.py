"""Run a loopback UI, JSON CLI, or stdio MCP using one proofreading engine."""
from __future__ import annotations

import argparse
import io
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

if getattr(sys, "frozen", False):
    sys.dont_write_bytecode = True

from engine import EditorError, ROOT, CONFIG, analyze, check_final, config, finalize_proposals, review_proposals, validate_unicode_input
from runtime_paths import cli_command, FROZEN
from decider import prepare as prepare_decider
import decider_runtime
import provider_settings
import integrations
import progress
import rpc_auth
import yomiyasu_updates
from service_runtime import data_id

TOOLS = [
    {"name": "japanese_editor_profiles", "description": "校正の用途・強さと接続状態を取得する。文章は送信しない。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "japanese_editor_analyze", "description": "原文の規則を点検する。generate=trueの場合のみ選択した外部サービスへ原文を送り、修正案を生成する。通常のホスト校正はホスト自身で案を作りreview_proposalsへ渡す。Sarashina生成はローカルDeciderの意味判定が必須。原本は変更しない。",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string", "maxLength": 12000},
         "profile": {"type": "string", "enum": [p["id"] for p in CONFIG["profiles"]]},
         "strength": {"type": "string", "enum": [s["id"] for s in CONFIG["strengths"]]},
         "context": {"type": "string", "maxLength": 2000}, "generate": {"type": "boolean", "default": False},
         "provider": {"type": "string", "enum": ["gemini", "sarashina", "claude"]}, "model": {"type": "string"},
         "judge": {"type": "boolean", "default": True}},
      "required": ["text"], "additionalProperties": False}},
    {"name": "japanese_editor_check_final", "description": "採用した修正後の全文をyomiyasu run/verifyで点検・照合する。品質や意味保持の保証ではない。ファイルは変更しない。",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"},
                       "final": {"type": "string"}, "profile": {"type": "string"},
                       "yomiyasu_revision": {"type": "string", "pattern": "^[0-9a-f]{40}$", "description": "前の結果と同じYomiyasuの版を使う場合に指定"}},
                     "required": ["text", "final"], "additionalProperties": False}},
]
EDIT_SCHEMA = {"type": "array", "maxItems": 50, "items": {"type": "object",
    "properties": {key: {"type": "string"} for key in ("before", "after", "reason", "rule")},
    "required": ["before", "after", "reason", "rule"], "additionalProperties": False}}
TOOLS.extend([
    {"name": "japanese_editor_finalize", "description": "採用した修正候補から共通エンジンが最終稿を組み立て、その正確な本文をrun/verifyする。原本を変更しない。",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}, "edits": EDIT_SCHEMA,
        "profile": {"type": "string"}, "yomiyasu_revision": {"type": "string", "pattern": "^[0-9a-f]{40}$"}}, "required": ["text", "edits"], "additionalProperties": False}},
    {"name": "japanese_editor_review_proposals", "description": "ホストが作った候補の位置・保護範囲・重複を点検し、ローカルDeciderで意味変化を判定する。別の生成モデルは呼ばない。",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}, "edits": EDIT_SCHEMA,
        "profile": {"type": "string"}, "strength": {"type": "string"}, "judge": {"type": "boolean", "default": True}}, "required": ["text", "edits"], "additionalProperties": False}},
    {"name": "japanese_editor_prepare_decider", "description": "Deciderで意味変化を判定するための公式形式の要求JSONを作る。ネット送信・課金・推論は実行しない。",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}, "edits": EDIT_SCHEMA,
        "profile": {"type": "string"}, "model": {"type": "string", "enum": ["pplx-decider-v1.2", "pplx-decider-flash-v1.2"]}},
        "required": ["text", "edits"], "additionalProperties": False}},
])

# These tools inspect text and assemble returned values without editing the
# user's files. External generation remains a separate, explicit analyze option.
for tool in TOOLS:
    external = tool["name"] == "japanese_editor_analyze"
    tool["annotations"] = {"readOnlyHint": not external, "destructiveHint": False,
                           "idempotentHint": not external, "openWorldHint": external}


def execute_tool(name, arguments):
    try:
        revision = arguments.get("yomiyasu_revision") if isinstance(arguments, dict) else None
        with yomiyasu_updates.using_revision(revision):
            return _execute_tool(name, arguments)
    except yomiyasu_updates.UpdateError:
        raise EditorError("指定したYomiyasuの版を利用できません。もう一度校正してから書き出してください。") from None


def _execute_tool(name, arguments):
    validate_unicode_input(arguments)
    if not isinstance(arguments, dict):
        raise EditorError("入力はJSONオブジェクトで指定してください。")
    if name in {"japanese_editor_profiles", "japanese_editor_analyze", "japanese_editor_review_proposals"} and os.environ.get("YEBISU_EDITOR_NO_AUTOSTART") != "1":
        from service_runtime import ensure_service
        ensure_service()
    if name == "japanese_editor_profiles":
        return config()
    if name == "japanese_editor_analyze":
        return analyze(arguments)
    if name == "japanese_editor_check_final":
        return check_final(arguments.get("text"), arguments.get("final"), arguments.get("profile", "article"))
    if name == "japanese_editor_finalize":
        return finalize_proposals(arguments)
    if name == "japanese_editor_review_proposals":
        return review_proposals(arguments)
    if name == "japanese_editor_prepare_decider":
        return prepare_decider(arguments)
    raise EditorError("不明なツールです。")


MCP_MAX_LINE_BYTES = 512_000


def _bounded_mcp_lines(stream):
    """Yield complete bounded lines, or None once per discarded oversized line."""
    while True:
        line = stream.readline(MCP_MAX_LINE_BYTES + 1)
        if not line:
            return
        if len(line) > MCP_MAX_LINE_BYTES:
            # Keep each allocation bounded while resynchronizing at the next
            # newline. An oversized final line can end at EOF without a newline.
            while line and not line.endswith(b"\n"):
                line = stream.readline(MCP_MAX_LINE_BYTES + 1)
            yield None
        else:
            yield line


def mcp():
    from service_runtime import ensure_service
    if os.environ.get("YEBISU_EDITOR_NO_AUTOSTART") != "1":
        ensure_service()
    # MCP stdio transport is UTF-8 newline-delimited JSON-RPC, no stdout logs.
    for line in _bounded_mcp_lines(sys.stdin.buffer):
        request_id = None
        try:
            if line is None:
                raise EditorError("入力サイズの上限を超えています。")
            request = json.loads(line)
            request_id = request.get("id")
            try:
                validate_unicode_input(request_id)
            except EditorError:
                request_id = None
                raise ValueError("Invalid request id") from None
            method, params = request.get("method"), request.get("params", {})
            if request_id is None:
                continue
            if method == "initialize":
                validate_unicode_input(params)
                result = {"protocolVersion": params.get("protocolVersion", "2024-11-05"),
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "yomiyasu-studio", "version": "0.2.12"},
                          "instructions": "日本語の校正は用途・強さを選び、ホスト自身が修正候補を作りreview_proposalsで比較する。採用案だけをfinalizeへ渡し返ったtextを最終稿にする。未実行の判定を成功と扱わない。小説・台詞の作風を保つ。generate=trueは外部送信を伴うため通常のホスト校正では不要。"}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                try:
                    data = execute_tool(params.get("name"), params.get("arguments", {}))
                    result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}], "isError": False}
                except EditorError as exc:
                    result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
                except Exception:
                    result = {"content": [{"type": "text", "text": "点検を完了できませんでした。接続状態を確認して、もう一度実行してください。"}], "isError": True}
            else:
                response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
                sys.stdout.buffer.write((json.dumps(response, ensure_ascii=False)+"\n").encode("utf-8"))
                sys.stdout.buffer.flush()
                continue
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except (EditorError, ValueError, AttributeError, TypeError) as exc:
            message = "入力サイズの上限（512,000バイト）を超えています。" if line is None else "Invalid request"
            response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": message}}
        sys.stdout.buffer.write((json.dumps(response, ensure_ascii=False)+"\n").encode("utf-8"))
        sys.stdout.buffer.flush()


class RequestReadError(EditorError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.http_status = status


class DeadlineSocketReader(io.RawIOBase):
    """A total request-input deadline, including clients that keep dripping bytes."""
    def __init__(self, handler):
        self.handler = handler

    def readable(self):
        return True

    def readinto(self, buffer):
        handler = self.handler
        deadline = handler._request_deadline
        remaining = handler.timeout if deadline is None else deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Request input deadline exceeded")
        handler.connection.settimeout(min(handler.timeout, remaining))
        return handler.connection.recv_into(buffer)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30
    semaphore = threading.BoundedSemaphore(1)

    def setup(self):
        super().setup()
        self.rfile.close()
        self._request_deadline = None
        self.rfile = io.BufferedReader(DeadlineSocketReader(self))

    def handle_one_request(self):
        self._request_deadline = time.monotonic() + self.timeout
        try:
            super().handle_one_request()
        except (ConnectionError, OSError):
            self.close_connection = True

    def _input_complete(self):
        self._request_deadline = None
        if hasattr(self, "connection"):
            self.connection.settimeout(self.timeout)

    def _read_body(self, length):
        try:
            raw = self.rfile.read(length)
        except (TimeoutError, OSError):
            self.close_connection = True
            raise RequestReadError("入力の受信が時間内に完了しませんでした。", 408) from None
        self._input_complete()
        if len(raw) != length:
            self.close_connection = True
            raise RequestReadError("入力の本文が最後まで届きませんでした。")
        return raw

    def _read_json(self, length):
        return self._decode_json(self._read_body(length))

    def _decode_json(self, raw):
        try:
            payload = json.loads(raw.decode("utf-8"))
        except UnicodeError:
            raise EditorError("入力はUTF-8のJSONで送信してください。") from None
        validate_unicode_input(payload)
        if not isinstance(payload, dict):
            raise EditorError("入力はJSONオブジェクトで指定してください。")
        return payload

    def reject(self, status, message):
        # An unread POST body must not become another HTTP/1.1 request.
        self.close_connection = True
        return self.send(status, {"error": message})

    def log_message(self, *_):
        pass

    def send(self, status, payload, content_type="application/json; charset=utf-8"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        context = getattr(self, "_rpc_context", None)
        signed_headers = rpc_auth.response_headers(context, status, data) if context else {}
        if context:
            self.close_connection = True
            self._rpc_context = None
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if getattr(self, "close_connection", False):
            self.send_header("Connection", "close")
        for key, value in signed_headers.items():
            self.send_header(key, value)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def valid_host(self):
        return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

    def do_GET(self):
        self._input_complete()
        if not self.valid_host():
            return self.reject(403, "ローカルホストから開いてください。")
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") is not None or len(lengths) > 1 or (lengths and lengths[0].strip() != "0"):
            return self.reject(400, "GETリクエストに本文を含めないでください。")
        parsed = urlparse(self.path)
        path = parsed.path
        if path == rpc_auth.CHALLENGE_PATH:
            try:
                query = parse_qs(parsed.query, strict_parsing=True)
                if set(query) != {"nonce"} or len(query["nonce"]) != 1:
                    raise rpc_auth.AuthenticationError("共有サービスの認証要求が不正です。")
                # A keep-alive connection can outlive the listener. Never turn
                # an old accepted socket into a proof oracle after its port is
                # released and acquired by another process. The lifecycle lock
                # orders proof issuance before shutdown/listener close.
                with self.server._lifecycle_lock:
                    if self.server.closing or self.server._closed.is_set():
                        return self.reject(503, "共有サービスは終了処理中です。アプリを再起動してください。")
                    challenge, self._rpc_session = rpc_auth.issue_challenge(query["nonce"][0], self.connection.getsockname()[1])
                    return self.send(200, challenge)
            except (rpc_auth.AuthenticationError, ValueError):
                return self.reject(401, "共有サービスの認証要求を確認できません。")
        if path == "/api/health":
            return self.send(200, {"application": "studio-yebisu-japanese-editor", "protocol_version": 1, "version": "0.2.12", "data_id": data_id(), "closing": self.server.closing})
        if path == "/api/providers":
            return self.send(200, provider_settings.public_config())
        if path == "/api/yomiyasu/status":
            return self.send(200, yomiyasu_updates.public_status())
        if path == "/api/integrations/codex":
            return self.send(200, integrations.status())
        if path == "/api/decider/status":
            if self.headers.get(rpc_auth.REQUEST_HEADER) is not None:
                try:
                    self._rpc_context = rpc_auth.verify_request(getattr(self, "_rpc_session", None), "GET", self.path, b"", self.headers)
                except rpc_auth.AuthenticationError as exc:
                    return self.reject(401, str(exc))
            return self.send(200, decider_runtime.local_status())
        if path == "/api/config":
            return self.send(200, config())
        if path.startswith("/api/progress/"):
            try:
                result = progress.lookup(path[len("/api/progress/"):])
            except progress.ProgressError as exc:
                return self.send(exc.status, {"error": str(exc)})
            if result is None:
                return self.send(404, {"error": "進捗はまだ登録されていないか、保存期限を過ぎています。"})
            return self.send(200, result)
        files = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
                 "/app.js": ("app.js", "application/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
        if path not in files:
            return self.send(404, {"error": "見つかりません。"})
        filename, mime = files[path]
        target = ROOT / "web" / filename
        if not target.is_file():
            return self.send(503, {"error": "画面の準備中です。"})
        self.send(200, target.read_bytes(), mime)

    def do_POST(self):
        if not self.valid_host():
            return self.reject(403, "ローカルホストから開いてください。")
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
            return self.reject(403, "この画面から操作してください。")
        route = urlparse(self.path).path
        if route not in ("/api/analyze", "/api/export", "/api/judge", "/api/decider/decide", "/api/decider/stop", "/api/settings", "/api/integrations/codex", "/api/service/stop", "/api/gemini/login"):
            return self.reject(404, "見つかりません。")
        if self.headers.get_content_type() != "application/json":
            return self.reject(415, "JSONで入力してください。")
        if self.headers.get("Transfer-Encoding") is not None or len(self.headers.get_all("Content-Length", [])) != 1:
            return self.reject(400, "入力サイズを一つのContent-Lengthで指定してください。")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self.reject(400, "入力サイズが不正です。")
        if not 0 < length <= 256000:
            return self.reject(413, "入力サイズの上限を超えています。")
        if getattr(self.server, "closing", False) and not route.startswith("/api/decider/"):
            return self.reject(503, "終了処理中です。現在の点検が終わるまで待っています。")
        if route in ("/api/settings", "/api/integrations/codex", "/api/service/stop", "/api/gemini/login"):
            try:
                payload = self._read_json(length)
                if route == "/api/settings":
                    if set(payload) - {"provider", "model", "api_key", "remove_key"}:
                        raise EditorError("生成設定の形式を確認してください。")
                    selected = {key: payload[key] for key in ("provider", "model") if key in payload}
                    if ("api_key" in payload or payload.get("remove_key") is True) and selected.get("provider") != "sarashina":
                        raise EditorError("APIキーの設定はSarashina用です。Geminiは公式CLIでGoogleログインしてください。")
                    result = provider_settings.save_settings(selected)
                    if "api_key" in payload:
                        result = provider_settings.save_api_key(selected.get("provider"), payload["api_key"])
                    elif payload.get("remove_key") is True:
                        result = provider_settings.remove_api_key(selected.get("provider"))
                    return self.send(200, result)
                if payload:
                    raise EditorError("この操作に追加の入力は不要です。")
                if route == "/api/gemini/login":
                    import cli_login
                    try:
                        return self.send(200, cli_login.launch())
                    except cli_login.LoginError as exc:
                        raise EditorError(str(exc)) from None
                if route == "/api/integrations/codex":
                    return self.send(200, integrations.install())
                from service_runtime import record_service_stop
                try:
                    record_service_stop()
                except OSError:
                    raise EditorError("終了の記録を保存できませんでした。ユーザー領域の空き容量と権限を確認してください。") from None
                self.server.closing = True
                self.send(200, {"status": "stopping"})
                def finish_service():
                    self.server.closing = True
                    with Handler.semaphore:
                        decider_runtime.local_stop()
                    self.server.shutdown()
                threading.Thread(target=finish_service, daemon=True).start()
                return
            except (EditorError, provider_settings.ProviderSettingsError, integrations.IntegrationError, ValueError) as exc:
                return self.send(getattr(exc, "http_status", 400), {"error": str(exc)})
        # Raw local worker requests bypass the analysis semaphore: an analysis
        # in this server can call the shared inference service on another thread.
        if route.startswith("/api/decider/"):
            try:
                if getattr(self, "_rpc_session", None) is None:
                    raise rpc_auth.AuthenticationError("共有サービスの認証が必要です。")
                raw = self._read_body(length)
                self._rpc_context = rpc_auth.verify_request(self._rpc_session, "POST", self.path, raw, self.headers)
                payload = self._decode_json(raw)
                if route == "/api/decider/stop":
                    decider_runtime.local_stop()
                    return self.send(200, {"status": "stopped"})
                if hasattr(self.server, "native_activity"):
                    self.server.native_activity = time.monotonic()
                result = decider_runtime.local_decide(payload.get("request"), timeout=payload.get("timeout"))
                if hasattr(self.server, "native_activity"):
                    self.server.native_activity = time.monotonic()
                return self.send(200, result)
            except decider_runtime.DeciderRuntimeError as exc:
                return self.send(503, {"error": exc.as_dict()})
            except rpc_auth.AuthenticationError as exc:
                return self.reject(401, str(exc))
            except (EditorError, json.JSONDecodeError) as exc:
                return self.send(getattr(exc, "http_status", 400), {"error": str(exc)})
        try:
            request_id = progress.validate_id(self.headers.get("X-Yebisu-Request-Id"))
        except progress.ProgressError as exc:
            return self.reject(exc.status, str(exc))
        if not self.semaphore.acquire(blocking=False):
            return self.reject(409, "別の点検を実行中です。完了後に操作してください。")
        try:
            with progress.operation(request_id, "checking" if route == "/api/export" else "rules"):
                payload = self._read_json(length)
                if route == "/api/analyze":
                    result = analyze(payload)
                elif route == "/api/judge":
                    result = review_proposals({**payload, "judge": True})
                else:
                    with yomiyasu_updates.using_revision(payload.get("yomiyasu_revision")):
                        result = check_final(payload.get("text"), payload.get("final"), payload.get("profile", "article"))
            self.send(200, result)
        except progress.ProgressError as exc:
            self.send(exc.status, {"error": str(exc)})
        except yomiyasu_updates.UpdateError:
            self.send(400, {"error": "校正に使ったYomiyasuの版を利用できません。もう一度校正してください。"})
        except (EditorError, json.JSONDecodeError) as exc:
            self.send(getattr(exc, "http_status", 400), {"error": str(exc)})
        except Exception:
            self.send(500, {"error": "処理を完了できませんでした。原文を残したまま再実行してください。"})
        finally:
            self.semaphore.release()


class EditorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"
    allow_reuse_port = False
    max_connections = 32
    request_queue_size = 16

    def __init__(self, port):
        self._lifecycle_lock = threading.RLock()
        self._connections = set()
        self._closed = threading.Event()
        self.closing = False
        self._connection_slots = threading.BoundedSemaphore(self.max_connections)
        super().__init__(("127.0.0.1", port), Handler)
        self.native_activity = time.monotonic()
        threading.Thread(target=self._idle_models, daemon=True).start()

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def process_request(self, request, client_address):
        if not self._connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self._lifecycle_lock:
            if self.closing or self._closed.is_set():
                self._connection_slots.release()
                self.shutdown_request(request)
                return
            self._connections.add(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._lifecycle_lock:
                self._connections.discard(request)
            self._connection_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._lifecycle_lock:
                self._connections.discard(request)
            self._connection_slots.release()

    def _idle_models(self):
        while not self._closed.wait(30):
            state = decider_runtime.local_status()
            if state.get("loaded") and state.get("status") == "ready" and time.monotonic()-self.native_activity > 300:
                try:
                    decider_runtime.local_stop()
                except decider_runtime.DeciderRuntimeError:
                    pass

    def _begin_shutdown(self):
        with self._lifecycle_lock:
            self.closing = True
            self._closed.set()

    def shutdown(self):
        # Stop issuing proofs before serve_forever exits, while retaining
        # existing authenticated RPC sockets until server_close is requested.
        self._begin_shutdown()
        super().shutdown()

    def server_close(self):
        # Disable proof issuance before releasing the listener's port. Closing
        # accepted sockets also removes stale keep-alive connections on BSD/macOS
        # where those sockets can coexist with a newly bound listener.
        self._begin_shutdown()
        super().server_close()
        with self._lifecycle_lock:
            connections = tuple(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()


def create_server(port=8896):
    server = EditorServer(port)
    yomiyasu_updates.start_background()
    return server


def main():
    if FROZEN:
        arguments = sys.argv[1:]
        if arguments and arguments[0] == "-B":
            arguments = arguments[1:]
        if arguments:
            candidate = Path(arguments[0])
            if candidate.name in ("yomiyasu_lint.py", "yomiyasu_check.py", "yomiyasu_diff.py"):
                try:
                    revision = yomiyasu_updates.revision_for_root(candidate.resolve().parent.parent)
                except (OSError, ValueError):
                    raise SystemExit("Unknown checker snapshot") from None
                sys.argv = [sys.argv[0], "internal-yomiyasu", candidate.name, "--yebisu-revision", revision, *arguments[1:]]
    if len(sys.argv) >= 3 and sys.argv[1] == "internal-yomiyasu":
        import runpy
        name = sys.argv[2]
        if name not in ("yomiyasu_lint.py", "yomiyasu_check.py", "yomiyasu_diff.py"):
            raise SystemExit("Unknown bundled checker")
        arguments = sys.argv[3:]
        revision = "bundled"
        if arguments[:1] == ["--yebisu-revision"]:
            if len(arguments) < 2:
                raise SystemExit("Missing checker snapshot")
            revision, arguments = arguments[1], arguments[2:]
        try:
            directory = yomiyasu_updates.root_for_revision(revision) / "scripts"
        except (OSError, ValueError):
            raise SystemExit("Invalid checker snapshot") from None
        sys.path.insert(0, str(directory))
        sys.argv = [str(directory / name), *arguments]
        runpy.run_path(str(directory / name), run_name="__main__")
        return
    parser = argparse.ArgumentParser(description="Yomiyasu Studio — 用途と作風に合わせる日本語校正")
    sub = parser.add_subparsers(dest="command", required=True)
    web = sub.add_parser("serve", help="ローカル画面を開くためのサーバー")
    web.add_argument("--port", type=int, default=8896)
    sub.add_parser("profiles", help="用途と接続状態をJSONで出力")
    sub.add_parser("mcp", help="stdio MCPとして起動")
    sub.add_parser("mcp-config", help="このアプリを起動するMCP設定をJSONで出力")
    sub.add_parser("review", help="標準入力の修正案を検証し、設定済みのローカルDeciderで判定")
    sub.add_parser("prepare-decider", help="標準入力JSONからDecider要求を準備。送信なし")
    sub.add_parser("decider-status", help="共有ローカルDeciderの状態を取得")
    sub.add_parser("decider-stop", help="共有ローカルDeciderのモデルを解放")
    sub.add_parser("install-codex", help="このPCのCodexへMCPと校正スキルを登録")
    sub.add_parser("gemini-login", help="Google公式Antigravity CLIを開いてGoogleログイン")
    sub.add_parser("stop-service", help="このアプリの共有サービスを終了")
    cli = sub.add_parser("analyze", help="標準入力の文章を点検")
    cli.add_argument("--profile", default="article")
    cli.add_argument("--strength", default="natural")
    cli.add_argument("--context", default="")
    cli.add_argument("--generate", action="store_true", help="選択した生成サービスへ原文を送信")
    cli.add_argument("--provider", choices=("gemini", "sarashina", "claude"))
    cli.add_argument("--model")
    args = parser.parse_args()
    if args.command in {"mcp", "profiles", "analyze", "review"}:
        yomiyasu_updates.start_background()
    if args.command == "mcp":
        mcp()
    elif args.command == "serve":
        server = create_server(args.port)
        print(f"Yomiyasu Studio: http://127.0.0.1:{server.server_port}\n終了は Ctrl+C", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            from service_runtime import record_service_stop
            try:
                record_service_stop()
            except OSError:
                pass
        finally:
            server.closing = True
            draining = None
            if not Handler.semaphore.acquire(blocking=False):
                # Keep the raw local judge endpoint available while an already
                # running generation completes its dependent comparison.
                draining = threading.Thread(target=server.serve_forever, daemon=True)
                draining.start()
                Handler.semaphore.acquire()
            try:
                decider_runtime.local_stop()
            finally:
                if draining:
                    server.shutdown()
                    draining.join(timeout=5)
                server.server_close()
                Handler.semaphore.release()
    else:
        try:
            if args.command == "profiles":
                result = config()
            elif args.command == "install-codex":
                result = integrations.install()
            elif args.command == "gemini-login":
                import cli_login
                try:
                    result = cli_login.launch()
                    print(result["notice"], file=sys.stderr, flush=True)
                    cli_login.wait_for_close()
                except cli_login.LoginError as exc:
                    raise EditorError(str(exc)) from None
            elif args.command == "stop-service":
                from service_runtime import stop_service
                try:
                    result = stop_service()
                except ValueError as exc:
                    raise EditorError(str(exc)) from None
            elif args.command == "decider-status":
                result = decider_runtime.status()
            elif args.command == "decider-stop":
                decider_runtime.stop()
                result = {"status": "stopped"}
            elif args.command == "mcp-config":
                command = cli_command("mcp")
                result = {"mcpServers": {"yebisu-japanese-editor": {"command": command[0], "args": command[1:],
                          "env": integrations.server_environment()}}}
            elif args.command in ("review", "prepare-decider"):
                payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
                validate_unicode_input(payload)
                result = (review_proposals(payload) if args.command == "review" else prepare_decider(payload))
            else:
                result = analyze({"text": sys.stdin.buffer.read().decode("utf-8"), "profile": args.profile,
                    "strength": args.strength, "context": args.context, "generate": args.generate,
                    "provider": args.provider, "model": args.model})
            sys.stdout.buffer.write((json.dumps(result, ensure_ascii=False, indent=2)+"\n").encode("utf-8"))
        except UnicodeError:
            sys.stderr.write("入力に不正なUnicode文字が含まれています。該当箇所を入力し直してください。\n")
            raise SystemExit(1) from None
        except (EditorError, json.JSONDecodeError, integrations.IntegrationError) as exc:
            sys.stderr.write(str(exc)+"\n")
            raise SystemExit(1)


if __name__ == "__main__":
    main()
