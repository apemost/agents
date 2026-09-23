#!/usr/bin/env python3
"""Inject task-manager recovery guidance after context compaction."""

from __future__ import annotations

import json
import sys
from typing import Any


ADDITIONAL_CONTEXT = """
Before continuing, invoke the task-manager skill, read its SKILL.md completely,
and recover or refresh the authoritative task file.
""".strip("\n")


def load_input() -> dict[str, Any]:
    """Read and validate the hook event payload from standard input."""
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        raise ValueError("hook input must be a JSON object")
    return payload


def main() -> int:
    """Emit shared SessionStart context only for compaction events."""
    try:
        payload = load_input()
    except (json.JSONDecodeError, ValueError) as error:
        print(
            f"session-start-task-manager: invalid hook input: {error}", file=sys.stderr
        )
        return 1

    if (
        payload.get("hook_event_name") != "SessionStart"
        or payload.get("source") != "compact"
    ):
        return 0

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": ADDITIONAL_CONTEXT,
        }
    }
    print(json.dumps(output, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
