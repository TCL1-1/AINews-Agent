from pathlib import Path

import pytest

from app.tools.filesystem import _safe_path


def test_safe_path_blocks_parent_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes"):
        _safe_path(tmp_path, "../secret.txt")


def test_safe_path_allows_workspace_child(tmp_path: Path) -> None:
    target = _safe_path(tmp_path, "notes/result.txt")
    assert tmp_path.resolve() in target.parents

