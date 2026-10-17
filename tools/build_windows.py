"""Build an unsigned Windows onedir app using an isolated, pinned environment."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time
import urllib.request
import zipfile

from build_release import VERSION

ROOT = Path(__file__).resolve().parents[1]
BUILD_ROOT = ROOT / ".local" / "build"
PROOF_SUFFIX = "v" + VERSION.replace(".", "")
BUILD_REQUIREMENTS = (
    "pywebview==6.2.1", "pyinstaller==6.22.3", "pyinstaller-hooks-contrib==2026.8",
    "pythonnet==3.2.1", "clr-loader==0.3.1", "cffi==2.1.1", "pycparser==3.11",
    "bottle==0.13.4", "proxy-tools==0.1.0", "typing-extensions==4.16.0",
    "altgraph==0.17.5", "packaging==26.3", "pefile==2024.8.26",
    "pywin32-ctypes==0.2.3", "setuptools==84.0.0",
)


def pinned_notice(url, expected, filename):
    """Build-time only retrieval of small, fixed upstream license artifacts."""
    path = BUILD_ROOT / "license_sources" / filename
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=60) as response:
            content = response.read(32*1024*1024+1)
        if len(content) > 32*1024*1024:
            raise SystemExit("License source exceeded the bounded download size")
        if hashlib.sha256(content).hexdigest() != expected:
            raise SystemExit("License source hash mismatch")
        path.write_bytes(content)
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise SystemExit("Cached license source hash mismatch")
    return path


def make_icon(target):
    """Original geometric 文 mark, generated without an image dependency."""
    images = []
    for size in (32, 64, 128):
        pixels = bytearray()
        for y in range(size-1, -1, -1):
            for x in range(size):
                u, v = (x+0.5)/size, (y+0.5)/size
                outside = (u < .035 or u > .965 or v < .035 or v > .965)
                mark = (.2 <= u <= .8 and .29 <= v <= .35) or (.44 <= u <= .56 and .13 <= v <= .24)
                mark |= .36 <= v <= .81 and (abs(u-(.32+.82*(v-.36))) < .045 or abs(u-(.68-.82*(v-.36))) < .045)
                red, green, blue, alpha = ((0, 0, 0, 0) if outside else (31, 51, 61, 255) if mark else (229, 190, 119, 255))
                pixels.extend((blue, green, red, alpha))
        mask = bytes(((size+31)//32)*4*size)
        header = struct.pack("<IIIHHIIIIII", 40, size, size*2, 1, 32, 0, len(pixels)+len(mask), 0, 0, 0, 0)
        images.append((size, header+pixels+mask))
    offset = 6+16*len(images)
    result = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    for size, data in images:
        result.extend(struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(data), offset))
        offset += len(data)
    for _, data in images:
        result.extend(data)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(result)


def collect_notices():
    destination = BUILD_ROOT / "third_party_licenses"
    destination.mkdir(parents=True, exist_ok=True)
    packages = []
    for requirement in BUILD_REQUIREMENTS:
        name, expected = requirement.split("==")
        distribution = importlib.metadata.distribution(name)
        if distribution.version != expected:
            raise SystemExit(f"Build dependency mismatch: {name} != {expected}")
        package_dir = destination / f"{name}-{distribution.version}"
        package_dir.mkdir(exist_ok=True)
        files = []
        for member in distribution.files or []:
            if not any(word in member.name.lower() for word in ("license", "copying", "notice")):
                continue
            original = Path(distribution.locate_file(member))
            if not original.is_file():
                continue
            # Keep the license's relative path, not an arbitrary wheel filename.
            relative = Path(*[part for part in member.parts if part not in ("..", ".")])
            copied = package_dir / relative
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, copied)
            files.append(copied.relative_to(destination).as_posix())
        metadata = {"name": name, "version": distribution.version,
                    "license": distribution.metadata.get("License-Expression") or distribution.metadata.get("License"),
                    "project_urls": distribution.metadata.get_all("Project-URL") or [], "notice_files": files}
        (package_dir / "PACKAGE.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2)+"\n", "utf-8")
        packages.append(metadata)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise SystemExit("Python LICENSE.txt is required for the binary distribution")
    shutil.copyfile(python_license, destination / "PYTHON-LICENSE.txt")
    # These notices are absent from the Python wheel's dist-info. Keep the
    # exact SDK notices and the proxy source's BSD notice beside its MIT metadata.
    sdk_url = "https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/1.0.3856.49/microsoft.web.webview2.1.0.3856.49.nupkg"
    sdk_hash = "bc0f76eb911b569838dc4aa8f8d325269b966bedb592863d26211aef3a099f1a"
    sdk = pinned_notice(sdk_url, sdk_hash, "webview2-1.0.3856.49.nupkg")
    sdk_destination = destination / "Microsoft.Web.WebView2-1.0.3856.49"
    sdk_destination.mkdir(exist_ok=True)
    sdk_binaries = []
    with zipfile.ZipFile(sdk) as archive:
        for filename in ("LICENSE.txt", "NOTICE.txt"):
            (sdk_destination / filename).write_bytes(archive.read(filename))
        webview_lib = Path(importlib.metadata.distribution("pywebview").locate_file("webview/lib"))
        for filename in ("Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll", "runtimes/win-x64/native/WebView2Loader.dll"):
            binary = webview_lib / filename
            sha256 = hashlib.sha256(binary.read_bytes()).hexdigest()
            matches = [member for member in archive.namelist() if member.endswith("/"+binary.name)
                       and hashlib.sha256(archive.read(member)).hexdigest() == sha256]
            if not matches:
                raise SystemExit("Bundled WebView2 SDK does not match the pinned licensed artifact")
            sdk_binaries.append({"file":filename, "sha256":sha256, "upstream_paths":matches})
    proxy_url = "https://raw.githubusercontent.com/jtushman/proxy_tools/ccd35a569b95bec271a59890780c7821756c548a/LICENSE.txt"
    proxy_hash = "a428fb8a2e762af3eb0a6edbbb88e9b42ccfee80fd9b423958bcacf9b9abbfe4"
    proxy_license = pinned_notice(proxy_url, proxy_hash, "proxy-tools-BSD-LICENSE.txt")
    shutil.copyfile(proxy_license, destination / "proxy-tools-0.1.0/UPSTREAM-BSD-LICENSE.txt")
    (destination / "EXTRA-SOURCES.json").write_text(json.dumps([
        {"component": "Microsoft.Web.WebView2", "version": "1.0.3856.49", "source": sdk_url, "sha256": sdk_hash, "bundled_binaries":sdk_binaries},
        {"component": "proxy-tools", "version": "0.1.0", "source": proxy_url, "sha256": proxy_hash,
         "note": "PyPI metadata says MIT; source header and upstream license say BSD. Preserve both notices."}], indent=2)+"\n", "utf-8")
    (destination / "PACKAGES.json").write_text(json.dumps({"python": sys.version, "packages": packages}, ensure_ascii=False, indent=2)+"\n", "utf-8")


def smoke_cli(directory):
    """Exercise the bundled checker+MCP with synthetic text, judge disabled."""
    original = "保存して下さい。上限は3件です。\r\n"
    payload = {"text":original, "profile":"article", "edits":[
        {"before":"して下さい", "after":"してください", "reason":"補助動詞の表記", "rule":"yomiyasu"}]}
    requests = [
        {"jsonrpc":"2.0", "id":1, "method":"initialize", "params":{"protocolVersion":"2024-11-05"}},
        {"jsonrpc":"2.0", "id":2, "method":"tools/list"},
        {"jsonrpc":"2.0", "id":3, "method":"tools/call", "params":{"name":"japanese_editor_review_proposals", "arguments":{**payload, "judge":False}}},
        {"jsonrpc":"2.0", "id":4, "method":"tools/call", "params":{"name":"japanese_editor_finalize", "arguments":payload}},
    ]
    process = subprocess.run([str(directory / "StudioYebisuEditorCLI.exe"), "mcp"],
        input=("\n".join(json.dumps(value, ensure_ascii=False) for value in requests)+"\n").encode("utf-8"),
        capture_output=True, timeout=60)
    responses = [json.loads(line) for line in process.stdout.decode("utf-8").splitlines()]
    errors = [value.get("id") for value in responses if value.get("error") or value.get("result", {}).get("isError")]
    passed = process.returncode == 0 and [value.get("id") for value in responses] == [1, 2, 3, 4] and not errors
    final = None
    if passed:
        final = json.loads(responses[-1]["result"]["content"][0]["text"])
        expected = original.replace("して下さい", "してください")
        passed = (final.get("text") == expected and final.get("check", {}).get("status") == "executed"
                  and final.get("verification", {}).get("status") == "executed"
                  and final.get("final_hash") == hashlib.sha256(expected.encode("utf-8")).hexdigest())
    record = {"status":"passed" if passed else "error", "exit":process.returncode, "responses":responses,
              "stderr":process.stderr.decode("utf-8", "replace"), "cloud_calls":0, "model_calls":0,
              "crlf_final_preserved": bool(final and final.get("text", "").endswith("\r\n"))}
    target = BUILD_ROOT / f"proofs/frozen-cli-{PROOF_SUFFIX}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, ensure_ascii=False, indent=2)+"\n", "utf-8")
    if not passed:
        raise SystemExit(f"Frozen CLI smoke failed; inspect {target}")
    print(target)


def smoke_gui(directory):
    target = BUILD_ROOT / f"proofs/native-window-{PROOF_SUFFIX}.json"
    target.unlink(missing_ok=True)
    process = subprocess.run([str(directory / "StudioYebisuEditor.exe"), "--smoke-test-output", str(target)], timeout=75)
    record = json.loads(target.read_text("utf-8"))
    if process.returncode or record.get("status") != "passed" or record.get("renderer") != "edgechromium":
        raise SystemExit(f"Native GUI smoke failed; inspect {target}")
    print(target)


def smoke_recovery(directory):
    """Fault only a newly created temporary service, never another listener."""
    sys.path.insert(0, str(ROOT))
    from desktop import service_identity
    if service_identity() is not None:
        raise SystemExit("復旧検証には8896が空いている必要があります。既存サービスは停止していません。")
    target = BUILD_ROOT / f"proofs/native-recovery-{PROOF_SUFFIX}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    gui = None
    with (target.parent / f"temporary-service-{PROOF_SUFFIX}.log").open("ab") as log:
        service = subprocess.Popen([str(directory / "StudioYebisuEditorCLI.exe"), "serve"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            deadline = time.monotonic()+15
            while service_identity() is None:
                if service.poll() is not None or time.monotonic() > deadline:
                    raise SystemExit("検証用サービスを起動できませんでした。")
                time.sleep(0.2)
            gui = subprocess.Popen([str(directory / "StudioYebisuEditor.exe"), "--smoke-recovery-output", str(target)])
            deadline = time.monotonic()+50
            while True:
                try:
                    record = json.loads(target.read_text("utf-8"))
                except (OSError, ValueError):
                    record = {}
                if record.get("phase") == "ready_to_drop":
                    break
                if record.get("status") == "error" or gui.poll() is not None or time.monotonic() > deadline:
                    raise SystemExit(f"復旧検証の画面を準備できませんでした: {target}")
                time.sleep(0.2)
            # This handle was created above, on an empty fixed port. Model/API
            # calls are disabled in the synthetic GUI test, so it has no model.
            service.terminate()
            service.wait(timeout=5)
            gui.wait(timeout=100)
            record = json.loads(target.read_text("utf-8"))
            record.update(temporary_service_pid=service.pid, temporary_service_exit=service.returncode, gui_exit=gui.returncode)
            target.write_text(json.dumps(record, ensure_ascii=False, indent=2)+"\n", "utf-8")
            if gui.returncode or record.get("status") != "passed":
                raise SystemExit(f"Native recovery smoke failed; inspect {target}")
            print(target)
        finally:
            # Only handles created by this test are eligible for cleanup.
            for process in (gui, service):
                if process is not None and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)


def main(argv=None):
    parser = argparse.ArgumentParser(description="専用環境で Windows GUI / CLI を作成する。モデルは同梱しない。")
    parser.add_argument("--skip-install", action="store_true", help="準備済みの固定版ビルド環境を使う")
    parser.add_argument("--collect-notices-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--smoke-cli", action="store_true", help="作成済みCLIのMCP・全文点検を合成文で検証。モデルや外部APIは使わない")
    parser.add_argument("--smoke-gui", action="store_true", help="作成済みの専用ウィンドウで規則の検査を実行し、閉じる")
    parser.add_argument("--smoke-recovery", action="store_true", help="空き8896で検証用サービスの消失・GUI復旧・明示停止を確認する")
    args = parser.parse_args(argv)
    if args.collect_notices_only:
        collect_notices()
        return
    if sys.platform != "win32" or sys.version_info < (3, 12):
        raise SystemExit("Windows x64 と Python 3.12 以降が必要です。")
    if struct.calcsize("P") != 8:
        raise SystemExit("64ビット版 Python を使用してください。")
    if args.smoke_cli or args.smoke_gui or args.smoke_recovery:
        directory = BUILD_ROOT / "dist/StudioYebisuEditor"
        if args.smoke_recovery:
            smoke_recovery(directory)
        if args.smoke_cli:
            smoke_cli(directory)
        if args.smoke_gui:
            smoke_gui(directory)
        return
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("uv を先に導入してください。既存の Python 環境は変更しません。")
    venv = BUILD_ROOT / ".venv"
    python = venv / "Scripts/python.exe"
    if not python.is_file():
        subprocess.run([uv, "venv", "--python", sys.executable, str(venv)], check=True)
    if not args.skip_install:
        subprocess.run([uv, "pip", "install", "--python", str(python), "--link-mode", "copy", *BUILD_REQUIREMENTS], check=True)
    subprocess.run([str(python), "-B", str(Path(__file__).resolve()), "--collect-notices-only"], check=True)
    make_icon(BUILD_ROOT / "icon.ico")
    subprocess.run([str(python), "-B", "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(BUILD_ROOT / "dist"), "--workpath", str(BUILD_ROOT / "pyinstaller"),
                    str(ROOT / "build/windows.spec")], cwd=ROOT, check=True,
                   env={**os.environ, "PYINSTALLER_CONFIG_DIR": str(BUILD_ROOT / "cache")})
    print(BUILD_ROOT / "dist/StudioYebisuEditor")


if __name__ == "__main__":
    main()
