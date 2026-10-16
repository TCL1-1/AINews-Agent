from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from app.tools.registry import ToolContext

MAX_FILE_BYTES = 1_000_000
ALLOWED_COMMANDS = {"git", "python", "python3"}


def _safe_path(workspace: Path, requested: str) -> Path:
    root = workspace.resolve()
    target = (root / requested).resolve()
    if target != root and root not in target.parents:
        raise ValueError("path escapes the agent workspace")
    return target


def list_dir(context: ToolContext, path: str = ".") -> dict[str, Any]:
    target = _safe_path(context.workspace, path)
    if not target.is_dir():
        raise ValueError("path is not a directory")
    entries = []
    for item in sorted(target.iterdir(), key=lambda value: value.name.lower())[:200]:
        entries.append(
            {
                "name": item.name,
                "type": "directory" if item.is_dir() else "file",
                "size": item.stat().st_size if item.is_file() else None,
            }
        )
    return {"path": str(target.relative_to(context.workspace)), "entries": entries}


def read_file(context: ToolContext, path: str, max_chars: int = 20000) -> dict[str, Any]:
    target = _safe_path(context.workspace, path)
    if not target.is_file():
        raise ValueError("path is not a file")
    if target.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("file is too large")
    content = target.read_text(encoding="utf-8")
    return {"path": path, "content": content[:max_chars], "truncated": len(content) > max_chars}


def search_content(context: ToolContext, keyword: str, dir: str = ".") -> dict[str, Any]:
    target = _safe_path(context.workspace, dir)
    if not target.is_dir():
        raise ValueError("dir is not a directory")
    matches: list[dict[str, Any]] = []
    lowered = keyword.lower()
    for file_path in target.rglob("*"):
        should_skip = (
            len(matches) >= 100
            or not file_path.is_file()
            or file_path.stat().st_size > MAX_FILE_BYTES
        )
        if should_skip:
            continue
        try:
            for line_number, line in enumerate(
                file_path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if lowered in line.lower():
                    matches.append(
                        {
                            "file": str(file_path.relative_to(context.workspace)),
                            "line": line_number,
                            "text": line[:500],
                        }
                    )
                    if len(matches) >= 100:
                        break
        except UnicodeDecodeError:
            continue
    return {"keyword": keyword, "matches": matches}


def write_file(context: ToolContext, path: str, content: str) -> dict[str, Any]:
    target = _safe_path(context.workspace, path)
    if target.exists():
        raise ValueError("refusing to overwrite an existing file")
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError("content is too large")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {
        "path": str(target.relative_to(context.workspace)),
        "bytes": target.stat().st_size,
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def bash(context: ToolContext, command: str) -> dict[str, Any]:
    if any(token in command for token in ("|", ">", "<", "&&", ";", "`", "$")):
        raise ValueError("shell operators are not allowed")
    argv = shlex.split(command, posix=False)
    if not argv or Path(argv[0]).name.lower() not in ALLOWED_COMMANDS:
        raise ValueError("command is not on the allowlist")
    executable = Path(argv[0]).name.lower()
    if executable in {"python", "python3"}:
        argv[0] = sys.executable
    elif executable == "git":
        git_path = shutil.which("git")
        if not git_path:
            raise ValueError("git executable is unavailable")
        argv[0] = git_path
    completed = subprocess.run(
        argv,
        cwd=context.workspace,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"},
    )
    return {
        "exit_code": completed.returncode,
        "stdout": completed.stdout[:10000],
        "stderr": completed.stderr[:10000],
    }
