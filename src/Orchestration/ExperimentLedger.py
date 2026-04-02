from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .Models import BootstrapArtifacts, ExperimentIterationResult


class ExperimentLedger:
    def __init__(self, ledger_path: Path) -> None:
        self._ledger_path = ledger_path
        self._ledger_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def ledger_path(self) -> Path:
        return self._ledger_path

    def append_entry(
        self,
        result: ExperimentIterationResult,
        target_repo_path: Path,
        worktree_path: Path,
        evaluation_command: str,
        docs_dir: Path,
        bootstrap_artifacts: BootstrapArtifacts,
    ) -> None:
        entry = {
            "run_id": result.run_id,
            "objective_name": result.objective_name,
            "target_repo_path": str(target_repo_path),
            "branch_name": result.branch_name,
            "best_branch_name": result.best_branch_name,
            "worktree_path": str(worktree_path.resolve()),
            "base_commit": result.base_commit,
            "result_commit": result.result_commit,
            "status": result.status,
            "improved": result.improved,
            "score": result.score,
            "score_delta": result.score_delta,
            "evaluation_command": evaluation_command,
            "session_log_path": str(result.session_log_path) if result.session_log_path else None,
            "running_instructions_path": str(docs_dir / "RUNNING_INSTRUCTIONS.md"),
            "evaluation_spec_path": str(docs_dir / "EVALUATION_SPEC.md"),
            "running_instructions_hash": self._hash_text(bootstrap_artifacts.running_instructions),
            "evaluation_spec_hash": self._hash_text(bootstrap_artifacts.evaluation_spec),
            "codex_response_summary": result.response_text,
            "evaluation_stdout": result.evaluation_stdout,
            "evaluation_stderr": result.evaluation_stderr,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def load_entries(self, objective_name: str | None = None) -> list[dict[str, object]]:
        if not self._ledger_path.exists():
            return []

        entries: list[dict[str, object]] = []
        with self._ledger_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                normalized = line.strip()
                if not normalized:
                    continue
                entry = json.loads(normalized)
                if objective_name and entry.get("objective_name") != objective_name:
                    continue
                entries.append(entry)
        return entries

    def _hash_text(self, value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
