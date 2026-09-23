#!/usr/bin/env python3
"""Merge repository config patches into the corresponding user config files."""

from __future__ import annotations

import argparse
from collections.abc import MutableMapping
from copy import deepcopy
from dataclasses import dataclass
import difflib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from typing import Any, TextIO

import tomlkit
from tomlkit.exceptions import ParseError


@dataclass(frozen=True)
class ConfigSpec:
    """Describe one repository patch and its destination below the user home."""

    source: Path
    target: Path
    kind: str


@dataclass(frozen=True)
class WriteOperation:
    """Hold one fully prepared config write or unchanged result."""

    display_path: Path
    write_path: Path
    content: str
    original_content: str | None
    existed: bool
    changed: bool


@dataclass(frozen=True)
class TransactionEntry:
    """Hold one staged replacement and its same-filesystem recovery data."""

    operation: WriteOperation
    transaction_dir: Path
    staged_path: Path
    staged_identity: tuple[int, int]
    backup_path: Path | None


CONFIG_SPECS = (
    ConfigSpec(Path("claude/settings.json"), Path(".claude/settings.json"), "json"),
    ConfigSpec(Path("codex/config.toml"), Path(".codex/config.toml"), "toml"),
    ConfigSpec(
        Path("codex/rules/agent.rules"),
        Path(".codex/rules/agent.rules"),
        "copy",
    ),
    ConfigSpec(
        Path("gemini/antigravity-cli/settings.json"),
        Path(".gemini/antigravity-cli/settings.json"),
        "json",
    ),
    ConfigSpec(Path("kimi-code/config.toml"), Path(".kimi-code/config.toml"), "toml"),
)


class ConfigSyncError(RuntimeError):
    """Report an actionable validation or filesystem error to the CLI."""


def _color_enabled(stream: TextIO) -> bool:
    if "NO_COLOR" in os.environ:
        return False
    if os.environ.get("CLICOLOR_FORCE") == "1":
        return True
    return stream.isatty() and os.environ.get("TERM", "dumb") != "dumb"


def _colored(text: str, color: str, enabled: bool) -> str:
    return f"\x1b[{color}m{text}\x1b[0m" if enabled else text


def _merge_values(target: Any, patch: Any) -> Any:
    if isinstance(target, dict) and isinstance(patch, dict):
        merged = deepcopy(target)
        for key, patch_value in patch.items():
            if key in merged:
                merged[key] = _merge_values(merged[key], patch_value)
            else:
                merged[key] = deepcopy(patch_value)
        return merged
    if isinstance(target, list) and isinstance(patch, list):
        return deepcopy(patch)
    return deepcopy(patch)


def _config_values_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _config_values_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _config_values_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return left == right


def _load_json(text: str, path: Path) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise ConfigSyncError(f"invalid JSON in {path}: {error}") from error
    if not isinstance(value, dict):
        raise ConfigSyncError(f"expected a JSON object in {path}")
    return value


def _merge_json(
    target_text: str | None,
    patch_text: str,
    *,
    target_path: Path,
    patch_path: Path,
) -> str:
    patch = _load_json(patch_text, patch_path)
    if target_text is None:
        merged = patch
    else:
        target = _load_json(target_text, target_path)
        merged = _merge_values(target, patch)
        if _config_values_equal(merged, target):
            return target_text
    return json.dumps(merged, indent=2, ensure_ascii=False) + "\n"


def _load_toml(text: str, path: Path) -> tomlkit.TOMLDocument:
    try:
        return tomlkit.parse(text)
    except ParseError as error:
        raise ConfigSyncError(f"invalid TOML in {path}: {error}") from error


def _merge_toml_items(target: Any, patch: Any) -> Any:
    if isinstance(target, MutableMapping) and isinstance(patch, MutableMapping):
        for key, patch_value in patch.items():
            if key in target:
                target_value = target[key]
                if isinstance(target_value, MutableMapping) and isinstance(
                    patch_value, MutableMapping
                ):
                    _merge_toml_items(target_value, patch_value)
                else:
                    target[key] = deepcopy(patch_value)
            else:
                target[key] = deepcopy(patch_value)
        return target

    return deepcopy(patch)


def _merge_toml(
    target_text: str | None,
    patch_text: str,
    *,
    target_path: Path,
    patch_path: Path,
) -> str:
    patch = _load_toml(patch_text, patch_path)
    if target_text is None:
        return patch_text

    target = _load_toml(target_text, target_path)
    target_values = target.unwrap()
    expected = _merge_values(target_values, patch.unwrap())
    if _config_values_equal(expected, target_values):
        return target_text

    merged = _merge_toml_items(target, patch)
    if not _config_values_equal(merged.unwrap(), expected):
        raise ConfigSyncError(f"could not apply TOML patch to {target_path}")
    return tomlkit.dumps(merged)


