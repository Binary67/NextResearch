from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from src.Agents.Codex import CodexSessionRunResult, CodexSessionRunner

from .ExperimentPrompts import (
    build_evaluation_spec_prompt,
    build_running_instructions_prompt,
    normalize_document_text,
)
from .GitWorkspace import GitWorkspaceManager
from .Models import BootstrapArtifacts, ExperimentOrchestratorError


def bootstrap_artifacts(
    *,
    target_path: Path,
    target_relative_path: Path,
    objective_slug: str,
    evaluation_command: str,
    evaluation_file_path: str | Path | None,
    bootstrap_id: str,
    bootstrap_ref: str,
    workspace: GitWorkspaceManager,
    worktrees_root: Path,
    codex_session_runner: CodexSessionRunner,
    environment: Mapping[str, str],
) -> BootstrapArtifacts:
    evaluation_relative_path = _resolve_evaluation_relative_path(
        target_path,
        evaluation_command,
        evaluation_file_path,
    )
    worktree_path = worktrees_root / objective_slug / bootstrap_id
    running_result: CodexSessionRunResult | None = None
    evaluation_result: CodexSessionRunResult | None = None
    running_log_path: Path | None = None
    evaluation_log_path: Path | None = None

    workspace.create_detached_worktree(worktree_path, bootstrap_ref)
    target_cwd = worktree_path / target_relative_path

    try:
        running_result = codex_session_runner.run(
            target_cwd,
            build_running_instructions_prompt(),
            environment=environment,
        )
        running_log_path = running_result.session_log_path

        evaluation_result = codex_session_runner.run(
            target_cwd,
            build_evaluation_spec_prompt(
                evaluation_command=evaluation_command,
                evaluation_relative_path=evaluation_relative_path,
            ),
            environment=environment,
        )
        evaluation_log_path = evaluation_result.session_log_path
    finally:
        workspace.remove_worktree(worktree_path)

    if running_result is None or evaluation_result is None:
        raise ExperimentOrchestratorError("Bootstrap sessions did not complete successfully.")

    return BootstrapArtifacts(
        running_instructions=normalize_document_text(running_result.turn_result.response_text),
        evaluation_spec=normalize_document_text(evaluation_result.turn_result.response_text),
        running_session_log_path=running_log_path,
        evaluation_session_log_path=evaluation_log_path,
    )


def _resolve_evaluation_relative_path(
    target_path: Path,
    evaluation_command: str,
    evaluation_file_path: str | Path | None,
) -> str:
    if evaluation_file_path is not None:
        candidate = Path(evaluation_file_path)
        candidate = candidate.resolve() if candidate.is_absolute() else (target_path / candidate).resolve()
    else:
        candidate = _infer_evaluation_file_path(target_path, evaluation_command)

    if candidate is None or not candidate.exists():
        raise ValueError("evaluation_file_path is required unless it can be inferred from evaluation_command.")
    return os.path.relpath(candidate, target_path)


def _infer_evaluation_file_path(target_path: Path, evaluation_command: str) -> Path | None:
    tokens = [token.strip("\"'") for token in evaluation_command.split()]
    for token in tokens:
        if token.endswith(".py"):
            candidate = (target_path / token).resolve()
            if candidate.exists():
                return candidate
    return None
