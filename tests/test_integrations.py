"""Registration must never run the CLI against real user config or auth."""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import integrations as integration
import yomiyasu_updates

COMMAND = ["python-native", "-B", "/application/app.py", "mcp"]
FAKE_CODEX_HOME = ROOT / "test-only-codex-home"


def native_config(codex_home=FAKE_CODEX_HOME):
    return (f'[mcp_servers.{integration.SERVER}]\ncommand = {json.dumps(COMMAND[0])}\n'
            f'args = {json.dumps(COMMAND[1:])}\n\n[mcp_servers.{integration.SERVER}.env]\n'
            'YEBISU_EDITOR_DATA_DIR = ' + json.dumps(str(integration.PERSISTENT_ROOT)) + '\n'
            'CODEX_HOME = ' + json.dumps(str(codex_home)) + '\n').encode("utf-8")


class MergeTests(unittest.TestCase):
    def test_preserves_unrelated_default_fields_comments_and_multiline_content_raw(self):
        original = b'''# user comment retained verbatim
model = "custom"
instructions = """
[mcp_servers.yebisu-japanese-editor]
command = "fake header inside a multiline string"
"""
literal = ''' + b"'''\n[mcp_servers.yebisu-japanese-editor.env]\nnot_a_setting = true\n'''\n" + b'''
[mcp_servers.godot-ai] # this explicit default matters to the owner
enabled = true
command = "godot"

# preserve this provider comment too
[mcp_servers.treg-free]
enabled = true
command = "treg"
[mcp_servers.node_repl]
args = []
command = "node"
values = [
  ["array data that is not a table"],
]
'''
        merged = integration.merge_owned_toml(original, native_config(), COMMAND, FAKE_CODEX_HOME)
        self.assertTrue(merged.startswith(original))
        before, after = tomllib.loads(original.decode()), tomllib.loads(merged.decode())
        self.assertEqual(integration._without_owned(before), integration._without_owned(after))
        self.assertTrue(after["mcp_servers"]["godot-ai"]["enabled"])
        self.assertEqual(after["mcp_servers"]["node_repl"]["args"], [])

    def test_repeated_merge_has_one_owned_table_and_no_timeout_duplicates(self):
        first = integration.merge_owned_toml(b"# retained\nmodel = 'custom'\n", native_config(), COMMAND, FAKE_CODEX_HOME)
        second = integration.merge_owned_toml(first, native_config(), COMMAND, FAKE_CODEX_HOME)
        self.assertEqual(second, first)
        self.assertEqual(second.count(b"startup_timeout_sec = 30"), 1)
        self.assertEqual(second.count(b"tool_timeout_sec = 600"), 1)
        self.assertEqual(second.count(f"[mcp_servers.{integration.SERVER}]".encode()), 1)

    def test_existing_quoted_owned_tables_only_are_replaced_and_comments_retained(self):
        original = (b'# top\n[mcp_servers."yebisu-japanese-editor"] # owned\ncommand = "old"\nargs = []\n'
                    b'startup_timeout_sec = 1\n\n# comment for next unrelated server\n'
                    b'[mcp_servers.other]\nenabled = true\ncommand = "other"\n'
                    b'[mcp_servers."yebisu-japanese-editor".env]\nOLD = "value"\n'
                    b'# last comment stays\n')
        merged = integration.merge_owned_toml(original, native_config(), COMMAND, FAKE_CODEX_HOME)
        self.assertIn(b'# comment for next unrelated server\n[mcp_servers.other]\nenabled = true\ncommand = "other"\n', merged)
        self.assertIn(b'# last comment stays\n', merged)
        self.assertNotIn(b'OLD = "value"', merged)
        self.assertEqual(tomllib.loads(merged.decode())["mcp_servers"][integration.SERVER]["command"], COMMAND[0])

    def test_bom_and_crlf_preserved_and_unsafe_formats_rejected_before_write(self):
        original = b"\xef\xbb\xbf# comment\r\nmodel = 'custom'\r\n"
        merged = integration.merge_owned_toml(original, native_config(), COMMAND, FAKE_CODEX_HOME)
        self.assertTrue(merged.startswith(original))
        self.assertNotIn(b"\n", merged.replace(b"\r\n", b""))
        for original in (b'mcp_servers = {other = {command = "x"}}\n',
                         b'mcp_servers = {"yebisu-japanese-editor" = {command = "x"}}\n'):
            with self.subTest(original=original), self.assertRaises(integration.IntegrationError):
                integration.merge_owned_toml(original, native_config(), COMMAND, FAKE_CODEX_HOME)
        with self.assertRaises(integration.IntegrationError):
            integration.merge_owned_toml(b"", b'model = "unexpected"\n' + native_config(), COMMAND, FAKE_CODEX_HOME)

    def test_exact_server_environment_is_required_including_codex_home(self):
        for generated in (native_config(FAKE_CODEX_HOME / "different"),
                          b"\n".join(line for line in native_config().split(b"\n") if not line.startswith(b"CODEX_HOME ="))):
            with self.subTest(generated=generated), self.assertRaises(integration.IntegrationError):
                integration.merge_owned_toml(b"", generated, COMMAND, FAKE_CODEX_HOME)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.base, self.home = Path(folder), Path(folder) / "home"
        self.config = self.home / ".codex/config.toml"
        self.config.parent.mkdir(parents=True)
        self.original = b"# preserve all bytes\r\n[mcp_servers.other]\r\nenabled = true\r\nargs = []\r\ncommand = 'other'\r\n"
        self.config.write_bytes(self.original)
        self.data, self.resources = self.base / "data", self.base / "resources"
        self.target = self.home / ".agents/skills" / integration.SKILL
        for name, content in {
            "skills/japanese-editor/SKILL.md": "---\nname: japanese-editor\n---\n[guide](../../vendor/yomiyasu/SKILL.md) [app](../../README.md)\n",
            "vendor/yomiyasu/SKILL.md": "Original upstream guide\n", "vendor/yomiyasu/LICENSE": "upstream notice\n",
            "README.md": "App guide [license](LICENSE) [notices](THIRD_PARTY_NOTICES.md) [plugins](docs/PLUGINS.md)\n",
            "THIRD_PARTY_NOTICES.md": "[upstream](vendor/yomiyasu/LICENSE)\n", "CONTRIBUTING.md": "Contributing\n", "SECURITY.md": "Security\n",
            "docs/PLUGINS.md": "Plugin guide\n", "docs/PROVIDERS.md": "Providers\n", "docs/DECIDER_RUNTIME.md": "Runtime\n",
            "docs/SOURCES.md": "Sources\n", "docs/STATUS.md": "Status\n", "docs/WINDOWS.md": "Windows\n", "docs/INSTALL.md": "Setup\n",
            "evaluation/cases.json": "[]\n", "verification.json": "{}\n", "LICENSE": "MIT notice\n",
            "evaluation/controls.json": "[]\n", "evaluation/runtime-results.json": "{}\n", "evaluation/README.md": "Public examples\n",
            "vendor/yomiyasu/UNICODE-LICENSE.txt": "Unicode notice\n",
        }.items():
            path = self.resources / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, "utf-8")
        vendor = self.resources / "vendor/yomiyasu"
        manifest = {"files": [{"path": name, "sha256": hashlib.sha256((vendor / name).read_bytes()).hexdigest()}
                              for name in ("SKILL.md", "LICENSE", "UNICODE-LICENSE.txt")]}
        (vendor / "UPSTREAM.json").write_text(json.dumps(manifest), "utf-8")
        (self.resources / "docs/PRIVATE_CONSULTATION.md").write_text("private development snapshot", "utf-8")
        (self.resources / "evaluation/private-receipt.json").write_text("not public", "utf-8")
        self.stack.enter_context(patch.object(integration, "RESOURCE_ROOT", self.resources))
        self.stack.enter_context(patch.object(yomiyasu_updates, "current_root", return_value=vendor))
        self.stack.enter_context(patch.object(integration, "PERSISTENT_ROOT", self.data))
        self.stack.enter_context(patch.object(integration, "MANIFEST", self.data / "codex-integration.json"))
        self.stack.enter_context(patch.object(integration, "_config_path", return_value=self.config))
        self.stack.enter_context(patch.object(integration.Path, "home", return_value=self.home))
        self.stack.enter_context(patch.object(integration, "cli_command", return_value=COMMAND))
        self.stack.enter_context(patch.object(integration, "codex_executable", return_value="native-codex"))
        self.native = self.stack.enter_context(patch.object(integration.subprocess, "run", side_effect=self.native_run))
        self.native_homes = []

    def native_run(self, arguments, **kwargs):
        native_home = Path(kwargs["env"]["CODEX_HOME"])
        self.assertNotEqual(native_home, self.config.parent)
        self.assertEqual(Path(kwargs["cwd"]), native_home)
        self.assertTrue(native_home.is_dir())
        self.assertFalse((native_home / "auth.json").exists())
        self.assertFalse((native_home / "config.toml").exists())
        self.assertEqual(arguments[:4], ["native-codex", "mcp", "add", integration.SERVER])
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
        self.assertIn("CODEX_HOME=" + str(self.config.parent.resolve()), arguments)
        self.assertNotEqual(native_home, self.config.parent.resolve())
        self.native_homes.append(native_home)
        (native_home / "config.toml").write_bytes(native_config(self.config.parent.resolve()))
        return Mock(returncode=0)

    def test_install_uses_isolated_cli_preserves_raw_config_and_repeats_cleanly(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake-key-must-not-be-forwarded"}):
            first = integration.install()
            first_bytes = self.config.read_bytes()
            second = integration.install()
        self.assertEqual((first["status"], second["status"]), ("installed", "installed"))
        self.assertTrue(first_bytes.startswith(self.original))
        own = tomllib.loads(first_bytes.decode("utf-8"))["mcp_servers"][integration.SERVER]
        self.assertEqual(own["env"], {"CODEX_HOME": str(self.config.parent.resolve()), "YEBISU_EDITOR_DATA_DIR": str(self.data)})
        manifest = json.loads(integration.MANIFEST.read_text("utf-8"))
        self.assertEqual(manifest["codex_home"], str(self.config.parent.resolve()))
        self.assertEqual(self.config.read_bytes(), first_bytes)
        self.assertTrue(all(not home.exists() for home in self.native_homes))
        backups = sorted((self.data / "backups").glob("*.toml"))
        self.assertEqual(len(backups), 2)
        self.assertIn(self.original, [path.read_bytes() for path in backups])
        guide = (self.target / "SKILL.md").read_text("utf-8")
        self.assertIn("name: " + integration.SKILL, guide)
        self.assertIn("references/yomiyasu/SKILL.md", guide)
        self.assertTrue((self.target / "references/yomiyasu/LICENSE").is_file())
        self.assertEqual({p.name for p in (self.target / "references/docs").iterdir()}, set(integration.PUBLIC_DOCS))
        self.assertEqual({p.name for p in (self.target / "references/evaluation").iterdir()}, set(integration.PUBLIC_EVALUATION))
        self.assertFalse(list(self.target.parent.glob(".yebisu-skill-*")))
        self.assertTrue((self.target / "references/LICENSE").is_file())
        notices = (self.target / "references/THIRD_PARTY_NOTICES.md").read_text("utf-8")
        self.assertIn("](yomiyasu/LICENSE)", notices)

    def test_collision_aborts_before_cli_skill_or_backup_writes(self):
        collision = f'[mcp_servers.{integration.SERVER}]\ncommand = "someone-else"\nargs = []\n'.encode()
        self.config.write_bytes(collision)
        with self.assertRaises(integration.IntegrationError):
            integration.install()
        self.native.assert_not_called()
        self.assertEqual(self.config.read_bytes(), collision)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.data.exists())

    def test_existing_same_command_with_old_environment_can_be_upgraded(self):
        integration.install()
        old = b"".join(line for line in self.config.read_bytes().splitlines(keepends=True) if not line.startswith(b"CODEX_HOME ="))
        self.config.write_bytes(old)
        integration.install()
        own = tomllib.loads(self.config.read_text("utf-8"))["mcp_servers"][integration.SERVER]
        self.assertEqual(own["env"], integration.server_environment())
        self.assertEqual(own["env"]["CODEX_HOME"], str(self.config.parent.resolve()))
        self.assertTrue(self.config.read_bytes().startswith(self.original))

    def test_changed_added_or_deleted_skill_file_aborts_before_cli_and_config_write(self):
        integration.install()
        registered = self.config.read_bytes()
        self.native.reset_mock()
        original = (self.target / "SKILL.md").read_bytes()
        (self.target / "SKILL.md").write_bytes(original + b"user change\n")
        with self.assertRaises(integration.IntegrationError):
            integration.install()
        (self.target / "SKILL.md").write_bytes(original)
        (self.target / "extra.txt").write_text("user file", "utf-8")
        with self.assertRaises(integration.IntegrationError):
            integration.install()
        (self.target / "extra.txt").unlink()
        (self.target / "LICENSE").unlink()
        with self.assertRaises(integration.IntegrationError):
            integration.install()
        self.native.assert_not_called()
        self.assertEqual(self.config.read_bytes(), registered)

    def test_concurrent_config_edit_during_isolated_cli_is_not_overwritten(self):
        def concurrent(arguments, **kwargs):
            result = self.native_run(arguments, **kwargs)
            self.config.write_bytes(self.original + b"# another process changed config\r\n")
            return result

        self.native.side_effect = concurrent
        with self.assertRaises(integration.IntegrationError):
            integration.install()
        self.assertEqual(self.config.read_bytes(), self.original + b"# another process changed config\r\n")
        self.assertFalse(self.target.exists())

    def test_write_guard_rejects_changed_or_unexpected_created_file(self):
        with self.assertRaises(integration.IntegrationError):
            integration._write_if_unchanged(self.config, b"old expected", b"replacement")
        with self.assertRaises(integration.IntegrationError):
            integration._write_if_unchanged(self.config, None, b"replacement")
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertFalse(list(self.config.parent.glob(".yebisu-config-*")))

    def test_native_failure_or_unrelated_generated_settings_leave_original_untouched(self):
        self.native.return_value = Mock(returncode=1)
        self.native.side_effect = None
        with self.assertRaises(integration.IntegrationError):
            integration.install()
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertFalse(self.target.exists())

        def unrelated(arguments, **kwargs):
            result = self.native_run(arguments, **kwargs)
            config = Path(kwargs["env"]["CODEX_HOME"]) / "config.toml"
            config.write_bytes(b'model = "unrequested-default"\n' + config.read_bytes())
            return result

        self.native.side_effect = unrelated
        with self.assertRaises(integration.IntegrationError):
            integration.install()
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.data.exists())

    def test_missing_public_resource_or_vendor_hash_failure_precedes_any_registration(self):
        source = self.resources / "docs/PROVIDERS.md"
        saved = source.read_bytes()
        source.unlink()
        with self.assertRaises(integration.IntegrationError):
            integration.install()
        source.write_bytes(saved)
        (self.resources / "vendor/yomiyasu/LICENSE").write_text("modified upstream", "utf-8")
        with self.assertRaises(integration.IntegrationError):
            integration.install()
        self.native.assert_not_called()
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.data.exists())

    def test_unresolved_readme_reference_aborts_before_registration(self):
        (self.resources / "README.md").write_text("[not distributed](docs/PRIVATE_CONSULTATION.md)", "utf-8")
        with self.assertRaises(integration.IntegrationError):
            integration.install()
        self.native.assert_not_called()
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_atomic_update_removes_formerly_manifested_private_docs(self):
        integration.install()
        legacy = self.target / "references/docs/OLD_PRIVATE_REVIEW.md"
        legacy.write_text("historical snapshot", "utf-8")
        record = json.loads(integration.MANIFEST.read_text("utf-8"))
        record["skill_sha256"][legacy.relative_to(self.target).as_posix()] = hashlib.sha256(legacy.read_bytes()).hexdigest()
        integration.MANIFEST.write_text(json.dumps(record), "utf-8")
        integration.install()
        self.assertFalse(legacy.exists())
        self.assertTrue((self.target / "references/docs/PLUGINS.md").is_file())
        self.assertFalse(list(self.target.parent.glob(".yebisu-skill-*")))

    def test_manifest_commit_failure_rolls_back_config_and_owned_skill(self):
        integration.install()
        config_before = self.config.read_bytes()
        guide_before = (self.target / "references/APP.md").read_bytes()
        manifest_before = integration.MANIFEST.read_bytes()
        (self.resources / "README.md").write_text("Updated public guide\n", "utf-8")
        real_write = integration._write_if_unchanged

        def fail_manifest(path, expected, replacement):
            if path == integration.MANIFEST:
                raise integration.IntegrationError("mock failure")
            return real_write(path, expected, replacement)

        with patch.object(integration, "_write_if_unchanged", side_effect=fail_manifest):
            with self.assertRaises(integration.IntegrationError):
                integration.install()
        self.assertEqual(self.config.read_bytes(), config_before)
        self.assertEqual((self.target / "references/APP.md").read_bytes(), guide_before)
        self.assertEqual(integration.MANIFEST.read_bytes(), manifest_before)
        self.assertFalse(list(self.target.parent.glob(".yebisu-skill-*")))


