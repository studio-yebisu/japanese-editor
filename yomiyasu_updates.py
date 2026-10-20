"""Official, bounded upstream snapshots; never modify the bundled fallback."""
from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile
import threading
import time
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from runtime_paths import RESOURCE_ROOT, PERSISTENT_ROOT

BUNDLED = RESOURCE_ROOT / "vendor/yomiyasu"
CACHE = PERSISTENT_ROOT / "yomiyasu"
REPOSITORY = "https://github.com/nanaism/yomiyasu"
API = "https://api.github.com/repos/nanaism/yomiyasu"
RAW = "https://raw.githubusercontent.com/nanaism/yomiyasu/"
MAX_FILE = 2_000_000
MAX_TOTAL = 12_000_000
MAX_FILES = 256
CHECK_INTERVAL = 600
UPDATE_BUDGET = 60
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_VERSION = re.compile(r"2\.[0-9]+\.[0-9]+\Z")
_CURRENT = ContextVar("yomiyasu_snapshot", default=None)
_START_LOCK = threading.Lock()
_STARTED = False
_STATE = None
_REQUIRED = {row["path"] for row in json.loads((BUNDLED / "UPSTREAM.json").read_text("utf-8"))["files"]}
_MESSAGES = {
    "idle": "起動時にYomiyasuの更新を確認します。",
    "checking": "公式リポジトリの更新を確認しています。今の版で校正できます。",
    "downloading": "Yomiyasuの新版を取得しています。校正中の版は変わりません。",
    "current": "Yomiyasuは確認した最新版です。",
    "updated": "Yomiyasuを更新しました。次の校正から新版を使います。",
    "reverted": "更新版の点検を完了できなかったため、同梱版を使っています。もう一度校正してください。",
    "offline": "更新を確認・取得できませんでした。保存済みの版で使えます。",
    "incompatible": "新版はこのアプリに対応していません。今の版を使い続けます。アプリの更新を確認してください。",
    "disabled": "保存済みのYomiyasuを使っています。起動時の更新確認は無効です。",
}


class UpdateError(ValueError):
    pass


def _read_json(path, maximum=300_000):
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise UpdateError("oversized metadata")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise UpdateError("invalid metadata object")
    return value


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _relative(value):
    if (not isinstance(value, str) or len(value) > 220 or "\\" in value
            or not value or value.startswith("/") or "\x00" in value):
        raise UpdateError("invalid package path")
    parts = value.split("/")
    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
    if any(part in ("", ".", "..") or part.endswith(".") or part.split(".")[0].lower() in reserved
           or not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts):
        raise UpdateError("invalid package path")
    if value in {"LICENSE", "UNICODE-LICENSE.txt", "SKILL.md"}:
        return value
    if parts[0] not in {"references", "scripts"} or PurePosixPath(value).suffix not in {".py", ".md", ".txt", ".json"}:
        raise UpdateError("unsupported package file")
    return value


