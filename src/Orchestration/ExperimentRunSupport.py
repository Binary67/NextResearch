from __future__ import annotations

import os
import shutil
from pathlib import Path

from src.Agents.Codex.SessionLog import CodexSessionLog
from src.EditPolicy import EditPolicy

from .GitWorkspace import GitWorkspaceManager
from .Models import ExperimentOrchestratorError


def write_run_docs(docs_dir: Path, documents: dict[str, str]) -> None:
    docs_dir.mkdir(parents=True, exist_ok=True)
    for name, content in documents.items():
        (docs_dir / name).write_text(content, encoding="utf-8")


def remove_run_docs(docs_dir: Path) -> None:
    if docs_dir.exists():
        shutil.rmtree(docs_dir)


def cleanup_experiment_workspaces(
    workspace: GitWorkspaceManager,
    orchestrator_worktree_path: Path,
    agent_worktree_path: Path,
    branch_name: str,
) -> None:
    try:
        workspace.remove_worktree(agent_worktree_path)
    finally:
        try:
            workspace.remove_worktree(orchestrator_worktree_path)
        finally:
            workspace.delete_branch(branch_name)


def print_edit_policy(edit_policy: EditPolicy) -> None:
    editable_text = ", ".join(edit_policy.editable_rule_paths()) or "all repo paths"
    non_editable_text = ", ".join(edit_policy.non_editable_rule_paths()) or "none"
    non_readable_text = ", ".join(edit_policy.non_readable_rule_paths()) or "none"
    print(f"Codex edit policy repo_root={edit_policy.repo_root}")
    print(f"Codex edit policy mode={edit_policy.mode_label}")
    print(f"Codex editable_paths={editable_text}")
    print(f"Codex non_editable_paths={non_editable_text}")
    print(f"Codex non_readable_paths={non_readable_text}")


def build_target_environment(cache_root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    for key in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH", "CONDA_PREFIX"):
        environment.pop(key, None)

    uv_cache_dir = cache_root / "uv"
    uv_cache_dir.mkdir(parents=True, exist_ok=True)
    environment["UV_CACHE_DIR"] = str(uv_cache_dir)
    return environment


def append_post_run_review(
    session_log: CodexSessionLog,
    workspace: GitWorkspaceManager,
    worktree_path: Path,
    session_log_path: Path,
    app_server_file_changes: int,
) -> None:
    if not worktree_path.exists():
        return

    workspace.run_git(worktree_path, "add", "-A")
    changed_paths = workspace.git_output_bytes(worktree_path, "diff", "--cached", "--name-only", "-z", "HEAD")
    git_tracked_changes = len([entry for entry in changed_paths.split(b"\0") if entry])
    text_paths = _staged_text_paths_for_log(workspace, worktree_path)
    git_diff = workspace.git_output(worktree_path, "diff", "--cached", "HEAD", "--", *text_paths) if text_paths else ""
    session_log.append_post_run_review(
        session_log_path,
        app_server_file_changes=app_server_file_changes,
        git_tracked_changes=git_tracked_changes,
        git_diff=git_diff,
    )


def build_edit_policy(
    worktree_path: Path,
    session_cwd: Path,
    editable_paths: tuple[str, ...],
    non_editable_paths: tuple[str, ...],
    non_readable_paths: tuple[str, ...],
) -> EditPolicy:
    return EditPolicy.from_paths(
        worktree_path,
        session_cwd=session_cwd,
        editable_paths=editable_paths,
        non_editable_paths=non_editable_paths,
        non_readable_paths=non_readable_paths,
    )


def build_agent_sparse_patterns(
    workspace: GitWorkspaceManager,
    orchestrator_worktree_path: Path,
    edit_policy: EditPolicy,
    target_relative_path: Path,
) -> list[str]:
    patterns = [
        path
        for path in workspace.list_tracked_paths(orchestrator_worktree_path)
        if edit_policy.evaluate_read_path(orchestrator_worktree_path / path).allowed
    ]
    docs_pattern = _docs_sparse_pattern(target_relative_path)
    if docs_pattern not in patterns:
        patterns.append(docs_pattern)
    return patterns


def blocked_commands_for_run(evaluation_command: str, non_readable_paths: tuple[str, ...]) -> tuple[str, ...]:
    blocked_commands: list[str] = [evaluation_command]
    for path in non_readable_paths:
        stripped = path.strip()
        if not stripped:
            continue
        blocked_commands.append(stripped)
        name = Path(stripped).name
        if name and name != stripped:
            blocked_commands.append(name)
    return tuple(dict.fromkeys(blocked_commands))


def build_effective_non_readable_paths(
    target_relative_path: Path,
    evaluation_relative_path: Path,
    non_readable_paths: tuple[str, ...],
) -> tuple[str, ...]:
    hidden_paths = list(non_readable_paths)
    evaluation_repo_relative_path = _target_scoped_path(target_relative_path, evaluation_relative_path)
    if evaluation_repo_relative_path not in hidden_paths:
        hidden_paths.append(evaluation_repo_relative_path)
    return tuple(hidden_paths)


def docs_excluded_patch_paths(target_relative_path: Path) -> tuple[str, ...]:
    return (_target_scoped_path(target_relative_path, Path(".nextresearch")),)


def _staged_text_paths_for_log(
    workspace: GitWorkspaceManager,
    worktree_path: Path,
) -> list[str]:
    numstat_output = workspace.git_output_bytes(
        worktree_path,
        "diff",
        "--cached",
        "--numstat",
        "--no-renames",
        "-z",
        "HEAD",
    )
    text_paths: list[str] = []
    seen_paths: set[str] = set()

    for entry in numstat_output.split(b"\0"):
        if not entry:
            continue
        fields = entry.split(b"\t", 2)
        if len(fields) != 3:
            raise ExperimentOrchestratorError("Unexpected git numstat output while building session log.")

        added, deleted, raw_path = fields
        if added == b"-" and deleted == b"-":
            continue

        path = raw_path.decode("utf-8", errors="replace")
        if path in seen_paths:
            continue
        seen_paths.add(path)
        text_paths.append(path)

    return text_paths


def _docs_sparse_pattern(target_relative_path: Path) -> str:
    target_prefix = target_relative_path.as_posix().strip("/")
    if not target_prefix or target_prefix == ".":
        return ".nextresearch/"
    return f"{target_prefix}/.nextresearch/"


def _target_scoped_path(target_relative_path: Path, relative_path: Path) -> str:
    target_prefix = target_relative_path.as_posix().strip("/")
    scoped_path = relative_path.as_posix().strip("/")
    if not target_prefix or target_prefix == ".":
        return scoped_path
    if not scoped_path:
        return target_prefix
    return f"{target_prefix}/{scoped_path}"
