from __future__ import annotations

import re
from pathlib import Path

from .Models import ExperimentOrchestratorError


def build_running_instructions_prompt() -> str:
    return _load_prompt_template("Running Instructions Prompt")


def build_evaluation_spec_prompt(evaluation_command: str, evaluation_relative_path: str) -> str:
    return _load_prompt_template("Evaluation Spec Prompt").format(
        evaluation_command=evaluation_command,
        evaluation_relative_path=evaluation_relative_path,
    )


def build_experiment_prompt(objective_name: str, agent_eval_budget: int, eval_tool_name: str = "orchestrator_run_eval") -> str:
    return _load_prompt_template("Experiment Prompt").format(
        objective_name=objective_name,
        agent_eval_budget=agent_eval_budget,
        eval_tool_name=eval_tool_name,
        running_instructions_path=".nextresearch/RUNNING_INSTRUCTIONS.md",
        evaluation_spec_path=".nextresearch/EVALUATION_SPEC.md",
        baseline_state_path=".nextresearch/BASELINE_STATE.md",
        experiment_history_path=".nextresearch/EXPERIMENT_HISTORY.md",
    )


def normalize_document_text(value: str) -> str:
    text = value.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
    return text + "\n"


def _load_prompt_template(section_title: str) -> str:
    prompt_templates_path = Path(__file__).resolve().parents[2] / "PromptTemplates.md"
    if not prompt_templates_path.exists():
        raise ExperimentOrchestratorError(f"Prompt templates file not found: {prompt_templates_path}")

    content = prompt_templates_path.read_text(encoding="utf-8")
    required_sections = (
        "Running Instructions Prompt",
        "Evaluation Spec Prompt",
        "Experiment Prompt",
    )
    heading_matches: list[tuple[int, int, str]] = []

    for title in required_sections:
        pattern = rf"^# {re.escape(title)}\s*$"
        matches = list(re.finditer(pattern, content, flags=re.MULTILINE))
        if not matches:
            raise ExperimentOrchestratorError(f'Missing prompt section "{title}" in {prompt_templates_path}')
        if len(matches) > 1:
            raise ExperimentOrchestratorError(f'Duplicate prompt section "{title}" in {prompt_templates_path}')
        match = matches[0]
        heading_matches.append((match.start(), match.end(), title))

    heading_matches.sort(key=lambda item: item[0])
    sections: dict[str, str] = {}
    for index, (_, heading_end, title) in enumerate(heading_matches):
        next_start = heading_matches[index + 1][0] if index + 1 < len(heading_matches) else len(content)
        sections[title] = content[heading_end:next_start].strip()

    template = sections.get(section_title)
    if template is None:
        raise ExperimentOrchestratorError(f'Missing prompt section "{section_title}" in {prompt_templates_path}')
    if not template:
        raise ExperimentOrchestratorError(f'Prompt section "{section_title}" is empty in {prompt_templates_path}')
    return template
