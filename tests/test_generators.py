"""No network, real keys, real credential-store access, or model calls."""
from contextlib import ExitStack
import ctypes
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import generators
import gemini_cli
import mac_credentials
import provider_settings as settings


SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"edits": {"type": "array", "maxItems": 2, "items": {
        "type": "object", "additionalProperties": False,
        "properties": {name: {"type": "string"} for name in ("before", "after", "reason", "rule")},
        "required": ["before", "after", "reason", "rule"]}},
        "questions": {"type": "array", "items": {"type": "string"}},
        "suggestions": {"type": "array", "items": {"type": "string"}}},
    "required": ["edits", "questions", "suggestions"],
}
PROPOSAL = {"edits": [{"before": "下さい", "after": "ください", "reason": "補助動詞", "rule": "表記"}],
            "questions": [], "suggestions": []}


def response(provider="sarashina", model=None, proposal=None, finish=None):
    text = json.dumps(PROPOSAL if proposal is None else proposal, ensure_ascii=False)
    return {"model": model or "sarashina3-mini", "choices": [
        {"finish_reason": finish or "stop", "message": {"content": text}}]}


class IsolatedSettings(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temporary = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.path = Path(temporary) / "providers.json"
        self.stack.enter_context(patch.object(settings, "SETTINGS_PATH", self.path))
        self.credential_read = self.stack.enter_context(patch.object(settings, "_credential_read", return_value=None))
        self.stack.enter_context(patch.object(settings.shutil, "which", return_value=None))
        self.stack.enter_context(patch.object(gemini_cli, "status", return_value={
            "cli_installed": False, "credential_cache_present": False, "configured": False,
            "credential_source": "none", "cli_status": "not_installed"}))
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))

    def test_missing_settings_default_to_named_gemini_but_unconfigured(self):
        public = settings.public_config()
        self.assertEqual((public["provider"], public["model"]), ("gemini", "gemini-3.8-flash-high"))
        self.assertFalse(any(item["available"] for item in public["providers"]))
        self.assertFalse(self.path.exists())

    def test_only_allowlisted_nonsecret_preferences_are_saved(self):
        public = settings.save_settings({"provider": "sarashina", "model": "sarashina3-nano"})
        stored = json.loads(self.path.read_text("utf-8"))
        self.assertEqual(stored, {"version": 1, "provider": "sarashina", "model": "sarashina3-nano"})
        self.assertEqual(public["model"], "sarashina3-nano")
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        for bad in ({"provider": "unknown"}, {"provider": []},
                    {"provider": "gemini", "model": "gemini-3.8-flash-lite"},
                    {"provider": "gemini", "api_key": "test-secret"}):
            with self.subTest(bad=bad), self.assertRaises(settings.ProviderSettingsError):
                settings.save_settings(bad)
        self.assertEqual(json.loads(self.path.read_text("utf-8")), stored)

    def test_corrupt_saved_choice_has_no_silent_provider_fallback(self):
        self.path.write_text('{"provider":"retired","model":"old"}', "utf-8")
        public = settings.public_config()
        self.assertIsNone(public["provider"])
        self.assertIsNone(public["model"])
        self.assertIn("configuration_error", public)
        with self.assertRaises(settings.ProviderSettingsError):
            settings.read_settings()

    def test_public_configuration_never_returns_environment_key(self):
        with patch.dict(os.environ, {"SARASHINA_API_KEY": "fake-private-key-never-public"}):
            public = settings.public_config()
            sarashina = next(item for item in public["providers"] if item["id"] == "sarashina")
            self.assertTrue(sarashina["configured"])
            self.assertEqual(sarashina["credential_source"], "environment")
            self.assertNotIn("fake-private-key-never-public", json.dumps(public))
            self.assertFalse(self.path.exists())

    def test_saved_sarashina_key_precedes_environment(self):
        with patch.dict(os.environ, {"SARASHINA_API_KEY": "fake-env"}):
            self.credential_read.return_value = "fake-saved"
            self.assertEqual(settings.api_key("sarashina"), "fake-saved")
            self.credential_read.return_value = None
            self.assertEqual(settings.api_key("sarashina"), "fake-env")

    def test_credential_store_failure_does_not_silently_switch_authentication(self):
        self.credential_read.side_effect = settings.ProviderSettingsError("credential_store_unavailable", "unavailable")
        with patch.dict(os.environ, {"SARASHINA_API_KEY": "fake-env"}):
            self.assertFalse(settings.credential_status("sarashina")["configured"])
            with self.assertRaises(settings.ProviderSettingsError) as error:
                settings.api_key("sarashina")
        self.assertEqual(error.exception.code, "credential_store_unavailable")

    def test_secret_save_uses_only_owned_windows_target_and_zeroes_temporary_buffer(self):
        class Credential(ctypes.Structure):
            _fields_ = [("Type", ctypes.c_uint32), ("Persist", ctypes.c_uint32),
                        ("TargetName", ctypes.c_wchar_p), ("UserName", ctypes.c_wchar_p),
                        ("CredentialBlobSize", ctypes.c_uint32),
                        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte))]

        captured = {}

        def write(pointer, flags):
            credential = ctypes.cast(pointer, ctypes.POINTER(Credential)).contents
            captured.update(target=credential.TargetName, kind=credential.Type, persist=credential.Persist,
                            value=ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize),
                            address=ctypes.addressof(credential.CredentialBlob.contents),
                            size=credential.CredentialBlobSize, flags=flags)
            return True

        api = Mock()
        api.CredWriteW.side_effect = write
        # Inspect the buffer during memset, while it is still alive.
        real_memset = ctypes.memset
        zeroed = []

        def wipe(buffer, value, length):
            result = real_memset(buffer, value, length)
            zeroed.append(bytes(buffer))
            return result

        with patch.object(settings, "_windows_api", return_value=(api, Credential)), patch.object(settings.ctypes, "memset", side_effect=wipe):
            public = settings.save_api_key("sarashina", "fake-private-key")
        self.assertEqual(captured["target"], settings.CREDENTIAL_PREFIX + "sarashina")
        self.assertEqual((captured["kind"], captured["persist"], captured["flags"]), (1, 2, 0))
        self.assertEqual(captured["value"], b"fake-private-key")
        self.assertEqual(zeroed, [b"\0" * len(b"fake-private-key")])
        self.assertFalse(self.path.exists())
        self.assertNotIn("fake-private-key", json.dumps(public))

    def test_remove_only_owned_saved_key_leaves_environment_visible(self):
        api = Mock()
        api.CredDeleteW.return_value = True
        with patch.object(settings, "_windows_api", return_value=(api, None)), patch.dict(os.environ, {"SARASHINA_API_KEY": "fake-env"}):
            public = settings.remove_api_key("sarashina")
            self.assertEqual(os.environ["SARASHINA_API_KEY"], "fake-env")
        api.CredDeleteW.assert_called_once_with(settings.CREDENTIAL_PREFIX + "sarashina", 1, 0)
        item = next(item for item in public["providers"] if item["id"] == "sarashina")
        self.assertEqual(item["credential_source"], "environment")

    def test_invalid_key_or_cli_key_save_never_calls_credential_manager(self):
        with patch.object(settings, "_windows_api") as api:
            for provider, key in (("sarashina", ""), ("sarashina", "a\nb"), ("sarashina", "a" * 2049), ("claude", "fake"), ("gemini", "fake")):
                with self.subTest(provider=provider, length=len(key)), self.assertRaises(settings.ProviderSettingsError):
                    settings.save_api_key(provider, key)
            api.assert_not_called()


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(settings, "read_settings", return_value={"version": 1, "provider": "gemini", "model": "gemini-3.8-flash-high"}))
        self.key = self.stack.enter_context(patch.object(settings, "api_key", return_value="fake-key"))
        self.post = self.stack.enter_context(patch.object(generators, "_post_json", return_value=response()))
        self.cli = self.stack.enter_context(patch.object(gemini_cli, "generate", return_value={
            "proposal": PROPOSAL, "identity": {"transport": "antigravity-cli", "reported_model": "gemini-3.8-flash-high"}}))

    def call(self, **payload):
        return generators.generate({"source": "保存して下さい。", "profile": "article", **payload}, "原文を守る校正方針", SCHEMA)

    def test_default_gemini_uses_cli_with_schema_and_never_reads_api_key(self):
        output = self.call()
        payload, instruction, schema = self.cli.call_args.args
        self.assertEqual(payload, {"source": "保存して下さい。", "profile": "article", "model": "gemini-3.8-flash-high"})
        self.assertEqual(instruction, "原文を守る校正方針")
        self.assertIs(schema, SCHEMA)
        self.assertEqual(output["proposal"], PROPOSAL)
        self.assertEqual(output["identity"]["reported_model"], "gemini-3.8-flash-high")
        self.assertEqual(output["identity"]["transport"], "antigravity-cli")
        self.key.assert_not_called()
        self.post.assert_not_called()

    def test_cli_typed_errors_propagate_without_api_or_other_provider_fallback(self):
        for code in ("not_configured", "cli_timeout", "identity_mismatch", "cli_failed"):
            self.cli.side_effect = gemini_cli.GeminiCliError(code, "安全なエラー")
            with self.subTest(code=code), self.assertRaises(generators.GenerationError) as error:
                self.call()
            self.assertEqual(error.exception.code, code)
        self.key.assert_not_called()
        self.post.assert_not_called()

    def test_missing_identity_and_cutoff_never_return_a_proposal(self):
        self.post.return_value = response()
        del self.post.return_value["model"]
        with self.assertRaises(generators.GenerationError) as missing:
            self.call(provider="sarashina")
        self.assertEqual(missing.exception.code, "identity_missing")
        self.post.return_value = response(finish="length")
        with self.assertRaises(generators.GenerationError) as cutoff:
            self.call(provider="sarashina")
        self.assertEqual(cutoff.exception.code, "generation_incomplete")
        self.post.return_value = response(model="sarashina3-nano")
        with self.assertRaises(generators.GenerationError) as mismatch:
            self.call(provider="sarashina")
        self.assertEqual(mismatch.exception.code, "identity_mismatch")

    def test_malformed_envelopes_are_typed_errors(self):
        variants = [
            {"model": "sarashina3-mini", "choices": [None]},
            {"model": "sarashina3-mini", "choices": [{"finish_reason": "stop", "message": None}]},
            {"model": "sarashina3-mini", "choices": None},
        ]
        for item in variants:
            self.post.return_value = item
            with self.subTest(item=item), self.assertRaises(generators.GenerationError):
                self.call(provider="sarashina")

    def test_schema_violation_and_non_json_are_rejected(self):
        for proposal in ({"edits": [], "questions": []}, {**PROPOSAL, "unrequested": "content"},
                         {**PROPOSAL, "edits": [{"before": 1, "after": "a", "reason": "b", "rule": "c"}]}):
            self.post.return_value = response(proposal=proposal)
            with self.subTest(proposal=proposal), self.assertRaises(generators.GenerationError) as error:
                self.call(provider="sarashina")
            self.assertEqual(error.exception.code, "invalid_proposal")
        self.post.return_value = response()
        self.post.return_value["choices"][0]["message"]["content"] = "```json\n{}\n```"
        with self.assertRaises(generators.GenerationError):
            self.call(provider="sarashina")

    def test_sarashina_explicit_short_text_limit_never_truncates(self):
        with self.assertRaises(generators.GenerationError) as error:
            self.call(provider="sarashina", source="あ" * 1501)
        self.assertEqual(error.exception.code, "input_too_long")
        self.post.assert_not_called()
        self.key.assert_not_called()
        self.post.return_value = response("sarashina", "sarashina3-nano")
        output = self.call(provider="sarashina", model="sarashina3-nano", source="あ" * 1500)
        endpoint, body, _, provider = self.post.call_args.args
        self.assertEqual((endpoint, provider), (generators.SARASHINA_ENDPOINT, "sarashina"))
        self.assertEqual(body["model"], "sarashina3-nano")
        self.assertEqual(body["max_completion_tokens"], 8000)
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertEqual(len(json.loads(body["messages"][1]["content"])["source"]), 1500)
        self.assertEqual(output["identity"]["reported_model"], "sarashina3-nano")

    def test_unconfigured_or_explicit_claude_never_falls_back(self):
        self.key.side_effect = settings.ProviderSettingsError("not_configured", "未設定")
        with self.assertRaises(generators.GenerationError) as error:
            self.call(provider="sarashina")
        self.assertEqual(error.exception.code, "not_configured")
        self.post.assert_not_called()
        with self.assertRaises(generators.GenerationError) as error:
            self.call(provider="claude")
        self.assertEqual(error.exception.code, "existing_adapter_required")
        self.post.assert_not_called()

    def test_invalid_requested_model_never_uses_another_model(self):
        with self.assertRaises(generators.GenerationError) as error:
            self.call(model="gemini-3.8-flash-lite")
        self.assertEqual(error.exception.code, "invalid_model")
        self.key.assert_not_called()
        self.post.assert_not_called()


