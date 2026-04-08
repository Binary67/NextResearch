from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .Models import ExperimentOrchestratorError


NEXTRESEARCH_CANDIDATE_TARGET_PATH = "NEXTRESEARCH_CANDIDATE_TARGET_PATH"
NEXTRESEARCH_CANDIDATE_REPO_ROOT = "NEXTRESEARCH_CANDIDATE_REPO_ROOT"


@dataclass(frozen=True)
class EvaluationOutcome:
    score: float
    stdout: str
    stderr: str


def build_candidate_environment(
    environment: Mapping[str, str] | None,
    candidate_target_path: Path,
    candidate_repo_root: Path,
) -> dict[str, str]:
    candidate_environment = dict(environment) if environment is not None else {}
    candidate_environment[NEXTRESEARCH_CANDIDATE_TARGET_PATH] = str(candidate_target_path.resolve())
    candidate_environment[NEXTRESEARCH_CANDIDATE_REPO_ROOT] = str(candidate_repo_root.resolve())
    return candidate_environment


class EvaluationRunner:
    def run(
        self,
        cwd: Path,
        evaluation_command: str,
        environment: Mapping[str, str] | None = None,
    ) -> EvaluationOutcome:
        completed = subprocess.run(
            evaluation_command,
            cwd=cwd,
            shell=True,
            text=True,
            capture_output=True,
            env=dict(environment) if environment is not None else None,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip() or completed.stdout.strip()
            raise ExperimentOrchestratorError(
                f"Evaluation command failed with exit code {completed.returncode}: {stderr}"
            )

        return EvaluationOutcome(
            score=self._parse_score(completed.stdout),
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def _parse_score(self, stdout: str) -> float:
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        if not lines:
            raise ExperimentOrchestratorError("Evaluation command did not print a score.")

        try:
            return float(lines[-1])
        except ValueError as exc:
            raise ExperimentOrchestratorError(
                "Evaluation command must print a numeric score on its last non-empty line."
            ) from exc
