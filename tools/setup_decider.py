"""Explicit local-only setup; official pinned Flash and isolated dependencies."""
import sys

# In particular, --check-only must not create an imported module's __pycache__.
sys.dont_write_bytecode = True

import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runtime_paths import PERSISTENT_ROOT
_worker_spec = importlib.util.spec_from_file_location("setup_decider_worker", ROOT / "tools/decider_worker.py")
worker = importlib.util.module_from_spec(_worker_spec)
_worker_spec.loader.exec_module(worker)
REVISION = "6d771ad7b5ecff5e93b8fc7b90d4c5ccc341954d"
REPO = "perplexity-ai/pplx-decider-flash-v1.2"
PINNED_DEPENDENCIES = {"torch": "2.14.0", "torchvision": "0.29.0", "transformers": "5.17.0",
                       "safetensors": "0.8.0", "pillow": "12.3.0", "huggingface-hub": "1.31.0"}
MAC24_TOTAL_BYTES = 24 * worker.GIB


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description="固定Flash版の専用環境と約9GBのモデルを準備")
    parser.add_argument("--preset", choices=("mac24",), help="Apple Silicon / macOS 14以降 / 総RAM 24 GiB以上向けの実験設定")
    parser.add_argument("--check-only", action="store_true", help="取得・インストール・設定変更をせず、環境と資源をJSONで点検")
    parser.add_argument("--device", choices=("cpu", "cuda:0"))
    parser.add_argument("--cpu-dtype", choices=("bfloat16", "float32"))
    parser.add_argument("--max-length", type=int, choices=(2048, 8192))
    parser.add_argument("--port", type=int, default=8896)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("ポート番号の範囲が不正です。")
    defaults = {"device": "cpu", "cpu_dtype": "bfloat16", "max_length": 2048}
    for field, value in defaults.items():
        explicit = getattr(args, field)
        if args.preset == "mac24" and explicit is not None and explicit != value:
            parser.error(f"--preset mac24 と --{field.replace('_', '-')}={explicit} は併用できません。")
        if explicit is None:
            setattr(args, field, value)
    return args


def macos_total_ram():
    """Read physical memory, independently of the worker's available-RAM estimate."""
    result = subprocess.run(["/usr/sbin/sysctl", "-n", "hw.memsize"], check=True, capture_output=True,
                            encoding="utf-8", timeout=5, env={**os.environ, "LC_ALL": "C", "LANG": "C"})
    text = result.stdout.strip()
    if len(text) > 32 or not text.isdecimal() or int(text) <= 0:
        raise ValueError("Unexpected hw.memsize")
    return int(text)


def dependency_snapshot(private):
    """Inspect only distribution metadata; do not import torch or launch the venv."""
    venv = private / ".venv"
    roots = [venv / "Lib/site-packages", *sorted((venv / "lib").glob("python*/site-packages"))]
    installed = {}
    for name in PINNED_DEPENDENCIES:
        for root in roots:
            for metadata in root.glob(name.replace("-", "_") + "-*.dist-info/METADATA"):
                try:
                    with metadata.open("r", encoding="utf-8") as stream:
                        # Only package headers are relevant; never read credentials or package code.
                        for _ in range(100):
                            line = stream.readline(4096)
                            if not line or line == "\n":
                                break
                            if line.startswith("Version: "):
                                installed[name] = line[9:].strip()
                                break
                except (OSError, UnicodeError):
                    continue
    return {"required": PINNED_DEPENDENCIES.copy(), "installed": installed,
            "versions_match": all(installed.get(name, "").split("+", 1)[0] == version
                                  for name, version in PINNED_DEPENDENCIES.items()),
            "package_imports": "not_run"}