class TransportTests(unittest.TestCase):
    def test_fixed_https_header_auth_no_proxy_no_redirect_no_key_in_url(self):
        fake_response = Mock()
        fake_response.__enter__ = Mock(return_value=fake_response)
        fake_response.__exit__ = Mock(return_value=False)
        fake_response.read.return_value = b'{"ok": true}'
        opener = Mock()
        opener.open.return_value = fake_response
        with patch.object(generators, "build_opener", return_value=opener) as factory:
            self.assertEqual(generators._post_json(generators.SARASHINA_ENDPOINT, {"source": "private-text"}, "fake-private-key", "sarashina"), {"ok": True})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, generators.SARASHINA_ENDPOINT)
        self.assertNotIn("fake-private-key", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-private-key")
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": generators.TIMEOUT_SECONDS})
        self.assertEqual(factory.call_args.args[0].proxies, {})
        self.assertIsInstance(factory.call_args.args[1], generators._NoRedirect)
        self.assertIsNone(factory.call_args.args[1].redirect_request(None, None, None, None, None, None))
        opener.open.assert_called_once()

    def test_http_errors_never_echo_server_body_url_key_or_source_and_do_not_retry(self):
        for code in (302, 400, 401, 403, 404, 429, 503):
            body = io.BytesIO(b"private-source fake-private-key")
            failure = HTTPError("https://example.invalid?key=fake-private-key", code, "private-source", {}, body)
            opener = Mock()
            opener.open.side_effect = failure
            with patch.object(generators, "build_opener", return_value=opener), self.subTest(code=code):
                with self.assertRaises(generators.GenerationError) as error:
                    generators._post_json(generators.SARASHINA_ENDPOINT, {}, "fake-private-key", "sarashina")
                self.assertEqual(error.exception.code, "http_error")
                self.assertNotIn("private-source", str(error.exception))
                self.assertNotIn("fake-private-key", str(error.exception))
                self.assertNotIn("example.invalid", str(error.exception))
                self.assertEqual(body.tell(), 0)
                opener.open.assert_called_once()

    def test_transport_errors_and_invalid_endpoint_do_not_leak_details(self):
        opener = Mock()
        opener.open.side_effect = URLError("private-source fake-private-key")
        with patch.object(generators, "build_opener", return_value=opener):
            with self.assertRaises(generators.GenerationError) as error:
                generators._post_json(generators.SARASHINA_ENDPOINT, {}, "fake-private-key", "sarashina")
        self.assertEqual(error.exception.code, "connection_error")
        self.assertNotIn("fake-private-key", str(error.exception))
        with patch.object(generators, "build_opener") as factory:
            with self.assertRaises(generators.GenerationError) as error:
                generators._post_json("http://127.0.0.1:9999", {}, "fake-private-key", "gemini")
            factory.assert_not_called()
        self.assertEqual(error.exception.code, "invalid_endpoint")

    def test_oversized_request_never_sends_and_oversized_response_is_typed(self):
        with patch.object(generators, "MAX_REQUEST_BYTES", 4), patch.object(generators, "build_opener") as factory:
            with self.assertRaises(generators.GenerationError) as error:
                generators._post_json(generators.SARASHINA_ENDPOINT, {"source": "あ"}, "fake-key", "sarashina")
            self.assertEqual(error.exception.code, "input_too_long")
            factory.assert_not_called()
        fake_response = Mock()
        fake_response.__enter__ = Mock(return_value=fake_response)
        fake_response.__exit__ = Mock(return_value=False)
        fake_response.read.return_value = b"12345"
        opener = Mock()
        opener.open.return_value = fake_response
        with patch.object(generators, "MAX_RESPONSE_BYTES", 4), patch.object(generators, "build_opener", return_value=opener):
            with self.assertRaises(generators.GenerationError) as error:
                generators._post_json(generators.SARASHINA_ENDPOINT, {}, "fake-key", "sarashina")
        self.assertEqual(error.exception.code, "response_too_large")


class MacKeychainTests(unittest.TestCase):
    """Validate the C ABI wiring with fake frameworks, never the real store."""

    def setUp(self):
        self.security, self.core = Mock(), Mock()
        self.security.SecKeychainFindGenericPassword.return_value = -25300
        self.security.SecKeychainAddGenericPassword.return_value = 0
        self.security.SecKeychainItemModifyAttributesAndData.return_value = 0
        self.security.SecKeychainItemDelete.return_value = 0
        self.frameworks = patch.object(mac_credentials, "_frameworks", return_value=(self.security, self.core))
        self.frameworks.start()
        self.addCleanup(self.frameworks.stop)

    def find_item(self, *, secret=None, status=0):
        if secret is not None:
            self.secret = ctypes.create_string_buffer(secret)

        def find(_keychain, service_length, service, account_length, account, length, data, item):
            self.assertEqual((service_length, service), (len(mac_credentials.SERVICE), mac_credentials.SERVICE))
            self.assertEqual(account_length, len(account))
            self.assertIn(account, (b"gemini", b"sarashina"))
            if status == 0:
                ctypes.cast(item, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(123)
                if data is not None:
                    ctypes.cast(length, ctypes.POINTER(ctypes.c_uint32))[0] = len(secret)
                    ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.cast(self.secret, ctypes.c_void_p)
            return status

        self.security.SecKeychainFindGenericPassword.side_effect = find

    def test_presence_does_not_request_password_and_releases_item(self):
        self.find_item()
        self.assertTrue(mac_credentials.read("gemini", presence_only=True))
        args = self.security.SecKeychainFindGenericPassword.call_args.args
        self.assertIsNone(args[5])
        self.assertIsNone(args[6])
        self.security.SecKeychainItemFreeContent.assert_not_called()
        self.assertEqual(self.core.CFRelease.call_args.args[0].value, 123)

    def test_read_frees_native_password_and_item_even_if_invalid(self):
        self.find_item(secret=b"fake-private-key")
        self.assertEqual(mac_credentials.read("sarashina"), "fake-private-key")
        self.security.SecKeychainItemFreeContent.assert_called_once()
        self.core.CFRelease.assert_called_once()
        self.security.reset_mock()
        self.core.reset_mock()
        self.find_item(secret=b"\xff")
        with self.assertRaises(mac_credentials.KeychainError) as error:
            mac_credentials.read("gemini")
        self.assertEqual(error.exception.code, "credential_invalid")
        self.security.SecKeychainItemFreeContent.assert_called_once()
        self.core.CFRelease.assert_called_once()

    def test_new_key_and_replace_use_only_fixed_service_account_and_native_data(self):
        captured = []

        def add(_keychain, service_length, service, account_length, account, length, data, item):
            captured.append((service, account, ctypes.string_at(data, length)))
            ctypes.cast(item, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(456)
            return 0

        self.security.SecKeychainAddGenericPassword.side_effect = add
        mac_credentials.save("gemini", "fake-key")
        self.assertEqual(captured, [(mac_credentials.SERVICE, b"gemini", b"fake-key")])
        self.assertEqual(self.core.CFRelease.call_args.args[0].value, 456)
        self.find_item()
        mac_credentials.save("sarashina", "fake-replacement")
        args = self.security.SecKeychainItemModifyAttributesAndData.call_args.args
        self.assertEqual(args[0].value, 123)
        self.assertIsNone(args[1])
        # The temporary native input buffer has been wiped by the time save returns.
        self.assertEqual(bytes(args[3]), b"\0" * args[2])
        self.assertEqual(self.core.CFRelease.call_args.args[0].value, 123)

    def test_failed_key_write_releases_item_and_wipes_temporary_buffer(self):
        self.find_item()
        self.security.SecKeychainItemModifyAttributesAndData.return_value = -128
        with self.assertRaises(mac_credentials.KeychainError) as error:
            mac_credentials.save("gemini", "fake-private-key")
        self.assertEqual(error.exception.code, "credential_save_failed")
        self.assertNotIn("fake-private-key", str(error.exception))
        args = self.security.SecKeychainItemModifyAttributesAndData.call_args.args
        self.assertEqual(bytes(args[3]), b"\0" * args[2])
        self.core.CFRelease.assert_called_once()

    def test_delete_only_found_owned_item_and_not_found_is_idempotent(self):
        mac_credentials.remove("gemini")
        self.security.SecKeychainItemDelete.assert_not_called()
        self.find_item()
        mac_credentials.remove("sarashina")
        self.assertEqual(self.security.SecKeychainItemDelete.call_args.args[0].value, 123)
        self.core.CFRelease.assert_called_once()
        with self.assertRaises(mac_credentials.KeychainError):
            mac_credentials.remove("unrelated")

    def test_mac_provider_dispatch_stays_secret_and_does_not_touch_windows_store(self):
        with patch.object(settings.sys, "platform", "darwin"), patch.object(settings, "_windows_api") as windows, \
             patch.object(settings, "public_config", return_value={"providers": []}), \
             patch.object(mac_credentials, "save") as save, patch.object(mac_credentials, "remove") as remove, \
             patch.object(mac_credentials, "read", return_value="fake-saved") as read:
            settings.save_api_key("sarashina", "fake-key")
            settings.remove_api_key("sarashina")
            self.assertEqual(settings.api_key("sarashina"), "fake-saved")
            save.assert_called_once_with("sarashina", "fake-key")
            remove.assert_called_once_with("sarashina")
            read.assert_called_once_with("sarashina", presence_only=False)
            windows.assert_not_called()

    def test_only_missing_framework_allows_explicit_environment_fallback(self):
        with patch.object(settings.sys, "platform", "darwin"), patch.dict(os.environ, {"SARASHINA_API_KEY": "fake-env"}, clear=True), \
             patch.object(mac_credentials, "read", side_effect=mac_credentials.KeychainError("credential_framework_unavailable", "missing")):
            self.assertEqual(settings.api_key("sarashina"), "fake-env")
            self.assertEqual(settings.credential_status("sarashina")["credential_source"], "environment")
        with patch.object(settings.sys, "platform", "darwin"), patch.dict(os.environ, {"SARASHINA_API_KEY": "fake-env"}, clear=True), \
             patch.object(mac_credentials, "read", side_effect=mac_credentials.KeychainError("credential_store_unavailable", "denied")):
            with self.assertRaises(settings.ProviderSettingsError):
                settings.api_key("sarashina")

    def test_framework_loader_uses_absolute_system_paths_and_declares_native_types(self):
        self.frameworks.stop()
        libraries = [self.security, self.core]
        with patch.object(mac_credentials.sys, "platform", "darwin"), patch.object(mac_credentials.ctypes, "CDLL", side_effect=libraries) as load:
            self.assertEqual(mac_credentials._frameworks(), (self.security, self.core))
        self.assertEqual([call.args[0] for call in load.call_args_list], [
            "/System/Library/Frameworks/Security.framework/Security",
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"])
        self.assertEqual(self.security.SecKeychainFindGenericPassword.restype, ctypes.c_int32)
        self.assertEqual(len(self.security.SecKeychainFindGenericPassword.argtypes), 8)
        self.assertIsNone(self.core.CFRelease.restype)


if __name__ == "__main__":
    unittest.main()
