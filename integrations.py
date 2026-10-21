"""Explicit, reversible Codex registration for this application only."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
import tempfile
import tomllib

from runtime_paths import RESOURCE_ROOT, PERSISTENT_ROOT, cli_command, hidden_process

SERVER = "yebisu-japanese-editor"
SKILL = "studio-yebisu-japanese-editor"
MANIFEST = PERSISTENT_ROOT / "codex-integration.json"
IS_WINDOWS = os.name == "nt"
PUBLIC_DOCS = ("PLUGINS.md", "PROVIDERS.md", "DECIDER_RUNTIME.md", "SOURCES.md", "STATUS.md", "WINDOWS.md", "INSTALL.md")
PUBLIC_ROOT_FILES = ("README.md", "THIRD_PARTY_NOTICES.md", "CONTRIBUTING.md", "SECURITY.md", "LICENSE", "verification.json")
PUBLIC_EVALUATION = ("README.md", "cases.json", "controls.json", "runtime-results.json", "cpp-parity-inputs.json", "cpp-parity-results.json")
PUBLIC_CPP = ("README.md", "manifest.json", "PERPLEXITY-LICENSE.txt", "PERPLEXITY-NOTICE.txt")


class IntegrationError(ValueError):
    pass


def codex_executable():
    candidate = shutil.which("codex.exe")
    if candidate:
        return candidate
    command = shutil.which("codex") or shutil.which("codex.cmd")
    if command:
        base = Path(command).parent / "node_modules/@openai/codex/node_modules/@openai"
        matches = list(base.glob("codex-win32-*/vendor/*/bin/codex.exe"))
        if matches:
            return str(matches[0])
        if not IS_WINDOWS:
            return command
    return None


def _config_path():
    explicit = os.environ.get("CODEX_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve() / "config.toml"
    try:
        if MANIFEST.is_file() and not MANIFEST.is_symlink():
            recorded = json.loads(MANIFEST.read_text("utf-8"))
            home = recorded.get("codex_home") if isinstance(recorded, dict) else None
            if isinstance(home, str) and Path(home).is_absolute():
                return Path(home) / "config.toml"
    except (OSError, ValueError, TypeError):
        pass
    return Path.home() / ".codex/config.toml"


def _read(path):
    return tomllib.loads(path.read_text("utf-8-sig")) if path.is_file() else {}


def server_environment(codex_home=None):
    """Explicit server environment, including the registered Codex identity."""
    home = Path(codex_home) if codex_home is not None else _config_path().parent.resolve()
    return {"YEBISU_EDITOR_DATA_DIR": str(PERSISTENT_ROOT), "CODEX_HOME": str(home)}


def _table_headers(text):
    """Find actual TOML headers, excluding strings, arrays and inline tables."""
    records, offset, quote, arrays, braces = [], 0, None, 0, 0
    for line in text.splitlines(keepends=True):
        if quote is None and arrays == 0 and braces == 0 and line.lstrip().startswith("["):
            try:
                value = tomllib.loads(line + "\n__yebisu_header_probe__ = true\n")
                path = []
                while isinstance(value, dict) and "__yebisu_header_probe__" not in value:
                    if len(value) != 1:
                        raise ValueError("ambiguous header")
                    key, value = next(iter(value.items()))
                    path.append(key)
                    if isinstance(value, list):
                        value = value[0]
                records.append((offset, tuple(path)))
            except (ValueError, IndexError):
                raise IntegrationError("Codex設定のテーブル形式に対応できません。既存設定は変更していません。") from None
            offset += len(line)
            continue
        index = 0
        while index < len(line):
            char = line[index]
            if quote:
                if quote in ('"""', "'''"):
                    if quote == '"""' and char == "\\":
                        index += 2
                        continue
                    if line.startswith(quote, index):
                        marker = quote[0]
                        while index < len(line) and line[index] == marker:
                            index += 1
                        quote = None
                        continue
                else:
                    if quote == '"' and char == "\\":
                        index += 2
                        continue
                    if char == quote:
                        quote = None
                index += 1
                continue
            if char == "#":
                break
            if line.startswith('"""', index) or line.startswith("'''", index):
                quote, index = line[index:index + 3], index + 3
                continue
            if char in ('"', "'"):
                quote = char
            elif char == "[":
                arrays += 1
            elif char == "]":
                arrays -= 1
            elif char == "{":
                braces += 1
            elif char == "}":
                braces -= 1
            index += 1
        offset += len(line)
    return records