def preflight(args, private):
    """Side-effect-free setup checks, not a model capability or inference test."""
    host = {"platform": sys.platform, "python_machine": platform.machine().lower(),
            "python_version": platform.python_version(), "macos_version": platform.mac_ver()[0]}
    issues = []
    resource = {}
    if args.preset == "mac24":
        if not (3, 12) <= sys.version_info[:2] <= (3, 14) or sys.version_info[:3] == (3, 14, 1):
            issues.append("mac24は固定依存のarm64 wheelがあるPython 3.12〜3.14が対象です（torchvisionが除外する3.14.1を除く）。")
        if sys.platform != "darwin":
            issues.append("mac24にはmacOSが必要です。")
        elif host["python_machine"] != "arm64" or sys.maxsize <= 2**32:
            issues.append("Apple Silicon上のarm64 Pythonが必要です。Rosettaのx86_64 Pythonは対象外です。")
        else:
            try:
                major = int(host["macos_version"].split(".", 1)[0])
            except (ValueError, IndexError):
                major = 0
            if major < 14:
                issues.append("固定したPyTorchのarm64 wheelにはmacOS 14以降が必要です。")
            try:
                total = macos_total_ram()
                resource.update(ram_total_bytes=total, preset_total_required_bytes=MAC24_TOTAL_BYTES)
                if total < MAC24_TOTAL_BYTES:
                    issues.append("mac24は総RAM 24 GiB以上を対象にした実験設定です。")
            except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
                issues.append("macOSの総RAMを確認できません。")
    # A wrong-platform preset never invokes platform-specific resource commands.
    if args.preset != "mac24" or sys.platform == "darwin":
        try:
            resource.update(worker.preflight({"device": args.device, "cpu_dtype": args.cpu_dtype,
                                              "max_length": args.max_length}))
        except worker.WorkerError as exc:
            resource.update(exc.details)
            issues.append(exc.message)
    uv = shutil.which("uv")
    if sys.version_info < (3, 12):
        issues.append("Python 3.12以降が必要です。")
    if uv is None:
        issues.append("セットアップにはuvが必要です。")
    return {"status": "ready_to_setup" if not issues else "not_ready", "check_only": args.check_only,
            "preset": args.preset, "experimental": args.preset == "mac24", "host": host,
            "selected": {"device": args.device, "cpu_dtype": args.cpu_dtype, "max_length": args.max_length,
                         "cpu_threads": 8}, "resources": resource, "issues": issues,
            "prerequisites": {"python_3_12_or_later": sys.version_info >= (3, 12), "uv_available": uv is not None},
            "dependencies": dependency_snapshot(private),
            "existing_config": "preserved_not_reconfigured" if (private / "config.json").exists() else "absent",
            "model_files_present": all((private / "models" / REVISION / name).is_file()
                                       for name in (*worker.REQUIRED_FILES, *worker.WEIGHT_HASHES)),
            "inference_verified": False, "native_mac_verified": False,
            "runtime_checks": {"model_hashes": "not_run", "cpu_bfloat16_ops": "not_run",
                               "cuda": "not_run", "inference": "not_run", "native_macos": "unverified"},
            "note": "準備できるかどうかの点検です。総RAMと現在の空きRAMは別です。演算対応・速度・実推論の成功は保証しません。"}


def main(argv=None):
    args = parse_arguments(argv)
    private = PERSISTENT_ROOT / "decider"
    if args.check_only or args.preset:
        report = preflight(args, private)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if args.check_only:
            return 0 if report["status"] == "ready_to_setup" else 2
        if report["status"] != "ready_to_setup":
            raise SystemExit("準備条件を満たしていません。取得・設定変更は行っていません。")
    uv = shutil.which("uv")
    if uv is None or sys.version_info < (3, 12):
        raise SystemExit("Python 3.12以降とuvを先に導入してください。既存環境は変更しません。")
    private.mkdir(parents=True, exist_ok=True)
    venv = private / ".venv"
    executable = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not executable.is_file():
        subprocess.run([uv, "venv", "--python", sys.executable, str(venv)], check=True)
    # This is an explicit setup command. Runtime inference never downloads or
    # installs anything. Both code and weights are verified against fixed hashes.
    subprocess.run([sys.executable, "-B", str(ROOT / "tools/download_decider.py")], check=True)
    model = private / "models" / REVISION
    subprocess.run([uv, "pip", "install", "--python", str(executable), "--link-mode", "copy", "-r", str(model / "requirements.txt")], check=True)
    configuration = {"enabled": True, "python": str(executable), "model_path": str(model),
                     "source_path": str(model / "source/src"), "model": "pplx-decider-flash-v1.2",
                     "revision": REVISION, "device": args.device, "cpu_dtype": args.cpu_dtype,
                     "cpu_threads": 8, "max_length": args.max_length, "startup_timeout_seconds": 300,
                     "request_timeout_seconds": 180, "service_url": f"http://127.0.0.1:{args.port}"}
    config = private / "config.json"
    if config.exists():
        print(f"既存設定を保持しました。変更する場合は {config} を編集してください。")
        if args.preset:
            print("mac24プリセットの値は既存設定へ反映していません。")
    else:
        config.write_bytes((json.dumps(configuration, ensure_ascii=False, indent=2)+"\n").encode("utf-8"))
    print("準備が終わりました。app.py serve を起動し、初回の判定時にモデルを読み込みます。")


if __name__ == "__main__":
    raise SystemExit(main())
