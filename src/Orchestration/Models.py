from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


class ExperimentOrchestratorError(RuntimeError):
    """Raised when the experiment workflow cannot complete successfully."""


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
    strategy: str
    why_it_should_help: str
    changed_files: tuple[str, ...]
    run_notes: tuple[str, ...]
    evaluation_stdout: str
    evaluation_stderr: str


@dataclass(frozen=True)
class ExperimentRunConfig:
    target_repo_path: str | Path
    objective_name: str
    iteration_count: int
    optimization_direction: Literal["minimize", "maximize"]
    hidden_eval_cwd: str | Path
    hidden_eval_command: str
    agent_eval_budget: int = 3
    baseline_branch: str | None = None
    editable_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.optimization_direction not in {"minimize", "maximize"}:
            raise ValueError("optimization_direction must be 'minimize' or 'maximize'.")
        if not isinstance(self.agent_eval_budget, int) or isinstance(self.agent_eval_budget, bool):
            raise ValueError("agent_eval_budget must be an integer.")
        if self.agent_eval_budget < 1:
            raise ValueError("agent_eval_budget must be at least 1.")

    @property
    def evaluation_key(self) -> str:
        material = (
            f"{Path(self.hidden_eval_cwd).expanduser().resolve(strict=False)}\n{self.hidden_eval_command}"
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()[:16]