def _strip_owned(text):
    headers, cuts = _table_headers(text), []
    for index, (start, path) in enumerate(headers):
        if path[:2] != ("mcp_servers", SERVER):
            continue
        end = headers[index + 1][0] if index + 1 < len(headers) else len(text)
        lines = text[start:end].splitlines(keepends=True)
        trailing = len(lines)
        while trailing > 1 and (not lines[trailing - 1].strip() or lines[trailing - 1].lstrip().startswith("#")):
            trailing -= 1
        # Preserve standalone comments before the following unrelated table.
        tail = lines[trailing:]
        if any(line.lstrip().startswith("#") for line in tail):
            end = start + len("".join(lines[:trailing]))
        cuts.append((start, end))
    for start, end in reversed(cuts):
        text = text[:start] + text[end:]
    return text, bool(cuts)


def _without_owned(data):
    rest = dict(data)
    servers = dict(rest.get("mcp_servers", {}))
    servers.pop(SERVER, None)
    if servers:
        rest["mcp_servers"] = servers
    else:
        rest.pop("mcp_servers", None)
    return rest


def merge_owned_toml(original, generated, command, codex_home):
    """Pure raw-byte merge. Only this application's MCP tables are replaced."""
    try:
        text = original.decode("utf-8-sig")
        before = tomllib.loads(text)
        native = tomllib.loads(generated.decode("utf-8-sig"))
        servers = native.get("mcp_servers", {})
        if set(native) != {"mcp_servers"} or set(servers) != {SERVER}:
            raise ValueError("native output contains unrelated settings")
        server = servers[SERVER]
        if not isinstance(server, dict) or set(server) - {"command", "args", "env", "enabled", "startup_timeout_sec", "tool_timeout_sec"}:
            raise ValueError("unsupported native fields")
        if [server.get("command"), *server.get("args", [])] != command:
            raise ValueError("unexpected command")
        expected_env = server_environment(codex_home)
        if not Path(codex_home).is_absolute() or server.get("env") != expected_env:
            raise ValueError("unexpected environment")
        stripped, found = _strip_owned(text)
        if SERVER in before.get("mcp_servers", {}) and not found:
            raise ValueError("owned inline or dotted settings cannot be safely replaced")
        newline = "\r\n" if "\r\n" in text else "\n"
        encode = lambda value: json.dumps(value, ensure_ascii=False)
        block = newline.join([f"[mcp_servers.{SERVER}]", "command = " + encode(command[0]),
                              "args = " + encode(command[1:]), "startup_timeout_sec = 30", "tool_timeout_sec = 600", "",
                              f"[mcp_servers.{SERVER}.env]", "YEBISU_EDITOR_DATA_DIR = " + encode(str(PERSISTENT_ROOT)),
                              "CODEX_HOME = " + encode(str(codex_home)), ""])
        separator = "" if not stripped or stripped.endswith(newline * 2) else newline if stripped.endswith(newline) else newline * 2
        merged = stripped + separator + block
        if _without_owned(before) != _without_owned(tomllib.loads(merged)):
            raise ValueError("unrelated semantics changed")
        return (b"\xef\xbb\xbf" if original.startswith(b"\xef\xbb\xbf") else b"") + merged.encode("utf-8")
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        raise IntegrationError("Codex設定の形式を安全に統合できません。既存設定は変更していません。") from None


def _write_if_unchanged(path, expected, replacement):
    """Guard raw contents immediately before replacing an owned config file."""
    temporary = None
    try:
        current = path.read_bytes() if path.exists() else None
        if current != expected:
            raise IntegrationError("登録中にCodex設定が変わりました。上書きを避けるため変更を中止しました。")
        if current == replacement:
            return
        if replacement is None:
            path.unlink()
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".yebisu-config-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(replacement)
        if path.exists():
            shutil.copymode(path, temporary)
        if (path.read_bytes() if path.exists() else None) != expected:
            raise IntegrationError("登録中にCodex設定が変わりました。上書きを避けるため変更を中止しました。")
        temporary.replace(path)
    except OSError:
        raise IntegrationError("Codex設定を保存できませんでした。既存設定の状態を確認してください。") from None
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _native_configuration(executable, command, codex_home):
    with tempfile.TemporaryDirectory(prefix="yebisu-codex-") as directory:
        home = Path(directory)
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR",
                   "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMDRIVE"}
        environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        environment["CODEX_HOME"] = str(home)
        try:
            result = subprocess.run([executable, "mcp", "add", SERVER, "--env", "YEBISU_EDITOR_DATA_DIR=" + str(PERSISTENT_ROOT),
                                     "--env", "CODEX_HOME=" + str(codex_home),
                                     "--", *command], capture_output=True, timeout=30, cwd=home, env=environment, **hidden_process())
            if result.returncode:
                raise IntegrationError("Codexの隔離設定でMCP登録形式を確認できませんでした。既存設定は変更していません。")
            return (home / "config.toml").read_bytes()
        except (OSError, subprocess.TimeoutExpired):
            raise IntegrationError("Codexの隔離設定を作成できませんでした。既存設定は変更していません。") from None


