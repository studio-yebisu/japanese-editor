"""Share one loopback Decider service; never fall back to a duplicate worker.

The application process uses only the standard library. Heavy dependencies and
model tensors remain in the explicitly configured worker Python environment.
Only an explicit DeciderRuntime instance and the server's local_* endpoints own
a direct worker; public module methods always use the configured service URL.
"""
from __future__ import annotations

import atexit
from collections import deque
from contextlib import contextmanager
import ctypes
import http.client
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit
import rpc_auth
from runtime_paths import RESOURCE_ROOT, PERSISTENT_ROOT

ROOT = RESOURCE_ROOT
CONFIG_PATH = PERSISTENT_ROOT / "decider" / "config.json"
WORKER_PATH = ROOT / "tools" / "decider_worker.py"
MODEL = "pplx-decider-flash-v1.2"
REVISION = "6d771ad7b5ecff5e93b8fc7b90d4c5ccc341954d"
MAX_REQUEST_BYTES = 256_000
CPU_BFLOAT16_MODE = "native_fast_false_cpu_bfloat16_override"
RECOVERABLE_REQUEST_ERRORS = frozenset({"request_too_long", "invalid_request", "cuda_out_of_memory"})
_DLL_LAUNCH_LOCK = threading.RLock()


def _frozen_windows():
    return os.name == "nt" and getattr(sys, "frozen", False)


def _external_environment(environment):
    """Keep PyInstaller hook DLL directories out of dedicated/system children."""
    clean = dict(environment)
    bundle = getattr(sys, "_MEIPASS", None)
    if not _frozen_windows() or not bundle:
        return clean
    root = Path(bundle).resolve()
    path_key = next((key for key in clean if key.upper() == "PATH"), None)
    if path_key is not None:
        entries = []
        for entry in clean[path_key].split(os.pathsep):
            try:
                bundled = entry and Path(entry.strip('"')).resolve().is_relative_to(root)
            except (OSError, ValueError):
                bundled = False
            if not bundled:
                entries.append(entry)
        clean[path_key] = os.pathsep.join(entries)
    return clean


def _windows_dll_api():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetDllDirectoryW.argtypes = [ctypes.c_uint32, ctypes.c_wchar_p]
    api.GetDllDirectoryW.restype = ctypes.c_uint32
    api.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
    api.SetDllDirectoryW.restype = ctypes.c_int
    return api


@contextmanager
def _external_dll_search():
    """Temporarily restore the Windows system DLL search for external children.

    PyInstaller's process-wide SetDllDirectory also affects children. Restore
    the exact prior directory immediately after launch, even if launch fails.
    """
    if not _frozen_windows():
        yield
        return
    with _DLL_LAUNCH_LOCK:
        api = _windows_dll_api()
        ctypes.set_last_error(0)
        needed = api.GetDllDirectoryW(0, None)
        if not needed and ctypes.get_last_error():
            raise OSError("cannot inspect inherited DLL search directory")
        previous = None
        if needed:
            buffer = ctypes.create_unicode_buffer(needed + 1)
            written = api.GetDllDirectoryW(len(buffer), buffer)
            if not written or written >= len(buffer):
                raise OSError("cannot inspect inherited DLL search directory")
            previous = buffer.value
        if not api.SetDllDirectoryW(None):
            raise OSError("cannot reset inherited DLL search directory")
        try:
            yield
        finally:
            if not api.SetDllDirectoryW(previous):
                raise OSError("cannot restore frozen DLL search directory")


class DeciderRuntimeError(RuntimeError):
    def __init__(self, code, message, details=None, *, recoverable=False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.recoverable = recoverable is True

    def as_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details, "recoverable": self.recoverable}


def _seconds(value, default, label):
    value = default if value is None else value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 3600:
        raise DeciderRuntimeError("invalid_config", f"{label}は0秒より大きく3,600秒以下で指定してください。")
    return float(value)


