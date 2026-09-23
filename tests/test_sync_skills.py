"""Integration tests for skill manifest synchronization."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import textwrap
import unittest

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "scripts/sync-skills.py"
REPOSITORY_MANIFEST = REPO_ROOT / "skills.yaml"


class SkillInstallerTests(unittest.TestCase):
    """Verify manifest validation and external command execution behavior."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.manifest = self.root / "skills.yaml"
        self.manifest.write_text(
            textwrap.dedent(
                """\
                schema_version: 1
                scope: global
                agents:
                - claude-code
                - codex
                skills:
                - name: alpha
                  source_type: github
                  source_url: https://github.com/example/one.git
                - name: beta
                  source_type: github
                  source_url: https://github.com/example/one.git
                - name: gamma
                  source_type: well-known
                  source_url: https://skills.example.com
                """
            ),
            encoding="utf-8",
        )

    def run_installer(
        self,
        *arguments: str,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run the installer with an isolated home and manifest."""
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [
                sys.executable,
                str(INSTALLER),
                "--manifest",
                str(self.manifest),
                *arguments,
            ],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def make_fake_toolchain(
        self,
        *,
        failing_source: str | None = None,
    ) -> tuple[Path, Path]:
        """Create a deterministic npx executable for apply-mode tests."""
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir(exist_ok=True)
        log = self.root / "npx.log"
        npx = fake_bin / "npx"
        failure = ""
        if failing_source is not None:
            failure = textwrap.dedent(
                f"""\
                case "$*" in
                  *{failing_source}*) exit 9 ;;
                esac
                """
            )
        npx.write_text(
            "#!/bin/sh\n"
            'printf \'%s\\t\' "$@" >> "$NPX_LOG"\n'
            "printf '\\n' >> \"$NPX_LOG\"\n"
            f"{failure}"
            "exit 0\n",
            encoding="utf-8",
        )
        npx.chmod(0o755)
        return fake_bin, log

    def write_installed_state(
        self,
        *names: str,
        source_overrides: dict[str, str] | None = None,
        missing_links: set[tuple[str, str]] | None = None,
    ) -> None:
        """Create CLI lock entries and readable skill files under the test home."""
        source_overrides = source_overrides or {}
        missing_links = missing_links or set()
        manifest = yaml.safe_load(self.manifest.read_text(encoding="utf-8"))
        by_name = {item["name"]: item for item in manifest["skills"]}
        lock_entries = {}
        for name in names:
            item = by_name[name]
            source_url = item["source_url"]
            if item["source_type"] == "well-known":
                source_url = f"{source_url}/.well-known/skills/{name}/SKILL.md"
            lock_entries[name] = {
                "source": "example/one" if name != "gamma" else "skills.example.com",
                "sourceType": item["source_type"],
                "sourceUrl": source_overrides.get(name, source_url),
                "skillPath": f"skills/{name}/SKILL.md" if name != "gamma" else None,
                "skillFolderHash": "a" * 40,
                "installedAt": "2026-09-22T00:00:00.000Z",
                "updatedAt": "2026-09-22T00:00:00.000Z",
            }
            if item["source_type"] == "well-known":
                lock_entries[name]["sourceBaseUrl"] = "https://skills.example.com"
                lock_entries[name]["wellKnownDigest"] = "sha256:" + "b" * 64
            for agent_dir in (".claude/skills", ".agents/skills"):
                if (agent_dir, name) in missing_links:
                    continue
                skill_file = self.home / agent_dir / name / "SKILL.md"
                skill_file.parent.mkdir(parents=True, exist_ok=True)
                skill_file.write_text(f"---\nname: {name}\n---\n", encoding="utf-8")

        lock_file = self.home / ".agents/.skill-lock.json"
        lock_file.parent.mkdir(exist_ok=True)
        lock_file.write_text(
            json.dumps({"version": 3, "skills": lock_entries}) + "\n",
            encoding="utf-8",
        )

    def test_default_mode_previews_grouped_commands_without_installing(self) -> None:
        """Catch accidental installation when the review-only default is used."""
        result = self.run_installer()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Preview only; pass --apply to install.", result.stdout)
        self.assertEqual(result.stdout.count("npx skills add"), 2)
        self.assertIn("--skill alpha --skill beta", result.stdout)
        self.assertIn("https://skills.example.com --skill gamma", result.stdout)
        self.assertIn("--agent claude-code --agent codex", result.stdout)

    def test_repository_manifest_previews_all_external_skills(self) -> None:
        """Catch a preview that omits or duplicates a declared skill."""
        env = os.environ.copy()
        env["HOME"] = str(self.home)

        result = subprocess.run(
            [sys.executable, str(INSTALLER)],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        command_lines = [
            line for line in result.stdout.splitlines() if line.startswith("npx ")
        ]
        self.assertTrue(REPOSITORY_MANIFEST.is_file())
        manifest = yaml.safe_load(REPOSITORY_MANIFEST.read_text(encoding="utf-8"))
        planned_skills = []
        for line in command_lines:
            command = shlex.split(line)
            self.assertEqual(command[:3], ["npx", "skills", "add"])
            source_url = command[3]
            planned_skills.extend(
                (source_url, command[index + 1])
                for index, argument in enumerate(command[:-1])
                if argument == "--skill"
            )
        self.assertCountEqual(
            planned_skills,
            [(skill["source_url"], skill["name"]) for skill in manifest["skills"]],
        )
        self.assertIn(("https://cli.sentry.dev", "sentry-cli"), planned_skills)
        self.assertIn(
            ("https://github.com/apemost/skills.git", "task-manager"),
            planned_skills,
        )
        self.assertIn(
            ("https://github.com/apemost/skills.git", "use-subagents"),
            planned_skills,
        )
        self.assertNotIn(
            "dispatching-parallel-agents",
            {skill["name"] for skill in manifest["skills"]},
        )
        self.assertFalse(
            any(
                key.startswith("observed_")
                for skill in manifest["skills"]
                for key in skill
            )
        )
        self.assertTrue(
            all(
                set(skill) == {"name", "source_type", "source_url"}
                for skill in manifest["skills"]
            )
        )

    def test_apply_runs_each_group_with_the_declared_cli_and_agents(self) -> None:
        """Catch wrong CLI arguments or omission of a manifest source group."""
        fake_bin, log = self.make_fake_toolchain()
        result = self.run_installer(
            "--apply",
            extra_env={
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "NPX_LOG": str(log),
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [line.rstrip("\t").split("\t") for line in log.read_text().splitlines()]
        self.assertEqual(
            calls,
            [
                [
                    "skills",
                    "add",
                    "https://github.com/example/one.git",
                    "--skill",
                    "alpha",
                    "--skill",
                    "beta",
                    "--global",
                    "--agent",
                    "claude-code",
                    "--agent",
                    "codex",
                    "--yes",
                ],
                [
                    "skills",
                    "add",
                    "https://skills.example.com",
                    "--skill",
                    "gamma",
                    "--global",
                    "--agent",
                    "claude-code",
                    "--agent",
                    "codex",
                    "--yes",
                ],
            ],
        )

    def test_apply_rejects_symlinked_skill_directory(self) -> None:
        """Catch writes through a user-owned skill directory link."""
        linked_source = self.root / "user-skills"
        linked_source.mkdir()
        claude_skills = self.home / ".claude/skills"
        claude_skills.parent.mkdir()
        claude_skills.symlink_to(linked_source, target_is_directory=True)
        fake_bin, log = self.make_fake_toolchain()

        result = self.run_installer(
            "--apply",
            extra_env={
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "NPX_LOG": str(log),
            },
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("skill directory is a symlink", result.stderr)
        self.assertFalse(log.exists())

    def test_apply_rejects_symlinked_user_lock(self) -> None:
        """Catch updates that would write through a user-owned lock link."""
        linked_lock = self.root / "user-lock.json"
        linked_lock.write_text('{"version": 3, "skills": {}}\n', encoding="utf-8")
        lock_target = self.home / ".agents/.skill-lock.json"
        lock_target.parent.mkdir()
        lock_target.symlink_to(linked_lock)
        fake_bin, log = self.make_fake_toolchain()

        result = self.run_installer(
            "--apply",
            extra_env={
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "NPX_LOG": str(log),
            },
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("skill lock is a symlink", result.stderr)
        self.assertFalse(log.exists())

    def test_apply_continues_after_a_group_failure_and_returns_failure(self) -> None:
        """Catch early exit that prevents independent sources from installing."""
        fake_bin, log = self.make_fake_toolchain(
            failing_source="https://github.com/example/one.git"
        )

        result = self.run_installer(
            "--apply",
            extra_env={
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "NPX_LOG": str(log),
            },
        )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(log.read_text().splitlines()), 2)
        self.assertIn("1 of 2 source groups failed", result.stderr)
        self.assertIn("https://github.com/example/one.git", result.stderr)

    def test_duplicate_skill_names_are_rejected_before_preview(self) -> None:
        """Catch ambiguous manifests that install one name from two entries."""
        content = self.manifest.read_text(encoding="utf-8")
        self.manifest.write_text(
            content.replace("- name: gamma", "- name: alpha"),
            encoding="utf-8",
        )

        result = self.run_installer()

        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate skill name: alpha", result.stderr)
        self.assertNotIn("npx ", result.stdout)

    def test_unsupported_agents_are_rejected_before_preview(self) -> None:
        """Catch a typo that would send skills to an unintended target."""
        content = self.manifest.read_text(encoding="utf-8")
        self.manifest.write_text(
            content.replace("- codex", "- unknown-agent"),
            encoding="utf-8",
        )

        result = self.run_installer()

        self.assertEqual(result.returncode, 2)
        self.assertIn("unsupported agent in manifest: unknown-agent", result.stderr)

    def test_apply_reports_a_missing_npx_without_a_traceback(self) -> None:
        """Catch an unhandled process error when npm tooling is unavailable."""
        fake_bin, log = self.make_fake_toolchain()
        (fake_bin / "npx").unlink()

        result = self.run_installer(
            "--apply",
            extra_env={"PATH": str(fake_bin), "NPX_LOG": str(log)},
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot find npx", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_incremental_preview_installs_only_new_skill_from_existing_source(
        self,
    ) -> None:
        """Catch reinstalling an unchanged skill when one neighbor is added."""
        self.write_installed_state("alpha", "gamma")

        result = self.run_installer()

        self.assertEqual(result.returncode, 0, result.stderr)
        commands = [
            line for line in result.stdout.splitlines() if line.startswith("npx ")
        ]
        self.assertEqual(
            commands,
            [
                "npx skills add https://github.com/example/one.git "
                "--skill beta --global --agent claude-code --agent codex --yes"
            ],
        )

    def test_incremental_apply_skips_complete_installations(self) -> None:
        """Catch unnecessary network calls on a repeated installation."""
        self.write_installed_state("alpha", "beta", "gamma")
        fake_bin, log = self.make_fake_toolchain()

        result = self.run_installer(
            "--apply",
            extra_env={"PATH": f"{fake_bin}:{os.environ['PATH']}", "NPX_LOG": str(log)},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No skill changes needed.", result.stdout)
        self.assertFalse(log.exists())

    def test_well_known_lock_uses_source_base_url(self) -> None:
        """Catch re-adding a well-known skill because its lock records the file URL."""
        self.write_installed_state("gamma")

        result = self.run_installer("--update")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Already installed: 1 skill(s).", result.stdout)
        self.assertNotIn("https://skills.example.com --skill gamma", result.stdout)
        self.assertIn("npx skills update gamma --global --yes", result.stdout)

    def test_universal_agents_share_global_skill_directory(self) -> None:
        """Catch reinstalling skills when universal agents lack private links."""
        self.manifest.write_text(
            self.manifest.read_text(encoding="utf-8").replace(
                "- codex\n", "- codex\n- antigravity-cli\n- kimi-code-cli\n"
            ),
            encoding="utf-8",
        )
        self.write_installed_state("alpha", "beta", "gamma")

        result = self.run_installer()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Already installed: 3 skill(s).", result.stdout)
        self.assertIn("No skill changes needed.", result.stdout)
        self.assertNotIn("npx skills add", result.stdout)

        fake_bin, log = self.make_fake_toolchain()
        applied = self.run_installer(
            "--apply",
            extra_env={"PATH": f"{fake_bin}:{os.environ['PATH']}", "NPX_LOG": str(log)},
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertFalse(log.exists())

    def test_incremental_apply_repairs_missing_canonical_entry(self) -> None:
        """Catch a lock-only check that ignores a missing canonical skill."""
        self.write_installed_state(
            "alpha", "beta", "gamma", missing_links={(".agents/skills", "alpha")}
        )
        fake_bin, log = self.make_fake_toolchain()

        result = self.run_installer(
            "--apply",
            extra_env={"PATH": f"{fake_bin}:{os.environ['PATH']}", "NPX_LOG": str(log)},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = log.read_text().splitlines()
        self.assertEqual(len(calls), 1)
        self.assertIn("--skill\talpha\t", calls[0])
        self.assertNotIn("--skill\tbeta\t", calls[0])

    def test_incremental_preview_reinstalls_only_changed_source(self) -> None:
        """Catch ignoring a manifest source change recorded in the user lock."""
        self.write_installed_state(
            "alpha",
            "beta",
            "gamma",
            source_overrides={"alpha": "https://github.com/example/old.git"},
        )

        result = self.run_installer()

        self.assertEqual(result.returncode, 0, result.stderr)
        commands = [
            line for line in result.stdout.splitlines() if line.startswith("npx ")
        ]
        self.assertEqual(len(commands), 1)
        self.assertIn("--skill alpha", commands[0])
        self.assertNotIn("--skill beta", commands[0])

    def test_update_apply_adds_missing_then_checks_only_existing_skills(self) -> None:
        """Catch an update that forgets missing skills or checks untracked names."""
        self.write_installed_state("alpha", "gamma")
        fake_bin, log = self.make_fake_toolchain()

        result = self.run_installer(
            "--update",
            "--apply",
            extra_env={"PATH": f"{fake_bin}:{os.environ['PATH']}", "NPX_LOG": str(log)},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [line.rstrip("\t").split("\t") for line in log.read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            calls[0][:5],
            ["skills", "add", "https://github.com/example/one.git", "--skill", "beta"],
        )
        self.assertEqual(
            calls[1],
            ["skills", "update", "alpha", "gamma", "--global", "--yes"],
        )

    def test_untracked_existing_skill_is_a_conflict(self) -> None:
        """Catch overwriting a user skill when the CLI lock has no owner record."""
        skill_file = self.home / ".claude/skills/alpha/SKILL.md"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_text("user content\n", encoding="utf-8")

        result = self.run_installer()

        self.assertEqual(result.returncode, 2)
        self.assertIn("existing skill alpha is not tracked", result.stderr)
        self.assertNotIn("npx skills add", result.stdout)

    def test_corrupt_lock_fails_without_reinstalling_everything(self) -> None:
        """Catch mass reinstall after silently treating malformed state as empty."""
        lock_file = self.home / ".agents/.skill-lock.json"
        lock_file.parent.mkdir()
        lock_file.write_text("{broken json", encoding="utf-8")

        result = self.run_installer()

        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid skill lock", result.stderr)
        self.assertNotIn("npx skills add", result.stdout)


if __name__ == "__main__":
    unittest.main()
