"""Pinned native Flash worker: UTF-8 JSON lines in/out; diagnostics on stderr.

No package installation, model download, hosted API, or automatic device fallback
is performed here. The parent process owns this worker's lifecycle.
"""
from __future__ import annotations

import argparse
import ctypes
import gc
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from unittest.mock import patch

# The dedicated worker Python also uses the distributed, standard-library-only
# public error policy. It must not import the UI runtime or model dependencies.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from decider_errors import public_error

MODEL = "pplx-decider-flash-v1.2"
REVISION = "6d771ad7b5ecff5e93b8fc7b90d4c5ccc341954d"
MANIFEST_SHA256 = "b92d6898a1a0cebeec8573c3ba34cfbb810a2920fc49da60097f3a8e2de24ebc"
GIB = 1024 ** 3
MAX_LINE_BYTES = 256_512
CPU_BFLOAT16_MODE = "native_fast_false_cpu_bfloat16_override"
SOURCE_HASHES = {
    "__init__.py": "48c26c24432476f2e4a43486d60e24ee0a6e124093a38fb8803686e99fecb5b3",
    "model.py": "ab5f42bd051d9e04cb9266e378d96dd72684c7a7f074efd9fc5117508e3dee91",
    "protocol.py": "b29d7a50d7214bd42397f8d3ddd5b5706cb8da2b257e175bf89b5a8da455d6a0",
    "types.py": "3de3e545d81692346f65c0f4ff9843952696a12f7b357cf6a5d0e8545badff8a",
    "fast.py": "afef05e1935be967b6ab6015bea3c86933dd5a45b7f349b0ed2ab5788a66cdbc",
}
WEIGHT_HASHES = {
    "model-00001-of-00002.safetensors": "1c1a0c1ab331c7aec1b4b7932eabf80593b56c694fe2b779209c0e0413df1cef",
    "model-00002-of-00002.safetensors": "21ba8301660e066f5849b60b1a36a242fc54ca18dd9bd526fdcbc50889171b70",
    "readout.safetensors": "6751578171026190d4db8078e97162b404fd984646e0e9aec13844e4dd5b1711",
}
REQUIRED_FILES = ("config.json", "decision_config.json", "joint_config.json", "model.safetensors.index.json",
                  "processor_config.json", "tokenizer.json", "tokenizer_config.json", "chat_template.jinja",
                  "release-manifest.json", "LICENSE", "NOTICE")


class WorkerError(RuntimeError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}


def _windows_memory():
    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                    ("total_phys", ctypes.c_ulonglong), ("available_phys", ctypes.c_ulonglong),
                    ("total_page", ctypes.c_ulonglong), ("available_page", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong), ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended", ctypes.c_ulonglong)]
    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise WorkerError("resource_check_failed", "メモリの空き容量を確認できません。")
    return status


def _macos_available_ram():
    # Apple's vm_stat prints free_count - speculative_count for "Pages free".
    # Thus these three printed counters do not double-count speculative pages.
    # https://github.com/apple-oss-distributions/system_cmds/blob/408bba7453608006b89772db185defbac8fe2fd0/vm_stat/vm_stat.c
    try:
        result = subprocess.run(["/usr/bin/vm_stat"], capture_output=True, check=True, timeout=5,
                                encoding="utf-8", env={**os.environ, "LC_ALL": "C", "LANG": "C"})
        if len(result.stdout) > 65536:
            raise ValueError("Unexpected vm_stat output size")
        header = re.search(r"\(page size of ([0-9]+) bytes\)", result.stdout)
        counters = re.findall(r"^Pages (free|inactive|speculative):\s*([0-9]+)\.\s*$", result.stdout, re.MULTILINE)
        if header is None or len(counters) != 3 or {name for name, _ in counters} != {"free", "inactive", "speculative"}:
            raise ValueError("Required vm_stat counters are missing")
        page_size = int(header.group(1))
        if not 1024 <= page_size <= 65536 or page_size & (page_size-1):
            raise ValueError("Unexpected vm_stat page size")
        return page_size*sum(int(value) for _, value in counters)
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError) as exc:
        raise WorkerError("resource_check_failed", "macOSの空きRAMの目安を確認できません。") from exc