def _resolve_write_path(target: Path) -> tuple[Path, bool]:
    if target.is_symlink():
        try:
            resolved = target.resolve(strict=True)
        except OSError as error:
            raise ConfigSyncError(
                f"cannot follow config symlink {target}: {error}"
            ) from error
        if not resolved.is_file():
            raise ConfigSyncError(f"config symlink does not point to a file: {target}")
        return resolved, True
    if target.exists():
        if not target.is_file():
            raise ConfigSyncError(f"config target is not a file: {target}")
        return target, True
    return target, False


def _prepare_operations(repo_root: Path, home: Path) -> list[WriteOperation]:
    operations: list[WriteOperation] = []
    config_root = repo_root / "config"
    for spec in CONFIG_SPECS:
        source = config_root / spec.source
        if not source.is_file():
            raise ConfigSyncError(f"config patch is missing: {source}")
        try:
            patch_text = source.read_text(encoding="utf-8")
        except OSError as error:
            raise ConfigSyncError(
                f"cannot read config patch {source}: {error}"
            ) from error

        display_path = home / spec.target
        write_path, existed = _resolve_write_path(display_path)
        try:
            target_text = write_path.read_text(encoding="utf-8") if existed else None
        except (OSError, UnicodeError) as error:
            raise ConfigSyncError(
                f"cannot read config target {display_path}: {error}"
            ) from error

        if spec.kind == "json":
            content = _merge_json(
                target_text,
                patch_text,
                target_path=display_path,
                patch_path=source,
            )
        elif spec.kind == "toml":
            content = _merge_toml(
                target_text,
                patch_text,
                target_path=display_path,
                patch_path=source,
            )
        elif spec.kind == "copy":
            content = patch_text
        else:
            raise ConfigSyncError(f"unsupported config type: {spec.kind}")
        operations.append(
            WriteOperation(
                display_path,
                write_path,
                content,
                target_text,
                existed,
                target_text != content,
            )
        )
    return operations


def _file_identity(path: Path) -> tuple[int, int]:
    file_stat = path.stat()
    return file_stat.st_dev, file_stat.st_ino


def _describe_exception(error: BaseException) -> str:
    message = str(error)
    return message if message else type(error).__name__


def _append_exception_detail(error: BaseException, detail: str) -> None:
    if not detail:
        return
    if isinstance(error, ConfigSyncError):
        error.args = (f"{error}; {detail}",)
    else:
        error.add_note(detail)


def _cleanup_transaction_dirs(
    entries: list[TransactionEntry],
    *,
    retained_dirs: set[Path] | None = None,
    catch_interrupts: bool = False,
) -> list[str]:
    retained = retained_dirs or set()
    errors: list[str] = []
    for entry in entries:
        if entry.transaction_dir in retained:
            continue
        try:
            shutil.rmtree(entry.transaction_dir)
        except FileNotFoundError:
            continue
        except OSError as error:
            errors.append(f"{entry.transaction_dir}: {error}")
        except BaseException as error:
            if not catch_interrupts:
                raise
            errors.append(f"{entry.transaction_dir}: {_describe_exception(error)}")
    return errors


def _stage_operation(
    operation: WriteOperation,
    entries: list[TransactionEntry],
) -> None:
    transaction_dir: Path | None = None
    try:
        path = operation.write_path
        path.parent.mkdir(parents=True, exist_ok=True)
        transaction_dir = Path(
            tempfile.mkdtemp(
                prefix=f".{path.name}.sync-configs-transaction-",
                dir=path.parent,
            )
        )
        staged_path = transaction_dir / "new-config"
        with staged_path.open(
            mode="x",
            encoding="utf-8",
            newline="",
        ) as staged_file:
            staged_file.write(operation.content)
            staged_file.flush()
            os.fsync(staged_file.fileno())

        backup_path: Path | None = None
        if operation.existed:
            original_stat = path.stat()
            staged_path.chmod(stat.S_IMODE(original_stat.st_mode))
            backup_path = transaction_dir / "original-config"
            os.link(path, backup_path)
            if _file_identity(path) != _file_identity(backup_path):
                raise ConfigSyncError(
                    f"rollback backup does not match {operation.display_path}"
                )
        else:
            staged_path.chmod(0o600)

        entry = TransactionEntry(
            operation,
            transaction_dir,
            staged_path,
            _file_identity(staged_path),
            backup_path,
        )
        entries.append(entry)
    except BaseException as error:
        cleanup_errors: list[str] = []
        transaction_is_registered = transaction_dir is not None and any(
            entry.transaction_dir == transaction_dir for entry in entries
        )
        if transaction_dir is not None and not transaction_is_registered:
            try:
                shutil.rmtree(transaction_dir)
            except FileNotFoundError:
                pass
            except BaseException as cleanup_error:
                cleanup_errors.append(
                    f"{transaction_dir}: {_describe_exception(cleanup_error)}"
                )
        cleanup_detail = (
            f"cleanup failed for {', '.join(cleanup_errors)}" if cleanup_errors else ""
        )
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            _append_exception_detail(error, cleanup_detail)
            raise
        if isinstance(error, ConfigSyncError):
            _append_exception_detail(error, cleanup_detail)
            raise
        if isinstance(error, (OSError, UnicodeError)):
            detail = f"; {cleanup_detail}" if cleanup_detail else ""
            raise ConfigSyncError(
                f"cannot stage config target {operation.display_path}: {error}{detail}"
            ) from error
        _append_exception_detail(error, cleanup_detail)
        raise


