"""Create allowlisted source/binary ZIPs, never models or personal state."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BUILD_ROOT = ROOT / ".local/build"
VERSION = "0.2.9"
SOURCE_FILES = ("app.py", "desktop.py", "engine.py", "decider.py", "decider_runtime.py", "decider_errors.py", "rpc_auth.py", "runtime_paths.py", "progress.py",
                "generators.py", "gemini_cli.py", "cli_login.py", "provider_settings.py", "mac_credentials.py", "service_runtime.py", "integrations.py",
                "install.py", "install.ps1", "install.sh", "plugin.json",
                "profiles.json", "LICENSE", "README.md", "NOTICES.md", "THIRD_PARTY_NOTICES.md",
                "CONTRIBUTING.md", "SECURITY.md", "start.ps1", "verification.json", ".gitignore", ".gitattributes",
                "docs/assets/editor.png")
PUBLIC_DOCS = ("STATUS.md", "PLUGINS.md", "PROVIDERS.md", "WINDOWS.md", "DECIDER_RUNTIME.md", "SOURCES.md",
               "INSTALL.md", "RELEASE.md", "RELEASING.md")
SOURCE_DIRECTORIES = ("web", "vendor", "skills", ".claude-plugin", "evaluation", "tests", "tools", "build")
FORBIDDEN_PARTS = {".local", ".git", ".venv", "__pycache__", "node_modules", "models", "logs", "cache"}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo", ".safetensors", ".gguf", ".key", ".pem", ".log"}


def digest(path):
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def permitted(relative):
    return not ({part.lower() for part in relative.parts} & FORBIDDEN_PARTS or relative.suffix.lower() in FORBIDDEN_SUFFIXES
                or relative.name.lower().startswith(".env"))


def source_files():
    paths = [ROOT / name for name in SOURCE_FILES if (ROOT / name).is_file()]
    paths.extend(ROOT / "docs" / name for name in PUBLIC_DOCS if (ROOT / "docs" / name).is_file())
    for directory in SOURCE_DIRECTORIES:
        paths.extend(path for path in (ROOT / directory).rglob("*") if path.is_file())
    for path in sorted(set(paths)):
        relative = path.relative_to(ROOT)
        if not permitted(relative):
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError(f"Source path escapes the application: {relative}")
        yield path, relative


def binary_files(directory):
    directory = directory.resolve()
    if not (directory / "YomiyasuStudio.exe").is_file() or not (directory / "YomiyasuStudioCLI.exe").is_file():
        raise ValueError("Both GUI and CLI executables are required")
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(directory)
        if not permitted(relative):
            raise ValueError(f"Private/model file in binary tree: {relative}")
        if path.is_symlink() or not path.resolve().is_relative_to(directory):
            raise ValueError(f"Binary path escapes the distribution: {relative}")
        yield path, relative


def create_zip(target, entries, prefix):
    entries = list(entries)
    manifest = [{"path": relative.as_posix(), "bytes": path.stat().st_size, "sha256": digest(path)} for path, relative in entries]
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path, relative in entries:
            info = zipfile.ZipInfo(f"{prefix}/{relative.as_posix()}", date_time=(2026, 10, 18, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (0o100755 if relative.suffix == ".sh" else 0o100644) << 16
            archive.writestr(info, path.read_bytes())
        info = zipfile.ZipInfo(f"{prefix}/RELEASE-MANIFEST.json", date_time=(2026, 10, 18, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 3
        info.external_attr = 0o100644 << 16
        archive.writestr(info, json.dumps({"version": VERSION, "files": manifest}, ensure_ascii=False, indent=2)+"\n")
    return {"file": target.name, "bytes": target.stat().st_size, "sha256": digest(target), "members": len(entries)+1}


def main(argv=None):
    parser = argparse.ArgumentParser(description="個人設定・モデルを除外し、ソースと Windows 配布 ZIP を作る")
    parser.add_argument("--source-only", action="store_true")
    args = parser.parse_args(argv)
    output = BUILD_ROOT / "releases"
    result = [create_zip(output / f"YomiyasuStudio-source-{VERSION}.zip", source_files(), f"YomiyasuStudio-source-{VERSION}")]
    if not args.source_only:
        result.append(create_zip(output / f"YomiyasuStudio-windows-x64-{VERSION}.zip", binary_files(BUILD_ROOT / "dist/YomiyasuStudio"), "YomiyasuStudio"))
    (output / "SHA256.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", "utf-8")
    print(json.dumps({"directory": str(output), "artifacts": result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
