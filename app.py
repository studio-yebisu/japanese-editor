"""Run a loopback UI, JSON CLI, or stdio MCP using one proofreading engine."""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

if getattr(sys, "frozen", False):
    sys.dont_write_bytecode = True

from engine import EditorError, ROOT, CONFIG, analyze, check_final, config, finalize_proposals, review_proposals
from runtime_paths import cli_command, FROZEN
from decider import prepare as prepare_decider
import decider_runtime
import provider_settings
import integrations
import progress
from service_runtime import data_id

TOOLS = [
    {"name": "japanese_editor_profiles", "description": "校正の用途・強さと接続状態を取得する。文章は送信しない。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "japanese_editor_analyze", "description": "原文の規則を点検する。generate=trueの場合のみ選択した外部サービスへ原文を送り、修正案を生成する。通常のホスト校正はホスト自身で案を作りreview_proposalsへ渡す。原本は変更しない。",
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
                       "final": {"type": "string"}, "profile": {"type": "string"}},
                     "required": ["text", "final"], "additionalProperties": False}},
]
EDIT_SCHEMA = {"type": "array", "maxItems": 50, "items": {"type": "object",
    "properties": {key: {"type": "string"} for key in ("before", "after", "reason", "rule")},
    "required": ["before", "after", "reason", "rule"], "additionalProperties": False}}
TOOLS.extend([
    {"name": "japanese_editor_finalize", "description": "採用した修正候補から共通エンジンが最終稿を組み立て、その正確な本文をrun/verifyする。原本を変更しない。",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}, "edits": EDIT_SCHEMA,
        "profile": {"type": "string"}}, "required": ["text", "edits"], "additionalProperties": False}},
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


def mcp():
    from service_runtime import ensure_service
    if os.environ.get("YEBISU_EDITOR_NO_AUTOSTART") != "1":
        ensure_service()
    # MCP stdio transport is UTF-8 newline-delimited JSON-RPC, no stdout logs.
    for line in sys.stdin.buffer:
        request_id = None
        try:
            if len(line) > 512000:
                raise EditorError("入力サイズの上限を超えています。")
            request = json.loads(line)
            request_id = request.get("id")
            method, params = request.get("method"), request.get("params", {})
            if request_id is None:
                continue
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion", "2024-11-05"),
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "yebisu-japanese-editor", "version": "0.2.5"},
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
        except (ValueError, AttributeError, TypeError) as exc:
            response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "Invalid request"}}
        sys.stdout.buffer.write((json.dumps(response, ensure_ascii=False)+"\n").encode("utf-8"))
        sys.stdout.buffer.flush()


