from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.Agents.Codex import CodexAgent, CodexAgentError, CodexTurnResult

from .EvaluationRunner import EvaluationRunner
from .ExperimentLedger import ExperimentLedger
from .GitWorkspace import GitWorkspaceManager
from .Models import BootstrapArtifacts, ExperimentIterationResult, ExperimentOrchestratorError, ExperimentRunConfig


@dataclass(frozen=True)
class _RepoContext:
    repo_root: Path
    target_path: Path
    target_relative_path: Path


@dataclass(frozen=True)
class _SessionRunResult:
    turn_result: CodexTurnResult
    session_log_path: Path | None


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
        self._worktrees_root = Path(worktrees_root) if worktrees_root is not None else self._logs_root / "Worktrees"
        self._worktrees_root.mkdir(parents=True, exist_ok=True)
        self._ledger = ExperimentLedger(self._logs_root / "codex_experiments.jsonl")
        self._evaluation_runner = EvaluationRunner()

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
        running_result: _SessionRunResult | None = None
        evaluation_result: _SessionRunResult | None = None
        running_log_path: Path | None = None
        evaluation_log_path: Path | None = None

        workspace.create_detached_worktree(worktree_path, bootstrap_ref)
        target_cwd = worktree_path / context.target_relative_path

        try:
            running_result = self._run_codex_session(target_cwd, self._build_running_instructions_prompt())
            running_log_path = running_result.session_log_path

            evaluation_result = self._run_codex_session(
                target_cwd,
                self._build_evaluation_spec_prompt(
                    evaluation_command=evaluation_command,
                    evaluation_relative_path=evaluation_relative_path,
                ),
            )
            evaluation_log_path = evaluation_result.session_log_path
        finally:
            workspace.remove_worktree(worktree_path)

        if running_result is None or evaluation_result is None:
            raise ExperimentOrchestratorError("Bootstrap sessions did not complete successfully.")

        return BootstrapArtifacts(
            running_instructions=self._normalize_document_text(running_result.turn_result.response_text),
            evaluation_spec=self._normalize_document_text(evaluation_result.turn_result.response_text),
            running_session_log_path=running_log_path,
            evaluation_session_log_path=evaluation_log_path,
        )

    def run_iterations(self, config: ExperimentRunConfig) -> list[ExperimentIterationResult]:
        if config.iteration_count < 1:
            raise ValueError("iteration_count must be at least 1.")

        context = self._resolve_repo_context(config.target_repo_path)
        objective_slug = self._slugify(config.objective_name)
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
        )
        results: list[ExperimentIterationResult] = []

        for _ in range(config.iteration_count):
            current_base_ref = best_branch_name if workspace.branch_exists(best_branch_name) else start_ref
            current_base_commit = workspace.rev_parse(current_base_ref)
            run_id = self._timestamp_token()
            branch_name = f"exp/{objective_slug}/{run_id}"
            worktree_path = self._worktrees_root / objective_slug / run_id
            run_cwd = worktree_path / context.target_relative_path
            docs_dir = run_cwd / ".nextresearch"
            score: float | None = None
            score_delta: float | None = None
            improved = False
            status = "failed"
            result_commit: str | None = None
            response_text = ""
            session_log_path: Path | None = None
            evaluation_stdout = ""
            evaluation_stderr = ""

            try:
                workspace.create_experiment_worktree(branch_name, worktree_path, current_base_commit)
                self._write_run_docs(docs_dir, bootstrap_artifacts)
                session_result = self._run_codex_session(
                    run_cwd,
                    self._build_experiment_prompt(objective_name=config.objective_name),
                )
                response_text = session_result.turn_result.response_text
                session_log_path = session_result.session_log_path

                evaluation_outcome = self._evaluation_runner.run(run_cwd, config.evaluation_command)
                score = evaluation_outcome.score
                evaluation_stdout = evaluation_outcome.stdout
                evaluation_stderr = evaluation_outcome.stderr
                score_delta = score - best_score
                improved = score > best_score
                status = "improved" if improved else "not_improved"

                self._remove_run_docs(docs_dir)

                if improved:
                    result_commit = workspace.commit_worktree_if_needed(
                        worktree_path=worktree_path,
                        branch_name=branch_name,
                        objective_slug=objective_slug,
                        run_id=run_id,
                    )
                    if result_commit is None:
                        result_commit = current_base_commit
                    workspace.force_branch(best_branch_name, result_commit)
                    best_score = score
                else:
                    result_commit = workspace.rev_parse("HEAD", cwd=worktree_path)
            except CodexAgentError as exc:
                status = "codex_failed"
                response_text = str(exc)
                self._remove_run_docs(docs_dir)
            except Exception as exc:
                if isinstance(exc, ExperimentOrchestratorError):
                    status = "evaluation_failed"
                self._remove_run_docs(docs_dir)
                if not response_text:
                    response_text = str(exc)
                if not evaluation_stderr:
                    evaluation_stderr = str(exc)
            finally:
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
                self._ledger.append_entry(
                    result=result,
                    target_repo_path=context.target_path,
                    worktree_path=worktree_path,
                    evaluation_command=config.evaluation_command,
                    docs_dir=docs_dir,
                    bootstrap_artifacts=bootstrap_artifacts,
                )
                self._cleanup_experiment_workspace(workspace, worktree_path, branch_name)
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

    def _run_codex_session(self, cwd: Path, instruction: str) -> _SessionRunResult:
        agent = CodexAgent(codex_executable=self._codex_executable, logs_root=self._logs_root)
        try:
            agent.start_session(str(cwd))
            turn_result = agent.run_instruction(instruction)
            session_log_path = agent.session_log_path
            agent.end_session()
        finally:
            agent.close()
        return _SessionRunResult(turn_result=turn_result, session_log_path=session_log_path)

    def _build_running_instructions_prompt(self) -> str:
        return self._load_prompt_template("Running Instructions Prompt")

    def _build_evaluation_spec_prompt(self, evaluation_command: str, evaluation_relative_path: str) -> str:
        return self._load_prompt_template("Evaluation Spec Prompt").format(
            evaluation_command=evaluation_command,
            evaluation_relative_path=evaluation_relative_path,
        )

    def _build_experiment_prompt(self, objective_name: str) -> str:
        return self._load_prompt_template("Experiment Prompt").format(
            objective_name=objective_name,
            running_instructions_path=".nextresearch/RUNNING_INSTRUCTIONS.md",
            evaluation_spec_path=".nextresearch/EVALUATION_SPEC.md",
        )

    def _load_prompt_template(self, section_title: str) -> str:
        prompt_templates_path = Path(__file__).resolve().parents[2] / "PromptTemplates.md"
        if not prompt_templates_path.exists():
            raise ExperimentOrchestratorError(f"Prompt templates file not found: {prompt_templates_path}")

        content = prompt_templates_path.read_text(encoding="utf-8")
        required_sections = (
            "Running Instructions Prompt",
            "Evaluation Spec Prompt",
            "Experiment Prompt",
        )
        heading_matches: list[tuple[int, int, str]] = []

        for title in required_sections:
            pattern = rf"^# {re.escape(title)}\s*$"
            matches = list(re.finditer(pattern, content, flags=re.MULTILINE))
            if not matches:
                raise ExperimentOrchestratorError(f'Missing prompt section "{title}" in {prompt_templates_path}')
            if len(matches) > 1:
                raise ExperimentOrchestratorError(f'Duplicate prompt section "{title}" in {prompt_templates_path}')
            match = matches[0]
            heading_matches.append((match.start(), match.end(), title))

        heading_matches.sort(key=lambda item: item[0])
        sections: dict[str, str] = {}
        for index, (_, heading_end, title) in enumerate(heading_matches):
            next_start = heading_matches[index + 1][0] if index + 1 < len(heading_matches) else len(content)
            sections[title] = content[heading_end:next_start].strip()

        template = sections.get(section_title)
        if template is None:
            raise ExperimentOrchestratorError(
                f'Missing prompt section "{section_title}" in {prompt_templates_path}'
            )
        if not template:
            raise ExperimentOrchestratorError(
                f'Prompt section "{section_title}" is empty in {prompt_templates_path}'
            )
        return template

    def _normalize_document_text(self, value: str) -> str:
        text = value.strip()
        if text.startswith("```") and text.endswith("```"):
            lines = text.splitlines()
            if len(lines) >= 3:
                text = "\n".join(lines[1:-1]).strip()
        return text + "\n"

    def _score_reference(
        self,
        context: _RepoContext,
        objective_slug: str,
        ref: str,
        evaluation_command: str,
        workspace: GitWorkspaceManager,
    ) -> float:
        worktree_path = self._worktrees_root / objective_slug / f"score-{self._timestamp_token()}"
        workspace.create_detached_worktree(worktree_path, ref)
        target_cwd = worktree_path / context.target_relative_path
        try:
            return self._evaluation_runner.run(target_cwd, evaluation_command).score
        finally:
            workspace.remove_worktree(worktree_path)

    def _write_run_docs(self, docs_dir: Path, artifacts: BootstrapArtifacts) -> None:
        docs_dir.mkdir(parents=True, exist_ok=True)
        (docs_dir / "RUNNING_INSTRUCTIONS.md").write_text(artifacts.running_instructions, encoding="utf-8")
        (docs_dir / "EVALUATION_SPEC.md").write_text(artifacts.evaluation_spec, encoding="utf-8")

    def _remove_run_docs(self, docs_dir: Path) -> None:
        if docs_dir.exists():
            shutil.rmtree(docs_dir)

    def _cleanup_experiment_workspace(
        self,
        workspace: GitWorkspaceManager,
        worktree_path: Path,
        branch_name: str,
    ) -> None:
        workspace.remove_worktree(worktree_path)
        workspace.delete_branch(branch_name)

    def _slugify(self, value: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9._/-]+", "-", value.strip().lower()).strip("-./")
        if not slug:
            raise ValueError("objective_name must contain at least one alphanumeric character.")
        return slug

    def _timestamp_token(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")

    def _default_logs_root(self) -> Path:
        return Path(__file__).resolve().parents[2] / "Logs"
