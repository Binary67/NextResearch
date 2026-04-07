from __future__ import annotations

from src.Orchestration import ExperimentOrchestrator, ExperimentRunConfig
from src.Orchestration.RunConfigFile import load_run_config


def main(config: ExperimentRunConfig) -> None:
    orchestrator = ExperimentOrchestrator()
    results = orchestrator.run_iterations(config)

    for result in results:
        print()
        print(
            f"{result.run_id} status={result.status} improved={result.improved} "
            f"score={result.score} delta={result.score_delta}"
        )


if __name__ == "__main__":
    main(load_run_config())