class BundledReferenceTests(unittest.TestCase):
    def test_current_public_bundle_resolves_every_documentation_link(self):
        contents = integration._skill_contents()
        self.assertIn("references/docs/INSTALL.md", contents)
        self.assertIn("references/yomiyasu/references/gemini-syntax.md", contents)


class DiscoveryTests(unittest.TestCase):
    def test_windows_npm_shim_resolves_shipped_native_binary(self):
        with tempfile.TemporaryDirectory() as folder:
            npm = Path(folder)
            native = npm / "node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x64/bin/codex.exe"
            native.parent.mkdir(parents=True)
            native.write_bytes(b"fake executable never run")
            with patch.object(integration, "IS_WINDOWS", True), patch.object(integration.shutil, "which", side_effect=lambda name: str(npm / "codex.cmd") if name == "codex.cmd" else None):
                self.assertEqual(integration.codex_executable(), str(native))

    def test_posix_command_is_supported_and_windows_shim_without_native_is_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            command = str(Path(folder) / "codex")
            with patch.object(integration.shutil, "which", side_effect=lambda name: command if name == "codex" else None):
                with patch.object(integration, "IS_WINDOWS", False):
                    self.assertEqual(integration.codex_executable(), command)
                with patch.object(integration, "IS_WINDOWS", True):
                    self.assertIsNone(integration.codex_executable())


class ConfigPathTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.home, self.recorded = folder / "user", folder / "registered-codex"
        self.manifest = folder / "codex-integration.json"
        self.stack.enter_context(patch.object(integration, "MANIFEST", self.manifest))
        self.stack.enter_context(patch.object(integration.Path, "home", return_value=self.home))
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))

    def test_no_environment_uses_own_recorded_absolute_home_and_public_environment(self):
        self.manifest.write_text(json.dumps({"codex_home": str(self.recorded)}), "utf-8")
        self.assertEqual(integration._config_path(), self.recorded / "config.toml")
        self.assertEqual(integration.server_environment(), {
            "CODEX_HOME": str(self.recorded.resolve()), "YEBISU_EDITOR_DATA_DIR": str(integration.PERSISTENT_ROOT)})
        self.assertFalse(self.recorded.exists())

    def test_explicit_environment_takes_precedence_over_recorded_home(self):
        self.manifest.write_text(json.dumps({"codex_home": str(self.recorded)}), "utf-8")
        explicit = self.home / "explicit-codex"
        with patch.dict(os.environ, {"CODEX_HOME": str(explicit)}):
            self.assertEqual(integration._config_path(), explicit.resolve() / "config.toml")
            self.assertEqual(integration.server_environment()["CODEX_HOME"], str(explicit.resolve()))

    def test_missing_malformed_relative_or_symlink_manifest_uses_default_without_other_reads(self):
        expected = self.home / ".codex/config.toml"
        self.assertEqual(integration._config_path(), expected)
        for content in ('{"codex_home":"relative/path"}', "[]", "not json", '{"codex_home":42}'):
            self.manifest.write_text(content, "utf-8")
            self.assertEqual(integration._config_path(), expected)
        symlink = Mock()
        symlink.is_file.return_value = True
        symlink.is_symlink.return_value = True
        with patch.object(integration, "MANIFEST", symlink):
            self.assertEqual(integration._config_path(), expected)
            symlink.read_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