def validate_package(root, *, expected=None):
    """Validate content without importing or executing downloaded Python."""
    metadata = _read_json(root / "UPSTREAM.json")
    revision, version = metadata.get("commit"), metadata.get("version")
    if (metadata.get("repository") != REPOSITORY or not isinstance(revision, str) or not _SHA.fullmatch(revision)
            or expected is not None and revision != expected or metadata.get("license") != "MIT"
            or not isinstance(version, str) or not _VERSION.fullmatch(version)):
        raise UpdateError("incompatible package metadata")
    rows = metadata.get("files")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_FILES:
        raise UpdateError("invalid manifest")
    seen, total, programs = set(), 0, {}
    for row in rows:
        if not isinstance(row, dict):
            raise UpdateError("invalid manifest entry")
        name = _relative(row["path"])
        folded = name.casefold()
        if folded in seen or type(row.get("bytes")) is not int or not 0 < row["bytes"] <= MAX_FILE:
            raise UpdateError("invalid manifest entry")
        seen.add(folded)
        target = root / name
        if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
            raise UpdateError("package escapes snapshot")
        if target.stat().st_size != row["bytes"]:
            raise UpdateError("incomplete package")
        raw = target.read_bytes()
        total += len(raw)
        if total > MAX_TOTAL or hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise UpdateError("package digest mismatch")
        oid = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
        if oid != row.get("git_blob_oid"):
            raise UpdateError("upstream blob mismatch")
        text = raw.decode("utf-8")
        if name.startswith("scripts/") and name.endswith(".py"):
            programs[name] = ast.parse(text, filename=name)
    if not {name.casefold() for name in _REQUIRED} <= seen:
        raise UpdateError("missing required materials")
    guide = (root / "SKILL.md").read_text("utf-8")
    if not re.search(r"^  version:\s*[\"']?" + re.escape(version) + r"[\"']?\s*$", guide, re.MULTILINE):
        raise UpdateError("guide version mismatch")
    if "MIT License" not in (root / "LICENSE").read_text("utf-8"):
        raise UpdateError("license changed")
    local = {PurePosixPath(name).stem for name in programs}
    for name, program in programs.items():
        if name.endswith(("yomiyasu_lint.py", "yomiyasu_diff.py", "yomiyasu_check.py")):
            versions = [node.value.value for node in ast.walk(program) if isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)
                        and isinstance(node.value, ast.Constant)]
            if versions != [version]:
                raise UpdateError("checker versions differ")
            expected_format, expected_version = {
                "yomiyasu_lint.py": ("yomiyasu-lint", 2), "yomiyasu_diff.py": ("yomiyasu-diff", 2),
                "yomiyasu_check.py": ("yomiyasu-check-result", 1),
            }[PurePosixPath(name).name]
            constants = {target.id: node.value.value for node in program.body if isinstance(node, ast.Assign)
                         and isinstance(node.value, ast.Constant) for target in node.targets if isinstance(target, ast.Name)}
            formats = []
            for node in ast.walk(program):
                if not isinstance(node, ast.Dict):
                    continue
                fields = {key.value: value for key, value in zip(node.keys, node.values) if isinstance(key, ast.Constant)}
                marker = fields.get("format")
                if isinstance(marker, ast.Constant) and marker.value == expected_format:
                    schema = fields.get("format_version")
                    formats.append(schema.value if isinstance(schema, ast.Constant) else constants.get(schema.id) if isinstance(schema, ast.Name) else None)
            if not formats or any(value != expected_version for value in formats):
                raise UpdateError("checker JSON contract changed")
        for node in ast.walk(program):
            modules = [item.name for item in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) and node.module and not node.level else []
            for module in modules:
                top = module.split(".")[0]
                if top in local or top == "__future__":
                    continue
                if top not in sys.stdlib_module_names:
                    raise UpdateError("new dependency requires app update")
                # The upstream uses fcntl/msvcrt only on their matching OS.
                platform_only = {"fcntl"} if os.name == "nt" else {"msvcrt", "winreg"}
                if getattr(sys, "frozen", False) and top not in platform_only and importlib.util.find_spec(top) is None:
                    raise UpdateError("dependency missing from frozen app")
    return metadata


def _rejected(revision):
    try:
        return revision in _read_json(CACHE / "rejected.json", 4096).get("commits", [])
    except (OSError, ValueError, TypeError):
        return False


def root_for_revision(revision):
    try:
        if revision == "bundled" or revision == _read_json(BUNDLED / "UPSTREAM.json")["commit"]:
            return BUNDLED
        if not isinstance(revision, str) or not _SHA.fullmatch(revision):
            raise UpdateError("invalid revision")
        if _rejected(revision):
            raise UpdateError("rejected snapshot")
        root = CACHE / "versions" / revision
        if (root.is_symlink() or (CACHE / "versions").is_symlink()
                or not root.resolve().is_relative_to(CACHE.resolve())):
            raise UpdateError("invalid snapshot root")
        validate_package(root, expected=revision)
        return root
    except (OSError, ValueError, KeyError, TypeError, RecursionError, SyntaxError):
        raise UpdateError("snapshot unavailable") from None


def active_root():
    try:
        pointer = _read_json(CACHE / "active.json", 4096)
        root = root_for_revision(pointer["commit"])
        current = _read_json(root / "UPSTREAM.json")["version"]
        bundled = _read_json(BUNDLED / "UPSTREAM.json")["version"]
        return root if tuple(map(int, current.split("."))) >= tuple(map(int, bundled.split("."))) else BUNDLED
    except (OSError, ValueError, KeyError, TypeError, RecursionError, SyntaxError):
        return BUNDLED


def current_root():
    return _CURRENT.get() or active_root()


def pinned(function):
    @wraps(function)
    def invoke(*args, **kwargs):
        if _CURRENT.get() is not None:
            return function(*args, **kwargs)
        token = _CURRENT.set(active_root())
        try:
            return function(*args, **kwargs)
        finally:
            _CURRENT.reset(token)
    return invoke


@contextmanager
def using_revision(revision=None):
    root = current_root() if revision is None else root_for_revision(revision)
    token = _CURRENT.set(root)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def revision_for_root(root):
    root = Path(root).resolve()
    if root == BUNDLED.resolve():
        return "bundled"
    revision = root.name
    if root_for_revision(revision).resolve() != root:
        raise UpdateError("unknown snapshot")
    return revision


