from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from src.Agents.Codex import CodexAgent
from src.Orchestration.PrepareContext.Models import (
    PrepareContextRequest,
    PrepareContextResult,
    PrepareContextWorkflowConfig,
)
from src.Orchestration.PrepareContext.Nodes import PrepareContextNodes
from src.Orchestration.PrepareContext.State import PrepareContextState
from src.Orchestration.Runs import RunArtifactsManager


class PrepareContextWorkflow:
    def __init__(
        self,
        codex_agent: CodexAgent | None = None,
        config: PrepareContextWorkflowConfig | None = None,
    ) -> None:
        resolved_config = config or PrepareContextWorkflowConfig()
        self._codex_agent = codex_agent or CodexAgent()
        self._run_artifacts = RunArtifactsManager(resolved_config.runs_root)
        self._nodes = PrepareContextNodes(self._codex_agent, self._run_artifacts)
        self._graph = self._build_graph()

    def run(self, request: PrepareContextRequest) -> PrepareContextResult:
        initial_state: PrepareContextState = {
            "objective": request.objective,
            "codebase_path": request.codebase_path,
            "editable_roots": list(request.editable_roots),
            "forbidden_roots": list(request.forbidden_roots),
            "codex_thread_id": request.codex_thread_id,
        }
        if request.run_id:
            initial_state["run_id"] = request.run_id

        final_state = self._graph.invoke(initial_state)

        return PrepareContextResult(
            run_id=final_state["run_id"],
            codex_thread_id=final_state.get("codex_thread_id"),
            context_summary=final_state.get("context_summary"),
            context_artifact_path=final_state.get("context_artifact_path"),
            success=final_state.get("node_status") == "completed",
            failure_reason=final_state.get("failure_reason"),
        )

    def close(self) -> None:
        self._codex_agent.close()

    def _build_graph(self):
        graph = StateGraph(PrepareContextState)
        graph.add_node("prepare_context_inputs", self._nodes.prepare_context_inputs)
        graph.add_node("start_codex_context_session", self._nodes.start_codex_context_session)
        graph.add_node("codex_read_codebase", self._nodes.codex_read_codebase)
        graph.add_node("persist_context_summary", self._nodes.persist_context_summary)
        graph.add_node("finish_prepare_context", self._nodes.finish_prepare_context)
        graph.add_edge(START, "prepare_context_inputs")
        graph.add_edge("prepare_context_inputs", "start_codex_context_session")
        graph.add_edge("start_codex_context_session", "codex_read_codebase")
        graph.add_edge("codex_read_codebase", "persist_context_summary")
        graph.add_edge("persist_context_summary", "finish_prepare_context")
        graph.add_edge("finish_prepare_context", END)
        return graph.compile()
