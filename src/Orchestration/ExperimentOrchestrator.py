from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.Agents.Codex import CodexAgentError, CodexSessionRunResult, CodexSessionRunner
from src.Agents.Codex.SessionLog import CodexSessionLog
from src.EditPolicy import EditPolicy

from .EvaluationRunner import EvaluationRunner
from .ExperimentLedger import ExperimentLedger
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
        target_environment = self._build_target_environment()
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
        target_environment = self._build_target_environment()
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
            session_log_path: Path | None = None
            evaluation_stdout = ""
            evaluation_stderr = ""
            app_server_file_changes = 0

            try:
                workspace.create_experiment_worktree(branch_name, orchestrator_worktree_path, current_base_commit)
                orchestrator_edit_policy = self._build_edit_policy(
                    orchestrator_worktree_path,
                    orchestrator_cwd,
                    config,
                )
                sparse_patterns = self._build_agent_sparse_patterns(
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
                self._write_run_docs(
                    docs_dir,
                    self._build_run_docs(
                        config=config,
                        target_repo_path=context.target_path,
                        bootstrap_artifacts=bootstrap_artifacts,
                        current_base_ref=current_base_ref,
                        current_base_commit=current_base_commit,
                        best_branch_name=best_branch_name,
                        best_score=best_score,
                    ),
                )
                agent_edit_policy = self._build_edit_policy(
                    agent_worktree_path,
                    agent_cwd,
                    config,
                )
                self._print_edit_policy(agent_edit_policy)
                session_result = self._codex_session_runner.run(
                    agent_cwd,
                    self._build_experiment_instruction(config.objective_name, agent_edit_policy),
                    edit_policy=agent_edit_policy,
                    environment=target_environment,
                    blocked_commands=self._blocked_commands_for_run(config),
                )
                response_text = session_result.turn_result.response_text
                session_log_path = session_result.session_log_path
                app_server_file_changes = len(session_result.turn_result.file_changes)
                self._remove_run_docs(docs_dir)
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
                self._remove_run_docs(docs_dir)

            if session_log_path is not None:
                try:
                    self._append_post_run_review(
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
                best_score = score
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
                        bootstrap_artifacts=bootstrap_artifacts,
                    )
            finally:
                self._cleanup_experiment_workspaces(
                    workspace,
                    orchestrator_worktree_path,
                    agent_worktree_path,
                    branch_name,
                )
            results.append(result)

        return results

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

    def _write_run_docs(self, docs_dir: Path, documents: dict[str, str]) -> None:
        docs_dir.mkdir(parents=True, exist_ok=True)
        for name, content in documents.items():
            (docs_dir / name).write_text(content, encoding="utf-8")

    def _remove_run_docs(self, docs_dir: Path) -> None:
        if docs_dir.exists():
            shutil.rmtree(docs_dir)

    def _cleanup_experiment_workspaces(
        self,
        workspace: GitWorkspaceManager,
        orchestrator_worktree_path: Path,
        agent_worktree_path: Path,
        branch_name: str,
    ) -> None:
        try:
            workspace.remove_worktree(agent_worktree_path)
        finally:
            try:
                workspace.remove_worktree(orchestrator_worktree_path)
            finally:
                workspace.delete_branch(branch_name)

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

    def _build_run_docs(
        self,
        config: ExperimentRunConfig,
        target_repo_path: Path,
        bootstrap_artifacts: BootstrapArtifacts,
        current_base_ref: str,
        current_base_commit: str,
        best_branch_name: str,
        best_score: float,
    ) -> dict[str, str]:
        comparable_entries = self._load_comparable_entries(
            target_repo_path=target_repo_path,
            config=config,
            bootstrap_artifacts=bootstrap_artifacts,
        )
        return {
            "RUNNING_INSTRUCTIONS.md": bootstrap_artifacts.running_instructions,
            "EVALUATION_SPEC.md": bootstrap_artifacts.evaluation_spec,
            "BASELINE_STATE.md": self._build_baseline_state_document(
                objective_name=config.objective_name,
                optimization_direction=config.optimization_direction,
                current_base_commit=current_base_commit,
                best_score=best_score,
                comparable_entries=comparable_entries,
                starting_from_best_known=current_base_ref == best_branch_name,
            ),
            "EXPERIMENT_HISTORY.md": self._build_experiment_history_document(
                comparable_entries=comparable_entries,
                optimization_direction=config.optimization_direction,
            ),
        }

    def _load_comparable_entries(
        self,
        target_repo_path: Path,
        config: ExperimentRunConfig,
        bootstrap_artifacts: BootstrapArtifacts,
    ) -> list[dict[str, object]]:
        running_hash = self._hash_text(bootstrap_artifacts.running_instructions)
        evaluation_hash = self._hash_text(bootstrap_artifacts.evaluation_spec)
        comparable_entries: list[dict[str, object]] = []

        for entry in self._ledger.load_entries():
            if (
                str(entry.get("target_repo_path")) != str(target_repo_path)
                or str(entry.get("objective_name")) != config.objective_name
                or str(entry.get("evaluation_command")) != config.evaluation_command
                or str(entry.get("optimization_direction")) != config.optimization_direction
                or str(entry.get("running_instructions_hash")) != running_hash
                or str(entry.get("evaluation_spec_hash")) != evaluation_hash
            ):
                continue
            comparable_entries.append(entry)

        return comparable_entries

    def _build_baseline_state_document(
        self,
        objective_name: str,
        optimization_direction: str,
        current_base_commit: str,
        best_score: float,
        comparable_entries: list[dict[str, object]],
        starting_from_best_known: bool,
    ) -> str:
        improved_entries = [entry for entry in comparable_entries if bool(entry.get("improved"))]
        no_improvement_streak = self._count_recent_non_improvements(comparable_entries)
        last_improved_entry = improved_entries[-1] if improved_entries else None

        if improved_entries:
            starting_point = "Current best-known version so far" if starting_from_best_known else "Comparable baseline"
            trend_note = (
                f"Recent runs have not improved for {no_improvement_streak} consecutive attempt(s)."
                if no_improvement_streak
                else "The previous comparable run improved the score."
            )
        else:
            starting_point = "Configured baseline with no prior comparable improvements"
            trend_note = "No prior comparable improvements yet."

        lines = [
            "# Baseline State",
            "",
            "## Current Position",
            f"- Objective: {objective_name}",
            f"- Optimization direction: {optimization_direction}",
            f"- Current best score: {self._format_float(best_score)}",
            f"- Current base commit: {current_base_commit}",
            f"- Starting point: {starting_point}",
            "",
            "## Comparable History Summary",
            f"- Comparable past runs: {len(comparable_entries)}",
            f"- Improved runs: {len(improved_entries)}",
            f"- Current no-improvement streak: {no_improvement_streak}",
            f"- Last improved run: {self._format_last_improved(last_improved_entry)}",
            "",
            "## Notes",
            f"- {trend_note}",
        ]
        return "\n".join(lines) + "\n"

    def _build_experiment_history_document(
        self,
        comparable_entries: list[dict[str, object]],
        optimization_direction: str,
    ) -> str:
        lines = [
            "# Experiment History",
            "",
            "## Summary",
            f"- Comparable runs: {len(comparable_entries)}",
            f"- Improved runs: {sum(1 for entry in comparable_entries if bool(entry.get('improved')))}",
            f"- Best score seen: {self._format_optional_float(self._best_score_from_entries(comparable_entries, optimization_direction))}",
            f"- Latest run status: {self._latest_status(comparable_entries)}",
            "",
            "## Recent And Representative Runs",
        ]

        selected_entries = self._select_history_entries(comparable_entries)
        if not selected_entries:
            lines.append("- No comparable prior runs.")
            return "\n".join(lines) + "\n"

        for entry in selected_entries:
            lines.extend(
                [
                    "",
                    f"### {self._string_value(entry, 'run_id', '(unknown run)')}",
                    f"- Completed at: {self._string_value(entry, 'completed_at', 'unknown')}",
                    f"- Status: {self._string_value(entry, 'status', 'unknown')}",
                    f"- Improved: {self._bool_label(entry.get('improved'))}",
                    f"- Score: {self._format_optional_float(self._float_value(entry.get('score')))}",
                    f"- Score delta: {self._format_optional_float(self._float_value(entry.get('score_delta')))}",
                    f"- Summary: {self._truncate_text(self._string_value(entry, 'codex_response_summary', 'No summary recorded.'))}",
                ]
            )
            failure_note = self._failure_note(entry)
            if failure_note:
                lines.append(f"- Failure note: {failure_note}")

        return "\n".join(lines) + "\n"

    def _select_history_entries(self, comparable_entries: list[dict[str, object]]) -> list[dict[str, object]]:
        recent_entries = comparable_entries[-4:]
        improved_entries = [entry for entry in comparable_entries if bool(entry.get("improved"))][-3:]
        failure_entries = [
            entry for entry in comparable_entries if self._string_value(entry, "status", "") != "improved"
        ][-2:]

        selected_entries: list[dict[str, object]] = []
        seen_run_ids: set[str] = set()

        for group in (recent_entries, improved_entries, failure_entries):
            for entry in reversed(group):
                run_id = self._string_value(entry, "run_id", "")
                if not run_id or run_id in seen_run_ids:
                    continue
                seen_run_ids.add(run_id)
                selected_entries.append(entry)
                if len(selected_entries) >= 10:
                    return selected_entries

        return selected_entries

    def _count_recent_non_improvements(self, comparable_entries: list[dict[str, object]]) -> int:
        streak = 0
        for entry in reversed(comparable_entries):
            if bool(entry.get("improved")):
                break
            streak += 1
        return streak

    def _best_score_from_entries(
        self,
        comparable_entries: list[dict[str, object]],
        optimization_direction: str,
    ) -> float | None:
        scores = [score for entry in comparable_entries if (score := self._float_value(entry.get("score"))) is not None]
        if not scores:
            return None
        if optimization_direction == "minimize":
            return min(scores)
        return max(scores)

    def _latest_status(self, comparable_entries: list[dict[str, object]]) -> str:
        if not comparable_entries:
            return "none"
        return self._string_value(comparable_entries[-1], "status", "unknown")

    def _format_last_improved(self, entry: dict[str, object] | None) -> str:
        if entry is None:
            return "None"

        run_id = self._string_value(entry, "run_id", "(unknown run)")
        score_delta = self._format_optional_float(self._float_value(entry.get("score_delta")))
        return f"{run_id} (delta {score_delta})"

    def _failure_note(self, entry: dict[str, object]) -> str:
        status = self._string_value(entry, "status", "")
        if status in {"improved", "not_improved"}:
            return ""

        stderr = self._string_value(entry, "evaluation_stderr", "")
        if stderr:
            return self._truncate_text(stderr)

        summary = self._string_value(entry, "codex_response_summary", "")
        if summary:
            return self._truncate_text(summary)
        return ""

    def _hash_text(self, value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _string_value(self, entry: dict[str, object], key: str, default: str) -> str:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return default

    def _float_value(self, value: object) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        return None

    def _format_float(self, value: float) -> str:
        return format(value, ".12g")

    def _format_optional_float(self, value: float | None) -> str:
        if value is None:
            return "None"
        return self._format_float(value)

    def _bool_label(self, value: object) -> str:
        return "yes" if bool(value) else "no"

    def _truncate_text(self, value: str, limit: int = 220) -> str:
        normalized = " ".join(value.split())
        if len(normalized) <= limit:
            return normalized or "(empty)"
        return normalized[: limit - 3].rstrip() + "..."

    def _print_edit_policy(self, edit_policy: EditPolicy) -> None:
        editable_text = ", ".join(edit_policy.editable_rule_paths()) or "all repo paths"
        non_editable_text = ", ".join(edit_policy.non_editable_rule_paths()) or "none"
        non_readable_text = ", ".join(edit_policy.non_readable_rule_paths()) or "none"
        print(f"Codex edit policy repo_root={edit_policy.repo_root}")
        print(f"Codex edit policy mode={edit_policy.mode_label}")
        print(f"Codex editable_paths={editable_text}")
        print(f"Codex non_editable_paths={non_editable_text}")
        print(f"Codex non_readable_paths={non_readable_text}")

    def _build_target_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        for key in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH", "CONDA_PREFIX"):
            environment.pop(key, None)

        uv_cache_dir = self._cache_root / "uv"
        uv_cache_dir.mkdir(parents=True, exist_ok=True)
        environment["UV_CACHE_DIR"] = str(uv_cache_dir)
        return environment

    def _append_post_run_review(
        self,
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
        text_paths = self._staged_text_paths_for_log(workspace, worktree_path)
        git_diff = workspace.git_output(worktree_path, "diff", "--cached", "HEAD", "--", *text_paths) if text_paths else ""
        self._session_log.append_post_run_review(
            session_log_path,
            app_server_file_changes=app_server_file_changes,
            git_tracked_changes=git_tracked_changes,
            git_diff=git_diff,
        )

    def _staged_text_paths_for_log(
        self,
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

    def _build_edit_policy(
        self,
        worktree_path: Path,
        session_cwd: Path,
        config: ExperimentRunConfig,
    ) -> EditPolicy:
        return EditPolicy.from_paths(
            worktree_path,
            session_cwd=session_cwd,
            editable_paths=config.editable_paths,
            non_editable_paths=config.non_editable_paths,
            non_readable_paths=config.non_readable_paths,
        )

    def _build_agent_sparse_patterns(
        self,
        workspace: GitWorkspaceManager,
        orchestrator_worktree_path: Path,
        edit_policy: EditPolicy,
        target_relative_path: Path,
    ) -> list[str]:
        patterns = [
            path
            for path in workspace.list_tracked_paths(orchestrator_worktree_path)
            if edit_policy.evaluate_read_path(orchestrator_worktree_path / path).allowed
        ]
        docs_pattern = self._docs_sparse_pattern(target_relative_path)
        if docs_pattern not in patterns:
            patterns.append(docs_pattern)
        return patterns

    def _docs_sparse_pattern(self, target_relative_path: Path) -> str:
        target_prefix = target_relative_path.as_posix().strip("/")
        if not target_prefix or target_prefix == ".":
            return ".nextresearch/"
        return f"{target_prefix}/.nextresearch/"

    def _blocked_commands_for_run(self, config: ExperimentRunConfig) -> tuple[str, ...]:
        blocked_commands: list[str] = [config.evaluation_command]
        for path in config.non_readable_paths:
            stripped = path.strip()
            if not stripped:
                continue
            blocked_commands.append(stripped)
            name = Path(stripped).name
            if name and name != stripped:
                blocked_commands.append(name)
        return tuple(dict.fromkeys(blocked_commands))
