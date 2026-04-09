from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .EvaluationRunner import EvaluationOutcome, EvaluationRunner, build_candidate_environment
from .GitWorkspace import GitWorkspaceManager
from .HiddenEvalSandbox import prepare_hidden_eval_sandbox
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
    orchestrator_patch: bytes
    evaluation_patch: bytes
    has_agent_changes: bool

    @property
    def is_modified(self) -> bool:
        return self.has_agent_changes


@dataclass(frozen=True)
class CachedEvaluationResult:
    fingerprint: str
    score: float | None
    stdout: str
    stderr: str
    failure_message: str | None = None


@dataclass(frozen=True)
class RetainedCandidate:
    snapshot: CandidateSnapshot
    score: float
    stdout: str
    stderr: str
    improved_relative_to_best: bool


@dataclass(frozen=True)
class FinalCandidateEvaluation:
    fingerprint: str
    score: float | None
    stdout: str
    stderr: str
    failure_message: str | None
    reused_cached_result: bool
    retained_modified_candidate: bool


class ExperimentEvalTool:
    def __init__(
        self,
        *,
        workspace: GitWorkspaceManager,
        agent_worktree_path: Path,
        orchestrator_worktree_path: Path,
        target_relative_path: Path,
        evaluation_base_ref: str,
        current_base_commit: str,
        hidden_eval_cwd: Path,
        hidden_eval_sandbox_path: Path,
        hidden_eval_command: str,
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
        self._evaluation_base_ref = evaluation_base_ref
        self._evaluation_base_commit = workspace.rev_parse(evaluation_base_ref)
        self._current_base_commit = current_base_commit
        self._hidden_eval_cwd = hidden_eval_cwd
        self._hidden_eval_sandbox_path = hidden_eval_sandbox_path
        self._hidden_eval_command = hidden_eval_command
        self._optimization_direction = optimization_direction
        self._best_score = best_score
        self._start_score = start_score
        self._evaluation_runner = evaluation_runner
        self._environment = dict(environment)
        self._budget_remaining = budget
        self._excluded_patch_paths = tuple(path for path in excluded_patch_paths if path)
        self._current_base_patch = self._build_current_base_patch()
        self._baseline_snapshot = CandidateSnapshot(
            fingerprint=hashlib.sha256(self._current_base_patch).hexdigest(),
            orchestrator_patch=b"",
            evaluation_patch=self._current_base_patch,
            has_agent_changes=False,
        )
        self._last_synced_fingerprint: str | None = None
        self._last_evaluation: CachedEvaluationResult | None = None
        self._best_modified_candidate: RetainedCandidate | None = None

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

        evaluation = self._evaluate_snapshot(snapshot)
        self._last_evaluation = evaluation
        if self._evaluation_consumes_budget(evaluation):
            self._budget_remaining -= 1
        if evaluation.failure_message is not None:
            return ToolEvaluationResponse(
                status="evaluation_failed",
                score=None,
                delta_vs_best=None,
                delta_vs_start=None,
                budget_remaining=self._budget_remaining,
                note=self._sanitize_failure_message(evaluation.failure_message),
            )

        self._retain_candidate_if_best(
            snapshot=snapshot,
            score=evaluation.score,
            stdout=evaluation.stdout,
            stderr=evaluation.stderr,
        )
        delta_vs_best = self._score_delta(evaluation.score, self._best_score)
        delta_vs_start = self._score_delta(evaluation.score, self._start_score)
        return ToolEvaluationResponse(
            status="completed",
            score=evaluation.score,
            delta_vs_best=delta_vs_best,
            delta_vs_start=delta_vs_start,
            budget_remaining=self._budget_remaining,
            note=self._success_note(evaluation.score),
        )

    def sync_current_candidate(self) -> CandidateSnapshot:
        snapshot = self._snapshot_candidate()
        self._sync_snapshot(snapshot)
        return snapshot

    def finalize_candidate(self) -> FinalCandidateEvaluation:
        current_snapshot = self.sync_current_candidate()
        current_evaluation, reused_current_result = self._evaluate_if_needed(current_snapshot)
        self._last_evaluation = current_evaluation
        if current_evaluation.failure_message is None:
            self._retain_candidate_if_best(
                snapshot=current_snapshot,
                score=current_evaluation.score,
                stdout=current_evaluation.stdout,
                stderr=current_evaluation.stderr,
            )

        retained_candidate = self._best_modified_candidate
        if retained_candidate is not None:
            self._sync_snapshot(retained_candidate.snapshot)
            return FinalCandidateEvaluation(
                fingerprint=retained_candidate.snapshot.fingerprint,
                score=retained_candidate.score,
                stdout=retained_candidate.stdout,
                stderr=retained_candidate.stderr,
                failure_message=None,
                reused_cached_result=reused_current_result
                and retained_candidate.snapshot.fingerprint == current_snapshot.fingerprint,
                retained_modified_candidate=True,
            )

        if current_evaluation.failure_message is None:
            self._sync_snapshot(current_snapshot)
            return FinalCandidateEvaluation(
                fingerprint=current_snapshot.fingerprint,
                score=current_evaluation.score,
                stdout=current_evaluation.stdout,
                stderr=current_evaluation.stderr,
                failure_message=None,
                reused_cached_result=reused_current_result,
                retained_modified_candidate=False,
            )

        baseline_snapshot = self._baseline_snapshot
        baseline_evaluation, reused_baseline_result = self._evaluate_if_needed(baseline_snapshot)
        self._last_evaluation = baseline_evaluation
        self._sync_snapshot(baseline_snapshot)
        return FinalCandidateEvaluation(
            fingerprint=baseline_snapshot.fingerprint,
            score=baseline_evaluation.score,
            stdout=baseline_evaluation.stdout,
            stderr=baseline_evaluation.stderr,
            failure_message=baseline_evaluation.failure_message,
            reused_cached_result=reused_baseline_result,
            retained_modified_candidate=False,
        )

    def _sync_snapshot(self, snapshot: CandidateSnapshot) -> None:
        if snapshot.fingerprint == self._last_synced_fingerprint:
            return

        self._workspace.reset_worktree_to_ref(
            self._orchestrator_worktree_path,
            self._current_base_commit,
            clean_untracked=True,
        )
        self._workspace.apply_patch(self._orchestrator_worktree_path, snapshot.orchestrator_patch)
        self._last_synced_fingerprint = snapshot.fingerprint

    def _snapshot_candidate(self) -> CandidateSnapshot:
        orchestrator_patch = self._workspace.diff_against_ref(
            self._agent_worktree_path,
            self._current_base_commit,
            exclude_paths=self._excluded_patch_paths,
        )
        evaluation_patch = self._compose_evaluation_patch(orchestrator_patch)
        return CandidateSnapshot(
            fingerprint=hashlib.sha256(evaluation_patch).hexdigest(),
            orchestrator_patch=orchestrator_patch,
            evaluation_patch=evaluation_patch,
            has_agent_changes=bool(orchestrator_patch),
        )

    def _run_evaluation(self, snapshot: CandidateSnapshot) -> EvaluationOutcome:
        prepare_hidden_eval_sandbox(
            source_path=self._hidden_eval_cwd,
            sandbox_path=self._hidden_eval_sandbox_path,
            workspace=self._workspace,
            patch=snapshot.evaluation_patch,
        )
        candidate_target_path = self._hidden_eval_sandbox_path / self._target_relative_path
        environment = build_candidate_environment(
            self._environment,
            candidate_target_path=candidate_target_path,
            candidate_repo_root=self._hidden_eval_sandbox_path,
        )
        return self._evaluation_runner.run(
            self._hidden_eval_sandbox_path,
            self._hidden_eval_command,
            environment=environment,
        )

    def _evaluate_if_needed(self, snapshot: CandidateSnapshot) -> tuple[CachedEvaluationResult, bool]:
        cached = self._last_evaluation
        if cached is not None and cached.fingerprint == snapshot.fingerprint:
            return cached, True
        return self._evaluate_snapshot(snapshot), False

    def _evaluate_snapshot(self, snapshot: CandidateSnapshot) -> CachedEvaluationResult:
        try:
            outcome = self._run_evaluation(snapshot)
        except ExperimentOrchestratorError as exc:
            failure_message = str(exc)
            return CachedEvaluationResult(
                fingerprint=snapshot.fingerprint,
                score=None,
                stdout="",
                stderr=failure_message,
                failure_message=failure_message,
            )

        return CachedEvaluationResult(
            fingerprint=snapshot.fingerprint,
            score=outcome.score,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
        )

    def _build_current_base_patch(self) -> bytes:
        if self._evaluation_base_commit == self._current_base_commit:
            return b""
        return self._workspace.diff_refs(self._evaluation_base_ref, self._current_base_commit)

    def _compose_evaluation_patch(self, orchestrator_patch: bytes) -> bytes:
        if not self._current_base_patch:
            return orchestrator_patch
        if not orchestrator_patch:
            return self._current_base_patch
        return self._current_base_patch + orchestrator_patch

    def _retain_candidate_if_best(
        self,
        *,
        snapshot: CandidateSnapshot,
        score: float,
        stdout: str,
        stderr: str,
    ) -> None:
        if not snapshot.is_modified:
            return

        retained_candidate = self._best_modified_candidate
        if retained_candidate is not None and not self._is_better_score(score, retained_candidate.score):
            return

        self._best_modified_candidate = RetainedCandidate(
            snapshot=snapshot,
            score=score,
            stdout=stdout,
            stderr=stderr,
            improved_relative_to_best=self._is_better_score(score, self._best_score),
        )

    def _score_delta(self, score: float, reference: float) -> float:
        if self._optimization_direction == "minimize":
            return reference - score
        if self._optimization_direction == "maximize":
            return score - reference
        raise ValueError(f"Unsupported optimization_direction: {self._optimization_direction}")

    def _is_better_score(self, score: float, reference: float) -> bool:
        if self._optimization_direction == "minimize":
            return score < reference
        if self._optimization_direction == "maximize":
            return score > reference
        raise ValueError(f"Unsupported optimization_direction: {self._optimization_direction}")

    def _success_note(self, score: float) -> str:
        if self._optimization_direction == "minimize":
            if score < self._best_score:
                return "Candidate improved relative to the current best score."
        else:
            if score > self._best_score:
                return "Candidate improved relative to the current best score."
        return "Candidate evaluated successfully but did not beat the current best score."

    def _evaluation_consumes_budget(self, evaluation: CachedEvaluationResult) -> bool:
        if evaluation.failure_message is None:
            return True
        return not evaluation.failure_message.startswith("Patch application failed")

    def _sanitize_failure_message(self, message: str) -> str:
        if message.startswith("Patch application failed"):
            return "Candidate could not be evaluated because its patch could not be applied cleanly."
        if message.startswith("Evaluation command failed"):
            return "Evaluation failed due to a runtime error."
        if "must print a numeric score" in message or "did not print a score" in message:
            return "Evaluation failed because the candidate produced invalid scoring output."
        return "Evaluation failed."
