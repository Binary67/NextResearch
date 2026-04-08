from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .Models import ExperimentIterationResult


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
        evaluation_key: str,
        optimization_direction: str,
    ) -> None:
        notes = self._build_notes(result)
        entry = {
            "run_id": result.run_id,
            "status": result.status,
            "improved": result.improved,
            "score": result.score,
            "score_delta": result.score_delta,
            "objective_name": result.objective_name,
            "target_repo_path": str(target_repo_path),
            "branch_name": result.branch_name,
            "best_branch_name": result.best_branch_name,
            "worktree_path": str(worktree_path.resolve()),
            "base_commit": result.base_commit,
            "result_commit": result.result_commit,
            "evaluation_key": evaluation_key,
            "optimization_direction": optimization_direction,
            "session_log_path": str(result.session_log_path) if result.session_log_path else None,
            "response_text": result.response_text,
            "strategy": result.strategy,
            "why_it_should_help": result.why_it_should_help,
            "files_changed": list(result.changed_files),
            "notes": notes,
            "evaluation_stdout": result.evaluation_stdout,
            "evaluation_stderr": result.evaluation_stderr,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def load_entries(
        self,
        objective_name: str | None = None,
        evaluation_key: str | None = None,
    ) -> list[dict[str, object]]:
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
                if evaluation_key and entry.get("evaluation_key") != evaluation_key:
                    continue
                entries.append(entry)
        return entries

    def _build_notes(self, result: ExperimentIterationResult) -> list[str]:
        notes: list[str] = []
        seen_notes: set[str] = set()

        for note in result.run_notes:
            normalized = self._normalize_text(note)
            if normalized and normalized not in seen_notes:
                seen_notes.add(normalized)
                notes.append(normalized)

        if result.status not in {"improved", "not_improved"}:
            fallback = self._normalize_text(result.evaluation_stderr) or self._normalize_text(result.response_text)
            if fallback and fallback not in seen_notes:
                notes.append(fallback)

        return notes

    def _normalize_text(self, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            return ""
        return " ".join(stripped.split())
