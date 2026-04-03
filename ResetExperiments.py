from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


TARGET_REPO_PATH = "D:/HousePricePrediction"
OBJECTIVE_NAME = "maximize-evaluation-score"
DELETE_ALL_LOGS = True


def reset_experiment_state(
    target_repo_path: str | Path,
    objective_name: str,
    delete_all_logs: bool = False,
    logs_root: str | Path | None = None,
) -> dict[str, int]:
    project_root = Path(__file__).resolve().parent
    resolved_logs_root = Path(logs_root).resolve() if logs_root is not None else project_root / "Logs"
    objective_slug = _slugify(objective_name)
    repo_root = _resolve_repo_root(Path(target_repo_path))
    objective_worktrees_root = resolved_logs_root / "Worktrees" / objective_slug

    removed_registered_worktrees = 0
    removed_branch_count = 0
    removed_session_logs = 0
    removed_ledger_entries = 0

    _run_git(repo_root, "worktree", "prune")

    for worktree_path in _list_registered_worktrees(repo_root):
        if worktree_path == repo_root:
            continue
        if worktree_path.is_relative_to(objective_worktrees_root):
            if worktree_path.exists():
                _run_git(repo_root, "worktree", "remove", "--force", str(worktree_path))
                removed_registered_worktrees += 1

    _run_git(repo_root, "worktree", "prune")

    for branch_name in _list_local_branches(repo_root):
        if branch_name == f"best/{objective_slug}" or branch_name.startswith(f"exp/{objective_slug}/"):
            _run_git(repo_root, "branch", "-D", branch_name)
            removed_branch_count += 1

    removed_worktree_directory = 0
    if objective_worktrees_root.exists():
        shutil.rmtree(objective_worktrees_root)
        removed_worktree_directory = 1

    removed_logs_directory = 0
    if delete_all_logs:
        if resolved_logs_root.exists():
            shutil.rmtree(resolved_logs_root)
            removed_logs_directory = 1
        return {
            "removed_registered_worktrees": removed_registered_worktrees,
            "removed_branches": removed_branch_count,
            "removed_worktree_directory": removed_worktree_directory,
            "removed_ledger_entries": 0,
            "removed_session_logs": 0,
            "removed_logs_directory": removed_logs_directory,
        }

    ledger_path = resolved_logs_root / "codex_experiments.jsonl"
    if ledger_path.exists():
        session_log_paths: set[Path] = set()
        kept_lines: list[str] = []

        with ledger_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                normalized = line.strip()
                if not normalized:
                    continue

                entry = json.loads(normalized)
                entry_objective_name = entry.get("objective_name")
                if isinstance(entry_objective_name, str) and _slugify(entry_objective_name) == objective_slug:
                    session_log_value = entry.get("session_log_path")
                    if isinstance(session_log_value, str) and session_log_value:
                        session_log_paths.add(Path(session_log_value))
                    removed_ledger_entries += 1
                    continue

                kept_lines.append(json.dumps(entry, ensure_ascii=False))

        if kept_lines:
            ledger_path.write_text("\n".join(kept_lines) + "\n", encoding="utf-8")
        else:
            ledger_path.unlink()

        for session_log_path in session_log_paths:
            if session_log_path.exists():
                session_log_path.unlink()
                removed_session_logs += 1

    return {
        "removed_registered_worktrees": removed_registered_worktrees,
        "removed_branches": removed_branch_count,
        "removed_worktree_directory": removed_worktree_directory,
        "removed_ledger_entries": removed_ledger_entries,
        "removed_session_logs": removed_session_logs,
        "removed_logs_directory": removed_logs_directory,
    }


def main() -> None:
    if TARGET_REPO_PATH == "D:/path/to/target-repo":
        raise ValueError("Set TARGET_REPO_PATH in ResetExperiments.py before running this script.")

    summary = reset_experiment_state(
        target_repo_path=TARGET_REPO_PATH,
        objective_name=OBJECTIVE_NAME,
        delete_all_logs=DELETE_ALL_LOGS,
    )

    print(f"Reset completed for objective '{OBJECTIVE_NAME}'.")
    for key, value in summary.items():
        print(f"{key}: {value}")


def _resolve_repo_root(target_repo_path: Path) -> Path:
    target_path = target_repo_path.expanduser().resolve()
    if not target_path.exists():
        raise ValueError(f"target_repo_path does not exist: {target_repo_path}")
    if not target_path.is_dir():
        raise ValueError(f"target_repo_path is not a directory: {target_repo_path}")
    return Path(_run_git(target_path, "rev-parse", "--show-toplevel"))


def _list_registered_worktrees(repo_root: Path) -> list[Path]:
    output = _run_git(repo_root, "worktree", "list", "--porcelain")
    worktree_paths: list[Path] = []

    for block in output.split("\n\n"):
        lines = [line for line in block.splitlines() if line]
        if not lines:
            continue
        first_line = lines[0]
        if first_line.startswith("worktree "):
            worktree_paths.append(Path(first_line.removeprefix("worktree ")).resolve())

    return worktree_paths


def _list_local_branches(repo_root: Path) -> list[str]:
    output = _run_git(repo_root, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    return [line.strip() for line in output.splitlines() if line.strip()]


def _run_git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {stderr}")
    return completed.stdout.strip()


def _slugify(value: str) -> str:
    cleaned = []
    previous_was_separator = False

    for character in value.strip().lower():
        if character.isalnum() or character in "._/-":
            cleaned.append(character)
            previous_was_separator = False
            continue
        if previous_was_separator:
            continue
        cleaned.append("-")
        previous_was_separator = True

    slug = "".join(cleaned).strip("-./")
    if not slug:
        raise ValueError("objective_name must contain at least one alphanumeric character.")
    return slug


if __name__ == "__main__":
    main()
