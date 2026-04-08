from __future__ import annotations

from collections import Counter
from html import escape
from pathlib import Path


_SVG_WIDTH = 960
_SVG_HEIGHT = 540
_LEFT_MARGIN = 80
_RIGHT_MARGIN = 40
_TOP_MARGIN = 72
_BOTTOM_MARGIN = 80
_CHART_WIDTH = _SVG_WIDTH - _LEFT_MARGIN - _RIGHT_MARGIN
_CHART_HEIGHT = _SVG_HEIGHT - _TOP_MARGIN - _BOTTOM_MARGIN

_RAW_SCORE_COLOR = "#2563eb"
_BEST_SCORE_COLOR = "#0f766e"
_AXIS_COLOR = "#334155"
_GRID_COLOR = "#cbd5e1"
_TEXT_COLOR = "#0f172a"
_MUTED_TEXT_COLOR = "#475569"
_BACKGROUND_COLOR = "#f8fafc"
_PLOT_BACKGROUND_COLOR = "#ffffff"
_STATUS_COLORS = {
    "improved": "#16a34a",
    "not_improved": "#64748b",
}
_DEFAULT_STATUS_COLOR = "#dc2626"


def progress_chart_path(logs_root: Path, objective_slug: str) -> Path:
    return logs_root / f"experiment_progress_{objective_slug}.svg"


