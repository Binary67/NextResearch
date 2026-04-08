from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .Models import ExperimentOrchestratorError


class GitWorkspaceManager:
    def __init__(self, repo_root: Path, worktrees_root: Path) -> None:
        self._repo_root = repo_root
        self._worktrees_root = worktrees_root

    @property
    def repo_root(self) -> Path:
        return self._repo_root

    @property
    def worktrees_root(self) -> Path:
        return self._worktrees_root

    def ensure_clean_repo(self) -> None:
        status_output = self.git_output(self._repo_root, "status", "--porcelain")
        if status_output:
            raise ExperimentOrchestratorError(
                f"Target repository must be clean before running experiments: {self._repo_root}"
            )

    def branch_exists(self, branch_name: str) -> bool:
        completed = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
            cwd=self._repo_root,
        )
        return completed.returncode == 0

    def current_branch(self) -> str:
        return self.git_output(self._repo_root, "branch", "--show-current")

    def rev_parse(self, ref: str, cwd: Path | None = None) -> str:
        return self.git_output(cwd or self._repo_root, "rev-parse", ref)

    def create_detached_worktree(self, worktree_path: Path, ref: str) -> None:
        if worktree_path.exists():
            shutil.rmtree(worktree_path)
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        self.run_git(self._repo_root, "worktree", "add", "--detach", str(worktree_path), ref)

    def create_experiment_worktree(self, branch_name: str, worktree_path: Path, base_commit: str) -> None:
        self.run_git(self._repo_root, "branch", branch_name, base_commit)
        if worktree_path.exists():
            shutil.rmtree(worktree_path)
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        self.run_git(self._repo_root, "worktree", "add", str(worktree_path), branch_name)

    def remove_worktree(self, worktree_path: Path) -> None:
        if not worktree_path.exists():
            return
        self.run_git(self._repo_root, "worktree", "remove", "--force", str(worktree_path))
        self.run_git(self._repo_root, "worktree", "prune")

    def delete_branch(self, branch_name: str) -> None:
        if self.branch_exists(branch_name):
            self.run_git(self._repo_root, "branch", "-D", branch_name)

    def force_branch(self, branch_name: str, target_commit: str) -> None:
        self.run_git(self._repo_root, "branch", "-f", branch_name, target_commit)

    def commit_worktree_if_needed(
        self,
        worktree_path: Path,
        branch_name: str,
        objective_slug: str,
        run_id: str,
    ) -> str | None:
        status_output = self.git_output(worktree_path, "status", "--porcelain")
        if not status_output:
            return None

        self.run_git(worktree_path, "add", "-A")
        commit_message = f"Codex experiment {objective_slug} {run_id}"
        self.run_git(worktree_path, "commit", "-m", commit_message)
        return self.git_output(worktree_path, "rev-parse", branch_name)

    def diff_against_ref(
        self,
        worktree_path: Path,
        ref: str,
        exclude_paths: tuple[str, ...] = (),
    ) -> bytes:
        self.run_git(worktree_path, "add", "--sparse", "-A")
        args = ["diff", "--cached", "--binary", ref]
        if exclude_paths:
            args.extend(["--", "."])
            args.extend(f":(exclude){path}" for path in exclude_paths if path)
        return self.git_output_bytes(worktree_path, *args)

    def apply_patch(self, worktree_path: Path, patch: bytes) -> None:
        if not patch.strip():
            return

        completed = subprocess.run(
            ["git", "apply", "--binary", "--whitespace=nowarn"],
            cwd=worktree_path,
            input=patch,
            capture_output=True,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            stdout = completed.stdout.decode("utf-8", errors="replace").strip()
            message = stderr or stdout or "git apply failed"
            raise ExperimentOrchestratorError(f"Patch application failed: {message}")

    def reset_worktree_to_ref(self, worktree_path: Path, ref: str, clean_untracked: bool = False) -> None:
        self.run_git(worktree_path, "reset", "--hard", ref)
        if clean_untracked:
            self.run_git(worktree_path, "clean", "-fd")

    def git_output(self, cwd: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            capture_output=True,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip() or completed.stdout.strip()
            raise ExperimentOrchestratorError(f"git {' '.join(args)} failed: {stderr}")
        return completed.stdout.strip()

    def git_output_bytes(self, cwd: Path, *args: str) -> bytes:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            stdout = completed.stdout.decode("utf-8", errors="replace").strip()
            message = stderr or stdout
            raise ExperimentOrchestratorError(f"git {' '.join(args)} failed: {message}")
        return completed.stdout

    def run_git(self, cwd: Path, *args: str) -> None:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            capture_output=True,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip() or completed.stdout.strip()
            raise ExperimentOrchestratorError(f"git {' '.join(args)} failed: {stderr}")

