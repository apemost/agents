#!/usr/bin/env python3
"""Preview or synchronize the skills declared by this repository."""

from __future__ import annotations

import argparse
from collections import OrderedDict
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "skills.yaml"
GLOBAL_SKILL_DIRS = {
    "claude-code": Path(".claude/skills"),
    "codex": Path(".agents/skills"),
    "antigravity-cli": Path(".agents/skills"),
    "kimi-code-cli": Path(".agents/skills"),
}


class ManifestError(ValueError):
    """Report invalid skill manifest content."""


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ManifestError(f"cannot read manifest {path}: {error}") from error
    if not isinstance(value, dict):
        raise ManifestError("manifest root must be a mapping")
    return value


def _require_string(mapping: dict[str, Any], key: str, context: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{context}.{key} must be a non-empty string")
    return value


def _validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != 1:
        raise ManifestError("schema_version must be 1")
    if manifest.get("scope") != "global":
        raise ManifestError("scope must be global")
    agents = manifest.get("agents")
    if not isinstance(agents, list) or not agents:
        raise ManifestError("agents must be a non-empty list")
    seen_agents: set[str] = set()
    for agent in agents:
        if not isinstance(agent, str) or not agent:
            raise ManifestError("each agent must be a non-empty string")
        if agent not in GLOBAL_SKILL_DIRS:
            raise ManifestError(f"unsupported agent in manifest: {agent}")
        if agent in seen_agents:
            raise ManifestError(f"duplicate agent: {agent}")
        seen_agents.add(agent)

    skills = manifest.get("skills")
    if not isinstance(skills, list) or not skills:
        raise ManifestError("skills must be a non-empty list")
    seen_names: set[str] = set()
    well_known_groups: dict[str, int] = {}
    for index, skill in enumerate(skills):
        context = f"skills[{index}]"
        if not isinstance(skill, dict):
            raise ManifestError(f"{context} must be a mapping")
        name = _require_string(skill, "name", context)
        source_type = _require_string(skill, "source_type", context)
        source_url = _require_string(skill, "source_url", context)
        if name in seen_names:
            raise ManifestError(f"duplicate skill name: {name}")
        seen_names.add(name)
        if source_type == "well-known":
            well_known_groups[source_url] = well_known_groups.get(source_url, 0) + 1
        elif source_type != "github":
            raise ManifestError(f"unsupported source_type for {name}: {source_type}")

    repeated_well_known = [
        source_url for source_url, count in well_known_groups.items() if count != 1
    ]
    if repeated_well_known:
        raise ManifestError(
            "well-known source URL must identify exactly one skill: "
            f"{repeated_well_known[0]}"
        )


def _lock_path() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        return Path(state_home) / "skills/.skill-lock.json"
    return Path.home() / ".agents/.skill-lock.json"


def _load_lock() -> dict[str, dict[str, Any]]:
    path = _lock_path()
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise ManifestError(f"invalid skill lock {path}: {error}") from error
    try:
        data = json.loads(content)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ManifestError(f"invalid skill lock {path}: {error}") from error
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("version"), int)
        or data["version"] < 3
        or not isinstance(data.get("skills"), dict)
        or any(not isinstance(entry, dict) for entry in data["skills"].values())
    ):
        raise ManifestError(f"invalid skill lock {path}: unsupported structure")
    return data["skills"]


def _is_current(
    skill: dict[str, Any], entry: dict[str, Any] | None, agents: list[str]
) -> bool:
    if entry is None:
        return False
    source_type = skill["source_type"]
    if entry.get("sourceType") != source_type:
        return False
    source_key = "sourceBaseUrl" if source_type == "well-known" else "sourceUrl"
    if entry.get(source_key) != skill["source_url"]:
        return False
    return all(
        (Path.home() / GLOBAL_SKILL_DIRS[agent] / skill["name"] / "SKILL.md").is_file()
        for agent in agents
    )


