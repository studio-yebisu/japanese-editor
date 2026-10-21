"""Windows EdgeChromium shell; one loopback service, no model downloads."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import urllib.error
import urllib.request

from runtime_paths import PERSISTENT_ROOT
from service_runtime import data_id
from engine import VERSION

TITLE = "Yomiyasu Studio"
PORT = 8896
URL = f"http://127.0.0.1:{PORT}"
APPLICATION = "studio-yebisu-japanese-editor"
SETTINGS_PATH = PERSISTENT_ROOT / "desktop.json"
STOP_PATH = PERSISTENT_ROOT / "service-stop.json"


class DesktopError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


def service_identity(port=PORT):
    """Distinguish an absent listener from another application on this port."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=4) as response:
            value = json.loads(response.read(16385))
        if (not isinstance(value, dict) or value.get("application") != APPLICATION
                or value.get("protocol_version") != 1):
            raise DesktopError("8896番ポートの応答を、このアプリとして確認できません。使用中のアプリを確認してください。")
        if value.get("data_id") != data_id():
            raise DesktopError("保存先の異なるこのアプリが8896番ポートを使用しています。使用中のアプリを確認してください。")
        if value.get("closing"):
            raise DesktopError("共有サービスを終了しています。処理が終わってから、もう一度起動してください。")
        if value.get("version") != VERSION:
            raise DesktopError("異なる版の共有サービスが起動しています。旧版の処理を終えた後、READMEの「更新と接続の案内」に従ってサービスを終了し、新版を開き直してください。")
        return value
    except urllib.error.HTTPError as exc:
        raise DesktopError("別のアプリ、またはこのアプリの古いバージョンが8896番ポートを使用しています。先に使用中のアプリを確認してください。") from exc
    except urllib.error.URLError as exc:
        # A refused connection means we may bind; a timeout is not permission to
        # replace or terminate a listener that could still be handling a task.
        if isinstance(exc.reason, ConnectionRefusedError) or getattr(exc.reason, "winerror", None) == 10061:
            return None
        raise DesktopError("ローカルの接続状態を確認できません。8896番ポートを使用中のアプリを確認してください。") from exc
    except (ValueError, TimeoutError) as exc:
        raise DesktopError("8896番ポートの応答を読み取れません。使用中のアプリを確認してください。") from exc