def _rollback_entries(
    entries: list[TransactionEntry],
) -> tuple[list[str], set[Path]]:
    errors: list[str] = []
    retained_dirs: set[Path] = set()
    for entry in reversed(entries):
        operation = entry.operation
        try:
            if operation.existed:
                if entry.backup_path is None or not entry.backup_path.exists():
                    raise OSError("rollback backup is missing")
                backup_identity = _file_identity(entry.backup_path)
                try:
                    target_identity = _file_identity(operation.write_path)
                except FileNotFoundError:
                    target_identity = None
                if target_identity != backup_identity:
                    os.replace(entry.backup_path, operation.write_path)
            else:
                try:
                    target_identity = _file_identity(operation.write_path)
                except FileNotFoundError:
                    target_identity = None
                if target_identity == entry.staged_identity:
                    operation.write_path.unlink()
                elif target_identity is not None:
                    raise OSError(
                        "target changed during synchronization; refusing to remove it"
                    )
        except BaseException as error:
            retained_dirs.add(entry.transaction_dir)
            backup_detail = (
                f"; recovery backup retained at {entry.backup_path}"
                if entry.backup_path is not None and entry.backup_path.exists()
                else f"; transaction directory retained at {entry.transaction_dir}"
            )
            errors.append(
                f"{operation.display_path}: {_describe_exception(error)}{backup_detail}"
            )
    return errors, retained_dirs


def _apply_operations(operations: list[WriteOperation]) -> None:
    entries: list[TransactionEntry] = []
    try:
        for operation in operations:
            if operation.changed:
                _stage_operation(operation, entries)
        for entry in entries:
            try:
                os.replace(entry.staged_path, entry.operation.write_path)
            except OSError as error:
                raise ConfigSyncError(
                    "cannot commit config target "
                    f"{entry.operation.display_path}: {error}"
                ) from error
    except BaseException as error:
        rollback_errors, retained_dirs = _rollback_entries(entries)
        cleanup_errors = _cleanup_transaction_dirs(
            entries,
            retained_dirs=retained_dirs,
            catch_interrupts=True,
        )
        details: list[str] = []
        if rollback_errors:
            details.append(f"rollback failed for {', '.join(rollback_errors)}")
        if cleanup_errors:
            details.append(f"cleanup failed for {', '.join(cleanup_errors)}")
        _append_exception_detail(error, "; ".join(details))
        raise

    try:
        cleanup_errors = _cleanup_transaction_dirs(entries)
    except (KeyboardInterrupt, SystemExit) as error:
        error.add_note(
            "configs were committed; temporary transaction directories may remain"
        )
        raise
    if cleanup_errors:
        raise ConfigSyncError(
            "configs were committed but temporary cleanup failed for "
            f"{', '.join(cleanup_errors)}"
        )


def _preview_operations(operations: list[WriteOperation]) -> None:
    use_color = _color_enabled(sys.stdout)
    changed = [operation for operation in operations if operation.changed]
    if not changed:
        print(_colored("No config changes needed.", "2", use_color))
        return

    for operation in changed:
        action = "update" if operation.existed else "create"
        print(_colored(f"  {action}: {operation.display_path}", "1;36", use_color))
        original_lines = (operation.original_content or "").splitlines(keepends=True)
        proposed_lines = operation.content.splitlines(keepends=True)
        difference = difflib.unified_diff(
            original_lines,
            proposed_lines,
            fromfile=str(operation.display_path) if operation.existed else "/dev/null",
            tofile=str(operation.display_path),
            n=0,
        )
        for line in difference:
            if line.startswith(("--- ", "+++ ")):
                color = "1;36"
            elif line.startswith("@@ "):
                color = "34"
            elif line.startswith("-"):
                color = "31"
            elif line.startswith("+"):
                color = "32"
            else:
                color = ""
            content = line[:-1] if line.endswith("\n") else line
            print(_colored(content, color, use_color) if color else content)
            if not line.endswith("\n"):
                print("\\ No newline at end of file")


def main(arguments: list[str] | None = None) -> int:
    """Preview or atomically update validated user config patches."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--preview",
        action="store_true",
        help="show the proposed config changes without writing files",
    )
    args = parser.parse_args(arguments if arguments is not None else [])
    repo_root = Path(__file__).resolve().parents[1]
    home = Path.home()
    try:
        operations = _prepare_operations(repo_root, home)
        if args.preview:
            _preview_operations(operations)
            return 0
        _apply_operations(operations)
        use_color = _color_enabled(sys.stdout)
        for operation in operations:
            if operation.changed:
                action = "updated" if operation.existed else "created"
            else:
                action = "unchanged"
            color = "32" if operation.changed else "2"
            print(_colored(f"  {action}: {operation.display_path}", color, use_color))
    except ConfigSyncError as error:
        print(
            _colored(
                f"sync-configs: error: {error}", "31", _color_enabled(sys.stderr)
            ),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