def _skill_hashes(target):
    if target.is_symlink() or not target.is_dir():
        raise IntegrationError("導入先スキルの形式を確認できません。既存ファイルは変更していません。")
    entries = list(target.rglob("*"))
    if any(path.is_symlink() for path in entries):
        raise IntegrationError("導入先スキルにリンクがあります。既存ファイルは変更していません。")
    return {file.relative_to(target).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
            for file in entries if file.is_file()}


def _check_skill(target, previous):
    if target.exists() or target.is_symlink():
        old_hashes = previous.get("skill_sha256")
        if not isinstance(old_hashes, dict) or _skill_hashes(target) != old_hashes:
            raise IntegrationError("導入済みスキルに変更があります。既存ファイルを保全するため更新を保留しました。")


def _skill_contents():
    """Preflight a complete, public-only skill before any config replacement."""
    try:
        files = {}
        for name in PUBLIC_ROOT_FILES:
            content = (RESOURCE_ROOT / name).read_bytes()
            if name.endswith(".md"):
                content = content.decode("utf-8").replace("](vendor/yomiyasu/", "](yomiyasu/").encode("utf-8")
            files["references/" + ("APP.md" if name == "README.md" else name)] = content
        files["LICENSE"] = files["references/LICENSE"]
        for name in PUBLIC_DOCS:
            files["references/docs/" + name] = (RESOURCE_ROOT / "docs" / name).read_bytes()
        for name in PUBLIC_EVALUATION:
            files["references/evaluation/" + name] = (RESOURCE_ROOT / "evaluation" / name).read_bytes()
        for name in PUBLIC_CPP:
            files["references/cpp/" + name] = (RESOURCE_ROOT / "cpp" / name).read_bytes()
        import yomiyasu_updates
        vendor = yomiyasu_updates.current_root()
        metadata = (vendor / "UPSTREAM.json").read_bytes()
        entries = json.loads(metadata)["files"]
        if not isinstance(entries, list) or not entries:
            raise ValueError("missing vendor manifest")
        for entry in entries:
            name = entry["path"]
            source = vendor / name
            if not isinstance(name, str) or source.is_symlink() or not source.resolve().is_relative_to(vendor.resolve()):
                raise ValueError("unsafe vendor path")
            content = source.read_bytes()
            if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                raise ValueError("vendor hash mismatch")
            destination = "references/yomiyasu/" + name
            if destination in files:
                raise ValueError("duplicate vendor path")
            files[destination] = content
        for name in ("SKILL.md", "LICENSE", "UNICODE-LICENSE.txt"):
            if "references/yomiyasu/" + name not in files:
                raise ValueError("missing vendor notice")
        files["references/yomiyasu/UPSTREAM.json"] = metadata
        guide = (RESOURCE_ROOT / "skills/japanese-editor/SKILL.md").read_text("utf-8")
        guide = guide.replace("name: japanese-editor", "name: " + SKILL)
        guide = guide.replace("../../vendor/yomiyasu", "references/yomiyasu").replace("../../README.md", "references/APP.md")
        guide = guide.replace("../../docs/", "references/docs/").replace("../../evaluation/", "references/evaluation/")
        files["SKILL.md"] = guide.encode("utf-8")
        for name, content in files.items():
            if not name.endswith(".md") or name.startswith("references/yomiyasu/"):
                continue
            for link in re.findall(r'\]\(([^)]+)\)', content.decode("utf-8")):
                if "://" in link or link.startswith("#"):
                    continue
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), link.split("#", 1)[0]))
                if resolved not in files:
                    raise ValueError("missing public reference")
        return files
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        raise IntegrationError("配布物に必要な公開スキル資料が不足しているか、原典の照合に失敗しました。Codex設定は変更していません。") from None


