from __future__ import annotations

from pathlib import Path
import tomllib

from .Models import ExperimentRunConfig


CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.toml"
STARTER_CONFIG = """# Fill in the required values below, then rerun: uv run Main.py
target_repo_path = "D:/path/to/target-repo"
objective_name = "maximize-evaluation-score"
evaluation_command = "uv run evaluation.py"
iteration_count = 3
optimization_direction = "minimize"
agent_eval_budget = 3

# Optional examples:
# evaluation_file_path = "evaluation.py"
# baseline_branch = "main"
# editable_paths = ["feature_engineering.py"]
# non_editable_paths = ["train.py"]
# non_readable_paths = ["evaluation.py", "data_processing.py"]
"""


def load_run_config(config_path: Path = CONFIG_PATH) -> ExperimentRunConfig:
    if not config_path.exists():
        config_path.write_text(STARTER_CONFIG, encoding="utf-8")
        raise SystemExit(
            f"Created starter config at {config_path}. Fill in the required values, "
            "then rerun `uv run Main.py`."
        )

    try:
        raw_config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"Invalid TOML in {config_path}: {exc}") from exc

    if not isinstance(raw_config, dict):
        raise SystemExit(f"{config_path} must contain top-level key/value pairs.")

    required_keys = (
        "target_repo_path",
        "objective_name",
        "evaluation_command",
        "iteration_count",
        "optimization_direction",
    )
    missing_keys = [key for key in required_keys if key not in raw_config]
    if missing_keys:
        missing_list = ", ".join(missing_keys)
        raise SystemExit(f"Missing required config field(s) in {config_path}: {missing_list}")

    errors: list[str] = []
    for key in ("target_repo_path", "objective_name", "evaluation_command", "optimization_direction"):
        value = raw_config[key]
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{key} must be a non-empty string.")

    iteration_count = raw_config["iteration_count"]
    if not isinstance(iteration_count, int) or isinstance(iteration_count, bool):
        errors.append("iteration_count must be an integer.")

    agent_eval_budget = raw_config.get("agent_eval_budget", 3)
    if not isinstance(agent_eval_budget, int) or isinstance(agent_eval_budget, bool):
        errors.append("agent_eval_budget must be an integer.")
    elif agent_eval_budget < 1:
        errors.append("agent_eval_budget must be at least 1.")

    for key in ("evaluation_file_path", "baseline_branch"):
        if key in raw_config:
            value = raw_config[key]
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{key} must be a non-empty string when provided.")

    for key in ("editable_paths", "non_editable_paths", "non_readable_paths"):
        if key not in raw_config:
            continue
        value = raw_config[key]
        if isinstance(value, str):
            if not value.strip():
                errors.append(f"{key} must not be empty.")
            continue
        if not isinstance(value, list):
            errors.append(f"{key} must be a string or an array of strings.")
            continue
        if any(not isinstance(item, str) or not item.strip() for item in value):
            errors.append(f"{key} entries must all be non-empty strings.")

    if errors:
        formatted_errors = "\n".join(f"- {error}" for error in errors)
        raise SystemExit(f"Invalid config in {config_path}:\n{formatted_errors}")

    return ExperimentRunConfig(
        target_repo_path=raw_config["target_repo_path"],
        objective_name=raw_config["objective_name"],
        evaluation_command=raw_config["evaluation_command"],
        iteration_count=iteration_count,
        optimization_direction=raw_config["optimization_direction"],
        agent_eval_budget=agent_eval_budget,
        evaluation_file_path=raw_config.get("evaluation_file_path"),
        baseline_branch=raw_config.get("baseline_branch"),
        editable_paths=raw_config.get("editable_paths", ()),
        non_editable_paths=raw_config.get("non_editable_paths", ()),
        non_readable_paths=raw_config.get("non_readable_paths", ()),
    )
