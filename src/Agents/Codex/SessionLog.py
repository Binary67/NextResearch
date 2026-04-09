from __future__ import annotations

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
class DynamicToolLogEntry:
    tool: str
    arguments: str = ""
    status: str | None = None
    success: bool | None = None
    result: str = ""


@dataclass(frozen=True)
class TurnLogEntry:
    user_request: str
    codex_response: str = ""
    commands: list[CommandLogEntry] = field(default_factory=list)
    file_changes: list[FileChangeLogEntry] = field(default_factory=list)
    dynamic_tool_calls: list[DynamicToolLogEntry] = field(default_factory=list)
    errors_and_recoveries: list[str] = field(default_factory=list)


class CodexSessionLog:
    def __init__(self, logs_root: Path | str | None = None) -> None:
        self._logs_root = Path(logs_root) if logs_root is not None else self._default_logs_root()
        self._logs_root.mkdir(parents=True, exist_ok=True)
        self._thread_paths: dict[str, Path] = {}

    @property
    def logs_root(self) -> Path:
        return self._logs_root

    def path_for_thread(self, thread_id: str) -> Path:
        if not thread_id or not thread_id.strip():
            raise ValueError("thread_id must be a non-empty string.")

        path = self._thread_paths.get(thread_id)
        if path is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = self._logs_root / f"codex_session_{timestamp}.md"
            self._thread_paths[thread_id] = path
        return path

    def append_session_started(self, thread_id: str, cwd: str | None) -> Path:
        return self._append_sections(
            self.path_for_thread(thread_id),
            [
                self._single_line_section("Session Started", self._describe_session(cwd)),
                self._single_line_section("Thread Id", thread_id),
            ],
        )

    def append_turn_started(self, thread_id: str, user_request: str) -> Path:
        return self._append_sections(
            self.path_for_thread(thread_id),
            [self._multi_line_section("User Request", user_request)],
        )

    def append_response_snapshot(self, thread_id: str, response_text: str) -> Path:
        return self._append_sections(
            self.path_for_thread(thread_id),
            [self._multi_line_section("Codex Response", response_text)],
        )

    def append_command_completed(self, thread_id: str, command: CommandLogEntry) -> Path:
        status_line = command.status or "unknown"
        if command.exit_code is not None:
            status_line = f"{status_line}; exit_code={command.exit_code}"

        return self._append_sections(
            self.path_for_thread(thread_id),
            [
                self._single_line_section("Commands Run", command.command),
                self._single_line_section("Command Status", status_line),
            ],
        )

    def append_turn_finished(self, thread_id: str, turn: TurnLogEntry, status: str) -> Path:
        work_summary = (
            f"Ran {len(turn.commands)} command(s) and {len(turn.dynamic_tool_calls)} dynamic tool call(s)."
        )
        sections: list[tuple[str, str]] = [
            self._single_line_section("Turn Status", status),
            self._single_line_section("Work Performed", work_summary),
        ]

        if not turn.commands:
            sections.append(self._single_line_section("Commands Run", "None"))

        if turn.errors_and_recoveries:
            errors_text = "\n".join(f"- {entry}" for entry in turn.errors_and_recoveries)
            sections.append(self._multi_line_section("Errors And Recoveries", errors_text))
        else:
            sections.append(self._single_line_section("Errors And Recoveries", "None"))

        return self._append_sections(self.path_for_thread(thread_id), sections)

    def append_post_run_review(
        self,
        path: Path,
        app_server_file_changes: int,
        git_tracked_changes: int,
        git_diff: str,
    ) -> Path:
        if git_diff.strip():
            diff_text = f"```diff\n{git_diff.rstrip()}\n```"
        elif git_tracked_changes == 0:
            diff_text = "(no git-tracked changes)"
        else:
            diff_text = "(no text/code git-tracked changes)"

        return self._append_sections(
            path,
            [
                self._single_line_section("App-Server Reported File Changes", str(app_server_file_changes)),
                self._single_line_section("Git-Tracked Changes Before Cleanup", str(git_tracked_changes)),
                self._multi_line_section("Git Diff", diff_text),
            ],
        )

    def append_writable_scope(self, thread_id: str, editable_paths: list[str]) -> Path:
        if editable_paths:
            content = "\n".join(f"- {path}" for path in editable_paths)
        else:
            content = "All repo paths\n"
        return self._append_sections(
            self.path_for_thread(thread_id),
            [self._multi_line_section("Writable Scope", content)],
        )

    def append_dynamic_tool_registration(self, thread_id: str, tools: list[tuple[str, str]]) -> Path:
        if not tools:
            return self.path_for_thread(thread_id)

        content = "\n".join(
            f"- {name}: {description}" if description else f"- {name}"
            for name, description in tools
        )
        return self._append_sections(
            self.path_for_thread(thread_id),
            [self._multi_line_section("Dynamic Tools", content)],
        )

    def append_policy_denial(self, thread_id: str, path: str, reason: str) -> Path:
        return self._append_sections(
            self.path_for_thread(thread_id),
            [
                self._single_line_section("Policy Denial", path),
                self._single_line_section("Policy Reason", reason),
            ],
        )

    def append_policy_violation(self, thread_id: str, message: str) -> Path:
        return self._append_sections(
            self.path_for_thread(thread_id),
            [self._multi_line_section("Policy Violation", message)],
        )

    def append_command_denial(self, thread_id: str, command: str, reason: str) -> Path:
        return self._append_sections(
            self.path_for_thread(thread_id),
            [
                self._single_line_section("Command Denial", command),
                self._single_line_section("Command Reason", reason),
            ],
        )

    def append_dynamic_tool_completed(self, thread_id: str, tool_call: DynamicToolLogEntry) -> Path:
        status_line = tool_call.status or "unknown"
        if tool_call.success is not None:
            status_line = f"{status_line}; success={str(tool_call.success).lower()}"

        sections = [
            self._single_line_section("Dynamic Tool Call", tool_call.tool),
            self._single_line_section("Dynamic Tool Status", status_line),
        ]
        if tool_call.arguments:
            sections.append(self._multi_line_section("Dynamic Tool Arguments", tool_call.arguments))
        if tool_call.result:
            sections.append(self._multi_line_section("Dynamic Tool Result", tool_call.result))
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
