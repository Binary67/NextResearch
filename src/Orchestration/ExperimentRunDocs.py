from __future__ import annotations

from pathlib import Path

from .Models import ExperimentRunConfig


def build_run_docs(
    config: ExperimentRunConfig,
    target_repo_path: Path,
    current_base_ref: str,
    current_base_commit: str,
    best_branch_name: str,
    best_score: float,
    ledger_entries: list[dict[str, object]],
) -> dict[str, str]:
    comparable_entries = load_comparable_entries(
        ledger_entries=ledger_entries,
        target_repo_path=target_repo_path,
        config=config,
    )
    return {
        "BASELINE_STATE.md": build_baseline_state_document(
            objective_name=config.objective_name,
            optimization_direction=config.optimization_direction,
            current_base_commit=current_base_commit,
            best_score=best_score,
            comparable_entries=comparable_entries,
            starting_from_best_known=current_base_ref == best_branch_name,
        ),
        "EXPERIMENT_HISTORY.md": build_experiment_history_document(
            comparable_entries=comparable_entries,
            optimization_direction=config.optimization_direction,
        ),
    }


def load_comparable_entries(
    ledger_entries: list[dict[str, object]],
    target_repo_path: Path,
    config: ExperimentRunConfig,
) -> list[dict[str, object]]:
    comparable_entries: list[dict[str, object]] = []

    for entry in ledger_entries:
        if (
            str(entry.get("target_repo_path")) != str(target_repo_path)
            or str(entry.get("objective_name")) != config.objective_name
            or str(entry.get("evaluation_key")) != config.evaluation_key
            or str(entry.get("optimization_direction")) != config.optimization_direction
        ):
            continue
        comparable_entries.append(entry)

    return comparable_entries


def build_baseline_state_document(
    objective_name: str,
    optimization_direction: str,
    current_base_commit: str,
    best_score: float,
    comparable_entries: list[dict[str, object]],
    starting_from_best_known: bool,
) -> str:
    improved_entries = [entry for entry in comparable_entries if bool(entry.get("improved"))]
    no_improvement_streak = _count_recent_non_improvements(comparable_entries)
    last_improved_entry = improved_entries[-1] if improved_entries else None

    if improved_entries:
        starting_point = "Current best-known version so far" if starting_from_best_known else "Comparable baseline"
        trend_note = (
            f"Recent runs have not improved for {no_improvement_streak} consecutive attempt(s)."
            if no_improvement_streak
            else "The previous comparable run improved the score."
        )
    else:
        starting_point = "Configured baseline with no prior comparable improvements"
        trend_note = "No prior comparable improvements yet."

    lines = [
        "# Baseline State",
        "",
        "## Current Position",
        f"- Objective: {objective_name}",
        f"- Optimization direction: {optimization_direction}",
        f"- Current best score: {_format_float(best_score)}",
        f"- Current base commit: {current_base_commit}",
        f"- Starting point: {starting_point}",
        "",
        "## Comparable History Summary",
        f"- Comparable past runs: {len(comparable_entries)}",
        f"- Improved runs: {len(improved_entries)}",
        f"- Current no-improvement streak: {no_improvement_streak}",
        f"- Last improved run: {_format_last_improved(last_improved_entry)}",
        "",
        "## Notes",
        f"- {trend_note}",
    ]
    return "\n".join(lines) + "\n"


def build_experiment_history_document(
    comparable_entries: list[dict[str, object]],
    optimization_direction: str,
) -> str:
    improved_entries = [entry for entry in comparable_entries if bool(entry.get("improved"))]
    current_streak_entries = _current_search_streak_entries(comparable_entries)
    included_entries = _history_entries_for_context(comparable_entries)
    lines = [
        "# Experiment History",
        "",
        "## Summary",
        f"- Comparable runs: {len(comparable_entries)}",
        f"- Included in history: {len(included_entries)}",
        f"- Improved runs: {len(improved_entries)}",
        f"- Current no-improvement streak: {_count_recent_non_improvements(comparable_entries)}",
        f"- Best score seen: {_format_optional_float(_best_score_from_entries(comparable_entries, optimization_direction))}",
        f"- Latest run status: {_latest_status(comparable_entries)}",
    ]

    if not comparable_entries:
        lines.extend(["", "## Included Runs", "- No comparable prior runs."])
        return "\n".join(lines) + "\n"

    lines.extend(["", "## Improved Runs"])
    if improved_entries:
        for entry in improved_entries:
            lines.extend(_render_history_entry(entry))
    else:
        lines.append("- No improved comparable runs.")

    lines.extend(["", "## Current Search Streak"])
    if current_streak_entries:
        for entry in current_streak_entries:
            lines.extend(_render_history_entry(entry))
    elif improved_entries:
        lines.append("- No runs after the latest improvement.")
    else:
        lines.append("- No current streak yet.")

    return "\n".join(lines) + "\n"


