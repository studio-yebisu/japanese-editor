"""Build the pinned PPDD protocol-v3 sidecar; no model inference or download."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "cpp/manifest.json").read_text("utf-8"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("cuda", "metal", "cpu"), default="metal" if sys.platform == "darwin" else "cuda" if sys.platform == "win32" else "cpu")
    parser.add_argument("--llama-source", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / ".local/decider-cpp/runtime")
    parser.add_argument("--cuda-root", type=Path)
    parser.add_argument("--cuda-architectures", default="75;80;86;89;90",
                        help="CMake CUDA architectures; use 89 for a local RTX 4090 build")
    parser.add_argument("--jobs", type=int, default=6)
    args = parser.parse_args()
    if not 1 <= args.jobs <= 16:
        parser.error("jobs must be 1..16")
    environment = os.environ.copy()
    cmake, ninja = shutil.which("cmake"), shutil.which("ninja")
    if os.name == "nt":
        vswhere = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft Visual Studio/Installer/vswhere.exe"
        if vswhere.is_file():
            result = subprocess.run([str(vswhere), "-latest", "-products", "*", "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"], capture_output=True, text=True, check=True)
            installation = Path(result.stdout.strip())
            setup = installation / "VC/Auxiliary/Build/vcvars64.bat"
            if setup.is_file():
                result = subprocess.run(f'call "{setup}" >nul && set', shell=True, executable=os.environ.get("COMSPEC", "C:/Windows/System32/cmd.exe"), capture_output=True, text=True, check=True)
                for line in result.stdout.splitlines():
                    key, marker, value = line.partition("=")
                    if marker and key and not key.startswith("="):
                        environment[key] = value
            bundled = installation / "Common7/IDE/CommonExtensions/Microsoft/CMake"
            cmake = cmake or str(bundled / "CMake/bin/cmake.exe")
            ninja = ninja or str(bundled / "Ninja/ninja.exe")
    if not cmake or not Path(cmake).is_file():
        raise SystemExit("Install CMake and a C++17 compiler first.")
    build = ROOT / ".local/decider-cpp" / ("build-" + args.backend)
    command = [cmake, "-S", str(ROOT / "cpp"), "-B", str(build), "-DCMAKE_BUILD_TYPE=Release",
               "-DCMAKE_INSTALL_PREFIX=" + str(args.output.resolve()), "-DGGML_CUDA=" + ("ON" if args.backend == "cuda" else "OFF"),
               "-DGGML_METAL=" + ("ON" if args.backend == "metal" else "OFF")]
    if ninja and Path(ninja).is_file():
        command += ["-G", "Ninja", "-DCMAKE_MAKE_PROGRAM=" + str(ninja)]
    if args.cuda_root:
        environment["CUDACXX"] = (args.cuda_root / ("bin/nvcc.exe" if os.name == "nt" else "bin/nvcc")).resolve().as_posix()
        command += ["-DCUDAToolkit_ROOT=" + args.cuda_root.resolve().as_posix(), "-DCMAKE_CUDA_COMPILER=" + environment["CUDACXX"]]
    if args.backend == "cuda":
        if any(not item.isdigit() for item in args.cuda_architectures.split(";")):
            parser.error("CUDA architectures must be semicolon-separated numbers")
        command += ["-DCMAKE_CUDA_ARCHITECTURES=" + args.cuda_architectures]
    if args.llama_source:
        upstream = args.llama_source.resolve()
        commit = subprocess.run(["git", "-C", str(upstream), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        if commit != MANIFEST["llama_commit"]:
            raise SystemExit("llama.cpp commit does not match cpp/manifest.json")
        command += ["-DFETCHCONTENT_SOURCE_DIR_LLAMA=" + str(upstream)]
    subprocess.run(command, env=environment, check=True)
    subprocess.run([cmake, "--build", str(build), "--config", "Release", "--target", "ppdd-worker", "--parallel", str(args.jobs)], env=environment, check=True)
    # Install only built runtime libraries and the worker. Never package model
    # files or the entire dependency source checkout.
    output = args.output.resolve() / "bin"
    output.mkdir(parents=True, exist_ok=True)
    suffix = ".exe" if os.name == "nt" else ""
    workers = list(build.rglob("ppdd-worker" + suffix))
    if len(workers) != 1:
        raise SystemExit("Cannot identify the built worker")
    shutil.copy2(workers[0], output / workers[0].name)
    extensions = (".dll",) if os.name == "nt" else (".dylib", ".metallib") if sys.platform == "darwin" else (".so",)
    for item in build.rglob("*"):
        if item.is_file() and any(item.name.endswith(ext) or ext + "." in item.name for ext in extensions):
            shutil.copy2(item, output / item.name)
    notices = args.output.resolve() / "licenses"
    notices.mkdir(exist_ok=True)
    upstream = args.llama_source.resolve() if args.llama_source else build / "_deps/llama-src"
    for name, relative in [("llama-MIT.txt", "LICENSE"), ("sha256-Public-Domain.txt", "vendor/hash/sha256/LICENSE")]:
        shutil.copy2(upstream / relative, notices / name)
    shutil.copy2(upstream / "vendor/nlohmann/json.hpp", notices / "nlohmann-json-with-MIT.hpp")
    if os.name == "nt":
        redist = Path(environment.get("VCToolsRedistDir", ""))
        crt = list((redist / "x64").glob("Microsoft.VC*.CRT"))
        if len(crt) != 1:
            raise SystemExit("Cannot identify the licensed MSVC x64 redistributable directory")
        for name in ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
            shutil.copy2(crt[0] / name, output / name)
        (notices / "MICROSOFT-REDISTRIBUTION.txt").write_text(
            "Microsoft Visual C++ Runtime\nCopyright (c) Microsoft Corporation. All rights reserved.\n\n"
            "Unmodified app-local runtime: msvcp140.dll, vcruntime140.dll, vcruntime140_1.dll.\n"
            "These Microsoft components are not licensed under the application's MIT license.\n"
            "Redistribution list: https://learn.microsoft.com/en-us/visualstudio/releases/2019/redistribution\n"
            "Microsoft runtime terms: https://visualstudio.microsoft.com/license-terms/vs2022-cruntime/\n", "utf-8")
    if args.backend == "cuda" and os.name == "nt":
        cuda = args.cuda_root or Path(environment.get("CUDA_PATH", ""))
        for name in ("cudart64_12.dll", "cublas64_12.dll", "cublasLt64_12.dll"):
            if not (cuda / "bin" / name).is_file():
                raise SystemExit("CUDA 12 redistributable runtime DLLs are required: " + name)
            shutil.copy2(cuda / "bin" / name, output / name)
        shutil.copy2(cuda / "EULA.txt", notices / "NVIDIA-CUDA-EULA.txt")
    import hashlib
    inventory = []
    for item in sorted(output.iterdir()):
        if item.is_file():
            with item.open("rb") as stream:
                inventory.append({"file": item.name, "sha256": hashlib.file_digest(stream, "sha256").hexdigest(), "bytes": item.stat().st_size})
    (args.output / "BUILD.json").write_text(json.dumps({**MANIFEST, "backend": args.backend,
        "cuda_architectures": args.cuda_architectures if args.backend == "cuda" else None,
        "files": inventory}, indent=2) + "\n", "utf-8")
    print(json.dumps({"runtime": str(args.output.resolve()), "backend": args.backend, "llama_commit": MANIFEST["llama_commit"]}))


if __name__ == "__main__":
    main()