class Handler(BaseHTTPRequestHandler):
    semaphore = threading.BoundedSemaphore(1)

    def log_message(self, *_):
        pass

    def send(self, status, payload, content_type="application/json; charset=utf-8"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def valid_host(self):
        return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

    def do_GET(self):
        if not self.valid_host():
            return self.send(403, {"error": "ローカルホストから開いてください。"})
        path = urlparse(self.path).path
        if path == "/api/health":
            return self.send(200, {"application": "studio-yebisu-japanese-editor", "protocol_version": 1, "version": "0.2.5", "data_id": data_id(), "closing": self.server.closing})
        if path == "/api/providers":
            return self.send(200, provider_settings.public_config())
        if path == "/api/integrations/codex":
            return self.send(200, integrations.status())
        if path == "/api/decider/status":
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
            return self.send(403, {"error": "ローカルホストから開いてください。"})
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
            return self.send(403, {"error": "この画面から操作してください。"})
        route = urlparse(self.path).path
        if route not in ("/api/analyze", "/api/export", "/api/judge", "/api/decider/decide", "/api/decider/stop", "/api/settings", "/api/integrations/codex", "/api/service/stop", "/api/gemini/login"):
            return self.send(404, {"error": "見つかりません。"})
        if self.headers.get_content_type() != "application/json":
            return self.send(415, {"error": "JSONで入力してください。"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self.send(400, {"error": "入力サイズが不正です。"})
        if not 0 < length <= 256000:
            return self.send(413, {"error": "入力サイズの上限を超えています。"})
        if getattr(self.server, "closing", False) and not route.startswith("/api/decider/"):
            return self.send(503, {"error": "終了処理中です。現在の点検が終わるまで待っています。"})
        if route in ("/api/settings", "/api/integrations/codex", "/api/service/stop", "/api/gemini/login"):
            try:
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise EditorError("入力はJSONオブジェクトで指定してください。")
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
                return self.send(400, {"error": str(exc)})
        # Raw local worker requests bypass the analysis semaphore: an analysis
        # in this server can call the shared inference service on another thread.
        if route.startswith("/api/decider/"):
            try:
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise EditorError("入力はJSONオブジェクトで指定してください。")
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
            except (EditorError, json.JSONDecodeError) as exc:
                return self.send(400, {"error": str(exc)})
        try:
            request_id = progress.validate_id(self.headers.get("X-Yebisu-Request-Id"))
        except progress.ProgressError as exc:
            return self.send(exc.status, {"error": str(exc)})
        if not self.semaphore.acquire(blocking=False):
            return self.send(409, {"error": "別の点検を実行中です。完了後に操作してください。"})
        try:
            with progress.operation(request_id, "checking" if route == "/api/export" else "rules"):
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise EditorError("入力はJSONオブジェクトで指定してください。")
                if route == "/api/analyze":
                    result = analyze(payload)
                elif route == "/api/judge":
                    result = review_proposals({**payload, "judge": True})
                else:
                    result = check_final(payload.get("text"), payload.get("final"), payload.get("profile", "article"))
            self.send(200, result)
        except progress.ProgressError as exc:
            self.send(exc.status, {"error": str(exc)})
        except (EditorError, json.JSONDecodeError) as exc:
            self.send(400, {"error": str(exc)})
        except Exception:
            self.send(500, {"error": "処理を完了できませんでした。原文を残したまま再実行してください。"})
        finally:
            self.semaphore.release()


class EditorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port):
        super().__init__(("127.0.0.1", port), Handler)
        self.native_activity = time.monotonic()
        self.closing = False
        self._closed = threading.Event()
        threading.Thread(target=self._idle_models, daemon=True).start()

    def _idle_models(self):
        while not self._closed.wait(30):
            state = decider_runtime.local_status()
            if state.get("loaded") and state.get("status") == "ready" and time.monotonic()-self.native_activity > 300:
                try:
                    decider_runtime.local_stop()
                except decider_runtime.DeciderRuntimeError:
                    pass

    def server_close(self):
        self._closed.set()
        super().server_close()


def create_server(port=8896):
    return EditorServer(port)


def main():
    if FROZEN:
        arguments = sys.argv[1:]
        if arguments and arguments[0] == "-B":
            arguments = arguments[1:]
        if arguments:
            allowed = {str((ROOT / "vendor/yomiyasu/scripts" / name).resolve()): name
                       for name in ("yomiyasu_lint.py", "yomiyasu_check.py", "yomiyasu_diff.py")}
            if arguments[0] in allowed:
                sys.argv = [sys.argv[0], "internal-yomiyasu", allowed[arguments[0]], *arguments[1:]]
    if len(sys.argv) >= 3 and sys.argv[1] == "internal-yomiyasu":
        import runpy
        name = sys.argv[2]
        if name not in ("yomiyasu_lint.py", "yomiyasu_check.py", "yomiyasu_diff.py"):
            raise SystemExit("Unknown bundled checker")
        directory = ROOT / "vendor/yomiyasu/scripts"
        sys.path.insert(0, str(directory))
        sys.argv = [str(directory / name), *sys.argv[3:]]
        runpy.run_path(str(directory / name), run_name="__main__")
        return
    parser = argparse.ArgumentParser(description="用途を切り替える日本語校正プロトタイプ")
    sub = parser.add_subparsers(dest="command", required=True)
    web = sub.add_parser("serve", help="ローカル画面を開くためのサーバー")
    web.add_argument("--port", type=int, default=8896)
    sub.add_parser("profiles", help="用途と接続状態をJSONで出力")
    sub.add_parser("mcp", help="stdio MCPとして起動")
    sub.add_parser("mcp-config", help="この実体を起動するMCP設定JSONを出力")
    sub.add_parser("review", help="標準入力の修正案を検証し、設定済みのローカルDeciderで判定")
    sub.add_parser("prepare-decider", help="標準入力JSONからDecider要求を準備。送信なし")
    sub.add_parser("decider-status", help="共有ローカルDeciderの状態を取得")
    sub.add_parser("decider-stop", help="共有ローカルDeciderのモデルを解放")
    sub.add_parser("install-codex", help="このPCのCodexへMCPと校正スキルを登録")
    sub.add_parser("gemini-login", help="Google公式Antigravity CLIを開きGoogleログイン")
    sub.add_parser("stop-service", help="このアプリの共有サービスを終了")
    cli = sub.add_parser("analyze", help="標準入力の文章を点検")
    cli.add_argument("--profile", default="article")
    cli.add_argument("--strength", default="natural")
    cli.add_argument("--context", default="")
    cli.add_argument("--generate", action="store_true", help="選択した生成サービスへ原文を送信")
    cli.add_argument("--provider", choices=("gemini", "sarashina", "claude"))
    cli.add_argument("--model")
    args = parser.parse_args()
    if args.command == "mcp":
        mcp()
    elif args.command == "serve":
        server = create_server(args.port)
        print(f"日本語校正: http://127.0.0.1:{server.server_port}\n終了は Ctrl+C", flush=True)
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
                result = (review_proposals(payload) if args.command == "review" else prepare_decider(payload))
            else:
                result = analyze({"text": sys.stdin.buffer.read().decode("utf-8"), "profile": args.profile,
                    "strength": args.strength, "context": args.context, "generate": args.generate,
                    "provider": args.provider, "model": args.model})
            sys.stdout.buffer.write((json.dumps(result, ensure_ascii=False, indent=2)+"\n").encode("utf-8"))
        except (EditorError, json.JSONDecodeError, integrations.IntegrationError) as exc:
            sys.stderr.write(str(exc)+"\n")
            raise SystemExit(1)


if __name__ == "__main__":
    main()