def _settings(path):
    try:
        data = json.loads(path.read_text("utf-8-sig"))
    except FileNotFoundError as exc:
        raise DeciderRuntimeError("not_configured", "ローカルDeciderの設定がありません。") from exc
    except (OSError, ValueError) as exc:
        raise DeciderRuntimeError("invalid_config", "ローカルDeciderの設定を読み取れません。") from exc
    if not isinstance(data, dict) or data.get("enabled") is not True:
        raise DeciderRuntimeError("disabled", "ローカルDeciderは有効になっていません。")
    if data.get("model", MODEL) != MODEL or data.get("revision", REVISION) != REVISION:
        raise DeciderRuntimeError("unsupported_model", "このランタイムは固定したFlash v1.2だけを実行します。")
    return data


def _configuration(path):
    data = _settings(path)
    data["cpu_dtype"] = data.get("cpu_dtype", "float32")
    if data["cpu_dtype"] not in ("float32", "bfloat16"):
        raise DeciderRuntimeError("invalid_config", "cpu_dtypeはfloat32またはbfloat16で指定してください。")
    for field in ("python", "model_path"):
        value = data.get(field)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise DeciderRuntimeError("invalid_config", f"{field}は絶対パスで指定してください。")
    if not Path(data["python"]).is_file() or not Path(data["model_path"]).is_dir():
        raise DeciderRuntimeError("missing_runtime", "設定されたPythonまたはモデルフォルダが見つかりません。")
    if data.get("device") not in ("cpu", "cuda", "cuda:0"):
        raise DeciderRuntimeError("invalid_config", "deviceをcpuまたはcuda:0で明示してください。")
    data["device"] = "cuda:0" if data["device"] == "cuda" else data["device"]
    source = data.get("source_path", str(Path(data["model_path"]) / "source" / "src"))
    if not isinstance(source, str) or not Path(source).is_absolute():
        raise DeciderRuntimeError("invalid_config", "source_pathは絶対パスで指定してください。")
    if not (Path(source) / "pplx_decider" / "model.py").is_file():
        raise DeciderRuntimeError("missing_runtime", "固定したDeciderの公式ソースが見つかりません。")
    data["source_path"] = source
    data["startup_timeout_seconds"] = _seconds(data.get("startup_timeout_seconds"), 300, "起動待ち時間")
    data["request_timeout_seconds"] = _seconds(data.get("request_timeout_seconds"), 180, "推論待ち時間")
    data["model"], data["revision"] = MODEL, REVISION
    return data


def _precision_identity(cfg):
    override = cfg.get("device") == "cpu" and cfg.get("cpu_dtype", "float32") == "bfloat16"
    return {"precision": "bfloat16" if override or cfg.get("device") in ("cuda", "cuda:0") else "float32",
            "precision_override": override, "reference_path": CPU_BFLOAT16_MODE if override else True}


def _validate_precision(output, cfg):
    expected = _precision_identity(cfg)
    if (expected["precision_override"] or "precision" in output) and any(output.get(key) != value for key, value in expected.items()):
        raise DeciderRuntimeError("identity_mismatch", "指定した精度と読み込み方式での実行を確認できませんでした。")


def _safe_error(error):
    code = error.get("code", "runtime_error") if isinstance(error, dict) else "runtime_error"
    messages = {"not_configured": "ローカルDeciderの設定がありません。", "disabled": "ローカルDeciderは無効です。",
                "insufficient_memory": "空きメモリが不足しているため、モデルを読み込んでいません。",
                "service_unavailable": "共有Deciderサービスに接続できません。", "timeout": "Deciderの応答が時間内に届きませんでした。",
                "service_authentication_failed": "共有Deciderサービスを認証できません。アプリと共有サービスを同じバージョンに更新してください。",
                "worker_stop_failed": "判定プロセスの停止を確認できません。重複起動を防ぐため再読み込みを保留しています。",
                "request_too_long": "入力がモデルのトークン上限を超えています。文章を短くして再実行してください。",
                "invalid_request": "Deciderへの要求形式を確認してください。",
                "missing_runtime": "専用環境またはモデルファイルが見つかりません。", "invalid_config": "Deciderの設定を確認してください。"}
    return {"code": code, "message": messages.get(code, "Deciderの処理を完了できませんでした。")}


