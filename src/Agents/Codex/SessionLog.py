from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class CommandLogEntry:
    command: str
    status: str | None = None
    exit_code: int | None = None


@dataclass(frozen=True)
class FileChangeLogEntry:
    path: str
    kind: str | None = None
    diff: str = ""


@dataclass(frozen=True)
class TurnLogEntry:
    user_request: str
    codex_response: str = ""
    commands: list[CommandLogEntry] = field(default_factory=list)
    file_changes: list[FileChangeLogEntry] = field(default_factory=list)
    errors_and_recoveries: list[str] = field(default_factory=list)


class CodexSessionLog:
    def __init__(self, logs_root: Path | str | None = None) -> None:
        self._logs_root = Path(logs_root) if logs_root is not None else self._default_logs_root()
        self._logs_root.mkdir(parents=True, exist_ok=True)

    @property
    def logs_root(self) -> Path:
        return self._logs_root

    def path_for_thread(self, thread_id: str) -> Path:
        if not thread_id or not thread_id.strip():
            raise ValueError("thread_id must be a non-empty string.")

        digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:16]
        return self._logs_root / f"codex_session_{digest}.md"

    def append_session_started(self, thread_id: str, cwd: str | None) -> Path:
        return self._append_sections(
            self.path_for_thread(thread_id),
            [self._single_line_section("Session Started", self._describe_session(cwd))],
        )

    def append_turn(self, thread_id: str, turn: TurnLogEntry) -> Path:
        work_summary = f"Ran {len(turn.commands)} command(s). Changed {len(turn.file_changes)} file(s)."
        sections: list[tuple[str, str]] = [
            self._multi_line_section("User Request", turn.user_request),
            self._multi_line_section("Codex Response", turn.codex_response or "(no final response)"),
            self._single_line_section("Work Performed", work_summary),
        ]

        if turn.commands:
            for command in turn.commands:
                sections.append(self._single_line_section("Commands Run", command.command))
                status_line = command.status or "unknown"
                if command.exit_code is not None:
                    status_line = f"{status_line}; exit_code={command.exit_code}"
                sections.append(self._single_line_section("Command Status", status_line))
        else:
            sections.append(self._single_line_section("Commands Run", "None"))

        if turn.file_changes:
            for file_change in turn.file_changes:
                changed_line = file_change.path
                if file_change.kind:
                    changed_line = f"{changed_line} ({file_change.kind})"
                sections.append(self._single_line_section("File Changed", changed_line))
                diff_text = file_change.diff.strip() or "(no diff available)"
                if diff_text != "(no diff available)":
                    diff_text = f"```diff\n{diff_text.rstrip()}\n```"
                sections.append(self._multi_line_section("Code Diff", diff_text))
        else:
            sections.append(self._single_line_section("File Changed", "None"))

        if turn.errors_and_recoveries:
            errors_text = "\n".join(f"- {entry}" for entry in turn.errors_and_recoveries)
            sections.append(self._multi_line_section("Errors And Recoveries", errors_text))
        else:
            sections.append(self._single_line_section("Errors And Recoveries", "None"))
        return self._append_sections(self.path_for_thread(thread_id), sections)

    def _append_sections(self, path: Path, sections: list[tuple[str, str]]) -> Path:
        with path.open("a", encoding="utf-8") as handle:
            if path.exists() and path.stat().st_size > 0:
                handle.write("\n")
            for title, content in sections:
                if "\n" in content:
                    handle.write(f"[{title}]:\n{content.rstrip()}\n")
                else:
                    handle.write(f"[{title}]: {content}\n")
        return path

    def _describe_session(self, cwd: str | None) -> str:
        timestamp = datetime.now(timezone.utc).isoformat()
        return f"{timestamp}; cwd={cwd or '(unknown)'}"

    def _single_line_section(self, title: str, content: str) -> tuple[str, str]:
        return title, content

    def _multi_line_section(self, title: str, content: str) -> tuple[str, str]:
        normalized = content.strip() or "(empty)"
        if "\n" not in normalized:
            normalized = f"{normalized}\n"
        return title, normalized

    def _default_logs_root(self) -> Path:
        return Path(__file__).resolve().parents[3] / "Logs"
