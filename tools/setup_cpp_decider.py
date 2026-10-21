"""Prepare the pinned Q8 worker without PyTorch or automatic model inference."""
import sys
sys.dont_write_bytecode = True

import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runtime_paths import PERSISTENT_ROOT

MANIFEST = json.loads((ROOT / "cpp/manifest.json").read_text("utf-8"))


def atomic_json(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".cpp-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="公式Q8 PPDDの準備。既存の実行設定は保持します。")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda:0", "metal"), default="auto")
    parser.add_argument("--runtime", type=Path, help="Built runtime directory containing bin/ppdd-worker")
    parser.add_argument("--build", action="store_true", help="Build from pinned llama.cpp source; compiler/CMake required")
    parser.add_argument("--check-only", action="store_true", help="No download, install, state writes or inference")
    args = parser.parse_args()
    if sys.platform == "darwin" and platform.machine() != "arm64":
        raise SystemExit("MacのC++・Metal版はApple Siliconのarm64 Pythonで準備してください。")
    runtime = args.runtime or ROOT / "cpp/runtime"
    if not (runtime / "bin").is_dir() and not args.runtime:
        runtime = ROOT / ".local/decider-cpp/runtime"
    executable = runtime.resolve() / "bin" / ("ppdd-worker.exe" if os.name == "nt" else "ppdd-worker")
    target = PERSISTENT_ROOT / "decider/gguf" / MANIFEST["gguf_revision"] / MANIFEST["gguf_file"]
    if args.check_only:
        print(json.dumps({"runtime_present": executable.is_file(), "model_present": target.is_file(),
                          "model_bytes": MANIFEST["gguf_bytes"], "ram_required_gib": 8,
                          "device": args.device, "inference_executed": False}))
        return
    if args.build:
        backend = "metal" if sys.platform == "darwin" else "cpu" if args.device == "cpu" or os.name != "nt" else "cuda"
        subprocess.run([sys.executable, "-B", str(ROOT / "tools/build_cpp_decider.py"), "--backend", backend,
                        "--output", str(runtime.resolve())], check=True)
    if not executable.is_file():
        raise SystemExit("C++ worker is missing. Use the Windows distribution, or --build with CMake and a C++17 compiler.")
    # Check resources before downloading. No weights/context are loaded here.
    if target.is_file():
        result = subprocess.run([str(executable), "--model", str(target), "--device", args.device, "--inspect"],
                                stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", timeout=30)
        output = json.loads(result.stdout)
        if result.returncode or output.get("ok") is not True:
            raise SystemExit("PPDD resource check failed: " + str(output.get("error", {}).get("code", "unknown")))
    subprocess.run([sys.executable, "-B", str(ROOT / "tools/download_cpp_decider.py")], check=True)
    private = PERSISTENT_ROOT / "decider"
    private.mkdir(parents=True, exist_ok=True)
    cfg = {"enabled": True, "backend": "llama_cpp", "model": MANIFEST["model"], "revision": MANIFEST["reference_revision"],
           "cpp_executable": str(executable), "gguf_path": str(target), "gguf_sha256": MANIFEST["gguf_sha256"],
           "device": args.device, "gpu_margin_mib": 1024, "max_length": 2048, "cpu_threads": 8,
           "startup_timeout_seconds": 300, "request_timeout_seconds": 180,
           "service_url": "http://127.0.0.1:8896", "service_timeout_seconds": 600}
    atomic_json(private / "llama_cpp-config.json", cfg)
    active = private / "config.json"
    created = not active.exists()
    if created:
        # Exclusive creation prevents a simultaneous setup from being replaced.
        try:
            with active.open("x", encoding="utf-8") as stream:
                json.dump(cfg, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
        except FileExistsError:
            created = False
    print(json.dumps({"status": "configured" if created else "prepared_existing_settings_preserved",
                      "backend": "llama_cpp", "device": args.device, "inference_executed": False}))
    print("準備できました。アプリの接続状態を更新し、既存設定がある場合は「PPDDの実行方式」でC++・公式Q8を選んで保存してください。")


if __name__ == "__main__":
    main()