def write_experiment_progress_svg(
    *,
    entries: list[dict[str, object]],
    objective_name: str,
    objective_slug: str,
    optimization_direction: str,
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    svg = _build_svg(
        entries=entries,
        objective_name=objective_name,
        objective_slug=objective_slug,
        optimization_direction=optimization_direction,
    )
    output_path.write_text(svg, encoding="utf-8")


def _build_svg(
    *,
    entries: list[dict[str, object]],
    objective_name: str,
    objective_slug: str,
    optimization_direction: str,
) -> str:
    scored_points = _build_scored_points(entries, optimization_direction)
    unscored_summary = _build_unscored_summary(entries)
    title = escape(f"Experiment Progress: {objective_name}")
    subtitle = escape(
        f"Objective: {objective_slug} | Iterations: {len(entries)} | Direction: {optimization_direction}"
    )

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{_SVG_WIDTH}" height="{_SVG_HEIGHT}" '
            f'viewBox="0 0 {_SVG_WIDTH} {_SVG_HEIGHT}" role="img" '
            f'aria-labelledby="title desc">'
        ),
        f'<title id="title">{title}</title>',
        f'<desc id="desc">{escape(_build_accessible_summary(entries, scored_points, unscored_summary))}</desc>',
        f'<rect width="{_SVG_WIDTH}" height="{_SVG_HEIGHT}" fill="{_BACKGROUND_COLOR}" />',
        (
            f'<rect x="{_LEFT_MARGIN - 20}" y="{_TOP_MARGIN - 24}" width="{_CHART_WIDTH + 40}" '
            f'height="{_CHART_HEIGHT + 48}" rx="16" fill="{_PLOT_BACKGROUND_COLOR}" stroke="#e2e8f0" />'
        ),
        f'<text x="{_LEFT_MARGIN}" y="36" font-size="24" font-weight="700" fill="{_TEXT_COLOR}">{title}</text>',
        f'<text x="{_LEFT_MARGIN}" y="58" font-size="13" fill="{_MUTED_TEXT_COLOR}">{subtitle}</text>',
    ]

    if not scored_points:
        parts.extend(_build_empty_state(unscored_summary))
    else:
        parts.extend(_build_chart(scored_points, unscored_summary))

    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def _build_chart(
    scored_points: list[dict[str, object]],
    unscored_summary: str,
) -> list[str]:
    point_count = len(scored_points)
    max_iteration = int(scored_points[-1]["iteration"])
    min_score = min(float(point["score"]) for point in scored_points)
    max_score = max(float(point["score"]) for point in scored_points)
    lower_bound, upper_bound = _score_bounds(min_score, max_score)

    parts = [
        f'<line x1="{_LEFT_MARGIN}" y1="{_TOP_MARGIN + _CHART_HEIGHT}" x2="{_LEFT_MARGIN + _CHART_WIDTH}" '
        f'y2="{_TOP_MARGIN + _CHART_HEIGHT}" stroke="{_AXIS_COLOR}" stroke-width="1.5" />',
        f'<line x1="{_LEFT_MARGIN}" y1="{_TOP_MARGIN}" x2="{_LEFT_MARGIN}" y2="{_TOP_MARGIN + _CHART_HEIGHT}" '
        f'stroke="{_AXIS_COLOR}" stroke-width="1.5" />',
    ]

    for tick_index, tick_value in enumerate(_score_ticks(lower_bound, upper_bound)):
        y = _y_position(tick_value, lower_bound, upper_bound)
        parts.append(
            f'<line x1="{_LEFT_MARGIN}" y1="{y:.2f}" x2="{_LEFT_MARGIN + _CHART_WIDTH}" y2="{y:.2f}" '
            f'stroke="{_GRID_COLOR}" stroke-width="1" />'
        )
        parts.append(
            f'<text x="{_LEFT_MARGIN - 12}" y="{y + 4:.2f}" text-anchor="end" font-size="12" '
            f'fill="{_MUTED_TEXT_COLOR}">{escape(_format_score(tick_value, tick_index == 0 or tick_index == 4))}</text>'
        )

    x_tick_count = min(max_iteration, 6)
    for tick_value in _iteration_ticks(max_iteration, x_tick_count):
        x = _x_position(tick_value, max_iteration)
        parts.append(
            f'<line x1="{x:.2f}" y1="{_TOP_MARGIN + _CHART_HEIGHT}" x2="{x:.2f}" y2="{_TOP_MARGIN + _CHART_HEIGHT + 6}" '
            f'stroke="{_AXIS_COLOR}" stroke-width="1" />'
        )
        parts.append(
            f'<text x="{x:.2f}" y="{_TOP_MARGIN + _CHART_HEIGHT + 24}" text-anchor="middle" font-size="12" '
            f'fill="{_MUTED_TEXT_COLOR}">{tick_value}</text>'
        )

    raw_score_path = _build_line_path(scored_points, max_iteration, lower_bound, upper_bound, "score")
    best_score_path = _build_line_path(scored_points, max_iteration, lower_bound, upper_bound, "best_score")
    parts.append(
        f'<path d="{raw_score_path}" fill="none" stroke="{_RAW_SCORE_COLOR}" stroke-width="2.5" '
        'stroke-linecap="round" stroke-linejoin="round" />'
    )
    parts.append(
        f'<path d="{best_score_path}" fill="none" stroke="{_BEST_SCORE_COLOR}" stroke-width="2.5" '
        'stroke-dasharray="7 5" stroke-linecap="round" stroke-linejoin="round" />'
    )

    for point in scored_points:
        x = _x_position(int(point["iteration"]), max_iteration)
        y = _y_position(float(point["score"]), lower_bound, upper_bound)
        color = _STATUS_COLORS.get(str(point["status"]), _DEFAULT_STATUS_COLOR)
        tooltip = escape(
            f"Iteration {point['iteration']} | status={point['status']} | "
            f"score={_format_score(float(point['score']), force_fixed=True)}"
        )
        parts.append(
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4.5" fill="{color}" stroke="#ffffff" stroke-width="1.5">'
            f"<title>{tooltip}</title></circle>"
        )

    parts.extend(_build_legend())
    parts.append(
        f'<text x="{_LEFT_MARGIN + _CHART_WIDTH / 2:.2f}" y="{_SVG_HEIGHT - 20}" text-anchor="middle" '
        f'font-size="13" fill="{_MUTED_TEXT_COLOR}">Iteration</text>'
    )
    parts.append(
        f'<text x="20" y="{_TOP_MARGIN + _CHART_HEIGHT / 2:.2f}" transform="rotate(-90 20 {_TOP_MARGIN + _CHART_HEIGHT / 2:.2f})" '
        f'text-anchor="middle" font-size="13" fill="{_MUTED_TEXT_COLOR}">Score</text>'
    )

    if unscored_summary:
        parts.append(
            f'<text x="{_LEFT_MARGIN}" y="{_SVG_HEIGHT - 50}" font-size="12.5" fill="{_MUTED_TEXT_COLOR}">'
            f'Unscored iterations: {escape(unscored_summary)}</text>'
        )

    if point_count == 1:
        parts.append(
            f'<text x="{_LEFT_MARGIN}" y="{_TOP_MARGIN - 8}" font-size="12" fill="{_MUTED_TEXT_COLOR}">'
            "Only one scored iteration so far; lines collapse to a single point.</text>"
        )

    return parts


