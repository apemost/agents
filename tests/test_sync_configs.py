"""Integration tests for syncing repository config patches into user configs."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync-configs.py"
INSTALL_SCRIPT = REPO_ROOT / "install.sh"
TASK_MANAGER_HOOK = REPO_ROOT / "scripts/hooks/session-start-task-manager.py"
SKILL_TARGET_DIRS = (
    Path(".agents/skills"),
    Path(".claude/skills"),
)


class SyncConfigsTests(unittest.TestCase):
    """Verify config patch behavior through the sync script CLI."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.home = Path(self.temp_dir.name)

    def write_home_file(self, relative_path: str, content: str) -> Path:
        """Write a fixture beneath the isolated home directory."""
        path = self.home / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def run_sync(
        self,
        *arguments: str,
        script: Path = SYNC_SCRIPT,
        cwd: Path = REPO_ROOT,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run the sync script against the isolated home directory."""
        env = os.environ.copy()
        env.pop("NO_COLOR", None)
        env.pop("CLICOLOR_FORCE", None)
        env["HOME"] = str(self.home)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, str(script), *arguments],
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def make_repo_fixture(self, name: str) -> Path:
        """Copy the sync script and config patches into an isolated repository."""
        repo = self.home / name
        shutil.copytree(REPO_ROOT / "config", repo / "config")
        (repo / "scripts").mkdir()
        shutil.copy2(SYNC_SCRIPT, repo / "scripts/sync-configs.py")
        return repo

    def load_sync_module(self):
        """Load the sync script as a module for deterministic fault injection."""
        module_name = f"_sync_configs_test_{id(self)}"
        spec = importlib.util.spec_from_file_location(module_name, SYNC_SCRIPT)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        self.addCleanup(sys.modules.pop, module_name, None)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _is_staged_replacement(path: str | Path) -> bool:
        """Return whether a path contains staged replacement content."""
        name = Path(path).name
        return ".sync-configs-stage-" in name or name == "new-config"

    @staticmethod
    def _is_rollback_backup(path: str | Path) -> bool:
        """Return whether a path contains an original config for rollback."""
        name = Path(path).name
        return ".sync-configs-backup-" in name or name == "original-config"

    def managed_paths(self) -> list[Path]:
        """Return every user config path managed by the sync script."""
        return [
            self.home / ".claude/settings.json",
            self.home / ".codex/config.toml",
            self.home / ".codex/rules/agent.rules",
            self.home / ".gemini/antigravity-cli/settings.json",
            self.home / ".kimi-code/config.toml",
        ]

    def seed_custom_configs(self) -> None:
        """Create fixtures with unmanaged keys and stale managed list items."""
        self.write_home_file(
            ".claude/settings.json",
            json.dumps(
                {
                    "custom": {"keep": True},
                    "effortLevel": "low",
                    "permissions": {"allow": ["Bash(stale-managed:*)"]},
                }
            ),
        )
        self.write_home_file(
            ".codex/config.toml",
            """# Keep this user comment
custom_value = "keep"
model_reasoning_effort = "low"

[features]
custom_feature = true
memories = false
""",
        )
        self.write_home_file(
            ".codex/rules/default.rules",
            """# Keep this custom rule
prefix_rule(pattern=["custom"], decision="allow")
""",
        )
        self.write_home_file(
            ".codex/rules/agent.rules",
            'prefix_rule(pattern=["stale"], decision="allow")\n',
        )
        self.write_home_file(
            ".kimi-code/config.toml",
            """custom_value = "keep"
default_thinking = false

[thinking]
custom_mode = "keep"
mode = "off"

[[permission.rules]]
decision = "allow"
scope = "user"
pattern = "Bash(stale-managed*)"
""",
        )

    def test_sync_applies_patch_ownership_semantics(self) -> None:
        self.seed_custom_configs()

        result = self.run_sync()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(path.is_file() for path in self.managed_paths()))

        claude_patch = json.loads(
            (REPO_ROOT / "config/claude/settings.json").read_text(encoding="utf-8")
        )
        claude = json.loads(
            (self.home / ".claude/settings.json").read_text(encoding="utf-8")
        )
        self.assertEqual(claude["custom"], {"keep": True})
        self.assertEqual(claude["effortLevel"], "low")
        self.assertEqual(
            claude["permissions"]["allow"],
            claude_patch["permissions"]["allow"],
        )
        self.assertEqual(claude["hooks"], claude_patch["hooks"])

        codex_path = self.home / ".codex/config.toml"
        codex_text = codex_path.read_text(encoding="utf-8")
        codex = tomllib.loads(codex_text)
        codex_patch = tomllib.loads(
            (REPO_ROOT / "config/codex/config.toml").read_text(encoding="utf-8")
        )
        self.assertIn("# Keep this user comment", codex_text)
        self.assertEqual(codex["custom_value"], "keep")
        self.assertTrue(codex["features"]["custom_feature"])
        self.assertFalse(codex["features"]["memories"])
        self.assertEqual(codex["model_reasoning_effort"], "low")
        self.assertEqual(
            codex["hooks"]["SessionStart"],
            codex_patch["hooks"]["SessionStart"],
        )

        kimi_patch = tomllib.loads(
            (REPO_ROOT / "config/kimi-code/config.toml").read_text(encoding="utf-8")
        )
        kimi = tomllib.loads(
            (self.home / ".kimi-code/config.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(kimi["custom_value"], "keep")
        self.assertEqual(kimi["thinking"]["custom_mode"], "keep")
        # default_thinking is no longer managed by the patch, so the user's
        # unmanaged value (seeded as false) must be preserved verbatim.
        self.assertFalse(kimi["default_thinking"])
        self.assertEqual(
            kimi["permission"]["rules"],
            kimi_patch["permission"]["rules"],
        )

        default_rules_text = (self.home / ".codex/rules/default.rules").read_text(
            encoding="utf-8"
        )
        self.assertEqual(
            default_rules_text,
            """# Keep this custom rule
prefix_rule(pattern=["custom"], decision="allow")
""",
        )
        self.assertEqual(
            (self.home / ".codex/rules/agent.rules").read_bytes(),
            (REPO_ROOT / "config/codex/rules/agent.rules").read_bytes(),
        )

    def test_sync_replaces_patch_managed_json_lists(self) -> None:
        claude_patch = json.loads(
            (REPO_ROOT / "config/claude/settings.json").read_text(encoding="utf-8")
        )
        claude_target = json.loads(json.dumps(claude_patch))
        claude_target["custom"] = {"keep": True}
        claude_target["sandbox"]["excludedCommands"].append("docker logs *")
        self.write_home_file(
            ".claude/settings.json",
            json.dumps(claude_target),
        )

        result = self.run_sync()

        self.assertEqual(result.returncode, 0, result.stderr)
        claude = json.loads(
            (self.home / ".claude/settings.json").read_text(encoding="utf-8")
        )
        self.assertEqual(claude["custom"], {"keep": True})
        self.assertEqual(
            claude["sandbox"]["excludedCommands"],
            claude_patch["sandbox"]["excludedCommands"],
        )

    def test_sync_replaces_patch_managed_toml_lists(self) -> None:
        kimi_patch = tomllib.loads(
            (REPO_ROOT / "config/kimi-code/config.toml").read_text(encoding="utf-8")
        )
        custom_rule = {
            "decision": "allow",
            "scope": "user",
            "pattern": "Bash(removed-upstream*)",
        }
        kimi_target = (
            'custom_value = "keep"\n\n'
            "[[permission.rules]]\n"
            'decision = "allow"\n'
            'scope = "user"\n'
            'pattern = "Bash(removed-upstream*)"\n'
        )
        self.write_home_file(".kimi-code/config.toml", kimi_target)

        result = self.run_sync()

        self.assertEqual(result.returncode, 0, result.stderr)
        kimi = tomllib.loads(
            (self.home / ".kimi-code/config.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(kimi["custom_value"], "keep")
        self.assertNotIn(custom_rule, kimi["permission"]["rules"])
        self.assertEqual(kimi["permission"]["rules"], kimi_patch["permission"]["rules"])

    def test_preview_shows_removed_user_rule_without_writing_configs(self) -> None:
        """Catch a preview that hides a replaced rule or changes user files."""
        path = self.write_home_file(
            ".claude/settings.json",
            '{"permissions":{"allow":["Bash(custom:*)"]}}\n',
        )
        original = path.read_bytes()

        result = self.run_sync("--preview")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--- ", result.stdout)
        self.assertIn("+++ ", result.stdout)
        self.assertIn("Bash(custom:*)", result.stdout)
        self.assertNotIn("\x1b[", result.stdout)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(any(p.exists() for p in self.managed_paths()[1:]))

    def test_preview_colors_diff_when_forced(self) -> None:
        """Catch an uncolored diff in an interactive installation."""
        self.write_home_file(
            ".claude/settings.json",
            '{"permissions":{"allow":["Bash(custom:*)"]}}\n',
        )

        result = self.run_sync(
            "--preview",
            extra_env={"CLICOLOR_FORCE": "1", "TERM": "xterm"},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"\x1b\[[0-9;]*m--- ")
        self.assertRegex(result.stdout, r"\x1b\[[0-9;]*m\+\+\+ ")
        self.assertRegex(result.stdout, r"\x1b\[[0-9;]*m@@ ")
        self.assertRegex(result.stdout, r"\x1b\[[0-9;]*m-.*Bash\(custom:\*\)")
        self.assertRegex(result.stdout, r"\x1b\[[0-9;]*m\+\s+\"autoMemoryEnabled\"")

    def test_no_color_disables_forced_diff_color(self) -> None:
        """Catch ANSI escapes in a user-requested plain-text preview."""
        result = self.run_sync(
            "--preview",
            extra_env={"CLICOLOR_FORCE": "1", "NO_COLOR": "", "TERM": "xterm"},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("\x1b[", result.stdout)

    def test_second_sync_is_byte_for_byte_idempotent(self) -> None:
        self.seed_custom_configs()
        first = self.run_sync()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(all(path.is_file() for path in self.managed_paths()))
        first_contents = {path: path.read_bytes() for path in self.managed_paths()}
        first_metadata = {
            path: (path.stat().st_ino, path.stat().st_mtime_ns)
            for path in self.managed_paths()
        }

        second = self.run_sync()

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(
            {path: path.read_bytes() for path in self.managed_paths()},
            first_contents,
        )
        self.assertEqual(
            {
                path: (path.stat().st_ino, path.stat().st_mtime_ns)
                for path in self.managed_paths()
            },
            first_metadata,
        )
        self.assertEqual(second.stdout.count("unchanged"), len(self.managed_paths()))

    def test_sync_corrects_json_boolean_stored_as_integer(self) -> None:
        claude_patch = json.loads(
            (REPO_ROOT / "config/claude/settings.json").read_text(encoding="utf-8")
        )
        claude_target = json.loads(json.dumps(claude_patch))
        claude_target["sandbox"]["enabled"] = 1
        claude_path = self.write_home_file(
            ".claude/settings.json",
            json.dumps(claude_target),
        )

        result = self.run_sync()

        self.assertEqual(result.returncode, 0, result.stderr)
        claude = json.loads(claude_path.read_text(encoding="utf-8"))
        self.assertIs(claude["sandbox"]["enabled"], True)
        self.assertIn(f"updated: {claude_path}", result.stdout)

    def test_sync_corrects_toml_boolean_stored_as_integer(self) -> None:
        codex_patch = (REPO_ROOT / "config/codex/config.toml").read_text(
            encoding="utf-8"
        )
        codex_target = codex_patch.replace(
            "check_for_update_on_startup = false",
            "check_for_update_on_startup = 0",
            1,
        )
        codex_path = self.write_home_file(".codex/config.toml", codex_target)

        result = self.run_sync()

        self.assertEqual(result.returncode, 0, result.stderr)
        codex = tomllib.loads(codex_path.read_text(encoding="utf-8"))
        self.assertIs(codex["check_for_update_on_startup"], False)
        self.assertIn(f"updated: {codex_path}", result.stdout)

    def test_invalid_source_patch_reports_source_path(self) -> None:
        cases = (
            (
                "invalid-json-repo",
                Path("claude/settings.json"),
                "{ invalid json\n",
                "invalid JSON",
                self.home / ".claude/settings.json",
            ),
            (
                "invalid-toml-repo",
                Path("codex/config.toml"),
                "invalid toml = [\n",
                "invalid TOML",
                self.home / ".codex/config.toml",
            ),
        )
        for repo_name, relative_source, invalid_text, error_label, target in cases:
            with self.subTest(relative_source=relative_source):
                repo = self.make_repo_fixture(repo_name)
                source = repo / "config" / relative_source
                source.write_text(invalid_text, encoding="utf-8")

                result = self.run_sync(
                    script=repo / "scripts/sync-configs.py",
                    cwd=repo,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"{error_label} in {source.resolve()}", result.stderr)
                self.assertNotIn(f"{error_label} in {target}", result.stderr)

    def test_later_staging_failure_does_not_update_earlier_config(self) -> None:
        claude_path = self.write_home_file(
            ".claude/settings.json",
            '{"effortLevel": "low"}\n',
        )
        original_claude = claude_path.read_bytes()
        self.write_home_file(".codex", "not a directory\n")

        result = self.run_sync()

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(claude_path.read_bytes(), original_claude)
        self.assertNotIn("updated:", result.stdout)
        self.assertNotIn("created:", result.stdout)

    def test_commit_failure_rolls_back_earlier_config(self) -> None:
        self.seed_custom_configs()
        claude_path = self.home / ".claude/settings.json"
        original_claude = claude_path.read_bytes()
        claude_path.chmod(0o640)
        original_stat = claude_path.stat()
        module = self.load_sync_module()
        real_replace = module.os.replace
        commit_calls = 0

        def fail_second_replace(source: Path, target: Path) -> None:
            nonlocal commit_calls
            if self._is_staged_replacement(source):
                commit_calls += 1
                if commit_calls == 2:
                    raise OSError("injected commit failure")
            real_replace(source, target)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(module.Path, "home", return_value=self.home),
            patch.object(module.os, "replace", side_effect=fail_second_replace),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = module.main()

        self.assertEqual(result, 1)
        self.assertEqual(claude_path.read_bytes(), original_claude)
        rolled_back_stat = claude_path.stat()
        self.assertEqual(rolled_back_stat.st_ino, original_stat.st_ino)
        self.assertEqual(rolled_back_stat.st_mtime_ns, original_stat.st_mtime_ns)
        self.assertEqual(rolled_back_stat.st_mode, original_stat.st_mode)
        self.assertEqual(rolled_back_stat.st_uid, original_stat.st_uid)
        self.assertEqual(rolled_back_stat.st_gid, original_stat.st_gid)
        self.assertIn("injected commit failure", stderr.getvalue())
        self.assertNotIn("updated:", stdout.getvalue())
        self.assertFalse(
            [path for path in self.home.rglob("*") if ".sync-configs-" in path.name]
        )

    def test_rollback_failure_retains_recovery_backup(self) -> None:
        self.seed_custom_configs()
        claude_path = self.home / ".claude/settings.json"
        original_claude = claude_path.read_bytes()
        claude_path.chmod(0o640)
        module = self.load_sync_module()
        real_replace = module.os.replace
        commit_calls = 0
        commit_failed = False

        def fail_commit_and_rollback(source: Path, target: Path) -> None:
            nonlocal commit_calls, commit_failed
            if self._is_staged_replacement(source):
                commit_calls += 1
                if commit_calls == 2:
                    commit_failed = True
                    raise OSError("injected commit failure")
            elif commit_failed and self._is_rollback_backup(source):
                raise OSError("injected rollback failure")
            real_replace(source, target)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(module.Path, "home", return_value=self.home),
            patch.object(module.os, "replace", side_effect=fail_commit_and_rollback),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = module.main()

        backups = [
            path
            for path in self.home.rglob("*")
            if path.is_file() and self._is_rollback_backup(path)
        ]
        self.assertEqual(result, 1)
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original_claude)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o640)
        self.assertIn("injected rollback failure", stderr.getvalue())
        self.assertIn(str(backups[0]), stderr.getvalue())
        self.assertNotIn("updated:", stdout.getvalue())

    def test_staging_interrupt_cleans_temporary_directories(self) -> None:
        claude_path = self.write_home_file(
            ".claude/settings.json",
            '{"effortLevel": "low"}\n',
        )
        original_claude = claude_path.read_bytes()
        module = self.load_sync_module()

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(module.Path, "home", return_value=self.home),
            patch.object(module.os, "fsync", side_effect=KeyboardInterrupt),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            self.assertRaises(KeyboardInterrupt),
        ):
            module.main()

        self.assertEqual(claude_path.read_bytes(), original_claude)
        self.assertFalse(
            [path for path in self.home.rglob("*") if ".sync-configs-" in path.name]
        )

    def test_backup_interrupt_leaves_originals_unchanged(self) -> None:
        self.seed_custom_configs()
        claude_path = self.home / ".claude/settings.json"
        codex_path = self.home / ".codex/config.toml"
        originals = {
            path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
            for path in (claude_path, codex_path)
        }
        module = self.load_sync_module()
        real_link = module.os.link
        link_calls = 0

        def interrupt_second_backup(source: Path, target: Path) -> None:
            nonlocal link_calls
            link_calls += 1
            if link_calls == 2:
                raise KeyboardInterrupt
            real_link(source, target)

        with (
            patch.object(module.Path, "home", return_value=self.home),
            patch.object(module.os, "link", side_effect=interrupt_second_backup),
            self.assertRaises(KeyboardInterrupt),
        ):
            module.main()

        for path, (content, inode, mtime_ns) in originals.items():
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(path.stat().st_ino, inode)
            self.assertEqual(path.stat().st_mtime_ns, mtime_ns)
        self.assertFalse(
            [path for path in self.home.rglob("*") if ".sync-configs-" in path.name]
        )

    def test_commit_interrupt_rolls_back_from_temporary_directory(self) -> None:
        self.seed_custom_configs()
        claude_path = self.home / ".claude/settings.json"
        original_content = claude_path.read_bytes()
        original_stat = claude_path.stat()
        module = self.load_sync_module()
        real_replace = module.os.replace
        commit_calls = 0

        def interrupt_second_commit(source: Path, target: Path) -> None:
            nonlocal commit_calls
            if self._is_staged_replacement(source):
                commit_calls += 1
                if commit_calls == 2:
                    raise KeyboardInterrupt
            real_replace(source, target)

        with (
            patch.object(module.Path, "home", return_value=self.home),
            patch.object(module.os, "replace", side_effect=interrupt_second_commit),
            self.assertRaises(KeyboardInterrupt),
        ):
            module.main()

        rolled_back_stat = claude_path.stat()
        self.assertEqual(claude_path.read_bytes(), original_content)
        self.assertEqual(rolled_back_stat.st_ino, original_stat.st_ino)
        self.assertEqual(rolled_back_stat.st_mtime_ns, original_stat.st_mtime_ns)
        self.assertEqual(rolled_back_stat.st_mode, original_stat.st_mode)
        self.assertEqual(rolled_back_stat.st_uid, original_stat.st_uid)
        self.assertEqual(rolled_back_stat.st_gid, original_stat.st_gid)
        self.assertFalse(
            [path for path in self.home.rglob("*") if ".sync-configs-" in path.name]
        )

    def test_staging_fsync_failure_cleans_temporary_files(self) -> None:
        for fail_at in (1, 2):
            with self.subTest(fail_at=fail_at):
                home = self.home / f"fsync-{fail_at}"
                claude_path = home / ".claude/settings.json"
                claude_path.parent.mkdir(parents=True)
                claude_path.write_text('{"effortLevel": "low"}\n', encoding="utf-8")
                module = self.load_sync_module()
                real_fsync = module.os.fsync
                fsync_calls = 0

                def fail_selected_fsync(file_descriptor: int) -> None:
                    nonlocal fsync_calls
                    fsync_calls += 1
                    if fsync_calls == fail_at:
                        raise OSError(f"injected fsync failure {fail_at}")
                    real_fsync(file_descriptor)

                stdout = io.StringIO()
                stderr = io.StringIO()
                with (
                    patch.object(module.Path, "home", return_value=home),
                    patch.object(module.os, "fsync", side_effect=fail_selected_fsync),
                    redirect_stdout(stdout),
                    redirect_stderr(stderr),
                ):
                    result = module.main()

                self.assertEqual(result, 1)
                self.assertIn(
                    f"injected fsync failure {fail_at}",
                    stderr.getvalue(),
                )
                self.assertNotIn("Traceback", stderr.getvalue())
                self.assertFalse(
                    [path for path in home.rglob("*") if ".sync-configs-" in path.name]
                )

    def test_cleanup_failure_uses_cli_error_format(self) -> None:
        self.seed_custom_configs()
        module = self.load_sync_module()

        stdout = io.StringIO()
        stderr = io.StringIO()
        result: int | None = None
        uncaught: OSError | None = None
        with (
            patch.object(module.Path, "home", return_value=self.home),
            patch.object(
                module.shutil,
                "rmtree",
                side_effect=OSError("injected cleanup failure"),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            try:
                result = module.main()
            except OSError as error:
                uncaught = error

        self.assertIsNone(uncaught)
        self.assertEqual(result, 1)
        self.assertIn("configs were committed", stderr.getvalue())
        self.assertIn("injected cleanup failure", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertNotIn("updated:", stdout.getvalue())

    def test_invalid_target_aborts_before_writing_any_config(self) -> None:
        claude_path = self.write_home_file(
            ".claude/settings.json", '{"effortLevel": "low"}\n'
        )
        original_claude = claude_path.read_bytes()
        self.write_home_file(".kimi-code/config.toml", "not valid toml = [\n")

        result = self.run_sync()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(".kimi-code/config.toml", result.stderr)
        self.assertEqual(claude_path.read_bytes(), original_claude)
        self.assertFalse((self.home / ".gemini/antigravity-cli/settings.json").exists())

    def test_legal_inline_table_and_multiline_string_are_preserved(self) -> None:
        codex_path = self.write_home_file(
            ".codex/config.toml",
            '''# Keep this user comment
description = """
[not-a-real-table]
"""
features = { memories = false, custom_feature = true }
''',
        )

        result = self.run_sync()

        self.assertEqual(result.returncode, 0, result.stderr)
        codex_text = codex_path.read_text(encoding="utf-8")
        codex = tomllib.loads(codex_text)
        self.assertIn("# Keep this user comment", codex_text)
        self.assertEqual(codex["description"], "[not-a-real-table]\n")
        self.assertFalse(codex["features"]["memories"])
        self.assertTrue(codex["features"]["custom_feature"])

    def test_agent_rules_are_exactly_managed_without_touching_default_rules(
        self,
    ) -> None:
        default_rules_path = self.write_home_file(
            ".codex/rules/default.rules",
            'prefix_rule(pattern=["user"], decision="prompt")\n',
        )
        original_default_rules = default_rules_path.read_bytes()
        agent_rules_path = self.write_home_file(
            ".codex/rules/agent.rules",
            'prefix_rule(pattern=["removed-upstream"], decision="allow")\n',
        )

        result = self.run_sync()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(default_rules_path.read_bytes(), original_default_rules)
        self.assertEqual(
            agent_rules_path.read_bytes(),
            (REPO_ROOT / "config/codex/rules/agent.rules").read_bytes(),
        )
        self.assertNotIn(
            "removed-upstream", agent_rules_path.read_text(encoding="utf-8")
        )

    def test_filesystem_errors_use_the_cli_error_format(self) -> None:
        self.write_home_file(".claude", "not a directory\n")

        result = self.run_sync()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sync-configs: error:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class InstallScriptTests(unittest.TestCase):
    """Verify isolated installation and config sync behavior."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.home = Path(self.temp_dir.name)
        self.fake_bin = self.home / "fake-bin"
        self.fake_bin.mkdir()
        fake_npx = self.fake_bin / "npx"
        fake_npx.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$*" >> "$NPX_LOG"\n'
            'exit "${FAKE_NPX_EXIT:-0}"\n',
            encoding="utf-8",
        )
        fake_npx.chmod(0o755)
        self.npx_log = self.home / "npx-commands.log"

    def run_install(
        self,
        answer: str,
        *,
        path: str | None = None,
        script: Path = INSTALL_SCRIPT,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run install.sh with isolated user state and scripted input."""
        env = os.environ.copy()
        env.pop("NO_COLOR", None)
        env.pop("CLICOLOR_FORCE", None)
        env["HOME"] = str(self.home)
        env["PATH"] = f"{self.fake_bin}:{path or os.environ['PATH']}"
        env["NPX_LOG"] = str(self.npx_log)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", str(script)],
            cwd=self.home,
            env=env,
            input=answer,
            text=True,
            capture_output=True,
            check=False,
        )

    def installed_symlink_paths(self) -> list[Path]:
        """Return every symlink managed by install.sh."""
        return [
            self.home / ".claude/CLAUDE.md",
            self.home / ".codex/AGENTS.md",
            self.home / ".gemini/GEMINI.md",
            self.home / ".kimi-code/AGENTS.md",
            self.home / ".claude/hooks/session-start-task-manager.py",
            self.home / ".codex/hooks/session-start-task-manager.py",
        ]

    def test_installs_client_specific_task_manager_hook_links(self) -> None:
        result = self.run_install("n\n")

        claude_hook = self.home / ".claude/hooks/session-start-task-manager.py"
        codex_hook = self.home / ".codex/hooks/session-start-task-manager.py"
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(claude_hook.is_symlink())
        self.assertEqual(claude_hook.readlink(), TASK_MANAGER_HOOK)
        self.assertTrue(codex_hook.is_symlink())
        self.assertEqual(codex_hook.readlink(), TASK_MANAGER_HOOK)

    def test_existing_instruction_file_is_preserved_when_overwrite_declined(self) -> None:
        """Catch silent replacement when the default answer is no."""
        target = self.home / ".claude/CLAUDE.md"
        target.parent.mkdir(parents=True)
        target.write_text("user-owned\n", encoding="utf-8")

        result = self.run_install("\nn\nn\n")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "user-owned\n")
        self.assertTrue((self.home / ".codex/AGENTS.md").is_symlink())

    def test_relative_link_to_repository_is_unchanged_without_prompt(self) -> None:
        """Catch prompting for a link that already resolves to this repository."""
        target = self.home / ".claude/CLAUDE.md"
        target.parent.mkdir(parents=True)
        relative_source = os.path.relpath(
            REPO_ROOT / "AGENTS.md", target.parent.resolve()
        )
        target.symlink_to(relative_source)
        self.assertEqual(target.resolve(), REPO_ROOT / "AGENTS.md")

        result = self.run_install("n\nn\n")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.readlink(target), relative_source)
        self.assertNotIn("Overwrite existing", result.stdout)
        self.assertIn("unchanged link", result.stdout)

    def test_accepted_instruction_replacement_keeps_recoverable_original(self) -> None:
        """Catch accepted replacement that destroys an existing file."""
        target = self.home / ".claude/CLAUDE.md"
        target.parent.mkdir(parents=True)
        target.write_text("user-owned\n", encoding="utf-8")

        result = self.run_install("y\nn\nn\n")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(target.is_symlink())
        self.assertEqual(target.readlink(), REPO_ROOT / "AGENTS.md")
        backups = list(target.parent.glob("CLAUDE.md.backup.*/original"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "user-owned\n")
        self.assertIn(str(backups[0]), result.stdout)

    def test_accepted_directory_replacement_keeps_directory_contents(self) -> None:
        """Catch ln placing a link inside an existing target directory."""
        target = self.home / ".claude/CLAUDE.md"
        target.mkdir(parents=True)
        (target / "note.txt").write_text("keep\n", encoding="utf-8")

        result = self.run_install("y\nn\nn\n")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(target.is_symlink())
        self.assertEqual(target.readlink(), REPO_ROOT / "AGENTS.md")
        backups = list(target.parent.glob("CLAUDE.md.backup.*/original/note.txt"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "keep\n")

    def test_prepares_normal_skill_directories_for_installer(self) -> None:
        """Create only the directories used by the CLI installer."""
        result = self.run_install("n\n")

        self.assertEqual(result.returncode, 0, result.stderr)
        for relative_dir in SKILL_TARGET_DIRS:
            target_dir = self.home / relative_dir
            self.assertTrue(target_dir.is_dir())
            self.assertFalse(target_dir.is_symlink())
            self.assertEqual(list(target_dir.iterdir()), [])

        self.assertFalse((self.home / ".agents/.skill-lock.json").exists())

    def test_install_ignores_universal_agent_private_skill_paths(self) -> None:
        """Keep private skill paths untouched when agents use the shared directory."""
        private_paths = (
            self.home / ".codex/skills",
            self.home / ".gemini/antigravity-cli/skills",
        )
        for target in private_paths:
            source = self.home / f"external-{target.parent.name}-skills"
            source.mkdir()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(source, target_is_directory=True)

        result = self.run_install("n\nn\n")

        self.assertEqual(result.returncode, 0, result.stderr)
        for target in private_paths:
            self.assertTrue(target.is_symlink())
            self.assertTrue(target.readlink().is_dir())

    def test_install_refuses_existing_shared_skill_directory_link(self) -> None:
        """Do not convert any existing shared skill directory link into a directory."""
        target = self.home / ".agents/skills"
        target.parent.mkdir(parents=True)
        source = self.home / "external-skills"
        source.mkdir()
        target.symlink_to(source, target_is_directory=True)

        result = self.run_install("n\nn\n")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("skill directory is a symlink", result.stderr)
        self.assertTrue(target.is_symlink())
        self.assertEqual(target.readlink(), source)
        self.assertFalse((self.home / ".claude/CLAUDE.md").exists())

    def test_install_runs_manifest_skill_add_after_preparing_directories(self) -> None:
        """Catch omission of the manifest installer from the top-level script."""
        result = self.run_install("n\ny\n", path="/usr/bin:/bin")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.npx_log.is_file())
        calls = self.npx_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(calls), 20)
        self.assertTrue(all(call.startswith("skills add ") for call in calls))
        self.assertTrue((self.home / ".agents/skills").is_dir())

    def test_install_adds_missing_skills_before_updating_existing_ones(self) -> None:
        """Catch the wrong integration flags or updating before missing installs."""
        skill = self.home / ".agents/skills/agent-browser/SKILL.md"
        for directory in SKILL_TARGET_DIRS:
            target = self.home / directory / "agent-browser/SKILL.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("---\nname: agent-browser\n---\n", encoding="utf-8")
        lock = self.home / ".agents/.skill-lock.json"
        lock.write_text(
            json.dumps(
                {
                    "version": 3,
                    "skills": {
                        "agent-browser": {
                            "source": "vercel-labs/agent-browser",
                            "sourceType": "github",
                            "sourceUrl": "https://github.com/vercel-labs/agent-browser.git",
                            "skillPath": "skills/agent-browser/SKILL.md",
                            "skillFolderHash": "a" * 40,
                            "installedAt": "2026-09-22T00:00:00.000Z",
                            "updatedAt": "2026-09-22T00:00:00.000Z",
                        }
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        result = self.run_install("n\ny\n", path="/usr/bin:/bin")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.npx_log.is_file())
        calls = self.npx_log.read_text(encoding="utf-8").splitlines()
        self.assertTrue(calls[0].startswith("skills add "))
        self.assertEqual(calls[-1], "skills update agent-browser --global --yes")
        self.assertFalse(any("--skill agent-browser" in call for call in calls[:-1]))
        self.assertTrue(skill.is_file())

    def test_install_propagates_skill_failure(self) -> None:
        """Catch a top-level success result after source installation fails."""
        result = self.run_install(
            "n\ny\n",
            path="/usr/bin:/bin",
            extra_env={"FAKE_NPX_EXIT": "9"},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source groups failed", result.stderr)
        self.assertTrue(self.npx_log.is_file())

    def test_refuses_untracked_existing_skill_without_deleting_it(self) -> None:
        """Catch overwrite of a user-owned skill without lock provenance."""
        existing_skill = self.home / ".agents/skills/task-manager"
        existing_skill.mkdir(parents=True)
        marker = existing_skill / "owner.txt"
        marker.write_text("user-owned\n", encoding="utf-8")

        result = self.run_install("n\ny\n")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(marker.read_text(encoding="utf-8"), "user-owned\n")
        self.assertFalse((existing_skill / "task-manager").exists())

    def test_yes_runs_config_sync_from_outside_the_repository(self) -> None:
        result = self.run_install("y\ny\n")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / ".claude/settings.json").is_file())
        self.assertIn("Syncing agent configs", result.stdout)

    def test_config_changes_are_previewed_before_decline(self) -> None:
        """Catch a prompt that asks for consent without showing the diff."""
        target = self.home / ".claude/settings.json"
        target.parent.mkdir(parents=True)
        target.write_text('{"permissions":{"allow":["Bash(custom:*)"]}}\n')
        original = target.read_bytes()

        result = self.run_install("y\nn\nn\n")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Bash(custom:*)", result.stdout)
        self.assertIn("--- ", result.stdout)
        self.assertIn("+++ ", result.stdout)
        self.assertEqual(target.read_bytes(), original)

    def test_install_colors_status_and_diff_when_forced(self) -> None:
        """Catch a colored diff that is lost when invoked through install.sh."""
        target = self.home / ".claude/settings.json"
        target.parent.mkdir(parents=True)
        target.write_text('{"permissions":{"allow":["Bash(custom:*)"]}}\n')
        original = target.read_bytes()

        result = self.run_install(
            "y\nn\nn\n",
            extra_env={"CLICOLOR_FORCE": "1", "TERM": "xterm"},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"\x1b\[[0-9;]*mPreparing skill directories")
        self.assertRegex(result.stdout, r"\x1b\[[0-9;]*m--- ")
        self.assertRegex(result.stdout, r"\x1b\[[0-9;]*m-.*Bash\(custom:\*\)")
        self.assertEqual(target.read_bytes(), original)

    def test_install_respects_no_color_when_forced(self) -> None:
        """Catch ANSI escapes when NO_COLOR is present."""
        result = self.run_install(
            "n\nn\n",
            extra_env={"CLICOLOR_FORCE": "1", "NO_COLOR": "", "TERM": "xterm"},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("\x1b[", result.stdout)

    def test_no_skips_config_sync(self) -> None:
        result = self.run_install("n\n")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / ".claude/settings.json").exists())
        self.assertIn("Skipped agent config sync", result.stdout)

    def test_empty_answer_skips_skill_installation(self) -> None:
        """Catch running network installation without an explicit yes."""
        result = self.run_install("n\n\n", path="/usr/bin:/bin")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Skipped skill installation.", result.stdout)
        self.assertFalse(self.npx_log.exists())

    def test_eof_skips_config_sync_without_failing_install(self) -> None:
        result = self.run_install("")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / ".claude/settings.json").exists())
        self.assertIn("Skipped agent config sync", result.stdout)

    def test_yes_uses_project_python_when_system_python_is_too_old(self) -> None:
        result = self.run_install("yes\nyes\n", path="/usr/bin:/bin")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / ".claude/settings.json").is_file())

    def test_skill_install_requires_a_project_python_environment(self) -> None:
        """Catch an obscure failure when neither uv nor the project venv exists."""
        fixture_repo = self.home / "without-env"
        fixture_repo.mkdir()
        fixture_install = fixture_repo / "install.sh"
        shutil.copy2(INSTALL_SCRIPT, fixture_install)

        result = self.run_install(
            "n\ny\n", path="/usr/bin:/bin", script=fixture_install
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unable to run scripts/install-skills.py", result.stderr)
        self.assertFalse(self.npx_log.exists())

    def test_yes_prefers_uv_over_a_stale_project_python(self) -> None:
        fixture_repo = self.home / "stale-repo"
        fixture_repo.mkdir()
        fixture_install = fixture_repo / "install.sh"
        shutil.copy2(INSTALL_SCRIPT, fixture_install)

        stale_python = fixture_repo / ".venv/bin/python"
        stale_python.parent.mkdir(parents=True)
        stale_python.write_text(
            "#!/usr/bin/env bash\n"
            'printf called > "$HOME/stale-python-called"\n'
            "exit 42\n",
            encoding="utf-8",
        )
        stale_python.chmod(0o755)

        fake_uv = self.fake_bin / "uv"
        fake_uv.write_text(
            '#!/usr/bin/env bash\nprintf "%s\\n" "$@" >> "$HOME/uv-args"\n',
            encoding="utf-8",
        )
        fake_uv.chmod(0o755)

        result = self.run_install(
            "yes\nyes\nyes\n",
            path="/usr/bin:/bin",
            script=fixture_install,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / "stale-python-called").exists())
        self.assertEqual(
            (self.home / "uv-args").read_text(encoding="utf-8").splitlines(),
            [
                "run",
                "--locked",
                "--project",
                str(fixture_repo),
                str(fixture_repo / "scripts/sync-configs.py"),
                "--preview",
                "run",
                "--locked",
                "--project",
                str(fixture_repo),
                str(fixture_repo / "scripts/sync-configs.py"),
                "run",
                "--locked",
                "--project",
                str(fixture_repo),
                str(fixture_repo / "scripts/install-skills.py"),
                "--update",
                "--apply",
            ],
        )

    def test_repeated_install_does_not_recreate_links_or_configs(self) -> None:
        first = self.run_install("yes\nyes\n")
        self.assertEqual(first.returncode, 0, first.stderr)
        links = self.installed_symlink_paths()
        configs = [
            self.home / ".claude/settings.json",
            self.home / ".codex/config.toml",
            self.home / ".codex/rules/agent.rules",
            self.home / ".gemini/antigravity-cli/settings.json",
            self.home / ".kimi-code/config.toml",
        ]
        first_link_metadata = {
            path: (path.lstat().st_ino, path.lstat().st_mtime_ns) for path in links
        }
        first_config_metadata = {
            path: (path.stat().st_ino, path.stat().st_mtime_ns) for path in configs
        }

        second = self.run_install("yes\nyes\n")

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(
            {path: (path.lstat().st_ino, path.lstat().st_mtime_ns) for path in links},
            first_link_metadata,
        )
        self.assertEqual(
            {path: (path.stat().st_ino, path.stat().st_mtime_ns) for path in configs},
            first_config_metadata,
        )
        self.assertEqual(second.stdout.count("unchanged link"), len(links))


if __name__ == "__main__":
    unittest.main()