class ServiceSession:
    def __init__(self, port=PORT):
        self.port = port
        self.server = None
        self.thread = None
        self.owned = False
        self._lock = threading.RLock()
        self._watch_stop = threading.Event()
        self._watcher = None
        self._stop_marker = self.read_stop_marker()
        self.recovery_disabled = False
        self._events = []

    @staticmethod
    def read_stop_marker():
        try:
            with STOP_PATH.open("rb") as stream:
                return hashlib.sha256(stream.read(4097)).hexdigest()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise DesktopError("共有サービスの終了状態を確認できません。保存先のアクセス権を確認してください。") from exc

    def _record(self, event):
        self._events.append({"event":event, "owned":self.owned, "pid":os.getpid(), "time":time.time()})
        self._events = self._events[-20:]
        try:
            PERSISTENT_ROOT.mkdir(parents=True, exist_ok=True)
            destination = PERSISTENT_ROOT / "desktop-service.json"
            destination.write_text(json.dumps({"events":self._events}, ensure_ascii=False, indent=2)+"\n", "utf-8")
        except OSError:
            pass  # Diagnostic metadata never takes precedence over the draft.

    def start(self):
        with self._lock:
            return self._start_unlocked()

    def _start_unlocked(self):
        if service_identity(self.port) is not None:
            self._record("attached_shared")
            return self
        import app
        try:
            self.server = app.create_server(self.port)
        except OSError:
            # Another copy may have won the bind race. Reuse only its explicit
            # protocol identity, never another port or a guessed process.
            if service_identity(self.port) is not None:
                self._record("attached_shared_after_bind_race")
                return self
            raise DesktopError("8896番ポートで起動できませんでした。使用中のアプリを確認してください。")
        self.thread = threading.Thread(target=self.server.serve_forever, name="editor-http", daemon=True)
        self.owned = True
        self.thread.start()
        if service_identity(self.port) is None:
            self.close()
            raise DesktopError("ローカルサーバーの起動を確認できませんでした。")
        self._record("started_owned")
        return self

    def close(self):
        self._watch_stop.set()
        if self._watcher and self._watcher is not threading.current_thread():
            self._watcher.join(timeout=3)
        with self._lock:
            self._close_owned_unlocked()

    def _close_owned_unlocked(self):
        if not self.owned:
            return
        import decider_runtime
        # Reject new work, while raw Decider RPC stays alive until an existing
        # generation+judgment finishes. Otherwise its external process can be
        # orphaned by shutdown of the daemon HTTP request thread.
        self.server.closing = True
        semaphore = self.server.RequestHandlerClass.semaphore
        semaphore.acquire()
        try:
            try:
                decider_runtime.local_stop()
            finally:
                try:
                    self.server.shutdown()
                finally:
                    self.server.server_close()
                    if self.thread:
                        self.thread.join(timeout=5)
                    self.owned = False
                    self._record("closed_owned")
        finally:
            semaphore.release()

    def recover_once(self):
        """Recover absence only; never overrule an explicit service stop."""
        with self._lock:
            if self.recovery_disabled or self._watch_stop.is_set():
                return "disabled"
            try:
                if self.read_stop_marker() != self._stop_marker:
                    self.recovery_disabled = True
                    self._close_owned_unlocked()
                    self._record("intentional_stop")
                    return "intentional_stop"
                if self.owned and self.thread and not self.thread.is_alive():
                    self._close_owned_unlocked()
                if service_identity(self.port) is not None:
                    return "healthy"
                if self.owned:
                    self._close_owned_unlocked()
                # Check again after a potentially slow probe/import race. The
                # marker is written before an explicit stop releases the port.
                if self.read_stop_marker() != self._stop_marker:
                    self.recovery_disabled = True
                    self._record("intentional_stop")
                    return "intentional_stop"
                self._start_unlocked()
                if self.read_stop_marker() != self._stop_marker:
                    self.recovery_disabled = True
                    self._close_owned_unlocked()
                    self._record("intentional_stop")
                    return "intentional_stop"
                self._record("recovered")
                return "recovered"
            except (DesktopError, OSError):
                return "unavailable"  # Another listener is never killed/replaced.

    def watch(self, on_recovered):
        def monitor():
            while not self._watch_stop.wait(2):
                try:
                    outcome = self.recover_once()
                except Exception:
                    self.recovery_disabled = True
                    self._record("recovery_error")
                    break
                if outcome == "recovered":
                    try:
                        on_recovered()
                    except Exception:
                        self._record("page_reconnect_deferred")
                elif outcome in ("intentional_stop", "disabled"):
                    break
        self._watcher = threading.Thread(target=monitor, name="editor-service-watch", daemon=True)
        self._watcher.start()


def reconnect_page(window):
    # A live editor may contain an unsaved draft. Recover its backend without a
    # navigation. Only an error/blank document is safe to reload automatically.
    has_editor = window.events.loaded.is_set() and window.evaluate_js("Boolean(document.querySelector('#source'))")
    if has_editor:
        window.evaluate_js("window.dispatchEvent(new Event('yebisu-service-reconnected'))")
    else:
        window.load_url(URL)


def read_window_settings(path=SETTINGS_PATH):
    try:
        value = json.loads(path.read_text("utf-8"))
        width, height = value.get("width"), value.get("height")
        if type(width) is int and type(height) is int and 760 <= width <= 3840 and 600 <= height <= 2160:
            return {"width": width, "height": height}
    except (OSError, ValueError, AttributeError):
        pass
    return {"width": 1280, "height": 900}


def save_window_settings(window, path=SETTINGS_PATH):
    width, height = window.width, window.height
    if type(width) is not int or type(height) is not int:
        return
    if not 760 <= width <= 3840 or not 600 <= height <= 2160:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"width": width, "height": height})+"\n", "utf-8")
    temporary.replace(path)


