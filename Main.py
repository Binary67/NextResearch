from __future__ import annotations

import json
from pathlib import Path

from src.Orchestration import PrepareContextRequest, PrepareContextWorkflow, PrepareContextResult


DEFAULT_OBJECTIVE = (
    "Understand the current repository structure and identify the main areas relevant to "
    "future experiment orchestration work."
)


def run_prepare_context(
    objective: str,
    codebase_path: str,
    workflow: PrepareContextWorkflow | None = None,
) -> PrepareContextResult:
    owned_workflow = workflow is None
    active_workflow = workflow or PrepareContextWorkflow()

    try:
        return active_workflow.run(
            PrepareContextRequest(
                objective=objective,
                codebase_path=codebase_path,
            )
        )
    finally:
        if owned_workflow:
            active_workflow.close()


def main() -> None:
    project_root = Path(__file__).resolve().parent
    result = run_prepare_context(
        objective=DEFAULT_OBJECTIVE,
        codebase_path=str(project_root),
    )

    print(json.dumps(result.__dict__, indent=2))

    if result.context_artifact_path:
        print(Path(result.context_artifact_path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