def _state(state, **details):
    global _STATE
    _STATE = {"state": state, **details}
    _write_json(CACHE / "status.json", _STATE)


def _sync_skill():
    # Refresh only our already-registered, unmodified skill. Never register a
    # new server or install hooks as a side effect of an upstream update.
    try:
        import integrations
        return integrations.refresh_yomiyasu()
    except Exception:
        return "deferred"


def reject_current_revision():
    """Do not retry generation; use the bundled snapshot on the next operation."""
    root = current_root()
    if root == BUNDLED:
        return False
    try:
        revision = _read_json(root / "UPSTREAM.json")["commit"]
        if not isinstance(revision, str) or not _SHA.fullmatch(revision):
            return False
        try:
            commits = _read_json(CACHE / "rejected.json", 4096).get("commits", [])
        except (OSError, ValueError, TypeError):
            commits = []
        commits = [value for value in commits if isinstance(value, str) and _SHA.fullmatch(value)] if isinstance(commits, list) else []
        _write_json(CACHE / "rejected.json", {"commits": list(dict.fromkeys([*commits, revision]))[-32:]})
        with using_revision("bundled"):
            _state("reverted", checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), skill_sync=_sync_skill())
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def public_status():
    root = current_root()
    try:
        identity = _read_json(root / "UPSTREAM.json")
    except (OSError, ValueError, TypeError):
        identity = _read_json(BUNDLED / "UPSTREAM.json")
    try:
        status = _read_json(CACHE / "status.json", 4096)
    except (OSError, ValueError, TypeError):
        status = {"state": "idle"}
    timestamp = status.get("checked_at") if isinstance(status.get("checked_at"), str) else ""
    if _STATE and (_STATE.get("state") == "disabled" or _STATE.get("checked_at", "") >= timestamp):
        status = _STATE
    state = status.get("state")
    state = state if isinstance(state, str) and state in _MESSAGES else "idle"
    if state in {"checking", "downloading"}:
        try:
            if time.time() - datetime.fromisoformat(status["checked_at"]).timestamp() > UPDATE_BUDGET + 30:
                state = "offline"
        except (ValueError, KeyError, TypeError):
            state = "offline"
    return {"state": state, "message": _MESSAGES[state], "active_version": identity.get("version"),
            "active_commit": identity.get("commit"), "source": "bundled" if root == BUNDLED else "downloaded",
            "checked_at": status.get("checked_at") if isinstance(status.get("checked_at"), str) else None, "repository": REPOSITORY,
            "skill_sync": status.get("skill_sync") if isinstance(status.get("skill_sync"), str) and status.get("skill_sync") in {"current", "updated", "modified", "not_installed", "deferred"} else None}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _fetch(url, maximum, deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError("update budget exceeded")
    request = Request(url, headers={"Accept": "application/vnd.github+json" if url.startswith(API) else "application/octet-stream",
                                   "User-Agent": "YomiyasuStudio-updater"})
    with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=min(8, max(.1, deadline-time.monotonic()))) as response:
        chunks, size = [], 0
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("update budget exceeded")
            chunk = response.read(min(65536, maximum + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > maximum:
                raise UpdateError("oversized upstream response")
            chunks.append(chunk)
        return b"".join(chunks)


@contextmanager
def _process_lock():
    CACHE.mkdir(parents=True, exist_ok=True)
    with (CACHE / "update.lock").open("a+b") as stream:
        stream.seek(0)
        if not stream.read(1):
            stream.write(b"0"); stream.flush()
        stream.seek(0)
        locked = False
        try:
            if os.name == "nt":
                import msvcrt
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1); locked = True
                except OSError:
                    pass
            else:
                import fcntl
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB); locked = True
                except OSError:
                    pass
            yield locked
        finally:
            if locked:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)


