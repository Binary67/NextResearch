from __future__ import annotations

import json
from pathlib import Path

from src.Orchestration import PrepareContextRequest, PrepareContextWorkflow


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    workflow = PrepareContextWorkflow()

    try:
        result = workflow.run(
            PrepareContextRequest(
                objective="Understand the current repository structure and identify the main areas relevant to future experiment orchestration work.",
                codebase_path=str(project_root),
            )
        )
    finally:
        workflow.close()

    print(json.dumps(result.__dict__, indent=2))
    if result.context_artifact_path:
        print(Path(result.context_artifact_path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