def _build_empty_state(unscored_summary: str) -> list[str]:
    message = "No scored iterations yet."
    parts = [
        (
            f'<text x="{_LEFT_MARGIN}" y="{_TOP_MARGIN + 60}" font-size="22" font-weight="600" '
            f'fill="{_TEXT_COLOR}">{message}</text>'
        )
    ]
    if unscored_summary:
        parts.append(
            f'<text x="{_LEFT_MARGIN}" y="{_TOP_MARGIN + 92}" font-size="14" fill="{_MUTED_TEXT_COLOR}">'
            f'Unscored iterations: {escape(unscored_summary)}</text>'
        )
    else:
        parts.append(
            f'<text x="{_LEFT_MARGIN}" y="{_TOP_MARGIN + 92}" font-size="14" fill="{_MUTED_TEXT_COLOR}">'
            "No experiment entries are available for this objective yet.</text>"
        )
    return parts


def _build_scored_points(
    entries: list[dict[str, object]],
    optimization_direction: str,
) -> list[dict[str, object]]:
    scored_points: list[dict[str, object]] = []
    best_score: float | None = None

    for iteration, entry in enumerate(entries, start=1):
        score_value = entry.get("score")
        if not isinstance(score_value, (int, float)) or isinstance(score_value, bool):
            continue

        score = float(score_value)
        if best_score is None:
            best_score = score
        elif optimization_direction == "minimize":
            best_score = min(best_score, score)
        elif optimization_direction == "maximize":
            best_score = max(best_score, score)
        else:
            raise ValueError(f"Unsupported optimization_direction: {optimization_direction}")

        status = entry.get("status")
        scored_points.append(
            {
                "iteration": iteration,
                "score": score,
                "best_score": best_score,
                "status": status if isinstance(status, str) and status else "unknown",
            }
        )

    return scored_points


def _build_unscored_summary(entries: list[dict[str, object]]) -> str:
    counts: Counter[str] = Counter()
    for entry in entries:
        score_value = entry.get("score")
        if isinstance(score_value, (int, float)) and not isinstance(score_value, bool):
            continue
        status = entry.get("status")
        normalized = status if isinstance(status, str) and status else "unknown"
        counts[normalized] += 1
    if not counts:
        return ""
    return ", ".join(f"{status} x{count}" for status, count in sorted(counts.items()))


def _build_accessible_summary(
    entries: list[dict[str, object]],
    scored_points: list[dict[str, object]],
    unscored_summary: str,
) -> str:
    if not scored_points:
        if unscored_summary:
            return f"No scored iterations yet. Unscored iterations: {unscored_summary}."
        return "No scored iterations yet."

    latest = scored_points[-1]
    summary = (
        f"{len(entries)} total iterations, {len(scored_points)} scored. Latest scored iteration "
        f"{latest['iteration']} has score {_format_score(float(latest['score']), force_fixed=True)}."
    )
    if unscored_summary:
        summary += f" Unscored iterations: {unscored_summary}."
    return summary