def has_webview2():
    import winreg
    relative = r"Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for hive, key_name in ((winreg.HKEY_CURRENT_USER, relative),
                           (winreg.HKEY_LOCAL_MACHINE, relative),
                           (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}")):
        try:
            with winreg.OpenKey(hive, key_name) as key:
                version = winreg.QueryValueEx(key, "pv")[0]
            if isinstance(version, str) and version and version != "0.0.0.0":
                return True
        except OSError:
            continue
    return False


def show_error(message):
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, TITLE, 0x10)
    elif sys.stderr is not None:
        print(message, file=sys.stderr)


def run_smoke_test(window, output, session, close=True):
    """Bounded renderer/UI proof with synthetic text; no generator or model."""
    record = {"status": "error", "native_window": True, "owned_server": session.owned,
              "cloud_calls": 0, "model_calls": 0}
    try:
        if not window.events.loaded.wait(30):
            raise DesktopError("画面の読み込みが時間内に完了しませんでした。")
        deadline = time.monotonic()+20
        while time.monotonic() < deadline:
            ready = window.evaluate_js("document.querySelector('#profile').options.length >= 6 && !document.querySelector('#profile').disabled")
            if ready:
                break
            time.sleep(0.2)
        else:
            record["page"] = window.evaluate_js("({url:location.href,title:document.title,profile_count:document.querySelector('#profile')?.options.length,status_text:document.querySelector('#status-text')?.textContent,error_text:document.querySelector('#error-message')?.textContent})")
            raise DesktopError("用途設定の読み込みが時間内に完了しませんでした。")
        window.evaluate_js("for (const id of ['enable-generation','enable-decider']) { const option=document.getElementById(id); option.checked=false; option.dispatchEvent(new Event('change', {bubbles:true})); } document.querySelector('#source').value='保存して下さい。上限は3件です。'; document.querySelector('#source').dispatchEvent(new Event('input', {bubbles:true})); document.querySelector('#analyze-generate').click();")
        deadline = time.monotonic()+20
        while time.monotonic() < deadline:
            record.update(window.evaluate_js("({title:document.title, profile_count:document.querySelector('#profile').options.length, source:document.querySelector('#source').value, status_text:document.querySelector('#status-text').textContent, rules_result_visible:!document.querySelector('#review-results').hidden, error_visible:!document.querySelector('#error-box').hidden})"))
            if record["rules_result_visible"] or record["error_visible"]:
                break
            time.sleep(0.2)
        import webview
        record.update(renderer=webview.renderer, window_title=window.title,
                      status="passed" if record.get("rules_result_visible") and not record.get("error_visible") else "error")
        if sys.platform == "win32" and window.native is not None:
            record.update(native_window_handle=int(window.native.Handle.ToInt64()),
                          native_window_title=str(window.native.Text), native_process_id=os.getpid())
    except Exception as exc:
        record["status"] = "error"
        record["error"] = str(exc)
    finally:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(record, ensure_ascii=False, indent=2)+"\n", "utf-8")
        if close:
            window.confirm_close = False
            window.destroy()
    return record


