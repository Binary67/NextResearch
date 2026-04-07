from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


class ExperimentOrchestratorError(RuntimeError):
    """Raised when the experiment workflow cannot complete successfully."""


def _normalize_path_policy_field(
    field_name: str,
    value: str | tuple[str, ...] | list[str],
) -> tuple[str, ...]:
    if isinstance(value, str):
        candidates = (value,)
    elif isinstance(value, (tuple, list)):
        candidates = tuple(value)
    else:
        raise TypeError(
            f"{field_name} must be a string, tuple[str, ...], or list[str]; "
            f"got {type(value).__name__}."
        )

    normalized: list[str] = []
    for raw_path in candidates:
        if not isinstance(raw_path, str):
            raise TypeError(f"{field_name} entries must be strings; got {type(raw_path).__name__}.")
        stripped = raw_path.strip()
        if not stripped:
            raise ValueError(f"{field_name} entries must be non-empty strings.")
        normalized.append(stripped)
    return tuple(normalized)


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
    evaluation_command: str
    iteration_count: int
    optimization_direction: Literal["minimize", "maximize"]
    evaluation_file_path: str | Path | None = None
    baseline_branch: str | None = None
    editable_paths: tuple[str, ...] = ()
    non_editable_paths: tuple[str, ...] = ()
    non_readable_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.optimization_direction not in {"minimize", "maximize"}:
            raise ValueError("optimization_direction must be 'minimize' or 'maximize'.")
        object.__setattr__(
            self,
            "editable_paths",
            _normalize_path_policy_field("editable_paths", self.editable_paths),
        )
        object.__setattr__(
            self,
            "non_editable_paths",
            _normalize_path_policy_field("non_editable_paths", self.non_editable_paths),
        )
        object.__setattr__(
            self,
            "non_readable_paths",
            _normalize_path_policy_field("non_readable_paths", self.non_readable_paths),
        )