def refresh_yomiyasu():
    """Update only managed references, retaining all Codex config bytes."""
    if not MANIFEST.is_file():
        return "not_installed"
    try:
        manifest_original = MANIFEST.read_bytes()
        previous = json.loads(manifest_original)
        config = _config_path()
        target = Path.home() / ".agents/skills" / SKILL
        if not target.exists() or not config.is_file():
            return "not_installed"
        _check_skill(target, previous)
        original = config.read_bytes()
        registered = tomllib.loads(original.decode("utf-8-sig")).get("mcp_servers", {}).get(SERVER, {})
        command = previous["command"]
        if [registered.get("command"), *registered.get("args", [])] != command:
            return "modified"
        contents = {name: (target / name).read_bytes() for name in previous["skill_sha256"]}
        import yomiyasu_updates
        vendor = yomiyasu_updates.current_root()
        metadata = (vendor / "UPSTREAM.json").read_bytes()
        manifest = yomiyasu_updates.validate_package(vendor)
        contents = {name: raw for name, raw in contents.items() if not name.startswith("references/yomiyasu/")}
        for row in manifest["files"]:
            contents["references/yomiyasu/" + row["path"]] = (vendor / row["path"]).read_bytes()
        contents["references/yomiyasu/UPSTREAM.json"] = metadata
        # Old managed guides named a fixed version next to the same link.
        contents["SKILL.md"] = re.sub(rb"yomiyasu [0-9]+\.[0-9]+\.[0-9]+", b"yomiyasu", contents["SKILL.md"])
        hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in contents.items()}
        if hashes == previous["skill_sha256"]:
            return "current"
        _replace_skill_and_manifest(target, contents, previous, manifest_original, command, config, original, original)
        return "updated"
    except IntegrationError:
        return "modified"
    except (OSError, ValueError, TypeError, KeyError):
        return "deferred"


def _replace_skill_and_manifest(target, contents, previous, manifest_original, command, config, original, merged):
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".yebisu-skill-", dir=target.parent) as directory:
        work = Path(directory)
        if work.resolve().parent != target.parent.resolve():
            raise IntegrationError("スキルの一時保存先を確認できません。Codex設定は変更していません。")
        staged, old = work / "new", work / "previous"
        for name, content in contents.items():
            path = staged / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        hashes = _skill_hashes(staged)
        _check_skill(target, previous)
        _write_if_unchanged(config, original, merged)
        swapped = False
        try:
            _check_skill(target, previous)
            if target.exists():
                target.rename(old)
            staged.rename(target)
            swapped = True
            manifest = json.dumps({"server": SERVER, "command": command, "skill_sha256": hashes,
                                   "codex_home": str(config.parent.resolve()),
                                   "installed_at": datetime.now(timezone.utc).isoformat()}, indent=2).encode("utf-8") + b"\n"
            _write_if_unchanged(MANIFEST, manifest_original, manifest)
        except (OSError, IntegrationError):
            if swapped:
                # A concurrent edit of the newly installed skill must survive.
                if _skill_hashes(target) != hashes:
                    if old.exists():
                        old.rename(target.with_name(work.name + "-recovery"))
                    raise IntegrationError("スキル更新中に変更を検出しました。変更を保全したため、MCPとスキルの登録状態を確認してください。") from None
                target.rename(staged)
            if old.exists():
                old.rename(target)
            try:
                _write_if_unchanged(config, merged, original)
            except IntegrationError:
                raise IntegrationError("登録中に設定が変わったため自動復元を保留しました。既存スキルは保持しています。MCPの登録状態を確認してください。") from None
            raise IntegrationError("スキルの更新を完了できませんでした。Codex設定と導入済みスキルを元の状態へ戻しました。") from None


def status():
    try:
        server = _read(_config_path()).get("mcp_servers", {}).get(SERVER)
        skill = Path.home() / ".agents/skills" / SKILL / "SKILL.md"
        registered = isinstance(server, dict)
        command = cli_command("mcp")
        current = (registered and server.get("enabled") is not False
                   and isinstance(server.get("args", []), list)
                   and [server.get("command"), *server.get("args", [])] == command
                   and isinstance(server.get("env"), dict)
                   and all(server["env"].get(key) == value for key, value in server_environment().items()))
        return {"available": codex_executable() is not None, "registered": isinstance(server, dict),
                "current_registration": current, "needs_update": registered and not current,
                "skill_installed": skill.is_file(), "server": SERVER,
                "notice": "登録後は新しいCodexセッションで利用できます。既存の会話はツール一覧の再読込が必要な場合があります。"}
    except (OSError, ValueError):
        return {"available": codex_executable() is not None, "registered": False, "skill_installed": False,
                "server": SERVER, "error": "Codexの設定を読み取れません。既存設定は変更していません。"}


def mcp_config():
    """Portable stdio registration, containing no provider credentials."""
    command = cli_command("mcp")
    return {"mcpServers": {SERVER: {"command": command[0], "args": command[1:],
                                    "env": server_environment()}}}