def _build_add_commands(
    skills: list[dict[str, Any]], agents: list[str]
) -> list[list[str]]:
    groups: OrderedDict[tuple[str, str], list[dict[str, Any]]] = OrderedDict()
    for skill in skills:
        key = (skill["source_url"], skill["source_type"])
        groups.setdefault(key, []).append(skill)

    commands: list[list[str]] = []
    for (source_url, _source_type), skills in groups.items():
        command = ["npx", "skills", "add", source_url]
        for skill in skills:
            command.extend(("--skill", skill["name"]))
        command.append("--global")
        for agent in agents:
            command.extend(("--agent", agent))
        command.append("--yes")
        commands.append(command)
    return commands


def _plan_commands(
    manifest: dict[str, Any], lock: dict[str, dict[str, Any]], *, update: bool
) -> tuple[list[list[str]], int]:
    pending: list[dict[str, Any]] = []
    current: list[str] = []
    agents = manifest["agents"]
    for skill in manifest["skills"]:
        entry = lock.get(skill["name"])
        if entry is None:
            for agent in agents:
                target = Path.home() / GLOBAL_SKILL_DIRS[agent] / skill["name"]
                if target.exists() or target.is_symlink():
                    raise ManifestError(
                        f"existing skill {skill['name']} is not tracked in the user "
                        f"lock: {target}"
                    )
        if _is_current(skill, entry, agents):
            current.append(skill["name"])
        else:
            pending.append(skill)
    commands = _build_add_commands(pending, agents)
    if update and current:
        commands.append(["npx", "skills", "update", *current, "--global", "--yes"])
    return commands, len(current)


def _parse_args(arguments: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview or install all skills declared by the manifest."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="manifest path (default: skills.yaml)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="execute the installation commands; the default only previews them",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="also check installed manifest skills for upstream updates",
    )
    return parser.parse_args(arguments)


def _check_skill_directories(agents: list[str]) -> None:
    home = Path.home()
    for agent in agents:
        relative_path = GLOBAL_SKILL_DIRS.get(agent)
        if relative_path is None:
            raise ManifestError(f"unsupported agent in manifest: {agent}")
        target = home / relative_path
        if target.is_symlink():
            raise ManifestError(f"skill directory is a symlink: {target}")
        if target.exists() and not target.is_dir():
            raise ManifestError(f"skill directory is not a directory: {target}")

    lock_path = _lock_path()
    if lock_path.is_symlink():
        raise ManifestError(f"skill lock is a symlink: {lock_path}")


def main(arguments: list[str] | None = None) -> int:
    """Run the skill synchronization CLI."""
    args = _parse_args(arguments if arguments is not None else sys.argv[1:])
    try:
        manifest = _load_manifest(args.manifest)
        _validate_manifest(manifest)
        _check_skill_directories(manifest["agents"])
        lock = _load_lock()
        commands, current_count = _plan_commands(manifest, lock, update=args.update)
    except (KeyError, ManifestError, TypeError) as error:
        print(f"sync-skills: error: {error}", file=sys.stderr)
        return 2

    if not args.apply:
        print("Preview only; pass --apply to install.")
        print(f"Already installed: {current_count} skill(s).")
        for command in commands:
            print(shlex.join(command))
        if not commands:
            print("No skill changes needed.")
        return 0

    if commands and shutil.which("npx") is None:
        print("sync-skills: error: cannot find npx in PATH", file=sys.stderr)
        return 2

    if not commands:
        print("No skill changes needed.")
        return 0

    failures: list[tuple[str, int]] = []
    for command in commands:
        print(f"+ {shlex.join(command)}", flush=True)
        result = subprocess.run(command, check=False)
        if result.returncode != 0:
            label = command[3] if command[2] == "add" else "update"
            failures.append((label, result.returncode))
    if failures:
        for source, returncode in failures:
            print(
                f"sync-skills: source failed ({returncode}): {source}",
                file=sys.stderr,
            )
        print(
            f"sync-skills: {len(failures)} of {len(commands)} source groups failed",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
