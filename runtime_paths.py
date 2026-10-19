"""Resources are read-only; per-user state stays outside the distribution."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

FROZEN = bool(getattr(sys, "frozen", False))
RESOURCE_ROOT = Path(sys._MEIPASS) / "editor" if FROZEN else Path(__file__).resolve().parent
_override = os.environ.get("YEBISU_EDITOR_DATA_DIR")
if _override:
    PERSISTENT_ROOT = Path(_override).expanduser().resolve()
elif os.name == "nt":
    PERSISTENT_ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "StudioYebisu" / "JapaneseEditor"
elif sys.platform == "darwin":
    PERSISTENT_ROOT = Path.home() / "Library/Application Support/StudioYebisu/JapaneseEditor"
else:
    PERSISTENT_ROOT = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "studio-yebisu/japanese-editor"

# Windows Store virtualization and junctions can name one directory differently.
# Use the same physical identity with and without a forwarded environment value.
PERSISTENT_ROOT = PERSISTENT_ROOT.resolve()


def cli_command(*arguments):
    if FROZEN:
        suffix = ".exe" if os.name == "nt" else ""
        directory = Path(sys.executable).resolve().parent
        command = directory / ("YomiyasuStudioCLI" + suffix)
        legacy = directory / ("StudioYebisuEditorCLI" + suffix)
        # Older installations remain usable until their complete bundle is updated.
        if not command.is_file() and legacy.is_file():
            command = legacy
        return [str(command), *map(str, arguments)]
    return [sys.executable, "-B", str(RESOURCE_ROOT / "app.py"), *map(str, arguments)]


def vendor_command(script, *arguments):
    if script not in ("yomiyasu_lint.py", "yomiyasu_check.py", "yomiyasu_diff.py"):
        raise ValueError("Unsupported bundled checker")
    if FROZEN:
        return cli_command("internal-yomiyasu", script, *arguments)
    return [sys.executable, "-B", str(RESOURCE_ROOT / "vendor/yomiyasu/scripts" / script), *map(str, arguments)]


def hidden_process():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
