"""CLI bundle rename compatibility using real files, without starting processes."""
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import runtime_paths


class CliCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.path_type = type(self.directory)

    def frozen_command(self, platform, *arguments):
        # Keep native filesystem paths while exercising both filename conventions.
        # Patching os.name alone would make pathlib choose a foreign path class.
        with patch.object(runtime_paths, "FROZEN", True), \
             patch.object(runtime_paths.sys, "executable", str(self.directory / "desktop")), \
             patch.object(runtime_paths.os, "name", platform), \
             patch.object(runtime_paths, "Path", self.path_type):
            return runtime_paths.cli_command(*arguments)

    @staticmethod
    def names(platform):
        suffix = ".exe" if platform == "nt" else ""
        return "YomiyasuStudioCLI" + suffix, "StudioYebisuEditorCLI" + suffix

    def test_new_bundle_cli_takes_priority_over_legacy_cli(self):
        for platform in ("nt", "posix"):
            with self.subTest(platform=platform):
                new, legacy = self.names(platform)
                (self.directory / new).write_bytes(b"new executable")
                (self.directory / legacy).write_bytes(b"legacy executable")
                self.assertEqual(self.frozen_command(platform, "mcp", 3),
                                 [str(self.directory / new), "mcp", "3"])

    def test_legacy_bundle_cli_is_used_when_new_cli_is_absent(self):
        for platform in ("nt", "posix"):
            with self.subTest(platform=platform):
                _, legacy = self.names(platform)
                (self.directory / legacy).write_bytes(b"legacy executable")
                self.assertEqual(self.frozen_command(platform, "profiles"),
                                 [str(self.directory / legacy), "profiles"])

    def test_missing_bundle_returns_new_path_without_creating_files(self):
        for platform in ("nt", "posix"):
            with self.subTest(platform=platform):
                new, _ = self.names(platform)
                self.assertEqual(self.frozen_command(platform, "mcp"),
                                 [str(self.directory / new), "mcp"])
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_directory_named_as_new_cli_does_not_hide_legacy_file(self):
        for platform in ("nt", "posix"):
            with self.subTest(platform=platform):
                new, legacy = self.names(platform)
                (self.directory / new).mkdir()
                (self.directory / legacy).write_bytes(b"legacy executable")
                self.assertEqual(self.frozen_command(platform), [str(self.directory / legacy)])

    def test_directory_named_as_legacy_cli_is_not_a_fallback(self):
        for platform in ("nt", "posix"):
            with self.subTest(platform=platform):
                new, legacy = self.names(platform)
                (self.directory / legacy).mkdir()
                self.assertEqual(self.frozen_command(platform), [str(self.directory / new)])

    def test_source_command_keeps_interpreter_resource_root_and_arguments(self):
        interpreter = str(self.directory / "python")
        resources = self.directory / "resources"
        argument = self.directory / "output.json"
        with patch.object(runtime_paths, "FROZEN", False), \
             patch.object(runtime_paths.sys, "executable", interpreter), \
             patch.object(runtime_paths, "RESOURCE_ROOT", resources):
            self.assertEqual(runtime_paths.cli_command("mcp-config", argument, 2),
                             [interpreter, "-B", str(resources / "app.py"), "mcp-config", str(argument), "2"])

    def test_cli_selection_preserves_existing_data_root_and_override(self):
        existing_root = runtime_paths.PERSISTENT_ROOT
        override = str(self.directory / "existing user data")
        with patch.dict(os.environ, {"YEBISU_EDITOR_DATA_DIR": override}):
            for platform in ("nt", "posix"):
                self.frozen_command(platform, "mcp")
                self.assertEqual(runtime_paths.PERSISTENT_ROOT, existing_root)
                self.assertEqual(os.environ["YEBISU_EDITOR_DATA_DIR"], override)
        self.assertEqual(list(self.directory.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