class DeciderRuntime:
    def __init__(self, config_path=CONFIG_PATH):
        self.config_path = Path(config_path).resolve()
        self._lock = threading.RLock()
        self._process = None
        self._messages = None
        self._signature = None
        self._ready = None
        self._last_error = None
        self._cleanup_diagnostic = None
        self._stdout_closed = None
        self._startup_rejection = None
        self._stderr = deque(maxlen=20)
        self._phase = "idle"
        self._last_status = {"model": MODEL, "revision": REVISION, "loaded": False,
                             "status": "not_configured", "phase": "idle"}

    def _cache_status(self, cfg=None):
        loaded = self._process is not None and self._process.poll() is None and self._ready is not None
        output = {"model": MODEL, "revision": REVISION, "loaded": loaded, "phase": self._phase,
                  "status": self._phase if self._phase in ("loading", "busy") else "ready" if loaded else "configured"}
        if cfg is not None:
            output["device"] = cfg["device"]
            output["max_length"] = cfg.get("max_length", 8192)
            output.update(_precision_identity(cfg))
        elif self._last_status.get("device"):
            output["device"] = self._last_status["device"]
        if self._last_error:
            output["last_error"] = _safe_error(self._last_error)
        if loaded:
            output["runtime"] = self._ready
        self._last_status = output
        return dict(output)

    def status(self):
        if not self._lock.acquire(blocking=False):
            return dict(self._last_status)
        try:
            base = {"model": MODEL, "revision": REVISION, "loaded": False}
            try:
                cfg = _configuration(self.config_path)
            except DeciderRuntimeError as exc:
                self._last_status = {**base, "status": exc.code, "phase": "idle", "error": _safe_error(exc.as_dict())}
                return dict(self._last_status)
            loaded = (self._process is not None and self._process.poll() is None and self._ready is not None
                      and self._signature == json.dumps(cfg, sort_keys=True))
            self._phase = "ready" if loaded else "idle"
            output = self._cache_status(cfg)
            if not loaded:
                output.pop("runtime", None)
                output["loaded"], output["status"] = False, "configured"
                self._last_status = output
            return output
        finally:
            self._lock.release()

    @staticmethod
    def _read_stdout(process, messages, closed=None):
        eof = False
        try:
            for line in process.stdout:
                messages.put(line)
            eof = True
        except (OSError, ValueError):
            pass
        finally:
            if eof and closed is not None:
                closed.set()
            messages.put(None)

    def _read_stderr(self, process):
        try:
            for line in process.stderr:
                self._stderr.append(line.rstrip()[:2000])
        except (OSError, ValueError):
            pass

    def _receive(self, timeout):
        try:
            line = self._messages.get(timeout=timeout)
        except queue.Empty as exc:
            raise DeciderRuntimeError("timeout", "ローカルDeciderが指定時間内に応答しませんでした。") from exc
        if line is None:
            raise DeciderRuntimeError("worker_exited", "ローカルDeciderのプロセスが終了しました。")
        try:
            message = json.loads(line)
        except ValueError as exc:
            raise DeciderRuntimeError("protocol_error", "ローカルDeciderの応答を読み取れません。") from exc
        if not isinstance(message, dict):
            raise DeciderRuntimeError("protocol_error", "ローカルDeciderの応答形式が不正です。")
        return message

    def _start(self, cfg):
        signature = json.dumps(cfg, sort_keys=True)
        if self._process is not None and self._process.poll() is None and self._signature == signature and self._ready:
            return
        self.stop()
        if not WORKER_PATH.is_file():
            raise DeciderRuntimeError("missing_runtime", "Deciderワーカーが見つかりません。")
        self._phase = "loading"
        self._cache_status(cfg)
        environment = os.environ.copy()
        environment.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
                           TOKENIZERS_PARALLELISM="false", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        environment = _external_environment(environment)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            with _external_dll_search():
                self._process = subprocess.Popen(
                    [cfg["python"], "-B", "-u", str(WORKER_PATH), "--config", str(self.config_path)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", bufsize=1, cwd=ROOT, env=environment, creationflags=flags,
                )
        except OSError as exc:
            raise DeciderRuntimeError("launch_error", "専用PythonでDeciderを起動できませんでした。") from exc
        self._messages = queue.Queue()
        self._stdout_closed = threading.Event()
        self._startup_rejection = None
        self._stderr.clear()
        self._signature = signature
        threading.Thread(target=self._read_stdout, args=(self._process, self._messages, self._stdout_closed), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(self._process,), daemon=True).start()
        ready = self._receive(cfg["startup_timeout_seconds"])
        if ready.get("event") != "ready" or ready.get("ok") is not True:
            error = ready.get("error", {})
            if (ready.get("event") == "startup_error" and ready.get("ok") is False
                    and ready.get("model") == MODEL and ready.get("revision") == REVISION
                    and ready.get("device") == cfg["device"]
                    and isinstance(error, dict) and error.get("code") in {"insufficient_memory", "resource_check_failed"}):
                # The trusted worker returns immediately after this pre-model
                # guard error. The exact process and its protocol EOF together
                # can prove graceful exit without a racy PID-based taskkill.
                self._startup_rejection = (self._process, self._stdout_closed)
            raise DeciderRuntimeError(error.get("code", "startup_error"), error.get("message", "Deciderの起動に失敗しました。"), error.get("details"))
        if ready.get("model") != MODEL or ready.get("revision") != REVISION or ready.get("device") != cfg["device"]:
            raise DeciderRuntimeError("identity_mismatch", "要求したモデル・版・デバイスと起動結果が一致しません。")
        _validate_precision(ready, cfg)
        self._ready = ready
        self._last_error = None
        self._phase = "ready"
        self._cache_status(cfg)

    def decide(self, request, timeout=None):
        if not isinstance(request, dict):
            raise DeciderRuntimeError("invalid_request", "Deciderへの要求はJSONオブジェクトで指定してください。", recoverable=True)
        try:
            serialized = json.dumps(request, ensure_ascii=False, allow_nan=False)
            if len(serialized.encode("utf-8")) > MAX_REQUEST_BYTES:
                raise ValueError("request too large")
        except (ValueError, TypeError, UnicodeError) as exc:
            raise DeciderRuntimeError("invalid_request", "Deciderへの要求が大きすぎるか、JSONとして扱えません。", recoverable=True) from exc
        with self._lock:
            try:
                cfg = _configuration(self.config_path)
                seconds = _seconds(timeout, cfg["request_timeout_seconds"], "推論待ち時間")
                begun = time.monotonic()
                self._start(cfg)
                if timeout is not None:
                    seconds = min(cfg["request_timeout_seconds"], seconds-(time.monotonic()-begun))
                    if seconds <= 0:
                        raise DeciderRuntimeError("timeout", "今回の判定時間の上限に達しました。")
                self._phase = "busy"
                self._cache_status(cfg)
                request_id = uuid.uuid4().hex
                self._process.stdin.write('{"id":' + json.dumps(request_id) + ',"request":' + serialized + '}\n')
                self._process.stdin.flush()
                response = self._receive(seconds)
                if response.get("id") != request_id:
                    raise DeciderRuntimeError("protocol_error", "Deciderの要求と応答のIDが一致しません。")
                if response.get("model") != MODEL or response.get("revision") != REVISION or response.get("device") != cfg["device"]:
                    raise DeciderRuntimeError("identity_mismatch", "Deciderの応答モデル・版・デバイスが一致しません。")
                _validate_precision(response, cfg)
                if response.get("ok") is not True:
                    error = response.get("error", {})
                    code = error.get("code", "inference_error")
                    raise DeciderRuntimeError(code, error.get("message", "Deciderの推論が完了しませんでした。"), error.get("details"),
                                              recoverable=code in RECOVERABLE_REQUEST_ERRORS and error.get("recoverable") is True)
                self._last_error = None
                self._phase = "ready"
                self._cache_status(cfg)
                return response
            except (OSError, BrokenPipeError) as exc:
                error = DeciderRuntimeError("worker_io_error", "Deciderとの通信が切れました。推論結果は得られていません。")
                self._last_error = error.as_dict()
                self._cleanup_after_error(error)
                raise error from exc
            except DeciderRuntimeError as exc:
                self._last_error = exc.as_dict()
                if exc.recoverable and self._process is not None and self._process.poll() is None and self._ready is not None:
                    self._phase = "ready"
                    self._cache_status(cfg)
                else:
                    self._cleanup_after_error(exc)
                raise

    def _cleanup_after_error(self, trigger):
        try:
            self.stop()
        except DeciderRuntimeError as cleanup:
            def code(value):
                return value if isinstance(value, str) and len(value) <= 64 and value.replace("_", "").isalnum() else "runtime_error"
            # Private diagnostic only: public status continues to expose the
            # sanitized cleanup error. Never log raw exceptions or worker text.
            self._cleanup_diagnostic = {"trigger_code": code(trigger.code), "cleanup_code": code(cleanup.code)}
            try:
                print("Decider cleanup failure: " + json.dumps(self._cleanup_diagnostic), file=sys.stderr, flush=True)
            except (OSError, ValueError):
                pass
            raise cleanup from trigger

    def _graceful_startup_exit(self, process):
        rejection = self._startup_rejection
        if not rejection or rejection[0] is not process:
            return False
        try:
            process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return rejection[1].wait(timeout=1)

    def _failed_stop(self, operation, return_code=None):
        error = DeciderRuntimeError(
            "worker_stop_failed", "ワーカー全体の終了を確認できません。既存プロセスを確認するまで、新しいワーカーは起動しません。",
            {"operation": operation, **({"return_code": return_code} if isinstance(return_code, int) else {})},
        )
        self._stop_failure = error
        self._last_error = error.as_dict()
        self._ready, self._signature = None, None
        self._phase = "stop_failed"
        self._cache_status()
        return error

    def stop(self):
        with self._lock:
            process = self._process
            pending_failure = getattr(self, "_stop_failure", None)
            guarded_exit = bool(self._startup_rejection and self._startup_rejection[0] is process)
            gracefully_exited = process is not None and guarded_exit and self._graceful_startup_exit(process)
            # A failed tree stop can leave a heavy child after its wrapper
            # exits. Retain ownership and block replacement rather than using
            # an exited/recycled PID to guess at that child's identity.
            if pending_failure is not None and not gracefully_exited and (process is None or process.poll() is not None):
                raise pending_failure
            if process is None:
                self._ready, self._signature = None, None
                self._phase = "idle"
                self._cache_status()
                return
            if guarded_exit and not gracefully_exited and process.poll() is not None:
                raise self._failed_stop("startup_exit_without_protocol_eof")
            if not gracefully_exited and process.poll() is None:
                operation, return_code = "process_stop", None
                try:
                    if os.name == "nt":
                        pid = process.pid
                        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
                            raise OSError("invalid owned worker PID")
                        # Windows venv Python may be a wrapper around the
                        # tensor-owning interpreter. Kill only this live,
                        # owned Popen process and its descendants.
                        kwargs = {"stdin": subprocess.DEVNULL, "capture_output": True, "timeout": 10,
                                  "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
                        if _frozen_windows():
                            kwargs["env"] = _external_environment(os.environ)
                        operation = "taskkill"
                        with _external_dll_search():
                            killed = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], **kwargs)
                        return_code = killed.returncode
                        if killed.returncode != 0:
                            raise OSError("owned worker tree termination failed")
                        operation = "wait_after_taskkill"
                        process.wait(timeout=5)
                    else:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    error = self._failed_stop(operation, return_code)
                    raise error from exc
            self._process = None
            self._stop_failure = None
            self._startup_rejection = None
            self._stdout_closed = None
            self._ready, self._signature = None, None
            self._phase = "idle"
            self._cache_status()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass


_RUNTIME = DeciderRuntime()
atexit.register(_RUNTIME.stop)


def local_status():
    """HTTP raw endpoints use this function, avoiding service recursion."""
    return _RUNTIME.status()


def local_decide(request, timeout=None):
    return _RUNTIME.decide(request, timeout=timeout)


def local_stop():
    _RUNTIME.stop()
    return {"status": "stopped", "phase": "idle", "loaded": False, "model": MODEL, "revision": REVISION}


def _service_configuration():
    try:
        data = json.loads(CONFIG_PATH.read_text("utf-8-sig"))
    except FileNotFoundError as exc:
        raise DeciderRuntimeError("not_configured", "共有Deciderサービスの設定がありません。") from exc
    except (OSError, ValueError) as exc:
        raise DeciderRuntimeError("invalid_config", "ローカルDeciderの設定を読み取れません。") from exc
    if not isinstance(data, dict):
        raise DeciderRuntimeError("invalid_config", "Deciderの設定形式が不正です。")
    if data.get("enabled") is not True:
        raise DeciderRuntimeError("disabled", "ローカルDeciderは有効になっていません。")
    if not data.get("service_url"):
        raise DeciderRuntimeError("service_unavailable", "共有Deciderサービスのservice_urlが設定されていません。")
    if data.get("model", MODEL) != MODEL or data.get("revision", REVISION) != REVISION:
        raise DeciderRuntimeError("unsupported_model", "このランタイムは固定したFlash v1.2だけを実行します。")
    if data.get("cpu_dtype", "float32") not in ("float32", "bfloat16"):
        raise DeciderRuntimeError("invalid_config", "cpu_dtypeはfloat32またはbfloat16で指定してください。")
    value = data["service_url"]
    try:
        url = urlsplit(value) if isinstance(value, str) else None
        valid = (url is not None and not any(ord(character) <= 32 or ord(character) == 127 for character in value)
                 and url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost")
                 and url.username is None and url.password is None and url.path in ("", "/")
                 and not url.query and not url.fragment and 0 < (80 if url.port is None else url.port) <= 65535)
    except ValueError:
        valid = False
    if not valid:
        raise DeciderRuntimeError("invalid_config", "service_urlは認証情報を含まないループバックHTTPアドレスで指定してください。")
    startup = _seconds(data.get("startup_timeout_seconds"), 300, "起動待ち時間")
    inference = _seconds(data.get("request_timeout_seconds"), 180, "推論待ち時間")
    remote = _seconds(data.get("remote_timeout_seconds"), startup + inference + 15, "共有サービス待ち時間")
    return {"url": value.rstrip("/"), "timeout": remote, "cpu_dtype": data.get("cpu_dtype", "float32"), "device": data.get("device")}