def available_ram():
    if sys.platform == "darwin":
        return _macos_available_ram()
    if os.name == "nt":
        return _windows_memory().available_phys
    try:
        fields = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        return int(fields["MemAvailable"].strip().split()[0]) * 1024
    except (OSError, KeyError, ValueError) as exc:
        raise WorkerError("resource_check_failed", "この環境の空きRAMを確認できません。") from exc


def available_commit():
    if sys.platform == "darwin":
        return None  # macOS does not expose the Windows/Linux fixed commit floor.
    if os.name == "nt":
        return _windows_memory().available_page
    try:
        fields = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        return max(0, int(fields["CommitLimit"].strip().split()[0]) - int(fields["Committed_AS"].strip().split()[0])) * 1024
    except (OSError, KeyError, ValueError) as exc:
        raise WorkerError("resource_check_failed", "コミット可能なメモリの余裕を確認できません。") from exc


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_configuration(path):
    if not path.is_absolute():
        raise WorkerError("invalid_config", "設定ファイルは絶対パスで指定してください。")
    config = json.loads(path.read_text("utf-8-sig"))
    if not isinstance(config, dict) or config.get("enabled") is not True:
        raise WorkerError("disabled", "ローカルDeciderは有効になっていません。")
    if config.get("model", MODEL) != MODEL or config.get("revision", REVISION) != REVISION:
        raise WorkerError("unsupported_model", "指定モデルまたは固定コミットが一致しません。")
    model_path = Path(config.get("model_path", ""))
    source_path = Path(config.get("source_path", str(model_path / "source" / "src")))
    if not model_path.is_absolute() or not source_path.is_absolute():
        raise WorkerError("invalid_config", "モデルとソースの場所は絶対パスで指定してください。")
    device = config.get("device")
    if device not in ("cpu", "cuda", "cuda:0"):
        raise WorkerError("invalid_config", "deviceをcpuまたはcuda:0で明示してください。")
    config["device"] = "cuda:0" if device == "cuda" else device
    config["cpu_dtype"] = config.get("cpu_dtype", "float32")
    if config["cpu_dtype"] not in ("float32", "bfloat16"):
        raise WorkerError("invalid_config", "cpu_dtypeはfloat32またはbfloat16で指定してください。")
    for field, default, low, high in (("cpu_threads", 8, 1, 64), ("max_length", 8192, 128, 32768)):
        value = config.get(field, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise WorkerError("invalid_config", f"{field}の範囲が不正です。")
        config[field] = value
    return config, model_path, source_path


def preflight(config, torch=None):
    ram = available_ram()
    cpu_bfloat16 = config["device"] == "cpu" and config.get("cpu_dtype", "float32") == "bfloat16"
    restricted_bfloat16 = cpu_bfloat16 and config.get("max_length", 8192) <= 2048
    required_ram = (11 if restricted_bfloat16 else 12 if cpu_bfloat16 or config["device"] != "cpu" else 24) * GIB
    resource = {"ram_available_bytes": ram, "ram_required_bytes": required_ram}
    if sys.platform == "darwin":
        resource["ram_check"] = "vm_stat_free_inactive_speculative_estimate"
    if ram < required_ram:
        raise WorkerError("insufficient_memory", "空きRAMが不足しているため、モデルを読み込みませんでした。", resource)
    if cpu_bfloat16 and sys.platform == "darwin":
        resource["commit_check"] = "not_available_on_macos"
    elif cpu_bfloat16:
        commit = available_commit()
        resource.update(commit_available_bytes=commit, commit_required_bytes=required_ram)
        if commit < required_ram:
            raise WorkerError("insufficient_memory", "コミット可能なメモリが不足しているため、モデルを読み込みませんでした。", resource)
    if config["device"] != "cpu" and torch is not None:
        if not torch.cuda.is_available():
            raise WorkerError("device_unavailable", "指定したCUDAデバイスを利用できません。")
        free, total = torch.cuda.mem_get_info(torch.device(config["device"]))
        if sys.platform == "win32":
            # WDDM can report a CUDA allocation budget much larger than free
            # dedicated VRAM. Check the same physical GPU by UUID as well.
            free = min(free, dedicated_cuda_memory(torch, config["device"]))
        resource.update(cuda_available_bytes=free, cuda_total_bytes=total, cuda_required_bytes=11 * GIB)
        if free < 11 * GIB:
            raise WorkerError("insufficient_memory", "空きVRAMが不足しているため、モデルを読み込みませんでした。", resource)
    return resource


def dedicated_cuda_memory(torch, device):
    try:
        identity = str(torch.cuda.get_device_properties(torch.device(device)).uuid).removeprefix("GPU-")
        if not re.fullmatch(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", identity):
            raise ValueError("invalid GPU identity")
        result = subprocess.run(["nvidia-smi", "--id=GPU-" + identity,
                                 "--query-gpu=memory.free,memory.total", "--format=csv,noheader,nounits"],
                                capture_output=True, text=True, timeout=5,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        fields = result.stdout.strip().split(",")
        if result.returncode or len(fields) != 2:
            raise ValueError("unknown dedicated VRAM")
        free, total = (int(value.strip()) for value in fields)
        if not 0 <= free <= total or total <= 0:
            raise ValueError("invalid dedicated VRAM")
        return free * 1024 ** 2
    except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
        raise WorkerError("resource_check_failed", "NVIDIAドライバーから空きVRAMを確認できません。") from None


def precision_identity(config):
    override = config["device"] == "cpu" and config.get("cpu_dtype", "float32") == "bfloat16"
    return {"precision": "bfloat16" if override or config["device"] != "cpu" else "float32",
            "precision_override": override, "reference_path": CPU_BFLOAT16_MODE if override else True}


def check_cpu_bfloat16_ops(torch):
    """A tiny capability check, not model inference or FP32/BF16 parity proof."""
    try:
        with torch.inference_mode():
            matrix = torch.ones((2, 4), dtype=torch.bfloat16, device="cpu")
            product = matrix @ matrix.T
            query = matrix.reshape(1, 1, 2, 4)
            attended = torch.nn.functional.scaled_dot_product_attention(query, query, query)
            if product.dtype != torch.bfloat16 or attended.dtype != torch.bfloat16 or not torch.isfinite(attended).all().item():
                raise ValueError("unexpected BF16 operation result")
    except Exception as exc:
        raise WorkerError("precision_unsupported", "このCPU環境でbfloat16演算を確認できません。別精度へは切り替えていません。") from exc


def load_native_decider(decider_class, torch, model_path, config, backbone_class=None):
    """Keep native forward/protocol intact; override only explicit CPU load dtype.

    Official 0.3.0 selects FP32 on CPU. This scoped loader override avoids first
    allocating an FP32 backbone. It does not claim the official CPU reference
    precision or numerical parity and never changes torch.float32 globally.
    """
    kwargs = {"device": config["device"], "fast": False, "max_length": config["max_length"],
              "cpu_threads": config["cpu_threads"], "batch_tokens": config["max_length"]}
    if not precision_identity(config)["precision_override"]:
        return decider_class.from_pretrained(model_path, **kwargs)
    if backbone_class is None:
        from pplx_decider.model import Qwen3_5Model
        backbone_class = Qwen3_5Model
    original_loader = backbone_class.from_pretrained

    def load_bfloat16(*args, **loader_kwargs):
        if loader_kwargs.get("dtype") is not torch.float32:
            raise WorkerError("precision_override_error", "公式CPUローダーの精度指定が想定と異なります。読み込みを停止しました。")
        return original_loader(*args, **{**loader_kwargs, "dtype": torch.bfloat16})

    with patch.object(backbone_class, "from_pretrained", side_effect=load_bfloat16):
        decider = decider_class.from_pretrained(model_path, **kwargs)
    decider.readout.to(dtype=torch.bfloat16)
    return decider


def verify_artifacts(model_path, source_path):
    for name in REQUIRED_FILES:
        if not (model_path / name).is_file():
            raise WorkerError("missing_artifact", f"必要なモデルファイルがありません: {name}")
    if file_hash(model_path / "release-manifest.json") != MANIFEST_SHA256:
        raise WorkerError("artifact_mismatch", "固定した公式release manifestと一致しません。")
    manifest = json.loads((model_path / "release-manifest.json").read_text("utf-8"))
    if manifest.get("repo_id") != "perplexity-ai/" + MODEL:
        raise WorkerError("identity_mismatch", "モデルの公式release manifestが一致しません。")
    for name, expected in SOURCE_HASHES.items():
        path = source_path / "pplx_decider" / name
        if not path.is_file() or file_hash(path) != expected:
            raise WorkerError("artifact_mismatch", f"固定した公式ソースと一致しません: {name}")
    for name in REQUIRED_FILES:
        expected = manifest.get("files", {}).get(name, {}).get("sha256")
        if name != "release-manifest.json" and (not expected or file_hash(model_path / name) != expected):
            raise WorkerError("artifact_mismatch", f"モデル付属ファイルのハッシュが一致しません: {name}")
    for name, expected in WEIGHT_HASHES.items():
        path = model_path / name
        if not path.is_file() or file_hash(path) != expected:
            raise WorkerError("artifact_mismatch", f"固定したモデル重みと一致しません: {name}")


def validate_request(request):
    if not isinstance(request, dict) or set(request) - {"state", "questions", "model"}:
        raise WorkerError("invalid_request", "このワーカーはテキストのstateとquestionsだけを受け付けます。")
    if request.get("model", MODEL) != MODEL or "state" not in request:
        raise WorkerError("invalid_request", "要求モデルまたはstateが不正です。")
    questions = request.get("questions")
    if not isinstance(questions, dict) or len(questions) != 1:
        raise WorkerError("invalid_request", "Flashには1要求につき1質問を渡してください。")
    try:
        json.dumps(request, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise WorkerError("invalid_request", "要求にJSONとして扱えない値があります。") from exc
    return {"state": request["state"], "questions": questions}


def classify_request_error(exc, request_error_type, length_error_type, torch_module, config):
    """Only known per-request rejections may reuse the loaded model safely."""
    if isinstance(exc, length_error_type):
        return public_error({"code": "request_too_long",
                "details": {"max_length": config["max_length"], "exception_type": "LengthError"}, "recoverable": True})
    if isinstance(exc, (request_error_type, json.JSONDecodeError, UnicodeDecodeError)):
        return public_error({"code": "invalid_request",
                "details": {"exception_type": type(exc).__name__}, "recoverable": True})
    if isinstance(exc, WorkerError):
        return public_error({"code": exc.code, "details": exc.details, "recoverable": exc.code == "invalid_request"})
    cuda_oom = getattr(getattr(torch_module, "cuda", None), "OutOfMemoryError", None)
    if config["device"] != "cpu" and isinstance(cuda_oom, type) and isinstance(exc, cuda_oom):
        recoverable = True
        try:
            # Native forward frames can retain intermediate tensors. Release
            # those frames and unused allocator blocks, retaining model weights.
            exc.__traceback__ = None
            gc.collect()
            torch_module.cuda.empty_cache()
        except Exception:
            recoverable = False
        return public_error({"code": "cuda_out_of_memory", "details": {"exception_type": type(exc).__name__}, "recoverable": recoverable})
    return public_error({"code": "inference_error", "details": {"exception_type": type(exc).__name__}, "recoverable": False})


def startup_error(exc):
    """No exception string, arbitrary code, path, argv or key crosses stdout."""
    details = dict(exc.details) if isinstance(exc, WorkerError) and isinstance(exc.details, dict) else {}
    details.setdefault("exception_type", type(exc).__name__)
    return public_error({"code": exc.code if isinstance(exc, WorkerError) else "startup_error", "details": details})


def main():
    parser = argparse.ArgumentParser(description="Fixed native Decider Flash worker")
    parser.add_argument("--config", default=os.environ.get("YEBISU_DECIDER_CONFIG"))
    parser.add_argument("--inspect-device", choices=("cpu", "cuda:0"), help="Check resources without loading model weights")
    args = parser.parse_args()
    # Preserve the original stdout pipe for protocol messages. Redirect even
    # native-library stdout writes to stderr before importing heavy libraries.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", newline="\n", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())

    def emit(payload):
        protocol.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")
        protocol.flush()

    started = time.perf_counter()
    config = {}
    identity = {"model": MODEL, "revision": REVISION, "device": None}
    try:
        if not args.config:
            raise WorkerError("not_configured", "Deciderの設定ファイルが指定されていません。")
        config, model_path, source_path = load_configuration(Path(args.config))
        if args.inspect_device:
            config["device"] = args.inspect_device
        identity["device"] = config["device"]
        identity.update(precision_identity(config))
        preflight(config)
        print("Decider: checking fixed artifacts and resources", file=sys.stderr, flush=True)
        os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
                          TOKENIZERS_PARALLELISM="false", HF_DEACTIVATE_ASYNC_LOAD="1")
        sys.path.insert(0, str(source_path))
        import torch
        resources = preflight(config, torch)
        if args.inspect_device:
            if config["device"] != "cpu":
                # Check driver/allocation compatibility too, without a model.
                torch.empty(1, device=config["device"])
                torch.cuda.synchronize()
            emit({"event": "resources", "ok": True, **identity, "resources": resources})
            return 0
        if identity["precision_override"]:
            check_cpu_bfloat16_ops(torch)
        verify_artifacts(model_path, source_path)
        from pplx_decider import Decider, RequestError, LengthError
        print(f"Decider: loading {MODEL} on {config['device']}", file=sys.stderr, flush=True)
        decider = load_native_decider(Decider, torch, model_path, config)
        emit({"event": "ready", "ok": True, **identity, "load_latency_ms": round((time.perf_counter() - started) * 1000, 1),
              "max_length": config["max_length"], "resources": resources})
    except Exception as exc:
        emit({"event": "startup_error", "ok": False, **identity, "error": startup_error(exc)})
        return 1

    while True:
        line = sys.stdin.buffer.readline(MAX_LINE_BYTES + 1)
        if not line:
            return 0
        request_id = None
        inference_started = time.perf_counter()
        try:
            if len(line) > MAX_LINE_BYTES:
                while line and not line.endswith(b"\n"):
                    line = sys.stdin.buffer.readline(MAX_LINE_BYTES + 1)
                raise WorkerError("invalid_request", "要求が大きすぎます。")
            envelope = json.loads(line)
            if not isinstance(envelope, dict) or not isinstance(envelope.get("id"), (str, int)):
                raise WorkerError("invalid_request", "要求IDがありません。")
            request_id = envelope["id"]
            request = validate_request(envelope.get("request"))
            result = decider.decide(request)
            emit({"id": request_id, "ok": True, "result": result, **identity,
                  "latency_ms": round((time.perf_counter() - inference_started) * 1000, 1)})
        except Exception as exc:
            error = classify_request_error(exc, RequestError, LengthError, torch, config)
            emit({"id": request_id, "ok": False, **identity, "error": error,
                  "latency_ms": round((time.perf_counter() - inference_started) * 1000, 1)})
            if error["recoverable"] is not True:
                return 1


if __name__ == "__main__":
    raise SystemExit(main())
