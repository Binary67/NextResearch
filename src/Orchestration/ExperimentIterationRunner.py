from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Mapping

from src.Agents.Codex import (
    CodexAgentError,
    CodexDynamicTool,
    CodexSessionRunner,
    DynamicToolCallRequest,
    DynamicToolCallResult,
)
from src.Agents.Codex.SessionLog import CodexSessionLog
from src.EditPolicy import EditPolicy

from .EvaluationRunner import EvaluationRunner
from .ExperimentBootstrap import resolve_evaluation_file_path
from .ExperimentEvalTool import ORCHESTRATOR_RUN_EVAL_TOOL, ExperimentEvalTool
from .ExperimentLedger import ExperimentLedger
from .ExperimentPrompts import build_experiment_prompt
from .ExperimentRunDocs import build_run_docs
from .ExperimentRunSupport import (
    append_post_run_review,
    blocked_commands_for_run,
    build_effective_non_readable_paths,
    build_agent_sparse_patterns,
    build_edit_policy,
    cleanup_experiment_workspaces,
    docs_excluded_patch_paths,
    print_edit_policy,
    remove_run_docs,
    write_run_docs,
)
from .GitWorkspace import GitWorkspaceManager
from .Models import BootstrapArtifacts, ExperimentIterationResult, ExperimentOrchestratorError, ExperimentRunConfig


def score_reference(
    *,
    target_relative_path: Path,
    objective_slug: str,
    ref: str,
    evaluation_command: str,
    score_id: str,
    workspace: GitWorkspaceManager,
    worktrees_root: Path,
    evaluation_runner: EvaluationRunner,
    environment: Mapping[str, str],
) -> float:
    worktree_path = worktrees_root / objective_slug / score_id
    workspace.create_detached_worktree(worktree_path, ref)
    target_cwd = worktree_path / target_relative_path
    try:
        return evaluation_runner.run(target_cwd, evaluation_command, environment=environment).score
    finally:
        workspace.remove_worktree(worktree_path)


def run_iteration(
    *,
    config: ExperimentRunConfig,
    target_path: Path,
    target_relative_path: Path,
    objective_slug: str,
    run_id: str,
    workspace: GitWorkspaceManager,
    worktrees_root: Path,
    target_environment: Mapping[str, str],
    bootstrap_artifacts: BootstrapArtifacts,
    best_branch_name: str,
    start_ref: str,
    best_score: float,
    ledger: ExperimentLedger,
    evaluation_runner: EvaluationRunner,
    codex_session_runner: CodexSessionRunner,
    session_log: CodexSessionLog,
) -> ExperimentIterationResult:
    current_base_ref = best_branch_name if workspace.branch_exists(best_branch_name) else start_ref
    current_base_commit = workspace.rev_parse(current_base_ref)
    evaluation_file = resolve_evaluation_file_path(
        target_path,
        config.evaluation_command,
        config.evaluation_file_path,
    )
    evaluation_relative_path = evaluation_file.relative_to(target_path)
    effective_non_readable_paths = build_effective_non_readable_paths(
        target_relative_path,
        evaluation_relative_path,
        config.non_readable_paths,
    )
    branch_name = f"exp/{objective_slug}/{run_id}"
    run_root = worktrees_root / objective_slug / run_id
    orchestrator_worktree_path = run_root / "orchestrator"
    agent_worktree_path = run_root / "agent"
    orchestrator_cwd = orchestrator_worktree_path / target_relative_path
    agent_cwd = agent_worktree_path / target_relative_path
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
    preserve_branch = False
    retained_modified_candidate = False

    try:
        workspace.create_experiment_worktree(branch_name, orchestrator_worktree_path, current_base_commit)
        orchestrator_edit_policy = build_edit_policy(
            orchestrator_worktree_path,
            orchestrator_cwd,
            target_relative_path,
            config.editable_paths,
            config.non_editable_paths,
            effective_non_readable_paths,
        )
        sparse_patterns = build_agent_sparse_patterns(
            workspace,
            orchestrator_worktree_path,
            orchestrator_edit_policy,
            target_relative_path,
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
                target_repo_path=target_path,
                bootstrap_artifacts=bootstrap_artifacts,
                current_base_ref=current_base_ref,
                current_base_commit=current_base_commit,
                best_branch_name=best_branch_name,
                best_score=best_score,
                ledger_entries=ledger.load_entries(),
            ),
        )
        agent_edit_policy = build_edit_policy(
            agent_worktree_path,
            agent_cwd,
            target_relative_path,
            config.editable_paths,
            config.non_editable_paths,
            effective_non_readable_paths,
        )
        print_edit_policy(agent_edit_policy)
        eval_tool = ExperimentEvalTool(
            workspace=workspace,
            agent_worktree_path=agent_worktree_path,
            orchestrator_worktree_path=orchestrator_worktree_path,
            target_relative_path=target_relative_path,
            current_base_commit=current_base_commit,
            evaluation_command=config.evaluation_command,
            optimization_direction=config.optimization_direction,
            best_score=best_score,
            start_score=best_score,
            evaluation_runner=evaluation_runner,
            environment=target_environment,
            budget=config.agent_eval_budget,
            excluded_patch_paths=docs_excluded_patch_paths(target_relative_path),
        )
        session_result = codex_session_runner.run(
            agent_cwd,
            _build_experiment_instruction(config.objective_name, config.agent_eval_budget, agent_edit_policy),
            edit_policy=agent_edit_policy,
            environment=target_environment,
            blocked_commands=blocked_commands_for_run(config.evaluation_command, effective_non_readable_paths),
            dynamic_tools=(_build_eval_dynamic_tool(eval_tool),),
        )
        response_text = session_result.turn_result.response_text
        strategy, why_it_should_help = _build_summary_fields(session_result.turn_result.response_text)
        session_log_path = session_result.session_log_path
        changed_files = _build_changed_files(session_result.turn_result.file_changes)
        run_notes = tuple(session_result.turn_result.errors_and_recoveries)
        app_server_file_changes = len(session_result.turn_result.file_changes)
        remove_run_docs(docs_dir)
        final_evaluation = eval_tool.finalize_candidate()
        score = final_evaluation.score
        evaluation_stdout = final_evaluation.stdout
        evaluation_stderr = final_evaluation.stderr
        if final_evaluation.failure_message is not None:
            raise ExperimentOrchestratorError(final_evaluation.failure_message)
        if score is None:
            raise ExperimentOrchestratorError("Final evaluation did not produce a score.")
        score_delta = _score_delta(score, best_score, config.optimization_direction)
        improved = _is_improvement(score, best_score, config.optimization_direction)
        status = "improved" if improved else "not_improved"
        retained_modified_candidate = final_evaluation.retained_modified_candidate
        preserve_branch = retained_modified_candidate and not improved
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
                session_log=session_log,
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
    elif retained_modified_candidate:
        result_commit = workspace.commit_worktree_if_needed(
            worktree_path=orchestrator_worktree_path,
            branch_name=branch_name,
            objective_slug=objective_slug,
            run_id=run_id,
        )
        if result_commit is None:
            result_commit = workspace.rev_parse("HEAD", cwd=orchestrator_worktree_path)
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
        ledger.append_entry(
            result=result,
            target_repo_path=target_path,
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
            preserve_branch=preserve_branch,
        )
    return result


