from __future__ import annotations

import os
import shutil
from pathlib import Path

from src.Agents.Codex.SessionLog import CodexSessionLog
from src.EditPolicy import EditPolicy

from .GitWorkspace import GitWorkspaceManager
from .Models import ExperimentOrchestratorError


def cleanup_experiment_workspaces(
    workspace: GitWorkspaceManager,
    orchestrator_worktree_path: Path,
    agent_worktree_path: Path,
    branch_name: str,
    preserve_branch: bool = False,
    extra_paths: tuple[Path, ...] = (),
) -> None:
    try:
        workspace.remove_worktree(agent_worktree_path)
    finally:
        try:
            workspace.remove_worktree(orchestrator_worktree_path)
        finally:
            try:
                for path in extra_paths:
                    if path.exists():
                        shutil.rmtree(path)
            finally:
                if not preserve_branch:
                    workspace.delete_branch(branch_name)


def print_edit_policy(edit_policy: EditPolicy) -> None:
    print(f"Codex writable scope repo_root={edit_policy.repo_root}")
    print(f"Codex editable_paths={edit_policy.writable_scope_summary()}")


def build_shared_target_environment(cache_root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    for key in (
        "VIRTUAL_ENV",
        "PYTHONHOME",
        "PYTHONPATH",
        "CONDA_PREFIX",
        "UV_PROJECT_ENVIRONMENT",
        "UV_CACHE_DIR",
        "UV_PYTHON",
        "UV_PYTHON_INSTALL_DIR",
        "UV_MANAGED_PYTHON",
        "UV_NO_MANAGED_PYTHON",
    ):
        environment.pop(key, None)

    uv_cache_dir = cache_root / "uv"
    uv_cache_dir.mkdir(parents=True, exist_ok=True)
    environment["UV_CACHE_DIR"] = str(uv_cache_dir)
    return environment


def build_agent_target_environment(runtime_root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    for key in (
        "VIRTUAL_ENV",
        "PYTHONHOME",
        "PYTHONPATH",
        "CONDA_PREFIX",
        "UV_PROJECT_ENVIRONMENT",
        "UV_CACHE_DIR",
        "UV_PYTHON",
        "UV_PYTHON_INSTALL_DIR",
        "UV_MANAGED_PYTHON",
        "UV_NO_MANAGED_PYTHON",
    ):
        environment.pop(key, None)

    runtime_root.mkdir(parents=True, exist_ok=True)
    project_environment_dir = runtime_root / "project-env"
    uv_cache_dir = runtime_root / "uv-cache"
    uv_python_install_dir = runtime_root / "uv-python"
    uv_cache_dir.mkdir(parents=True, exist_ok=True)
    uv_python_install_dir.mkdir(parents=True, exist_ok=True)
    environment["UV_PROJECT_ENVIRONMENT"] = str(project_environment_dir)
    environment["UV_CACHE_DIR"] = str(uv_cache_dir)
    environment["UV_PYTHON_INSTALL_DIR"] = str(uv_python_install_dir)
    environment["UV_MANAGED_PYTHON"] = "1"
    environment["VIRTUAL_ENV"] = str(project_environment_dir)
    existing_path = environment.get("PATH", "")
    scripts_dir = _project_environment_scripts_dir(project_environment_dir)
    environment["PATH"] = (
        f"{scripts_dir}{os.pathsep}{existing_path}" if existing_path else str(scripts_dir)
    )
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
    target_relative_path: Path,
    editable_paths: tuple[str, ...] = (),
) -> EditPolicy:
    return EditPolicy.from_paths(
        worktree_path,
        session_cwd=session_cwd,
        editable_paths=editable_paths,
        blocked_write_paths=(),
    )


def excluded_candidate_patch_paths(target_relative_path: Path) -> tuple[str, ...]:
    return candidate_runtime_artifact_paths(target_relative_path)


def candidate_runtime_artifact_paths(target_relative_path: Path) -> tuple[str, ...]:
    return tuple(
        _target_scoped_path(target_relative_path, Path(relative_path))
        for relative_path in runtime_generated_candidate_paths()
    )


def runtime_generated_candidate_paths() -> tuple[str, ...]:
    return (
        "model.pkl",
    )


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


def _target_scoped_path(target_relative_path: Path, relative_path: Path) -> str:
    target_prefix = target_relative_path.as_posix().strip("/")
    scoped_path = relative_path.as_posix().strip("/")
    if not target_prefix or target_prefix == ".":
        return scoped_path
    if not scoped_path:
        return target_prefix
    return f"{target_prefix}/{scoped_path}"


def _project_environment_scripts_dir(project_environment_dir: Path) -> Path:
    if os.name == "nt":
        return project_environment_dir / "Scripts"
    return project_environment_dir / "bin"
