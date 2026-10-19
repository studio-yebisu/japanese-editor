"""No real login, browser, console, credential access, or generation."""
from contextlib import contextmanager
from email.message import Message
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli_login
import gemini_cli
import provider_settings
import app


class GeminiSetupTests(unittest.TestCase):
    def test_previous_saved_flash_default_migrates_without_mutating_user_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"providers.json"
            original = b'{"version":1,"provider":"gemini","model":"gemini-3.8-flash"}'
            path.write_bytes(original)
            with patch.object(provider_settings,"SETTINGS_PATH",path):
                self.assertEqual(provider_settings.read_settings()["model"],"gemini-3.8-flash-high")
            self.assertEqual(path.read_bytes(),original)
            with self.assertRaises(provider_settings.ProviderSettingsError):
                provider_settings._preference({"provider":"gemini","model":"gemini-3.8-flash"})

    def handler(self, payload, origin="http://127.0.0.1:8896", route="/api/gemini/login"):
        handler = object.__new__(app.Handler)
        handler.server = SimpleNamespace(server_port=8896, closing=False)
        handler.path = route
        handler.headers = Message()
        raw = json.dumps(payload).encode()
        for key, value in {"Host":"127.0.0.1:8896", "Origin":origin,
                           "Content-Type":"application/json", "Content-Length":str(len(raw))}.items():
            handler.headers[key] = value
        handler.rfile = io.BytesIO(raw)
        handler.send = Mock()
        return handler

    def test_login_endpoint_accepts_only_empty_same_origin_action(self):
        with patch.object(cli_login, "launch", return_value={"status":"opened"}) as login:
            allowed = self.handler({})
            allowed.do_POST()
            self.assertEqual(allowed.send.call_args.args, (200,{"status":"opened"}))
            login.assert_called_once_with()
            login.reset_mock()
            for handler in (self.handler({"command":"arbitrary"}), self.handler({},origin="https://other.example")):
                handler.do_POST()
                self.assertIn(handler.send.call_args.args[0], (400,403))
            login.assert_not_called()

    def test_gemini_api_payload_is_rejected_before_preference_or_key_mutation(self):
        with patch.object(provider_settings,"save_settings") as save, patch.object(provider_settings,"save_api_key") as key:
            handler = self.handler({"provider":"gemini","model":"gemini-3.8-flash","api_key":"unused"},route="/api/settings")
            handler.do_POST()
        self.assertEqual(handler.send.call_args.args[0],400)
        save.assert_not_called()
        key.assert_not_called()

    def test_gemini_uses_cli_status_without_any_api_key_read(self):
        state = {"configured": False, "cli_installed": True, "credential_cache_present": False,
                 "credential_source": "google_cli_login", "cli_status": "login_required"}
        with patch.object(gemini_cli, "status", return_value=state), \
             patch.object(provider_settings, "_credential_read", side_effect=AssertionError("must not read Gemini API keys")):
            self.assertEqual(provider_settings.credential_status("gemini"), state)
        self.assertEqual(provider_settings.PROVIDERS["gemini"]["transport"], "antigravity-cli")
        self.assertEqual(provider_settings.PROVIDERS["sarashina"]["auth_method"], "api_key")

    def test_gemini_key_operations_are_rejected_before_secret_store_access(self):
        with patch.object(provider_settings, "_credential_read") as read, \
             patch.object(provider_settings, "_windows_api") as write:
            for action in (lambda: provider_settings.api_key("gemini"),
                           lambda: provider_settings.save_api_key("gemini", "unused-key"),
                           lambda: provider_settings.remove_api_key("gemini")):
                with self.assertRaises(provider_settings.ProviderSettingsError):
                    action()
            read.assert_not_called()
            write.assert_not_called()

    def test_windows_login_opens_exact_command_in_interactive_console(self):
        # Keep the Windows flag explicit when the fixture runs on POSIX.
        new_console_flag = 0x10
        with patch.object(cli_login.sys, "platform", "win32"), \
             patch.object(cli_login.subprocess, "CREATE_NEW_CONSOLE", new_console_flag, create=True), \
             patch.object(cli_login.subprocess, "Popen") as start:
            cli_login._terminal_process(["agy.exe"], {"HOME":"fake"}, "login-work")
        arguments = start.call_args.args[0]
        self.assertTrue(arguments[0].endswith("powershell.exe"))
        self.assertEqual(arguments[1:4], ["-NoProfile", "-NoExit", "-Command"])
        self.assertEqual(arguments[4], "& 'agy.exe'")
        self.assertEqual(start.call_args.kwargs, {"cwd":"login-work", "env":{"HOME":"fake"},
                          "creationflags":new_console_flag})

    def test_macos_launcher_does_not_persist_inherited_credentials(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(cli_login.sys, "platform", "darwin"), \
             patch.object(cli_login.subprocess, "Popen") as start:
            cli_login._terminal_process(["/safe/agy"],
                {"HOME":"/user/home", "GEMINI_CLI_HOME":"/user/home/.gemini", "OTHER_API_KEY":"never-persist-me",
                 "GEMINI_API_KEY":"never-use-api", "GEMINI_CLI_SYSTEM_SETTINGS_PATH":"/safe/settings.json"}, directory)
            script = (Path(directory)/"Gemini-Google-Login.command").read_text("utf-8")
            self.assertNotIn("never-persist-me", script)
            self.assertNotIn("never-use-api", script)
            self.assertNotIn("GEMINI_CLI_SYSTEM_SETTINGS_PATH", script)
            self.assertEqual(start.call_args.args[0][:4], ["/usr/bin/open", "-W", "-a", "Terminal"])

    def test_login_setup_errors_are_sanitized(self):
        @contextmanager
        def rejected(**_):
            raise OSError("private-token-or-path")
            yield
        with patch.object(gemini_cli, "launch_context", rejected):
            with self.assertRaises(cli_login.LoginError) as error:
                cli_login.launch()
        self.assertNotIn("private-token-or-path", str(error.exception))
        self.assertFalse(cli_login._ACTIVE)

    def test_repeated_click_does_not_open_another_login_window(self):
        with patch.object(cli_login, "_ACTIVE", True), patch.object(cli_login.threading, "Thread") as thread:
            self.assertEqual(cli_login.launch()["status"], "already_open")
        thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