def _is_improvement(score: float, best_score: float, optimization_direction: str) -> bool:
    if optimization_direction == "minimize":
        return score < best_score
    if optimization_direction == "maximize":
        return score > best_score
    raise ValueError(f"Unsupported optimization_direction: {optimization_direction}")


def _score_delta(score: float, best_score: float, optimization_direction: str) -> float:
    if optimization_direction == "minimize":
        return best_score - score
    if optimization_direction == "maximize":
        return score - best_score
    raise ValueError(f"Unsupported optimization_direction: {optimization_direction}")


def _build_experiment_instruction(
    objective_name: str,
    agent_eval_budget: int,
    edit_policy: EditPolicy,
) -> str:
    return (
        f"{edit_policy.prompt_prefix()}\n\n"
        f"{build_experiment_prompt(objective_name=objective_name, agent_eval_budget=agent_eval_budget)}"
    )


def _build_eval_dynamic_tool(eval_tool: ExperimentEvalTool) -> CodexDynamicTool:
    return CodexDynamicTool(
        name=ORCHESTRATOR_RUN_EVAL_TOOL,
        description=(
            "Run the orchestrator-managed evaluation on the current candidate state and return sanitized score "
            "feedback without exposing evaluator internals."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        handler=lambda request: _run_eval_dynamic_tool(eval_tool, request),
    )


def _run_eval_dynamic_tool(eval_tool: ExperimentEvalTool, request: DynamicToolCallRequest) -> DynamicToolCallResult:
    if request.arguments not in (None, {}):
        return DynamicToolCallResult(
            text=json.dumps(
                {
                    "status": "evaluation_failed",
                    "score": None,
                    "delta_vs_best": None,
                    "delta_vs_start": None,
                    "budget_remaining": eval_tool.budget_remaining,
                    "note": "This tool does not accept arguments.",
                },
                ensure_ascii=False,
                indent=2,
            ),
            success=False,
        )
    return DynamicToolCallResult(text=eval_tool.evaluate_current_candidate().to_tool_text())


def _build_summary_fields(response_text: str) -> tuple[str, str]:
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


def _build_changed_files(file_changes: list[object]) -> tuple[str, ...]:
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
