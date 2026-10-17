"""Open Google's official CLI for a user-operated Google login."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading

import gemini_cli

_LOCK = threading.Lock()
_ACTIVE = False


class LoginError(ValueError):
    pass


def _terminal_process(command, environment, cwd):
    if sys.platform == "win32":
        # Keep a real terminal open, including a useful error if CLI startup
        # fails. All arguments are fixed executable/config paths, never text
        # from the manuscript or the HTTP request. Quote PowerShell literals.
        console = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        line = "& " + " ".join("'" + str(argument).replace("'", "''") + "'" for argument in command)
        from decider_runtime import _external_dll_search, _external_environment
        with _external_dll_search():
            return subprocess.Popen([str(console), "-NoProfile", "-NoExit", "-Command", line], cwd=cwd,
                                    env=_external_environment(environment), creationflags=subprocess.CREATE_NEW_CONSOLE)
    if sys.platform == "darwin":
        # Export only non-secret runtime paths, never the parent's whole env.
        names = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM"}
        clean = {name: value for name, value in environment.items() if name in names}
        clean.setdefault("TERM", "xterm-256color")
        launch = Path(cwd) / "Gemini-Google-Login.command"
        arguments = ["/usr/bin/env", "-i", *[f"{name}={value}" for name, value in clean.items()], *command]
        launch.write_bytes(("#!/bin/zsh\ncd " + shlex.quote(str(cwd)) + "\nexec " + shlex.join(arguments) + "\n").encode("utf-8"))
        launch.chmod(0o700)
        return subprocess.Popen(["/usr/bin/open", "-W", "-a", "Terminal", str(launch)])
    raise LoginError("この環境では、ターミナルで agy を起動してGoogleログインを行ってください。")


def launch():
    global _ACTIVE
    with _LOCK:
        if _ACTIVE:
            return {"status": "already_open", "notice": "Antigravity CLIのログイン用ウィンドウは開いています。そこでGoogleログインを完了し、接続状態を更新してください。"}
        _ACTIVE = True
    started = threading.Event()
    failures = []

    def run():
        global _ACTIVE
        try:
            with gemini_cli.launch_context(interactive=True) as (command, environment, cwd):
                process = _terminal_process(command, environment, cwd)
                started.set()
                process.wait()
        except Exception:
            failures.append(True)
            started.set()
        finally:
            with _LOCK:
                _ACTIVE = False

    threading.Thread(target=run, name="gemini-google-login", daemon=True).start()
    if not started.wait(10) or failures:
        raise LoginError("Antigravity CLIを開けませんでした。ターミナルで agy を起動し、Googleログインを行ってください。")
    return {"status": "opened", "notice": "公式Antigravity CLIを開きました。契約したGoogleアカウントでログインしてください。完了後に接続状態を更新できます。"}


def wait_for_close():
    """Keep a CLI-launched login context alive until its terminal closes."""
    while _ACTIVE:
        threading.Event().wait(1)