def _check():
    checked = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _process_lock() as locked:
        if not locked:
            return
        try:
            previous = _read_json(CACHE / "status.json", 4096)
            timestamp = datetime.fromisoformat(previous["checked_at"]).timestamp()
            if previous.get("state") not in {"checking", "downloading"} and 0 <= time.time()-timestamp < CHECK_INTERVAL:
                if previous.get("skill_sync") == "deferred":
                    _state(previous["state"], checked_at=previous["checked_at"], skill_sync=_sync_skill())
                return
        except (OSError, ValueError, TypeError, KeyError):
            pass
        _state("checking", checked_at=checked)
        try:
            deadline = time.monotonic() + UPDATE_BUDGET
            heads = json.loads(_fetch(API + "/commits?sha=main&per_page=1", 256_000, deadline))
            if not isinstance(heads, list) or len(heads) != 1:
                raise UpdateError("invalid upstream history")
            head = heads[0]
            revision = head["sha"]
            if not isinstance(revision, str) or not _SHA.fullmatch(revision):
                raise UpdateError("invalid upstream head")
            if _rejected(revision):
                _state("reverted", checked_at=checked, skill_sync=_sync_skill()); return
            current = _read_json(active_root() / "UPSTREAM.json")
            if current["commit"] == revision:
                _state("current", checked_at=checked, skill_sync=_sync_skill()); return
            tree = json.loads(_fetch(API + "/git/trees/" + revision + "?recursive=1", 2_000_000, deadline))
            if tree.get("truncated") is not False:
                raise UpdateError("incomplete upstream tree")
            files = []
            for item in tree["tree"]:
                original = item["path"]
                if original in {"LICENSE", "UNICODE-LICENSE.txt"}:
                    name = original
                elif original.startswith("skills/yomiyasu/"):
                    name = original[len("skills/yomiyasu/"):]
                    if not (name == "SKILL.md" or name.startswith(("references/", "scripts/"))):
                        continue
                else:
                    continue
                if item["type"] == "tree":
                    continue
                name = _relative(name)
                if item.get("mode") not in {"100644", "100755"} or item["type"] != "blob" or type(item.get("size")) is not int or not 0 < item["size"] <= MAX_FILE:
                    raise UpdateError("unsupported upstream item")
                files.append((name, original, item))
            if not 1 <= len(files) <= MAX_FILES or sum(row[2]["size"] for row in files) > MAX_TOTAL:
                raise UpdateError("package limits exceeded")
            if shutil.disk_usage(CACHE).free < 2 * MAX_TOTAL:
                raise OSError("insufficient cache space")
            _state("downloading", checked_at=checked)
            staging = Path(tempfile.mkdtemp(prefix="staging-", dir=CACHE))
            try:
                def download(row):
                    name, original, item = row
                    raw = _fetch(RAW + revision + "/" + quote(original, safe="/"), MAX_FILE, deadline)
                    oid = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
                    if len(raw) != item["size"] or oid != item["sha"]:
                        raise UpdateError("upstream content mismatch")
                    target = staging / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(raw)
                    return {"path": name, "original_path": original, "url": RAW + revision + "/" + quote(original, safe="/"),
                            "git_blob_oid": oid, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
                with ThreadPoolExecutor(max_workers=4) as pool:
                    rows = list(pool.map(download, files))
                match = re.search(r"^  version:\s*[\"']?([0-9]+\.[0-9]+\.[0-9]+)", (staging / "SKILL.md").read_text("utf-8"), re.MULTILINE)
                if not match:
                    raise UpdateError("missing upstream version")
                metadata = {"name": "yomiyasu", "version": match[1], "repository": REPOSITORY, "commit": revision,
                            "commit_date": head["commit"]["committer"]["date"], "license": "MIT", "additional_notice": "UNICODE-LICENSE.txt",
                            "modifications": "None. Exact official package bytes. No hooks installed or enabled.", "files": sorted(rows, key=lambda row: row["path"])}
                _write_json(staging / "UPSTREAM.json", metadata)
                validate_package(staging, expected=revision)
                if tuple(map(int, match[1].split("."))) < tuple(map(int, current["version"].split("."))):
                    raise UpdateError("refuse version downgrade")
                destination = CACHE / "versions" / revision
                if (destination.is_symlink() or destination.parent.is_symlink()
                        or not destination.resolve().is_relative_to(CACHE.resolve())):
                    raise UpdateError("invalid snapshot destination")
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    validate_package(destination, expected=revision)
                else:
                    staging.rename(destination)
                _write_json(CACHE / "active.json", {"commit": revision})
                _state("updated", checked_at=checked, skill_sync=_sync_skill())
            finally:
                if staging.exists() and not staging.is_symlink() and staging.resolve().is_relative_to(CACHE.resolve()):
                    shutil.rmtree(staging)
        except UpdateError:
            _state("incompatible", checked_at=checked)
        except (OSError, ValueError, TypeError, KeyError, SyntaxError, RecursionError):
            _state("offline", checked_at=checked)


def start_background():
    global _STARTED, _STATE
    with _START_LOCK:
        if _STARTED:
            return
        _STARTED = True
    if os.environ.get("YEBISU_YOMIYASU_OFFLINE") == "1":
        _STATE = {"state": "disabled"}
        return
    def run():
        global _STATE
        try:
            _check()
        except Exception:
            # No tracebacks, paths, credentials or manuscript in UI/startup.
            _STATE = {"state": "offline", "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    threading.Thread(target=run, name="yomiyasu-update", daemon=True).start()