def _remote(service, path, payload=None, timeout=3):
    try:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if body is not None and len(body) > MAX_REQUEST_BYTES:
            raise ValueError("request too large")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise DeciderRuntimeError("invalid_request", "Deciderへの要求が大きすぎるか、JSONとして扱えません。", recoverable=True) from exc
    try:
        # Authenticate the exclusively bound loopback peer before sending any
        # manuscript bytes, then bind request and response to that same socket.
        # There is no legacy unsigned fallback, proxy or redirect path.
        status_code, raw = rpc_auth.authenticated_request(service["url"], path, body, timeout=timeout)
        output = json.loads(raw)
        if not isinstance(output, dict):
            raise ValueError("response is not an object")
        if status_code == 200:
            return output
        error = output.get("error")
        if status_code in (400, 409, 422, 503) and isinstance(error, dict) and isinstance(error.get("code"), str):
            raise DeciderRuntimeError(error["code"], str(error.get("message", "Deciderの処理が完了しませんでした。")), error.get("details"),
                                      recoverable=error["code"] in RECOVERABLE_REQUEST_ERRORS and error.get("recoverable") is True)
        if status_code in (400, 409, 422, 503) and isinstance(output.get("code"), str):
            raise DeciderRuntimeError(output["code"], str(error or "Deciderの処理が完了しませんでした。"))
        raise DeciderRuntimeError("service_unavailable", "共有Deciderサービスに接続できません。サーバーの起動状態を確認してください。")
    except rpc_auth.AuthenticationError as exc:
        raise DeciderRuntimeError("service_authentication_failed", str(exc)) from exc
    except (OSError, http.client.HTTPException) as exc:
        raise DeciderRuntimeError("service_unavailable", "共有Deciderサービスに接続できません。サーバーの起動状態を確認してください。") from exc
    except ValueError as exc:
        raise DeciderRuntimeError("service_protocol_error", "共有Deciderサービスの応答形式が不正です。") from exc


