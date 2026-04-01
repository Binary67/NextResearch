from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from src.Orchestration.PrepareContext.Prompts import build_prepare_context_prompt
from src.Orchestration.PrepareContext.State import PrepareContextState
from src.Orchestration.Runs import RunArtifactsManager


class CodexSessionAgent(Protocol):
    @property
    def thread_id(self) -> str | None: ...

    def start_session(self, cwd: str) -> None: ...

    def resume_session(self, thread_id: str) -> None: ...

    def run_instruction(self, instruction: str) -> str: ...


class PrepareContextNodes:
    def __init__(self, codex_agent: CodexSessionAgent, run_artifacts: RunArtifactsManager) -> None:
        self._codex_agent = codex_agent
        self._run_artifacts = run_artifacts

    def prepare_context_inputs(self, state: PrepareContextState) -> PrepareContextState:
        run_id = state.get("run_id") or self._run_artifacts.generate_run_id()
        patch: PrepareContextState = {"run_id": run_id}

        objective = (state.get("objective") or "").strip()
        if not objective:
            patch["node_status"] = "failed"
            patch["failure_reason"] = "objective must be a non-empty string."
            return patch

        codebase_path = self._normalize_directory(state.get("codebase_path"))
        if codebase_path is None:
            patch["node_status"] = "failed"
            patch["failure_reason"] = "codebase_path must point to an existing directory."
            return patch

        editable_roots = self._normalize_roots(
            roots=state.get("editable_roots") or [str(codebase_path)],
            codebase_path=codebase_path,
            field_name="editable_roots",
        )
        if isinstance(editable_roots, str):
            patch["node_status"] = "failed"
            patch["failure_reason"] = editable_roots
            return patch

        forbidden_roots = self._normalize_roots(
            roots=state.get("forbidden_roots") or [],
            codebase_path=codebase_path,
            field_name="forbidden_roots",
        )
        if isinstance(forbidden_roots, str):
            patch["node_status"] = "failed"
            patch["failure_reason"] = forbidden_roots
            return patch

        run_directory = self._run_artifacts.create_run_directory(run_id)
        patch.update(
            {
                "objective": objective,
                "codebase_path": str(codebase_path),
                "editable_roots": editable_roots,
                "forbidden_roots": forbidden_roots,
                "run_directory": str(run_directory),
                "created_at": self._timestamp(),
                "node_status": "inputs_prepared",
                "failure_reason": None,
            }
        )
        return patch

    def start_codex_context_session(self, state: PrepareContextState) -> PrepareContextState:
        if state.get("failure_reason"):
            return {}

        try:
            existing_thread_id = state.get("codex_thread_id")
            if existing_thread_id:
                if self._codex_agent.thread_id != existing_thread_id:
                    self._codex_agent.resume_session(existing_thread_id)
            else:
                self._codex_agent.start_session(state["codebase_path"])

            thread_id = self._codex_agent.thread_id
            if not thread_id:
                raise RuntimeError("Codex session did not expose a thread id.")

            return {
                "codex_thread_id": thread_id,
                "node_status": "context_session_started",
            }
        except Exception as exc:
            return {
                "node_status": "failed",
                "failure_reason": str(exc),
            }

    def codex_read_codebase(self, state: PrepareContextState) -> PrepareContextState:
        if state.get("failure_reason"):
            return {}

        prompt = build_prepare_context_prompt(
            objective=state["objective"],
            editable_roots=state["editable_roots"],
            forbidden_roots=state["forbidden_roots"],
        )

        try:
            summary = self._codex_agent.run_instruction(prompt).strip()
            if not summary:
                raise RuntimeError("Codex returned an empty context summary.")

            return {
                "context_summary": summary,
                "node_status": "context_summary_created",
            }
        except Exception as exc:
            return {
                "node_status": "failed",
                "failure_reason": str(exc),
            }

    def persist_context_summary(self, state: PrepareContextState) -> PrepareContextState:
        if state.get("failure_reason"):
            return {}

        try:
            context_artifact_path = self._run_artifacts.write_context_summary(
                run_directory=Path(state["run_directory"]),
                summary=state["context_summary"] or "",
            )
            return {
                "context_artifact_path": str(context_artifact_path),
                "node_status": "context_summary_persisted",
            }
        except Exception as exc:
            return {
                "node_status": "failed",
                "failure_reason": str(exc),
            }

    def finish_prepare_context(self, state: PrepareContextState) -> PrepareContextState:
        failure_reason = state.get("failure_reason")
        node_status = "completed"
        if failure_reason:
            node_status = "failed"
        elif not state.get("context_summary"):
            failure_reason = "The prepare-context workflow completed without a context summary."
            node_status = "failed"

        patch: PrepareContextState = {
            "node_status": node_status,
            "failure_reason": failure_reason,
        }

        run_directory = state.get("run_directory")
        if run_directory:
            metadata_path = self._run_artifacts.write_run_metadata(
                run_directory=Path(run_directory),
                metadata=self._build_metadata({**state, **patch}),
            )
            patch["metadata_artifact_path"] = str(metadata_path)

        return patch

    def _build_metadata(self, state: PrepareContextState) -> dict[str, object]:
        metadata: dict[str, object] = {
            "run_id": state.get("run_id"),
            "objective": state.get("objective"),
            "codebase_path": state.get("codebase_path"),
            "editable_roots": state.get("editable_roots"),
            "forbidden_roots": state.get("forbidden_roots"),
            "codex_thread_id": state.get("codex_thread_id"),
            "context_artifact_path": state.get("context_artifact_path"),
            "node_status": state.get("node_status"),
            "failure_reason": state.get("failure_reason"),
            "created_at": state.get("created_at"),
            "updated_at": self._timestamp(),
        }

        if state.get("context_summary") is not None:
            metadata["context_summary_length"] = len(state["context_summary"])

        return metadata

    def _normalize_directory(self, path_text: str | None) -> Path | None:
        if not path_text or not path_text.strip():
            return None

        path = Path(path_text).expanduser()
        if not path.exists() or not path.is_dir():
            return None
        return path.resolve()

    def _normalize_roots(
        self,
        roots: list[str],
        codebase_path: Path,
        field_name: str,
    ) -> list[str] | str:
        normalized_roots: list[str] = []
        for root in roots:
            path = self._normalize_directory(root)
            if path is None:
                return f"{field_name} must contain only existing directories."
            try:
                path.relative_to(codebase_path)
            except ValueError:
                return f"{field_name} must stay within the codebase path."
            normalized_roots.append(str(path))

        return normalized_roots

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat()
