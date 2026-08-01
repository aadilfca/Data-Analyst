#!/usr/bin/env python3
"""
PreToolUse guard for Edit/Write: force a permission prompt only when a change looks like a
large deletion/replacement, so trivial edits keep auto-approving under the project's blanket
Edit/Write allow rule, but a sizable chunk of code (or a whole file) being wiped/replaced still
stops for confirmation.

Tunable via env vars (no code edit needed):
  GUARD_LARGE_EDIT_LINE_THRESHOLD  - minimum lines removed before size even matters (default 20)
  GUARD_LARGE_EDIT_SHRINK_RATIO    - ask if new/old line ratio falls below this (default 0.5)
"""
import json
import os
import sys


def line_count(text):
    if not text:
        return 0
    return text.count("\n") + 1


def emit_ask(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def main():
    threshold = int(os.environ.get("GUARD_LARGE_EDIT_LINE_THRESHOLD", "20"))
    ratio = float(os.environ.get("GUARD_LARGE_EDIT_SHRINK_RATIO", "0.5"))

    try:
        data = json.load(sys.stdin)
    except Exception:
        return  # malformed input - don't block on a guard failure

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {}) or {}

    if tool_name == "Edit":
        old_string = tool_input.get("old_string", "")
        new_string = tool_input.get("new_string", "")
        old_lines = line_count(old_string)
        new_lines = line_count(new_string)
        if old_lines >= threshold and new_lines < old_lines * ratio:
            emit_ask(
                f"This edit removes ~{old_lines} lines and replaces them with only "
                f"~{new_lines} - confirm before a large deletion/replacement."
            )
        return

    if tool_name == "Write":
        file_path = tool_input.get("file_path", "")
        new_content = tool_input.get("content", "")
        new_lines = line_count(new_content)
        if file_path and os.path.isfile(file_path):
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    old_lines = sum(1 for _ in f)
            except Exception:
                old_lines = 0
            if old_lines >= threshold and new_lines < old_lines * ratio:
                emit_ask(
                    f"This write would shrink {file_path} from ~{old_lines} lines to only "
                    f"~{new_lines} - confirm before overwriting."
                )
        return


if __name__ == "__main__":
    main()
