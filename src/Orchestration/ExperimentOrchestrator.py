from __future__ import annotations

import re
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path

from src.Agents.Codex import CodexSessionRunner
from src.Agents.Codex.SessionLog import CodexSessionLog

from .EvaluationRunner import EvaluationRunner
from .ExperimentLedger import ExperimentLedger
from .ExperimentIterationRunner import run_iteration, score_reference
from .ExperimentRunSupport import build_shared_target_environment
from .ExperimentVisualization import progress_chart_path, write_experiment_progress_svg
from .GitWorkspace import GitWorkspaceManager
from .Models import ExperimentIterationResult, ExperimentOrchestratorError, ExperimentRunConfig


@dataclass(frozen=True)
class _RepoContext:
    repo_root: Path
    target_path: Path
    target_relative_path: Path


class ExperimentOrchestrator:
    def __init__(
        self,
        codex_executable: str | None = None,
        logs_root: Path | str | None = None,
        worktrees_root: Path | str | None = None,
    ) -> None:
        self._codex_executable = codex_executable
        self._logs_root = Path(logs_root) if logs_root is not None else self._default_logs_root()
        self._logs_root.mkdir(parents=True, exist_ok=True)
        self._cache_root = self._default_cache_root()
        self._cache_root.mkdir(parents=True, exist_ok=True)
        self._worktrees_root = Path(worktrees_root) if worktrees_root is not None else self._logs_root / "Worktrees"
        self._worktrees_root.mkdir(parents=True, exist_ok=True)
        self._ledger = ExperimentLedger(self._logs_root / "codex_experiments.jsonl")
        self._evaluation_runner = EvaluationRunner()
        self._codex_session_runner = CodexSessionRunner(
            codex_executable=self._codex_executable,
            logs_root=self._logs_root,
        )
        self._session_log = CodexSessionLog(self._logs_root)

    @property
    def logs_root(self) -> Path:
        return self._logs_root

    @property
    def worktrees_root(self) -> Path:
        return self._worktrees_root

    @property
    def ledger_path(self) -> Path:
        return self._ledger.ledger_path

    def run_iterations(self, config: ExperimentRunConfig) -> list[ExperimentIterationResult]:
        if config.iteration_count < 1:
            raise ValueError("iteration_count must be at least 1.")

        context = self._resolve_repo_context(config.target_repo_path)
        objective_slug = self._slugify(config.objective_name)
        target_environment = build_shared_target_environment(self._cache_root)
        hidden_eval_cwd = self._resolve_hidden_eval_cwd(config.hidden_eval_cwd)
        workspace = GitWorkspaceManager(context.repo_root, self._worktrees_root)
        workspace.ensure_clean_repo()
        best_branch_name = f"best/{objective_slug}"
        start_ref = self._resolve_start_ref(workspace, objective_slug, config.baseline_branch)
        best_score = score_reference(
            hidden_eval_cwd=hidden_eval_cwd,
            hidden_eval_command=config.hidden_eval_command,
            target_relative_path=context.target_relative_path,
            objective_slug=objective_slug,
            ref=start_ref,
            score_id=f"score-{self._timestamp_token()}",
            workspace=workspace,
            worktrees_root=self._worktrees_root,
            evaluation_runner=self._evaluation_runner,
            environment=target_environment,
        )
        results: list[ExperimentIterationResult] = []

        for _ in range(config.iteration_count):
            result = run_iteration(
                config=config,
                target_path=context.target_path,
                target_relative_path=context.target_relative_path,
                objective_slug=objective_slug,
                run_id=self._timestamp_token(),
                workspace=workspace,
                worktrees_root=self._worktrees_root,
                target_environment=target_environment,
                hidden_eval_cwd=hidden_eval_cwd,
                best_branch_name=best_branch_name,
                start_ref=start_ref,
                best_score=best_score,
                ledger=self._ledger,
                evaluation_runner=self._evaluation_runner,
                codex_session_runner=self._codex_session_runner,
                session_log=self._session_log,
            )
            if result.improved and result.score is not None:
                best_score = result.score
            results.append(result)

        ledger_entries = self._ledger.load_entries(config.objective_name, config.evaluation_key)
        write_experiment_progress_svg(
            entries=ledger_entries,
            objective_name=config.objective_name,
            objective_slug=objective_slug,
            optimization_direction=config.optimization_direction,
            output_path=progress_chart_path(self._logs_root, objective_slug),
        )
        return results

    def load_ledger_entries(
        self,
        objective_name: str | None = None,
        evaluation_key: str | None = None,
    ) -> list[dict[str, object]]:
        return self._ledger.load_entries(objective_name, evaluation_key)

    def _resolve_repo_context(self, target_repo_path: str | Path) -> _RepoContext:
        target_path = Path(target_repo_path).expanduser().resolve()
        if not target_path.exists():
            raise ValueError(f"target_repo_path does not exist: {target_repo_path}")
        if not target_path.is_dir():
            raise ValueError(f"target_repo_path is not a directory: {target_repo_path}")

        repo_root_value = GitWorkspaceManager(target_path, self._worktrees_root).git_output(
            target_path,
            "rev-parse",
            "--show-toplevel",
        )
        if not repo_root_value:
            raise ExperimentOrchestratorError(f"Could not resolve git root for {target_path}")
        repo_root = Path(repo_root_value)
        target_relative_path = target_path.relative_to(repo_root)
        return _RepoContext(repo_root=repo_root, target_path=target_path, target_relative_path=target_relative_path)

    def _resolve_start_ref(
        self,
        workspace: GitWorkspaceManager,
        objective_slug: str,
        baseline_branch: str | None,
    ) -> str:
        best_branch_name = f"best/{objective_slug}"
        if workspace.branch_exists(best_branch_name):
            return best_branch_name
        if baseline_branch:
            return baseline_branch

        current_branch = workspace.current_branch()
        if current_branch:
            return current_branch
        return workspace.rev_parse("HEAD")

    def _resolve_hidden_eval_cwd(self, hidden_eval_cwd: str | Path) -> Path:
        candidate = Path(hidden_eval_cwd).expanduser().resolve()
        if not candidate.exists():
            raise ValueError(f"hidden_eval_cwd does not exist: {hidden_eval_cwd}")
        if not candidate.is_dir():
            raise ValueError(f"hidden_eval_cwd is not a directory: {hidden_eval_cwd}")
        return candidate

    def _slugify(self, value: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9._/-]+", "-", value.strip().lower()).strip("-./")
        if not slug:
            raise ValueError("objective_name must contain at least one alphanumeric character.")
        return slug

    def _timestamp_token(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")

    def _default_logs_root(self) -> Path:
        return Path(__file__).resolve().parents[2] / "Logs"

    def _default_cache_root(self) -> Path:
        return Path(__file__).resolve().parents[2] / "Cache"
