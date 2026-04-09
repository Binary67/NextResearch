from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .GitWorkspace import GitWorkspaceManager


_IGNORED_NAMES = (
    ".git",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".uv-cache",
    ".uv-python",
)


def rebuild_hidden_eval_sandbox(source_path: Path, sandbox_path: Path) -> None:
    if sandbox_path.exists():
        shutil.rmtree(sandbox_path)
    sandbox_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source_path,
        sandbox_path,
        ignore=shutil.ignore_patterns(*_IGNORED_NAMES),
    )
    subprocess.run(
        ["git", "init", "-q"],
        cwd=sandbox_path,
        check=True,
        capture_output=True,
    )


def prepare_hidden_eval_sandbox(
    *,
    source_path: Path,
    sandbox_path: Path,
    workspace: GitWorkspaceManager,
    patch: bytes,
) -> None:
    rebuild_hidden_eval_sandbox(source_path, sandbox_path)
    workspace.apply_patch(sandbox_path, patch)
