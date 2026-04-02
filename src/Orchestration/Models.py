from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class ExperimentOrchestratorError(RuntimeError):
    """Raised when the experiment workflow cannot complete successfully."""


@dataclass(frozen=True)
class BootstrapArtifacts:
    running_instructions: str
    evaluation_spec: str
    running_session_log_path: Path | None
    evaluation_session_log_path: Path | None


@dataclass(frozen=True)
class ExperimentIterationResult:
    run_id: str
    objective_name: str
    branch_name: str
    best_branch_name: str
    status: str
    improved: bool
    score: float | None
    score_delta: float | None
    base_commit: str
    result_commit: str | None
    session_log_path: Path | None
    response_text: str
    evaluation_stdout: str
    evaluation_stderr: str


@dataclass(frozen=True)
class ExperimentRunConfig:
    target_repo_path: str | Path
    objective_name: str
    evaluation_command: str
    iteration_count: int
    evaluation_file_path: str | Path | None = None
    baseline_branch: str | None = None
