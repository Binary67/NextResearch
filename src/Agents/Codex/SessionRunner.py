from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from src.EditPolicy import EditPolicy

from .Agent import CodexAgent, CodexTurnResult


@dataclass(frozen=True)
class CodexSessionRunResult:
    turn_result: CodexTurnResult
    session_log_path: Path | None


@dataclass(frozen=True)
class _GitPathSnapshot:
    status: str
    fingerprint: str


class CodexSessionRunner:
    def __init__(
        self,
        codex_executable: str | None = None,
        logs_root: Path | str | None = None,
    ) -> None:
        self._codex_executable = codex_executable
        self._logs_root = logs_root

    def run(
        self,
        cwd: Path,
        instruction: str,
        edit_policy: EditPolicy | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> CodexSessionRunResult:
        agent = CodexAgent(
            codex_executable=self._codex_executable,
            logs_root=self._logs_root,
            edit_policy=edit_policy,
            environment=environment,
        )
        try:
            agent.start_session(str(cwd))
            baseline_snapshot = self._snapshot_git_changes(cwd) if edit_policy is not None else {}
            turn_result = agent.run_instruction(instruction)
            session_log_path = agent.session_log_path
            if edit_policy is not None:
                current_snapshot = self._snapshot_git_changes(cwd)
                changed_paths = self._collect_session_changed_paths(baseline_snapshot, current_snapshot)
                violations = edit_policy.find_disallowed_paths(changed_paths)
                if violations:
                    violation_message = "; ".join(
                        f"{entry.display_path}: {entry.reason}" for entry in violations
                    )
                    agent.append_policy_violation(violation_message)
                    raise agent.build_error(f"Codex modified disallowed paths: {violation_message}")
            agent.end_session()
        finally:
            agent.close()

        return CodexSessionRunResult(turn_result=turn_result, session_log_path=session_log_path)

    def _snapshot_git_changes(self, cwd: Path) -> dict[str, _GitPathSnapshot]:
        completed = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all", "-z"],
            cwd=cwd,
            capture_output=True,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            stdout = completed.stdout.decode("utf-8", errors="replace").strip()
            message = stderr or stdout or "git status failed"
            raise RuntimeError(f"Could not inspect git worktree changes: {message}")
        return self._parse_git_status_porcelain(cwd, completed.stdout)

    def _parse_git_status_porcelain(self, cwd: Path, output: bytes) -> dict[str, _GitPathSnapshot]:
        if not output:
            return {}

        entries = output.split(b"\0")
        snapshots: dict[str, _GitPathSnapshot] = {}
        index = 0

        while index < len(entries):
            raw_entry = entries[index]
            index += 1
            if not raw_entry:
                continue

            entry = raw_entry.decode("utf-8", errors="replace")
            if len(entry) < 3:
                continue

            status = entry[:2]
            primary_path = entry[3:]
            self._record_snapshot(snapshots, cwd, primary_path, status)

            if "R" in status or "C" in status:
                if index < len(entries):
                    secondary_entry = entries[index].decode("utf-8", errors="replace")
                    index += 1
                    self._record_snapshot(snapshots, cwd, secondary_entry, status)

        return snapshots

    def _collect_session_changed_paths(
        self,
        baseline_snapshot: dict[str, _GitPathSnapshot],
        current_snapshot: dict[str, _GitPathSnapshot],
    ) -> list[str]:
        changed_paths: list[str] = []
        for path in sorted(set(baseline_snapshot) | set(current_snapshot)):
            if baseline_snapshot.get(path) == current_snapshot.get(path):
                continue
            changed_paths.append(path)
        return changed_paths

    def _record_snapshot(
        self,
        snapshots: dict[str, _GitPathSnapshot],
        cwd: Path,
        path: str,
        status: str,
    ) -> None:
        if not path:
            return
        snapshots[path] = _GitPathSnapshot(
            status=status,
            fingerprint=self._fingerprint_path(cwd, path),
        )

    def _fingerprint_path(self, cwd: Path, path: str) -> str:
        candidate = (cwd / Path(path)).resolve(strict=False)
        if not candidate.exists():
            return "missing"
        if candidate.is_dir():
            return "dir"

        digest = hashlib.sha256()
        with candidate.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
