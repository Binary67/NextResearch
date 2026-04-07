from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .EvaluationRunner import EvaluationOutcome, EvaluationRunner
from .GitWorkspace import GitWorkspaceManager
from .Models import ExperimentOrchestratorError


ORCHESTRATOR_RUN_EVAL_TOOL = "orchestrator_run_eval"


@dataclass(frozen=True)
class ToolEvaluationResponse:
    status: str
    score: float | None
    delta_vs_best: float | None
    delta_vs_start: float | None
    budget_remaining: int
    note: str

    def to_tool_text(self) -> str:
        return json.dumps(
            {
                "status": self.status,
                "score": self.score,
                "delta_vs_best": self.delta_vs_best,
                "delta_vs_start": self.delta_vs_start,
                "budget_remaining": self.budget_remaining,
                "note": self.note,
            },
            ensure_ascii=False,
            indent=2,
        )


@dataclass(frozen=True)
class CandidateSnapshot:
    fingerprint: str
    patch: bytes


@dataclass(frozen=True)
class CachedEvaluationResult:
    fingerprint: str
    score: float | None
    stdout: str
    stderr: str
    failure_message: str | None = None


@dataclass(frozen=True)
class FinalCandidateEvaluation:
    fingerprint: str
    score: float | None
    stdout: str
    stderr: str
    failure_message: str | None
    reused_cached_result: bool


class ExperimentEvalTool:
    def __init__(
        self,
        *,
        workspace: GitWorkspaceManager,
        agent_worktree_path: Path,
        orchestrator_worktree_path: Path,
        target_relative_path: Path,
        current_base_commit: str,
        evaluation_command: str,
        optimization_direction: str,
        best_score: float,
        start_score: float,
        evaluation_runner: EvaluationRunner,
        environment: Mapping[str, str],
        budget: int,
        excluded_patch_paths: tuple[str, ...] = (),
    ) -> None:
        self._workspace = workspace
        self._agent_worktree_path = agent_worktree_path
        self._orchestrator_worktree_path = orchestrator_worktree_path
        self._target_relative_path = target_relative_path
        self._current_base_commit = current_base_commit
        self._evaluation_command = evaluation_command
        self._optimization_direction = optimization_direction
        self._best_score = best_score
        self._start_score = start_score
        self._evaluation_runner = evaluation_runner
        self._environment = dict(environment)
        self._budget_remaining = budget
        self._excluded_patch_paths = tuple(path for path in excluded_patch_paths if path)
        self._last_synced_fingerprint: str | None = None
        self._last_evaluation: CachedEvaluationResult | None = None

    @property
    def budget_remaining(self) -> int:
        return self._budget_remaining

    def evaluate_current_candidate(self) -> ToolEvaluationResponse:
        if self._budget_remaining < 1:
            return ToolEvaluationResponse(
                status="budget_exhausted",
                score=None,
                delta_vs_best=None,
                delta_vs_start=None,
                budget_remaining=0,
                note="No evaluation calls remain for this run.",
            )

        snapshot = self.sync_current_candidate()
        self._budget_remaining -= 1

        try:
            outcome = self._run_evaluation()
        except ExperimentOrchestratorError as exc:
            self._last_evaluation = CachedEvaluationResult(
                fingerprint=snapshot.fingerprint,
                score=None,
                stdout="",
                stderr=str(exc),
                failure_message=str(exc),
            )
            return ToolEvaluationResponse(
                status="evaluation_failed",
                score=None,
                delta_vs_best=None,
                delta_vs_start=None,
                budget_remaining=self._budget_remaining,
                note=self._sanitize_failure_message(str(exc)),
            )

        self._last_evaluation = CachedEvaluationResult(
            fingerprint=snapshot.fingerprint,
            score=outcome.score,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
        )
        delta_vs_best = self._score_delta(outcome.score, self._best_score)
        delta_vs_start = self._score_delta(outcome.score, self._start_score)
        return ToolEvaluationResponse(
            status="completed",
            score=outcome.score,
            delta_vs_best=delta_vs_best,
            delta_vs_start=delta_vs_start,
            budget_remaining=self._budget_remaining,
            note=self._success_note(outcome.score),
        )

    def sync_current_candidate(self) -> CandidateSnapshot:
        snapshot = self._snapshot_candidate()
        if snapshot.fingerprint == self._last_synced_fingerprint:
            return snapshot

        self._workspace.reset_worktree_to_ref(
            self._orchestrator_worktree_path,
            self._current_base_commit,
            clean_untracked=True,
        )
        self._workspace.apply_patch(self._orchestrator_worktree_path, snapshot.patch)
        self._last_synced_fingerprint = snapshot.fingerprint
        return snapshot

    def finalize_candidate(self) -> FinalCandidateEvaluation:
        snapshot = self.sync_current_candidate()
        if self._last_evaluation is not None and self._last_evaluation.fingerprint == snapshot.fingerprint:
            return FinalCandidateEvaluation(
                fingerprint=snapshot.fingerprint,
                score=self._last_evaluation.score,
                stdout=self._last_evaluation.stdout,
                stderr=self._last_evaluation.stderr,
                failure_message=self._last_evaluation.failure_message,
                reused_cached_result=True,
            )

        try:
            outcome = self._run_evaluation()
        except ExperimentOrchestratorError as exc:
            failure_message = str(exc)
            self._last_evaluation = CachedEvaluationResult(
                fingerprint=snapshot.fingerprint,
                score=None,
                stdout="",
                stderr=failure_message,
                failure_message=failure_message,
            )
            return FinalCandidateEvaluation(
                fingerprint=snapshot.fingerprint,
                score=None,
                stdout="",
                stderr=failure_message,
                failure_message=failure_message,
                reused_cached_result=False,
            )

        self._last_evaluation = CachedEvaluationResult(
            fingerprint=snapshot.fingerprint,
            score=outcome.score,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
        )
        return FinalCandidateEvaluation(
            fingerprint=snapshot.fingerprint,
            score=outcome.score,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            failure_message=None,
            reused_cached_result=False,
        )

    def _snapshot_candidate(self) -> CandidateSnapshot:
        patch = self._workspace.diff_against_ref(
            self._agent_worktree_path,
            self._current_base_commit,
            exclude_paths=self._excluded_patch_paths,
        )
        return CandidateSnapshot(
            fingerprint=hashlib.sha256(patch).hexdigest(),
            patch=patch,
        )

    def _run_evaluation(self) -> EvaluationOutcome:
        return self._evaluation_runner.run(
            self._orchestrator_worktree_path / self._target_relative_path,
            self._evaluation_command,
            environment=self._environment,
        )

    def _score_delta(self, score: float, reference: float) -> float:
        if self._optimization_direction == "minimize":
            return reference - score
        if self._optimization_direction == "maximize":
            return score - reference
        raise ValueError(f"Unsupported optimization_direction: {self._optimization_direction}")

    def _success_note(self, score: float) -> str:
        if self._optimization_direction == "minimize":
            if score < self._best_score:
                return "Candidate improved relative to the current best score."
        else:
            if score > self._best_score:
                return "Candidate improved relative to the current best score."
        return "Candidate evaluated successfully but did not beat the current best score."

    def _sanitize_failure_message(self, message: str) -> str:
        if message.startswith("Evaluation command failed"):
            return "Evaluation failed due to a runtime error."
        if "must print a numeric score" in message or "did not print a score" in message:
            return "Evaluation failed because the candidate produced invalid scoring output."
        return "Evaluation failed."