def _public_status(output):
    allowed = ("status", "phase", "loaded", "model", "revision", "device", "precision", "precision_override", "reference_path", "max_length")
    clean = {key: output[key] for key in allowed if key in output}
    for key in ("error", "last_error"):
        if key in output:
            clean[key] = _safe_error(output[key])
    runtime = output.get("runtime")
    if isinstance(runtime, dict):
        clean["runtime"] = {key: runtime[key] for key in ("load_latency_ms", "max_length", "reference_path", "precision", "precision_override") if key in runtime}
        resources = runtime.get("resources")
        if isinstance(resources, dict):
            fields = ("ram_available_bytes", "ram_required_bytes", "commit_available_bytes", "commit_required_bytes",
                      "cuda_available_bytes", "cuda_total_bytes", "cuda_required_bytes")
            clean["runtime"]["resources"] = {key: resources[key] for key in fields if isinstance(resources.get(key), (int, float))}
    return clean


def status():
    try:
        service = _service_configuration()
        output = _remote(service, "/api/decider/status", timeout=3)
        if output.get("model") != MODEL or output.get("revision") != REVISION:
            raise DeciderRuntimeError("identity_mismatch", "共有Deciderサービスのモデルまたは版が一致しません。")
        return {**_public_status(output), "transport": "local_service"}
    except DeciderRuntimeError as exc:
        return {"status": exc.code, "phase": "idle", "loaded": False, "model": MODEL, "revision": REVISION,
                "error": _safe_error(exc.as_dict())}


def decide(request, timeout=None):
    if not isinstance(request, dict):
        raise DeciderRuntimeError("invalid_request", "Deciderへの要求はJSONオブジェクトで指定してください。", recoverable=True)
    service = _service_configuration()
    seconds = _seconds(timeout, service["timeout"], "共有サービス待ち時間")
    output = _remote(service, "/api/decider/decide", {"request": request, "timeout": seconds}, timeout=min(3600, seconds+5))
    if output.get("model") != MODEL or output.get("revision") != REVISION:
        raise DeciderRuntimeError("identity_mismatch", "共有Deciderサービスのモデルまたは版が一致しません。")
    _validate_precision(output, service)
    if output.get("ok") is not True:
        error = output.get("error", {})
        code = error.get("code", "inference_error")
        raise DeciderRuntimeError(code, error.get("message", "Deciderの推論が完了しませんでした。"), error.get("details"),
                                  recoverable=code in RECOVERABLE_REQUEST_ERRORS and error.get("recoverable") is True)
    return output


def stop():
    service = _service_configuration()
    return _remote(service, "/api/decider/stop", {}, timeout=15)
