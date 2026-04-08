from __future__ import annotations

from pathlib import Path
import tomllib

from .Models import ExperimentRunConfig


CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.toml"
STARTER_CONFIG = """# Fill in the required values below, then rerun: uv run Main.py
target_repo_path = "D:/path/to/target-repo"
objective_name = "maximize-evaluation-score"
iteration_count = 3
optimization_direction = "minimize"
hidden_eval_cwd = "D:/path/to/hidden-eval"
hidden_eval_command = "uv run hidden_eval.py"
agent_eval_budget = 3

# Optional examples:
# baseline_branch = "main"
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
        "iteration_count",
        "optimization_direction",
        "hidden_eval_cwd",
        "hidden_eval_command",
    )
    missing_keys = [key for key in required_keys if key not in raw_config]
    if missing_keys:
        missing_list = ", ".join(missing_keys)
        raise SystemExit(f"Missing required config field(s) in {config_path}: {missing_list}")

    errors: list[str] = []
    for key in (
        "target_repo_path",
        "objective_name",
        "optimization_direction",
        "hidden_eval_cwd",
        "hidden_eval_command",
    ):
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

    for key in ("baseline_branch",):
        if key in raw_config:
            value = raw_config[key]
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{key} must be a non-empty string when provided.")

    removed_keys = (
        "evaluation_command",
        "evaluation_file_path",
        "editable_paths",
        "non_editable_paths",
        "non_readable_paths",
    )
    present_removed_keys = [key for key in removed_keys if key in raw_config]
    if present_removed_keys:
        errors.append(
            "Removed config field(s) are no longer supported: " + ", ".join(present_removed_keys) + "."
        )

    allowed_keys = set(required_keys) | {"agent_eval_budget", "baseline_branch"}
    unexpected_keys = sorted(key for key in raw_config if key not in allowed_keys and key not in removed_keys)
    if unexpected_keys:
        errors.append("Unsupported config field(s): " + ", ".join(unexpected_keys) + ".")

    if errors:
        formatted_errors = "\n".join(f"- {error}" for error in errors)
        raise SystemExit(f"Invalid config in {config_path}:\n{formatted_errors}")

    return ExperimentRunConfig(
        target_repo_path=raw_config["target_repo_path"],
        objective_name=raw_config["objective_name"],
        iteration_count=iteration_count,
        optimization_direction=raw_config["optimization_direction"],
        hidden_eval_cwd=raw_config["hidden_eval_cwd"],
        hidden_eval_command=raw_config["hidden_eval_command"],
        agent_eval_budget=agent_eval_budget,
        baseline_branch=raw_config.get("baseline_branch"),
    )
