from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class PrepareContextRequest:
    objective: str
    codebase_path: str
    editable_roots: tuple[str, ...] = field(default_factory=tuple)
    forbidden_roots: tuple[str, ...] = field(default_factory=tuple)
    codex_thread_id: str | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class PrepareContextWorkflowConfig:
    runs_root: Path | str | None = None


@dataclass(frozen=True)
class PrepareContextResult:
    run_id: str
    codex_thread_id: str | None
    context_summary: str | None
    context_artifact_path: str | None
    success: bool
    failure_reason: str | None
