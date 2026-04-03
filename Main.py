from __future__ import annotations

from pathlib import Path

from src.Orchestration import ExperimentOrchestrator, ExperimentRunConfig


PROJECT_ROOT = "D:/HousePricePrediction"
DEFAULT_CONFIG = ExperimentRunConfig(
    target_repo_path=PROJECT_ROOT,
    objective_name="maximize-evaluation-score",
    evaluation_command="uv run evaluation.py",
    iteration_count=5,
    editable_paths=(),
    non_editable_paths=("evaluation.py",
                        "data_processing.py"),
)


def main(config: ExperimentRunConfig = DEFAULT_CONFIG) -> None:
    orchestrator = ExperimentOrchestrator()
    results = orchestrator.run_iterations(config)

    for result in results:
        print(
            f"{result.run_id} status={result.status} improved={result.improved} "
            f"score={result.score} delta={result.score_delta}"
        )


if __name__ == "__main__":
    main()