def check_connection():
    """Exercise our actual stdio entry point without a model or manuscript."""
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "japanese_editor_analyze", "arguments": {
                "text": "接続確認用の例文です。", "profile": "article", "generate": False, "judge": False}}},
    ]
    environment = {**os.environ, **server_environment(), "YEBISU_EDITOR_NO_AUTOSTART": "1",
                   "YEBISU_YOMIYASU_OFFLINE": "1"}
    try:
        result = subprocess.run(cli_command("mcp"),
                                input="\n".join(json.dumps(item, ensure_ascii=False) for item in requests)+"\n",
                                capture_output=True, encoding="utf-8", timeout=30,
                                env=environment, **hidden_process())
        # This fixed exchange has a small response. Never relay arbitrary
        # stdout/stderr, provider metadata or paths as an error diagnostic.
        if result.returncode or len(result.stdout) > 128_000:
            raise ValueError("entry point failed")
        responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        if len(responses) != 3 or any(not isinstance(row, dict) or row.get("error") for row in responses):
            raise ValueError("invalid exchange")
        if [row.get("id") for row in responses] != [1, 2, 3]:
            raise ValueError("invalid response identity")
        server = responses[0]["result"]["serverInfo"]
        tools = responses[1]["result"]["tools"]
        tool_result = responses[2]["result"]
        if tool_result.get("isError"):
            raise ValueError("tool failed")
        analysis = json.loads(tool_result["content"][0]["text"])
        if (server.get("name") != "yomiyasu-studio"
                or not isinstance(tools, list)
                or "japanese_editor_review_proposals" not in {item.get("name") for item in tools if isinstance(item, dict)}
                or analysis.get("stages") != {"rules": "executed", "generation": "not_requested", "decider": "not_run"}):
            raise ValueError("unexpected capability")
        return {"status": "passed", "transport": "stdio", "version": server["version"],
                "tools": len(tools), "rules": "executed", "generation": "not_requested", "decider": "not_run",
                "notice": "MCPの起動と規則点検を確認しました。AI側の登録後、新しい会話でツールが使えるか確認してください。"}
    except subprocess.TimeoutExpired:
        raise IntegrationError("MCPの起動確認が30秒以内に終わりませんでした。アプリの実行ファイルとアクセス権を確認してください。原文や外部AIは使っていません。") from None
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError, UnicodeError):
        raise IntegrationError("MCPの起動または規則点検を確認できませんでした。アプリを展開し直し、AI連携の導入案内を確認してください。原文や外部AIは使っていません。") from None


def install():
    executable = codex_executable()
    if not executable:
        raise IntegrationError("Codex CLIが見つかりません。Codexを導入し、再度登録してください。")
    config = _config_path()
    codex_home = config.parent.resolve()
    try:
        original = config.read_bytes() if config.exists() else None
        before = tomllib.loads((original or b"").decode("utf-8-sig"))
        manifest_original = MANIFEST.read_bytes() if MANIFEST.is_file() else None
        previous = json.loads(manifest_original.decode("utf-8-sig")) if manifest_original is not None else {}
        servers = before.get("mcp_servers", {})
        if not isinstance(servers, dict) or not isinstance(previous, dict):
            raise ValueError("unsupported metadata")
    except (OSError, ValueError, UnicodeError):
        raise IntegrationError("既存のCodex設定または導入記録を読み取れません。変更していません。") from None
    command = cli_command("mcp")
    existing = servers.get(SERVER)
    if existing is not None:
        if not isinstance(existing, dict) or not isinstance(existing.get("args", []), list):
            raise IntegrationError("同名のMCP設定が既にあります。衝突を避けるため変更していません。")
        if [existing.get("command"), *existing.get("args", [])] not in (command, previous.get("command")):
            raise IntegrationError("同名のMCP設定が既にあります。衝突を避けるため変更していません。")
    target = Path.home() / ".agents/skills" / SKILL
    _check_skill(target, previous)
    contents = _skill_contents()
    generated = _native_configuration(executable, command, codex_home)
    merged = merge_owned_toml(original or b"", generated, command, codex_home)
    PERSISTENT_ROOT.mkdir(parents=True, exist_ok=True)
    if original is not None:
        backup = PERSISTENT_ROOT / "backups" / ("codex-config-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".toml")
        backup.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(original)
    try:
        _replace_skill_and_manifest(target, contents, previous, manifest_original, command, config, original, merged)
    except OSError:
        raise IntegrationError("公開スキルの準備を完了できませんでした。既存ファイルの利用状態を確認してください。") from None
    return {**status(), "status": "installed", "notice": "Codexへの登録が完了しました。新しいCodexセッションから日本語校正を依頼できます。"}