def run_recovery_smoke(window, output, session):
    """Coordinate with a test-owned temporary CLI; no drafts leave this PC."""
    record = {"status":"error", "cloud_calls":0, "model_calls":0}
    def write():
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(record, ensure_ascii=False, indent=2)+"\n", "utf-8")
    def wait_for(predicate, message, timeout=30):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.2)
        raise DesktopError(message)
    try:
        initial = run_smoke_test(window, output, session, close=False)
        if initial.get("status") != "passed" or session.owned:
            raise DesktopError("復旧検証用の共有サービスと画面を確認できませんでした。")
        draft = initial["source"]
        token = "synthetic-dom-"+str(time.time_ns())
        window.evaluate_js("window.__yebisuRecoveryToken = "+json.dumps(token))
        record.update(status="running", phase="ready_to_drop", initially_shared=True, native_process_id=os.getpid())
        write()
        wait_for(lambda: session.owned and service_identity() is not None, "GUI所有サービスへの復旧を確認できませんでした。")
        record.update(phase="recovered", recovered_owned=True,
                      draft_preserved=window.evaluate_js("document.querySelector('#source').value") == draft,
                      dom_preserved=window.evaluate_js("window.__yebisuRecoveryToken") == token)
        if not record["draft_preserved"] or not record["dom_preserved"]:
            raise DesktopError("復旧時に原稿またはDOMが変わりました。")
        window.evaluate_js("window.__yebisuRecoveryCheck=null; fetch('/api/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:document.querySelector('#source').value,profile:'article',generate:false})}).then(r=>r.json()).then(v=>window.__yebisuRecoveryCheck=v.stages?.rules==='executed'?'passed':'error').catch(()=>window.__yebisuRecoveryCheck='error');")
        wait_for(lambda: window.evaluate_js("window.__yebisuRecoveryCheck") is not None, "復旧後の規則検査が完了しませんでした。")
        record["rules_after_recovery"] = window.evaluate_js("window.__yebisuRecoveryCheck") == "passed"
        if not record["rules_after_recovery"]:
            raise DesktopError("復旧後の規則検査に失敗しました。")
        from service_runtime import stop_service
        stop_service()
        wait_for(lambda: session.recovery_disabled and not session.owned, "明示的なサービス停止を確認できませんでした。")
        time.sleep(3)
        record.update(explicit_stop_not_revived=service_identity() is None,
                      stop_marker_changed=session.read_stop_marker() != session._stop_marker)
        if not record["explicit_stop_not_revived"] or not record["stop_marker_changed"]:
            raise DesktopError("明示的な停止後の復旧抑制を確認できませんでした。")
        record.update(status="passed", phase="completed")
    except Exception as exc:
        record.update(status="error", error=str(exc))
    finally:
        write()
        window.confirm_close = False
        window.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description=TITLE)
    parser.add_argument("--smoke-test-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--smoke-recovery-output", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.smoke_test_output and args.smoke_recovery_output:
        parser.error("検証モードは一つだけ指定してください。")
    smoke_output = args.smoke_test_output or args.smoke_recovery_output
    if smoke_output:
        smoke_output = smoke_output.expanduser().resolve()
    if args.smoke_test_output:
        args.smoke_test_output = args.smoke_test_output.expanduser().resolve()
    if sys.platform not in ("win32", "darwin"):
        raise SystemExit("この起動方法はWindows / macOS向けです。ブラウザー版は app.py serve を使用してください。")
    session = None
    try:
        import yomiyasu_updates
        yomiyasu_updates.start_background()
        session = ServiceSession()
        if sys.platform == "win32" and not has_webview2():
            raise DesktopError("Microsoft Edge WebView2 Runtime が必要です。Microsoft 公式のダウンロードページから導入し、もう一度起動してください。\nhttps://developer.microsoft.com/microsoft-edge/webview2/\n自動ダウンロードは行いません。")
        session.start()
        import webview
        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.settings["ALLOW_FILE_URLS"] = False
        window = webview.create_window(TITLE, URL, min_size=(760, 600), text_select=True,
                                       confirm_close=True, **read_window_settings())
        window.events.closing += lambda: save_window_settings(window)
        def started():
            session.watch(lambda: reconnect_page(window))
            if args.smoke_test_output:
                run_smoke_test(window, args.smoke_test_output, session)
            elif args.smoke_recovery_output:
                run_recovery_smoke(window, smoke_output, session)
        webview.start(started, gui="edgechromium" if sys.platform == "win32" else None, private_mode=False,
                      storage_path=str(PERSISTENT_ROOT / "webview"), debug=False,
                      localization={"global.quitConfirmation": "このウィンドウを閉じますか？ 保存していない文章は残りません。"})
        if smoke_output:
            proof = json.loads(smoke_output.read_text("utf-8"))
            return 0 if proof.get("status") == "passed" else 1
        return 0
    except Exception as exc:
        if smoke_output:
            smoke_output.parent.mkdir(parents=True, exist_ok=True)
            smoke_output.write_text(json.dumps({"status":"error", "error":str(exc), "cloud_calls":0, "model_calls":0}, ensure_ascii=False, indent=2)+"\n", "utf-8")
        else:
            show_error(f"起動できませんでした。\n\n{exc}")
        return 1
    finally:
        if session is not None:
            session.close()


if __name__ == "__main__":
    raise SystemExit(main())