def _build_legend() -> list[str]:
    legend_y = _TOP_MARGIN - 4
    improved_color = _STATUS_COLORS["improved"]
    not_improved_color = _STATUS_COLORS["not_improved"]
    parts = [
        f'<line x1="{_LEFT_MARGIN + 420}" y1="{legend_y}" x2="{_LEFT_MARGIN + 450}" y2="{legend_y}" '
        f'stroke="{_RAW_SCORE_COLOR}" stroke-width="2.5" />',
        (
            f'<text x="{_LEFT_MARGIN + 458}" y="{legend_y + 4}" font-size="12.5" fill="{_MUTED_TEXT_COLOR}">'
            "Score</text>"
        ),
        f'<line x1="{_LEFT_MARGIN + 520}" y1="{legend_y}" x2="{_LEFT_MARGIN + 550}" y2="{legend_y}" '
        f'stroke="{_BEST_SCORE_COLOR}" stroke-width="2.5" stroke-dasharray="7 5" />',
        (
            f'<text x="{_LEFT_MARGIN + 558}" y="{legend_y + 4}" font-size="12.5" fill="{_MUTED_TEXT_COLOR}">'
            "Running best</text>"
        ),
        f'<circle cx="{_LEFT_MARGIN + 680}" cy="{legend_y}" r="4.5" fill="{improved_color}" />',
        (
            f'<text x="{_LEFT_MARGIN + 692}" y="{legend_y + 4}" font-size="12.5" fill="{_MUTED_TEXT_COLOR}">'
            "Improved</text>"
        ),
        f'<circle cx="{_LEFT_MARGIN + 780}" cy="{legend_y}" r="4.5" fill="{not_improved_color}" />',
        (
            f'<text x="{_LEFT_MARGIN + 792}" y="{legend_y + 4}" font-size="12.5" fill="{_MUTED_TEXT_COLOR}">'
            "Not improved</text>"
        ),
    ]
    return parts


def _score_bounds(min_score: float, max_score: float) -> tuple[float, float]:
    if min_score == max_score:
        padding = 1.0 if min_score == 0 else abs(min_score) * 0.05
        return min_score - padding, max_score + padding

    padding = (max_score - min_score) * 0.1
    return min_score - padding, max_score + padding


def _score_ticks(lower_bound: float, upper_bound: float) -> list[float]:
    interval = (upper_bound - lower_bound) / 4
    return [lower_bound + (interval * index) for index in range(5)]


def _iteration_ticks(max_iteration: int, tick_count: int) -> list[int]:
    if max_iteration <= 1:
        return [1]
    if tick_count <= 1:
        return [1, max_iteration]

    ticks = {1, max_iteration}
    span = max_iteration - 1
    for index in range(1, tick_count - 1):
        value = 1 + round((span * index) / (tick_count - 1))
        ticks.add(int(value))
    return sorted(ticks)


def _build_line_path(
    scored_points: list[dict[str, object]],
    max_iteration: int,
    lower_bound: float,
    upper_bound: float,
    key: str,
) -> str:
    commands: list[str] = []
    for index, point in enumerate(scored_points):
        x = _x_position(int(point["iteration"]), max_iteration)
        y = _y_position(float(point[key]), lower_bound, upper_bound)
        command = "M" if index == 0 else "L"
        commands.append(f"{command} {x:.2f} {y:.2f}")
    return " ".join(commands)


def _x_position(iteration: int, max_iteration: int) -> float:
    if max_iteration <= 1:
        return _LEFT_MARGIN + (_CHART_WIDTH / 2)
    return _LEFT_MARGIN + (((iteration - 1) / (max_iteration - 1)) * _CHART_WIDTH)


def _y_position(score: float, lower_bound: float, upper_bound: float) -> float:
    if upper_bound <= lower_bound:
        return _TOP_MARGIN + (_CHART_HEIGHT / 2)
    normalized = (score - lower_bound) / (upper_bound - lower_bound)
    return _TOP_MARGIN + _CHART_HEIGHT - (normalized * _CHART_HEIGHT)


def _format_score(value: float, force_fixed: bool = False) -> str:
    if force_fixed or (abs(value) >= 0.001 and abs(value) < 1000):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return f"{value:.3e}"
