from __future__ import annotations

from typing import TypedDict


class PrepareContextState(TypedDict, total=False):
    objective: str
    codebase_path: str
    editable_roots: list[str]
    forbidden_roots: list[str]
    codex_thread_id: str | None
    run_id: str
    run_directory: str
    context_summary: str | None
    context_artifact_path: str | None
    metadata_artifact_path: str | None
    created_at: str
    node_status: str
    failure_reason: str | None
