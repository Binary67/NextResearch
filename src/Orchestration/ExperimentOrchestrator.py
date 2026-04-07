from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.Agents.Codex import CodexAgentError, CodexSessionRunResult, CodexSessionRunner
from src.Agents.Codex.SessionLog import CodexSessionLog
from src.EditPolicy import EditPolicy

from .EvaluationRunner import EvaluationRunner
from .ExperimentLedger import ExperimentLedger
from .ExperimentRunDocs import build_run_docs
from .ExperimentRunSupport import (
    append_post_run_review,
    blocked_commands_for_run,
    build_agent_sparse_patterns,
    build_edit_policy,
    build_target_environment,
    cleanup_experiment_workspaces,
    print_edit_policy,
    remove_run_docs,
    write_run_docs,
)
from .GitWorkspace import GitWorkspaceManager
from .Models import BootstrapArtifacts, ExperimentIterationResult, ExperimentOrchestratorError, ExperimentRunConfig
from .ExperimentPrompts import (
    build_evaluation_spec_prompt,
    build_experiment_prompt,
    build_running_instructions_prompt,
    normalize_document_text,
)


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

    def bootstrap_artifacts(
        self,
        target_repo_path: str | Path,
        objective_name: str,
        evaluation_command: str,
        evaluation_file_path: str | Path | None = None,
        baseline_branch: str | None = None,
    ) -> BootstrapArtifacts:
        context = self._resolve_repo_context(target_repo_path)
        objective_slug = self._slugify(objective_name)
        target_environment = build_target_environment(self._cache_root)
        workspace = GitWorkspaceManager(context.repo_root, self._worktrees_root)
        workspace.ensure_clean_repo()
        evaluation_relative_path = self._resolve_evaluation_relative_path(
            context.target_path,
            evaluation_command,
            evaluation_file_path,
        )
        bootstrap_ref = self._resolve_start_ref(workspace, objective_slug, baseline_branch)
        bootstrap_id = f"bootstrap-{self._timestamp_token()}"
        worktree_path = self._worktrees_root / objective_slug / bootstrap_id
        running_result: CodexSessionRunResult | None = None
        evaluation_result: CodexSessionRunResult | None = None
        running_log_path: Path | None = None
        evaluation_log_path: Path | None = None

        workspace.create_detached_worktree(worktree_path, bootstrap_ref)
        target_cwd = worktree_path / context.target_relative_path

        try:
            running_result = self._codex_session_runner.run(
                target_cwd,
                build_running_instructions_prompt(),
                environment=target_environment,
            )
            running_log_path = running_result.session_log_path

            evaluation_result = self._codex_session_runner.run(
                target_cwd,
                build_evaluation_spec_prompt(
                    evaluation_command=evaluation_command,
                    evaluation_relative_path=evaluation_relative_path,
                ),
                environment=target_environment,
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

    def run_iterations(self, config: ExperimentRunConfig) -> list[ExperimentIterationResult]:
        if config.iteration_count < 1:
            raise ValueError("iteration_count must be at least 1.")

        context = self._resolve_repo_context(config.target_repo_path)
        objective_slug = self._slugify(config.objective_name)
        target_environment = build_target_environment(self._cache_root)
        workspace = GitWorkspaceManager(context.repo_root, self._worktrees_root)
        workspace.ensure_clean_repo()
        best_branch_name = f"best/{objective_slug}"
        bootstrap_artifacts = self.bootstrap_artifacts(
            target_repo_path=config.target_repo_path,
            objective_name=config.objective_name,
            evaluation_command=config.evaluation_command,
            evaluation_file_path=config.evaluation_file_path,
            baseline_branch=config.baseline_branch,
        )

        start_ref = self._resolve_start_ref(workspace, objective_slug, config.baseline_branch)
        best_score = self._score_reference(
            context=context,
            objective_slug=objective_slug,
            ref=start_ref,
            evaluation_command=config.evaluation_command,
            workspace=workspace,
            environment=target_environment,
        )
        results: list[ExperimentIterationResult] = []

        for _ in range(config.iteration_count):
            result = self._run_single_iteration(
                config=config,
                context=context,
                objective_slug=objective_slug,
                workspace=workspace,
                target_environment=target_environment,
                bootstrap_artifacts=bootstrap_artifacts,
                best_branch_name=best_branch_name,
                start_ref=start_ref,
                best_score=best_score,
            )
            if result.improved and result.score is not None:
                best_score = result.score
            results.append(result)

        return results

    def _run_single_iteration(
        self,
        config: ExperimentRunConfig,
        context: _RepoContext,
        objective_slug: str,
        workspace: GitWorkspaceManager,
        target_environment: dict[str, str],
        bootstrap_artifacts: BootstrapArtifacts,
        best_branch_name: str,
        start_ref: str,
        best_score: float,
    ) -> ExperimentIterationResult:
        current_base_ref = best_branch_name if workspace.branch_exists(best_branch_name) else start_ref
        current_base_commit = workspace.rev_parse(current_base_ref)
        run_id = self._timestamp_token()
        branch_name = f"exp/{objective_slug}/{run_id}"
        run_root = self._worktrees_root / objective_slug / run_id
        orchestrator_worktree_path = run_root / "orchestrator"
        agent_worktree_path = run_root / "agent"
        orchestrator_cwd = orchestrator_worktree_path / context.target_relative_path
        agent_cwd = agent_worktree_path / context.target_relative_path
        docs_dir = agent_cwd / ".nextresearch"
        score: float | None = None
        score_delta: float | None = None
        improved = False
        status = "failed"
        result_commit: str | None = None
        response_text = ""
        strategy = ""
        why_it_should_help = ""
        session_log_path: Path | None = None
        changed_files: tuple[str, ...] = ()
        run_notes: tuple[str, ...] = ()
        evaluation_stdout = ""
        evaluation_stderr = ""
        app_server_file_changes = 0

        try:
            workspace.create_experiment_worktree(branch_name, orchestrator_worktree_path, current_base_commit)
            orchestrator_edit_policy = build_edit_policy(
                orchestrator_worktree_path,
                orchestrator_cwd,
                config,
            )
            sparse_patterns = build_agent_sparse_patterns(
                workspace,
                orchestrator_worktree_path,
                orchestrator_edit_policy,
                context.target_relative_path,
            )
            workspace.create_sparse_detached_worktree(
                agent_worktree_path,
                current_base_commit,
                sparse_patterns,
            )
            agent_cwd.mkdir(parents=True, exist_ok=True)
            write_run_docs(
                docs_dir,
                build_run_docs(
                    config=config,
                    target_repo_path=context.target_path,
                    bootstrap_artifacts=bootstrap_artifacts,
                    current_base_ref=current_base_ref,
                    current_base_commit=current_base_commit,
                    best_branch_name=best_branch_name,
                    best_score=best_score,
                    ledger_entries=self._ledger.load_entries(),
                ),
            )
            agent_edit_policy = build_edit_policy(
                agent_worktree_path,
                agent_cwd,
                config,
            )
            print_edit_policy(agent_edit_policy)
            session_result = self._codex_session_runner.run(
                agent_cwd,
                self._build_experiment_instruction(config.objective_name, agent_edit_policy),
                edit_policy=agent_edit_policy,
                environment=target_environment,
                blocked_commands=blocked_commands_for_run(config),
            )
            response_text = session_result.turn_result.response_text
            strategy, why_it_should_help = self._build_summary_fields(session_result.turn_result.response_text)
            session_log_path = session_result.session_log_path
            changed_files = self._build_changed_files(session_result.turn_result.file_changes)
            run_notes = tuple(session_result.turn_result.errors_and_recoveries)
            app_server_file_changes = len(session_result.turn_result.file_changes)
            remove_run_docs(docs_dir)
            workspace.apply_patch(
                orchestrator_worktree_path,
                workspace.diff_against_ref(agent_worktree_path, current_base_commit),
            )

            evaluation_outcome = self._evaluation_runner.run(
                orchestrator_cwd,
                config.evaluation_command,
                environment=target_environment,
            )
            score = evaluation_outcome.score
            evaluation_stdout = evaluation_outcome.stdout
            evaluation_stderr = evaluation_outcome.stderr
            score_delta = self._score_delta(score, best_score, config.optimization_direction)
            improved = self._is_improvement(score, best_score, config.optimization_direction)
            status = "improved" if improved else "not_improved"
        except CodexAgentError as exc:
            status = "codex_failed"
            response_text = str(exc)
            session_log_path = exc.session_log_path or session_log_path
        except ExperimentOrchestratorError as exc:
            message = str(exc)
            if message.startswith("Evaluation command failed") or (
                "Evaluation command must print a numeric score" in message
            ) or ("Evaluation command did not print a score" in message):
                status = "evaluation_failed"
            if not response_text:
                response_text = message
            if not evaluation_stderr:
                evaluation_stderr = message
        except Exception as exc:
            if not response_text:
                response_text = str(exc)
            if not evaluation_stderr:
                evaluation_stderr = str(exc)
        finally:
            remove_run_docs(docs_dir)

        if session_log_path is not None:
            try:
                append_post_run_review(
                    session_log=self._session_log,
                    workspace=workspace,
                    worktree_path=orchestrator_worktree_path,
                    session_log_path=session_log_path,
                    app_server_file_changes=app_server_file_changes,
                )
            except Exception as exc:
                message = f"Post-run git review logging failed: {exc}"
                response_text = f"{response_text}\n\n{message}".strip() if response_text else message
                if evaluation_stderr:
                    evaluation_stderr = f"{evaluation_stderr.rstrip()}\n{message}"
                else:
                    evaluation_stderr = message

        if improved:
            result_commit = workspace.commit_worktree_if_needed(
                worktree_path=orchestrator_worktree_path,
                branch_name=branch_name,
                objective_slug=objective_slug,
                run_id=run_id,
            )
            if result_commit is None:
                result_commit = current_base_commit
            workspace.force_branch(best_branch_name, result_commit)
        elif orchestrator_worktree_path.exists():
            result_commit = workspace.rev_parse("HEAD", cwd=orchestrator_worktree_path)

        result = ExperimentIterationResult(
            run_id=run_id,
            objective_name=config.objective_name,
            branch_name=branch_name,
            best_branch_name=best_branch_name,
            status=status,
            improved=improved,
            score=score,
            score_delta=score_delta,
            base_commit=current_base_commit,
            result_commit=result_commit,
            session_log_path=session_log_path,
            response_text=response_text,
            strategy=strategy,
            why_it_should_help=why_it_should_help,
            changed_files=changed_files,
            run_notes=run_notes,
            evaluation_stdout=evaluation_stdout,
            evaluation_stderr=evaluation_stderr,
        )
        try:
            self._ledger.append_entry(
                result=result,
                target_repo_path=context.target_path,
                worktree_path=orchestrator_worktree_path,
                evaluation_command=config.evaluation_command,
                optimization_direction=config.optimization_direction,
                docs_dir=docs_dir,
            )
        finally:
            cleanup_experiment_workspaces(
                workspace,
                orchestrator_worktree_path,
                agent_worktree_path,
                branch_name,
            )
        return result

    def load_ledger_entries(self, objective_name: str | None = None) -> list[dict[str, object]]:
        return self._ledger.load_entries(objective_name)

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

    def _resolve_evaluation_relative_path(
        self,
        target_path: Path,
        evaluation_command: str,
        evaluation_file_path: str | Path | None,
    ) -> str:
        if evaluation_file_path is not None:
            candidate = Path(evaluation_file_path)
            candidate = candidate.resolve() if candidate.is_absolute() else (target_path / candidate).resolve()
        else:
            candidate = self._infer_evaluation_file_path(target_path, evaluation_command)

        if candidate is None or not candidate.exists():
            raise ValueError("evaluation_file_path is required unless it can be inferred from evaluation_command.")
        return os.path.relpath(candidate, target_path)

    def _infer_evaluation_file_path(self, target_path: Path, evaluation_command: str) -> Path | None:
        tokens = [token.strip("\"'") for token in evaluation_command.split()]
        for token in tokens:
            if token.endswith(".py"):
                candidate = (target_path / token).resolve()
                if candidate.exists():
                    return candidate
        return None

    def _score_reference(
        self,
        context: _RepoContext,
        objective_slug: str,
        ref: str,
        evaluation_command: str,
        workspace: GitWorkspaceManager,
        environment: dict[str, str],
    ) -> float:
        worktree_path = self._worktrees_root / objective_slug / f"score-{self._timestamp_token()}"
        workspace.create_detached_worktree(worktree_path, ref)
        target_cwd = worktree_path / context.target_relative_path
        try:
            return self._evaluation_runner.run(target_cwd, evaluation_command, environment=environment).score
        finally:
            workspace.remove_worktree(worktree_path)

    def _is_improvement(self, score: float, best_score: float, optimization_direction: str) -> bool:
        if optimization_direction == "minimize":
            return score < best_score
        if optimization_direction == "maximize":
            return score > best_score
        raise ValueError(f"Unsupported optimization_direction: {optimization_direction}")

    def _score_delta(self, score: float, best_score: float, optimization_direction: str) -> float:
        if optimization_direction == "minimize":
            return best_score - score
        if optimization_direction == "maximize":
            return score - best_score
        raise ValueError(f"Unsupported optimization_direction: {optimization_direction}")

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

    def _build_experiment_instruction(self, objective_name: str, edit_policy: EditPolicy) -> str:
        return f"{edit_policy.prompt_prefix()}\n\n{build_experiment_prompt(objective_name=objective_name)}"

    def _build_summary_fields(self, response_text: str) -> tuple[str, str]:
        normalized = response_text.replace("\r\n", "\n").strip()
        if not normalized:
            return "", ""

        pattern = re.compile(
            r"(?ms)^\s*(Strategy|Why this should help):\s*(.*?)(?=^\s*(?:Strategy|Why this should help):|\Z)"
        )
        fields = {
            label: " ".join(value.split())
            for label, value in pattern.findall(normalized)
            if value.strip()
        }
        strategy = fields.get("Strategy", "")
        why_it_should_help = fields.get("Why this should help", "")
        if strategy or why_it_should_help:
            return strategy, why_it_should_help

        paragraphs = [" ".join(paragraph.split()) for paragraph in normalized.split("\n\n") if paragraph.strip()]
        if not paragraphs:
            return "", ""

        strategy = paragraphs[0]
        why_it_should_help = paragraphs[1] if len(paragraphs) > 1 else ""
        return strategy, why_it_should_help

    def _build_changed_files(self, file_changes: list[object]) -> tuple[str, ...]:
        paths: list[str] = []
        seen_paths: set[str] = set()

        for entry in file_changes:
            path = getattr(entry, "path", "")
            if not isinstance(path, str):
                continue
            normalized = path.strip()
            if not normalized or normalized in seen_paths:
                continue
            seen_paths.add(normalized)
            paths.append(normalized)

        return tuple(paths)