def _render_history_entry(entry: dict[str, object]) -> list[str]:
    lines = [
        "",
        f"### {_string_value(entry, 'run_id', '(unknown run)')}",
        f"- Completed at: {_string_value(entry, 'completed_at', 'unknown')}",
        f"- Status: {_string_value(entry, 'status', 'unknown')}",
        f"- Improved: {_bool_label(entry.get('improved'))}",
        f"- Score: {_format_optional_float(_float_value(entry.get('score')))}",
        f"- Score delta: {_format_optional_float(_float_value(entry.get('score_delta')))}",
        f"- Strategy: {_string_value(entry, 'strategy', 'No strategy recorded.')}",
        f"- Why this should help: {_string_value(entry, 'why_it_should_help', 'No rationale recorded.')}",
        f"- Files changed: {_format_files_changed(entry)}",
    ]
    notes = _string_list(entry.get("notes"))
    if notes:
        lines.append("- Notes:")
        lines.extend(f"  - {note}" for note in notes)
    return lines


def _history_entries_for_context(comparable_entries: list[dict[str, object]]) -> list[dict[str, object]]:
    if not comparable_entries:
        return []

    if not any(bool(entry.get("improved")) for entry in comparable_entries):
        return comparable_entries

    last_improved_index = max(
        index for index, entry in enumerate(comparable_entries) if bool(entry.get("improved"))
    )
    selected_entries: list[dict[str, object]] = []
    seen_run_ids: set[str] = set()

    for entry in comparable_entries:
        run_id = _string_value(entry, "run_id", "")
        if not bool(entry.get("improved")) or not run_id or run_id in seen_run_ids:
            continue
        seen_run_ids.add(run_id)
        selected_entries.append(entry)

    for entry in comparable_entries[last_improved_index + 1 :]:
        run_id = _string_value(entry, "run_id", "")
        if not run_id or run_id in seen_run_ids:
            continue
        seen_run_ids.add(run_id)
        selected_entries.append(entry)

    return selected_entries


def _current_search_streak_entries(comparable_entries: list[dict[str, object]]) -> list[dict[str, object]]:
    if not comparable_entries:
        return []

    improved_indexes = [index for index, entry in enumerate(comparable_entries) if bool(entry.get("improved"))]
    if not improved_indexes:
        return comparable_entries

    return comparable_entries[improved_indexes[-1] + 1 :]


def _count_recent_non_improvements(comparable_entries: list[dict[str, object]]) -> int:
    streak = 0
    for entry in reversed(comparable_entries):
        if bool(entry.get("improved")):
            break
        streak += 1
    return streak


def _best_score_from_entries(
    comparable_entries: list[dict[str, object]],
    optimization_direction: str,
) -> float | None:
    scores = [score for entry in comparable_entries if (score := _float_value(entry.get("score"))) is not None]
    if not scores:
        return None
    if optimization_direction == "minimize":
        return min(scores)
    return max(scores)


def _latest_status(comparable_entries: list[dict[str, object]]) -> str:
    if not comparable_entries:
        return "none"
    return _string_value(comparable_entries[-1], "status", "unknown")


def _format_last_improved(entry: dict[str, object] | None) -> str:
    if entry is None:
        return "None"

    run_id = _string_value(entry, "run_id", "(unknown run)")
    score_delta = _format_optional_float(_float_value(entry.get("score_delta")))
    return f"{run_id} (delta {score_delta})"


def _string_value(entry: dict[str, object], key: str, default: str) -> str:
    value = entry.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _float_value(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _format_float(value: float) -> str:
    return format(value, ".12g")


def _format_optional_float(value: float | None) -> str:
    if value is None:
        return "None"
    return _format_float(value)


def _bool_label(value: object) -> str:
    return "yes" if bool(value) else "no"


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []

    items: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            continue
        normalized = " ".join(entry.split())
        if normalized:
            items.append(normalized)
    return items


def _format_files_changed(entry: dict[str, object]) -> str:
    paths = _string_list(entry.get("files_changed"))
    if not paths:
        return "None"
    return ", ".join(paths)
