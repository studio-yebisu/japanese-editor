"""Pinned libllama Q8 protocol-v3 worker, supervised by the shared runtime."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid

from runtime_paths import RESOURCE_ROOT

MANIFEST = json.loads((RESOURCE_ROOT / "cpp/manifest.json").read_text("utf-8"))
IDENTITY = {"backend": "llama_cpp", "precision": "q8_0", "precision_override": True,
            "reference_path": "llama_cpp_protocol_v3_q8_0", "quantization": "Q8_0",
            "gguf_revision": MANIFEST["gguf_revision"], "gguf_sha256": MANIFEST["gguf_sha256"],
            "llama_commit": MANIFEST["llama_commit"], "worker_protocol": MANIFEST["worker_protocol"], "protocol_version": 3}
PUBLIC_IDENTITY_FIELDS = (*IDENTITY, "requested_device", "gpu_layers", "total_layers", "cpu_assisted", "oom_retries")


def configuration(data):
    import decider_runtime as runtime
    for field in ("cpp_executable", "gguf_path"):
        value = data.get(field)
        if not isinstance(value, str) or not Path(value).is_absolute() or not Path(value).is_file():
            raise runtime.DeciderRuntimeError("missing_runtime", "")
    if data.get("gguf_sha256") != MANIFEST["gguf_sha256"] or Path(data["gguf_path"]).stat().st_size != MANIFEST["gguf_bytes"]:
        raise runtime.DeciderRuntimeError("artifact_mismatch", "")
    if data.get("device", "auto") not in ("auto", "cpu", "cuda:0", "metal"):
        raise runtime.DeciderRuntimeError("invalid_config", "")
    for name, default, lower, upper in (("max_length", 2048, 64, 4096), ("gpu_margin_mib", 1024, 256, 16384), ("cpu_threads", 8, 1, 32)):
        value = data.get(name, default)
        if type(value) is not int or not lower <= value <= upper:
            raise runtime.DeciderRuntimeError("invalid_config", "")
        data[name] = value
    data["backend"], data["device"] = "llama_cpp", data.get("device", "auto")
    data["startup_timeout_seconds"] = runtime._seconds(data.get("startup_timeout_seconds"), 300, "起動待ち時間")
    data["request_timeout_seconds"] = runtime._seconds(data.get("request_timeout_seconds"), 180, "推論待ち時間")
    data["model"], data["revision"] = runtime.MODEL, runtime.REVISION
    return data


def command(cfg, cap=-1, inspect=False):
    result = [cfg["cpp_executable"], "--model", cfg["gguf_path"], "--device", cfg["device"],
              "--max-length", str(cfg["max_length"]), "--gpu-margin-mib", str(cfg["gpu_margin_mib"]),
              "--threads", str(cfg["cpu_threads"]), "--gpu-layer-cap", str(cap)]
    return result + (["--inspect"] if inspect else [])


def validate_identity(output, cfg):
    import decider_runtime as runtime
    allowed = ("cpu", "cuda:0", "metal") if cfg["device"] == "auto" else ("cpu", cfg["device"])
    if (output.get("model") != runtime.MODEL or output.get("revision") != runtime.REVISION
            or output.get("device") not in allowed or any(output.get(key) != value for key, value in IDENTITY.items())
            or type(output.get("gpu_layers")) is not int or type(output.get("total_layers")) is not int
            or output["total_layers"] != 32 or not 0 <= output["gpu_layers"] <= 33
            or (output["device"] == "cpu") != (output["gpu_layers"] == 0)
            or output.get("cpu_assisted") is not (0 < output["gpu_layers"] < 33)
            or output.get("requested_device") != cfg["device"]):
        raise runtime.DeciderRuntimeError("identity_mismatch", "")


def inspect(cfg, device):
    import decider_runtime as runtime
    selected = {**cfg, "device": device}
    try:
        with runtime._external_dll_search():
            process = subprocess.run(command(selected, inspect=True), stdin=subprocess.DEVNULL,
                                     capture_output=True, encoding="utf-8", timeout=30,
                                     env=runtime._external_environment(os.environ),
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        output = json.loads(process.stdout)
        if process.returncode or output.get("ok") is not True:
            error = runtime.public_error(output.get("error"))
            raise runtime.DeciderRuntimeError(error["code"], "", error["details"])
        validate_identity(output, selected)
        return runtime.public_error({"details": output.get("resources")})["details"]
    except runtime.DeciderRuntimeError:
        raise
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise runtime.DeciderRuntimeError("resource_check_failed", "") from None


def start(owner, cfg, deadline):
    import decider_runtime as runtime
    signature = json.dumps(cfg, sort_keys=True)
    if owner._process is not None and owner._process.poll() is None and owner._signature == signature and owner._ready:
        return
    owner.stop()
    runtime._remaining(deadline)
    if getattr(owner, "_cpp_config_signature", None) != signature:
        owner._cpp_cap = -1
        owner._cpp_config_signature = signature
    owner._phase = "loading"
    owner._cache_status(cfg)
    owner._cpp_events = {"oom": threading.Event(), "stderr_closed": threading.Event()}
    try:
        with runtime._external_dll_search():
            owner._process = subprocess.Popen(command(cfg, owner._cpp_cap), stdin=subprocess.PIPE,
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                               encoding="utf-8", bufsize=1, cwd=str(Path(cfg["cpp_executable"]).parent),
                                               env=runtime._external_environment(os.environ),
                                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except OSError:
        raise runtime.DeciderRuntimeError("launch_error", "") from None
    owner._messages = queue.Queue()
    owner._stdout_closed = threading.Event()
    owner._startup_rejection = None
    owner._stderr.clear()
    owner._signature = signature
    threading.Thread(target=owner._read_stdout, args=(owner._process, owner._messages, owner._stdout_closed), daemon=True).start()
    threading.Thread(target=owner._read_stderr, args=(owner._process, owner._cpp_events), daemon=True).start()
    startup_deadline = min(deadline, time.monotonic() + cfg["startup_timeout_seconds"])
    while True:
        output = owner._receive(runtime._remaining(startup_deadline))
        if output.get("ok") is not True:
            error = runtime.public_error(output.get("error"))
            if output.get("event") == "startup_error":
                owner._startup_rejection = (owner._process, owner._stdout_closed)
            raise runtime.DeciderRuntimeError(error["code"], "", error["details"])
        validate_identity(output, cfg)
        if output.get("event") == "loading":
            owner._cpp_last_layers = output["gpu_layers"]
            continue
        if output.get("event") != "ready":
            raise runtime.DeciderRuntimeError("protocol_error", "")
        owner._ready = output
        owner._phase = "ready"
        owner._last_error = None
        owner._cache_status(cfg)
        return


def decide(owner, cfg, serialized, deadline):
    import decider_runtime as runtime
    import progress
    retries = 0
    for attempt in range(3):
        try:
            owner._start(cfg, deadline)
            inference_deadline = min(deadline, time.monotonic() + cfg["request_timeout_seconds"])
            owner._phase = "busy"
            owner._cache_status(cfg)
            request_id = uuid.uuid4().hex
            owner._send('{"id":' + json.dumps(request_id) + ',"request":' + serialized + '}\n', inference_deadline)
            output = owner._receive(runtime._remaining(inference_deadline))
            validate_identity(output, cfg)
            if output.get("id") != request_id:
                raise runtime.DeciderRuntimeError("protocol_error", "")
            if output.get("ok") is not True:
                error = runtime.public_error(output.get("error"))
                raise runtime.DeciderRuntimeError(error["code"], "", error["details"], recoverable=error["recoverable"])
            output["oom_retries"] = retries
            owner._phase = "ready"
            owner._last_error = None
            owner._ready["oom_retries"] = retries
            owner._cache_status(cfg)
            return output
        except runtime.DeciderRuntimeError as error:
            events = getattr(owner, "_cpp_events", {})
            fatal = error.code in ("worker_exited", "allocation_failed", "inference_error", "startup_error")
            if fatal:
                events.get("stderr_closed", threading.Event()).wait(.25)
            if error.code == "worker_exited":
                # EOF can arrive just before the OS process handle becomes
                # signalled. Wait on our handle; never taskkill a recycled PID.
                try:
                    owner._process.wait(timeout=min(1, max(.01, deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    pass
            oom = error.code == "cuda_out_of_memory" or (fatal and events.get("oom", threading.Event()).is_set())
            if (not oom or cfg["device"] == "cpu" or getattr(owner, "_cpp_last_layers", 0) == 0
                    or attempt == 2 or time.monotonic() >= deadline):
                raise
            layers = owner._cpp_last_layers
            owner.stop()  # Confirm the old owner is gone before loading again.
            owner._cpp_cap = max(0, layers // 2) if attempt == 0 else 0
            retries += 1
            progress.report("decider", message="decider_oom_retry", attempt=retries, max_attempts=2)
    raise runtime.DeciderRuntimeError("cuda_out_of_memory", "")
