from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from src.EditPolicy import EditPolicy

from .SessionLog import (
    CodexSessionLog,
    CommandLogEntry,
    DynamicToolLogEntry,
    FileChangeLogEntry,
    TurnLogEntry,
)


class CodexAgentError(RuntimeError):
    """Raised when the Codex app-server session cannot complete a request."""

    def __init__(self, message: str, session_log_path: Path | None = None) -> None:
        super().__init__(message)
        self.session_log_path = session_log_path


@dataclass(frozen=True)
class CodexTurnResult:
    response_text: str
    commands: list[CommandLogEntry] = field(default_factory=list)
    file_changes: list[FileChangeLogEntry] = field(default_factory=list)
    dynamic_tool_calls: list[DynamicToolLogEntry] = field(default_factory=list)
    errors_and_recoveries: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DynamicToolCallRequest:
    call_id: str
    thread_id: str
    turn_id: str
    tool: str
    arguments: object


@dataclass(frozen=True)
class DynamicToolCallResult:
    text: str
    success: bool = True


DynamicToolHandler = Callable[[DynamicToolCallRequest], DynamicToolCallResult]


@dataclass(frozen=True)
class CodexDynamicTool:
    name: str
    description: str
    input_schema: object
    handler: DynamicToolHandler
    defer_loading: bool = False

    def to_thread_start_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }
        if self.defer_loading:
            payload["deferLoading"] = True
        return payload


@dataclass
class _CommandLogState:
    command: str = ""
    status: str | None = None
    exit_code: int | None = None

    def update_from_item(self, item: dict[str, Any]) -> None:
        command = item.get("command")
        if isinstance(command, str) and command:
            self.command = command

        status = item.get("status")
        if isinstance(status, str) and status:
            self.status = status

        exit_code = item.get("exitCode")
        if isinstance(exit_code, int):
            self.exit_code = exit_code

    def to_entry(self) -> CommandLogEntry:
        return CommandLogEntry(
            command=self.command or "(unknown command)",
            status=self.status,
            exit_code=self.exit_code,
        )


@dataclass
class _FileChangeState:
    changes: list[dict[str, str | None]] = field(default_factory=list)
    output: str = ""
    status: str | None = None

    def update_from_item(self, item: dict[str, Any]) -> None:
        status = item.get("status")
        if isinstance(status, str) and status:
            self.status = status

        raw_changes = item.get("changes")
        if not isinstance(raw_changes, list):
            return

        changes: list[dict[str, str | None]] = []
        for raw_change in raw_changes:
            if not isinstance(raw_change, dict):
                continue

            path = raw_change.get("path")
            kind = raw_change.get("kind")
            diff = raw_change.get("diff")
            if not isinstance(path, str) or not path:
                continue

            changes.append(
                {
                    "path": path,
                    "kind": kind if isinstance(kind, str) and kind else None,
                    "diff": diff if isinstance(diff, str) else None,
                }
            )

        if changes:
            self.changes = changes

    def append_output(self, value: str) -> None:
        if value:
            self.output += value

    def to_entries(self) -> list[FileChangeLogEntry]:
        if not self.changes:
            return []

        entries: list[FileChangeLogEntry] = []
        for change in self.changes:
            diff = change["diff"] or ""
            if not diff and self.output.strip():
                diff = self.output

            entries.append(
                FileChangeLogEntry(
                    path=change["path"] or "(unknown file)",
                    kind=change["kind"],
                    diff=diff,
                )
            )
        return entries


@dataclass
class _DynamicToolCallState:
    tool: str = ""
    arguments: object = field(default_factory=dict)
    status: str | None = None
    success: bool | None = None
    result_text: str = ""

    def update_from_item(self, item: dict[str, Any]) -> None:
        tool = item.get("tool")
        if isinstance(tool, str) and tool:
            self.tool = tool

        if "arguments" in item:
            self.arguments = item.get("arguments")

        status = item.get("status")
        if isinstance(status, str) and status:
            self.status = status

        success = item.get("success")
        if isinstance(success, bool):
            self.success = success

        content_items = item.get("contentItems")
        if isinstance(content_items, list):
            formatted = self._format_content_items(content_items)
            if formatted:
                self.result_text = formatted

    def set_response(self, result: DynamicToolCallResult) -> None:
        self.success = result.success
        self.result_text = result.text

    def to_entry(self) -> DynamicToolLogEntry:
        return DynamicToolLogEntry(
            tool=self.tool or "(unknown tool)",
            arguments=self._format_value(self.arguments),
            status=self.status,
            success=self.success,
            result=self.result_text,
        )

    def _format_content_items(self, content_items: list[object]) -> str:
        lines: list[str] = []
        for item in content_items:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "inputText":
                text = item.get("text")
                if isinstance(text, str) and text:
                    lines.append(text)
                    continue
            lines.append(json.dumps(item, ensure_ascii=False, indent=2))
        return "\n".join(lines).strip()

    def _format_value(self, value: object) -> str:
        if value in ({}, None):
            return "{}"
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, indent=2)


@dataclass
class _TurnLogCollector:
    user_request: str
    command_states: dict[str, _CommandLogState] = field(default_factory=dict)
    file_change_states: dict[str, _FileChangeState] = field(default_factory=dict)
    dynamic_tool_states: dict[str, _DynamicToolCallState] = field(default_factory=dict)
    errors_and_recoveries: list[str] = field(default_factory=list)

    def note_error(self, message: str) -> None:
        if message and message not in self.errors_and_recoveries:
            self.errors_and_recoveries.append(message)

    def command_state(self, item_id: str) -> _CommandLogState:
        state = self.command_states.get(item_id)
        if state is None:
            state = _CommandLogState()
            self.command_states[item_id] = state
        return state

    def file_change_state(self, item_id: str) -> _FileChangeState:
        state = self.file_change_states.get(item_id)
        if state is None:
            state = _FileChangeState()
            self.file_change_states[item_id] = state
        return state

    def dynamic_tool_state(self, item_id: str) -> _DynamicToolCallState:
        state = self.dynamic_tool_states.get(item_id)
        if state is None:
            state = _DynamicToolCallState()
            self.dynamic_tool_states[item_id] = state
        return state

    def to_entry(self, codex_response: str) -> TurnLogEntry:
        commands = [state.to_entry() for state in self.command_states.values()]
        file_changes: list[FileChangeLogEntry] = []
        for state in self.file_change_states.values():
            file_changes.extend(state.to_entries())
        dynamic_tool_calls = [state.to_entry() for state in self.dynamic_tool_states.values()]

        return TurnLogEntry(
            user_request=self.user_request,
            codex_response=codex_response,
            commands=commands,
            file_changes=file_changes,
            dynamic_tool_calls=dynamic_tool_calls,
            errors_and_recoveries=self.errors_and_recoveries.copy(),
        )


class CodexAgent:
    def __init__(
        self,
        codex_executable: str | None = None,
        client_name: str = "nextresearch",
        client_title: str = "NextResearch",
        client_version: str = "0.1.0",
        logs_root: Path | str | None = None,
        edit_policy: EditPolicy | None = None,
        environment: Mapping[str, str] | None = None,
        blocked_commands: tuple[str, ...] = (),
        dynamic_tools: tuple[CodexDynamicTool, ...] = (),
    ) -> None:
        self._codex_executable = codex_executable or self._resolve_codex_executable()
        self._client_name = client_name
        self._client_title = client_title
        self._client_version = client_version
        self._session_log = CodexSessionLog(logs_root)
        self._edit_policy = edit_policy
        self._environment = dict(environment) if environment is not None else None
        self._blocked_commands = tuple(entry for entry in blocked_commands if entry)
        self._dynamic_tools = dynamic_tools
        self._dynamic_tool_map = {tool.name: tool for tool in dynamic_tools}
        self._process: subprocess.Popen[str] | None = None
        self._next_request_id = 1
        self._pending_messages: deque[dict[str, Any]] = deque()
        self._thread_id: str | None = None

    @property
    def thread_id(self) -> str | None:
        return self._thread_id

    @property
    def session_log_path(self) -> Path | None:
        if self._thread_id is None:
            return None
        return self._session_log.path_for_thread(self._thread_id)

    def start(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return

        self._process = subprocess.Popen(
            [self._codex_executable, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=self._environment,
        )

        initialize_result = self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": self._client_name,
                    "title": self._client_title,
                    "version": self._client_version,
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        if not isinstance(initialize_result, dict):
            raise CodexAgentError("Codex app-server returned an invalid initialize response.")

        self._notify("initialized", {})

    def start_session(self, cwd: str) -> None:
        normalized_cwd = self._normalize_cwd(cwd)
        self.start()

        if self._thread_id is not None:
            self.end_session()

        result = self._request(
            "thread/start",
            self._build_thread_start_params(normalized_cwd),
        )

        self._thread_id = self._extract_thread_id_from_session_result(result, "thread/start")
        self._session_log.append_session_started(self._thread_id, normalized_cwd)
        if self._edit_policy is not None:
            self._session_log.append_edit_policy(
                self._thread_id,
                self._edit_policy.mode_label,
                list(self._edit_policy.editable_rule_paths()),
                list(self._edit_policy.non_editable_rule_paths()),
                list(self._edit_policy.non_readable_rule_paths()),
            )
        if self._dynamic_tools:
            self._session_log.append_dynamic_tool_registration(
                self._thread_id,
                [(tool.name, tool.description) for tool in self._dynamic_tools],
            )

    def end_session(self) -> None:
        if self._thread_id is None:
            return

        thread_id = self._thread_id
        self._thread_id = None
        self._request("thread/unsubscribe", {"threadId": thread_id})

    def run_instruction(self, instruction: str) -> CodexTurnResult:
        if not instruction or not instruction.strip():
            raise ValueError("instruction must be a non-empty string.")
        if self._thread_id is None:
            raise CodexAgentError("Codex session is not started. Call start_session(cwd) before run_instruction().")

        turn_result = self._request(
            "turn/start",
            {
                "threadId": self._require_thread_id(),
                "input": [{"type": "text", "text": instruction}],
            },
        )
        turn_id = self._extract_turn_id(turn_result)
        return self._consume_turn(turn_id, instruction)

    def close(self) -> None:
        if self._process is None:
            return

        process = self._process
        self._process = None
        self._pending_messages.clear()
        self._thread_id = None

        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

        if process.stdin is not None:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()

    def append_policy_violation(self, message: str) -> None:
        if self._thread_id is None or not message:
            return
        self._session_log.append_policy_violation(self._thread_id, message)

    def build_error(self, message: str) -> CodexAgentError:
        return CodexAgentError(message, self.session_log_path)

    def _consume_turn(self, expected_turn_id: str, instruction: str) -> CodexTurnResult:
        message_buffers: dict[str, str] = {}
        last_message_text = ""
        final_answer_text: str | None = None
        collector = _TurnLogCollector(user_request=instruction)
        last_logged_response: str | None = None
        did_finish_log = False
        thread_id = self._require_thread_id()
        self._session_log.append_turn_started(thread_id, instruction)

        def build_turn_result(response_text: str) -> CodexTurnResult:
            turn_entry = collector.to_entry(response_text)
            return CodexTurnResult(
                response_text=turn_entry.codex_response,
                commands=turn_entry.commands,
                file_changes=turn_entry.file_changes,
                dynamic_tool_calls=turn_entry.dynamic_tool_calls,
                errors_and_recoveries=turn_entry.errors_and_recoveries,
            )

        def append_response_snapshot(response_text: str) -> None:
            nonlocal last_logged_response
            if not response_text:
                return
            if response_text == last_logged_response:
                return
            self._session_log.append_response_snapshot(thread_id, response_text)
            last_logged_response = response_text

        def finalize_turn_log(
            response_text: str,
            status: str,
            error_message: str | None = None,
        ) -> CodexTurnResult:
            nonlocal did_finish_log
            if error_message:
                collector.note_error(error_message)
            result = build_turn_result(response_text)
            if did_finish_log:
                return result
            append_response_snapshot(result.response_text or "(no final response)")
            self._session_log.append_turn_finished(
                thread_id,
                TurnLogEntry(
                    user_request=instruction,
                    codex_response=result.response_text,
                    commands=result.commands,
                    file_changes=result.file_changes,
                    dynamic_tool_calls=result.dynamic_tool_calls,
                    errors_and_recoveries=result.errors_and_recoveries,
                ),
                status,
            )
            did_finish_log = True
            return result

        try:
            while True:
                message = self._read_message()
                if self._handle_server_request(message, collector):
                    continue
                self._raise_for_server_request(message)

                if "id" in message:
                    raise CodexAgentError(f"Unexpected JSON-RPC response while waiting for turn events: {message!r}")

                method = message.get("method")
                params = message.get("params", {})

                if method == "item/started":
                    item = params.get("item", {})
                    item_id = item.get("id")
                    item_type = item.get("type")
                    if isinstance(item_id, str) and item_id:
                        if item_type == "commandExecution":
                            collector.command_state(item_id).update_from_item(item)
                        elif item_type == "fileChange":
                            collector.file_change_state(item_id).update_from_item(item)
                        elif item_type == "dynamicToolCall":
                            collector.dynamic_tool_state(item_id).update_from_item(item)
                    continue

                if method == "item/agentMessage/delta":
                    item_id = params["itemId"]
                    message_buffers[item_id] = message_buffers.get(item_id, "") + params["delta"]
                    last_message_text = message_buffers[item_id]
                    continue

                if method == "item/fileChange/outputDelta":
                    item_id = params.get("itemId")
                    output_text = self._extract_delta_text(params)
                    if isinstance(item_id, str) and output_text:
                        collector.file_change_state(item_id).append_output(output_text)
                    continue

                if method == "item/completed":
                    item = params.get("item", {})
                    if item.get("type") == "agentMessage":
                        text = item.get("text", "")
                        item_id = item.get("id")
                        if item_id:
                            message_buffers[item_id] = text
                        last_message_text = text
                        append_response_snapshot(text)
                        if item.get("phase") == "final_answer":
                            final_answer_text = text
                    elif item.get("type") == "commandExecution":
                        item_id = item.get("id")
                        if isinstance(item_id, str) and item_id:
                            command_state = collector.command_state(item_id)
                            command_state.update_from_item(item)
                            if command_state.status in {"failed", "declined"}:
                                error_message = (
                                    f"Command `{command_state.command or '(unknown command)'}` ended with status "
                                    f"{command_state.status}"
                                )
                                if command_state.exit_code is not None:
                                    error_message = f"{error_message} (exit_code={command_state.exit_code})"
                                collector.note_error(f"{error_message}.")
                            self._session_log.append_command_completed(thread_id, command_state.to_entry())
                    elif item.get("type") == "fileChange":
                        item_id = item.get("id")
                        if isinstance(item_id, str) and item_id:
                            file_change_state = collector.file_change_state(item_id)
                            file_change_state.update_from_item(item)
                            if file_change_state.status in {"failed", "declined"}:
                                collector.note_error(f"File change item ended with status {file_change_state.status}.")
                    elif item.get("type") == "dynamicToolCall":
                        item_id = item.get("id")
                        if isinstance(item_id, str) and item_id:
                            dynamic_tool_state = collector.dynamic_tool_state(item_id)
                            dynamic_tool_state.update_from_item(item)
                            if dynamic_tool_state.status in {"failed", "declined"} or dynamic_tool_state.success is False:
                                collector.note_error(
                                    f"Dynamic tool `{dynamic_tool_state.tool or '(unknown tool)'}` ended with status "
                                    f"{dynamic_tool_state.status or 'unknown'}."
                                )
                            self._session_log.append_dynamic_tool_completed(thread_id, dynamic_tool_state.to_entry())
                    continue

                if method == "turn/completed":
                    turn = params.get("turn", {})
                    turn_id = turn.get("id")
                    if turn_id != expected_turn_id:
                        self._pending_messages.append(message)
                        continue

                    status = turn.get("status")
                    if status == "failed":
                        error = turn.get("error") or {}
                        error_message = error.get("message", "Codex turn failed.")
                        finalize_turn_log(final_answer_text or last_message_text, "failed", error_message)
                        raise self.build_error(error_message)
                    if status == "interrupted":
                        error_message = "Codex turn was interrupted."
                        finalize_turn_log(final_answer_text or last_message_text, "interrupted", error_message)
                        raise self.build_error(error_message)
                    if status != "completed":
                        error_message = f"Unexpected Codex turn status: {status!r}"
                        finalize_turn_log(final_answer_text or last_message_text, f"unexpected:{status!r}", error_message)
                        raise self.build_error(error_message)
                    response_text = final_answer_text or last_message_text
                    return finalize_turn_log(response_text, "completed")
        except Exception as exc:
            finalize_turn_log(final_answer_text or last_message_text, "failed", str(exc))
            raise

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_request_id
        self._next_request_id += 1
        self._write_message({"method": method, "id": request_id, "params": params})
        deferred_messages: list[dict[str, Any]] = []

        while True:
            message = self._read_message()
            self._raise_for_server_request(message)

            if message.get("id") != request_id:
                deferred_messages.append(message)
                continue

            if "error" in message:
                error = message["error"]
                raise self.build_error(error.get("message", f"Codex request failed for {method}."))
            if "result" not in message:
                raise self.build_error(f"Codex response for {method} did not include a result.")
            if deferred_messages:
                self._pending_messages.extendleft(reversed(deferred_messages))
            return message["result"]

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._write_message({"method": method, "params": params})

    def _write_message(self, message: dict[str, Any]) -> None:
        process = self._require_process()
        if process.stdin is None:
            raise self.build_error("Codex app-server stdin is not available.")

        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def _read_message(self) -> dict[str, Any]:
        if self._pending_messages:
            return self._pending_messages.popleft()

        process = self._require_process()
        if process.stdout is None:
            raise self.build_error("Codex app-server stdout is not available.")

        line = process.stdout.readline()
        if line == "":
            exit_code = process.poll()
            raise self.build_error(
                "Codex app-server closed the connection unexpectedly."
                if exit_code is None
                else f"Codex app-server exited unexpectedly with code {exit_code}."
            )

        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise self.build_error(f"Codex app-server returned invalid JSON: {line!r}") from exc

        if not isinstance(payload, dict):
            raise self.build_error(f"Codex app-server returned an unexpected message: {payload!r}")
        return payload

    def _extract_turn_id(self, result: dict[str, Any]) -> str:
        try:
            return result["turn"]["id"]
        except (KeyError, TypeError) as exc:
            raise self.build_error("Codex app-server returned an invalid turn/start response.") from exc

    def _extract_thread_id_from_session_result(self, result: dict[str, Any], operation: str) -> str:
        try:
            return result["thread"]["id"]
        except (KeyError, TypeError) as exc:
            raise self.build_error(f"Codex app-server returned an invalid {operation} response.") from exc

    def _normalize_cwd(self, cwd: str | None) -> str | None:
        if cwd is None:
            return None

        candidate = Path(cwd).expanduser()
        if not candidate.exists():
            raise ValueError(f"cwd does not exist: {cwd}")
        if not candidate.is_dir():
            raise ValueError(f"cwd is not a directory: {cwd}")
        return str(candidate.resolve())

    def _raise_for_server_request(self, message: dict[str, Any]) -> None:
        if "method" in message and "id" in message and "result" not in message and "error" not in message:
            raise self.build_error(
                f"Codex requested client-side handling for {message['method']}, which this wrapper does not support."
            )

    def _require_process(self) -> subprocess.Popen[str]:
        if self._process is None:
            raise self.build_error("Codex agent is not started.")
        return self._process

    def _resolve_codex_executable(self) -> str:
        if sys.platform.startswith("win"):
            return shutil.which("codex.cmd") or shutil.which("codex") or "codex"
        return shutil.which("codex") or "codex"

    def _require_thread_id(self) -> str:
        if self._thread_id is None:
            raise self.build_error("Codex thread is not initialized.")
        return self._thread_id

    def _extract_delta_text(self, params: dict[str, Any]) -> str:
        preferred_keys = ("delta", "output", "text")
        for key in preferred_keys:
            value = params.get(key)
            if isinstance(value, str) and value:
                return value

        content = params.get("content")
        if isinstance(content, str) and content:
            return content
        if isinstance(content, (dict, list)):
            return json.dumps(content, ensure_ascii=False, indent=2)

        values: list[str] = []
        for key, value in params.items():
            if key in {"itemId", "threadId", "turnId"}:
                continue
            if isinstance(value, str) and value:
                values.append(value)
            elif isinstance(value, (dict, list)):
                values.append(json.dumps(value, ensure_ascii=False, indent=2))

        return "\n".join(values).strip()

    def _build_thread_start_params(self, normalized_cwd: str | None) -> dict[str, Any]:
        if self._edit_policy is None:
            params: dict[str, Any] = {
                "approvalPolicy": "never",
                "cwd": normalized_cwd,
                "sandbox": "danger-full-access",
            }
        else:
            params = {
                "approvalPolicy": "on-request",
                "cwd": normalized_cwd,
                "sandbox": "workspace-write",
            }
        if self._dynamic_tools:
            params["dynamicTools"] = [tool.to_thread_start_dict() for tool in self._dynamic_tools]
        return params

    def _handle_server_request(
        self,
        message: dict[str, Any],
        collector: _TurnLogCollector | None = None,
    ) -> bool:
        if "method" not in message or "id" not in message or "result" in message or "error" in message:
            return False

        method = message["method"]
        params = message.get("params", {})

        if method == "item/fileChange/requestApproval":
            response = self._build_file_change_approval_response(params, collector)
            self._write_message({"id": message["id"], "result": response})
            return True

        if method == "item/permissions/requestApproval":
            response = self._build_permissions_approval_response(params, collector)
            self._write_message({"id": message["id"], "result": response})
            return True

        if method == "item/commandExecution/requestApproval":
            response = self._build_command_approval_response(params, collector)
            self._write_message({"id": message["id"], "result": response})
            return True

        if method == "item/tool/call":
            response = self._build_dynamic_tool_call_response(params, collector)
            self._write_message({"id": message["id"], "result": response})
            return True

        return False

    def _build_file_change_approval_response(
        self,
        params: dict[str, Any],
        collector: _TurnLogCollector | None,
    ) -> dict[str, str]:
        if self._edit_policy is None:
            return {"decision": "accept"}

        requested_paths = self._extract_requested_file_change_paths(params, collector)
        if not requested_paths:
            reason = "Declined file change approval because Codex did not provide paths to validate"
            self._record_policy_denial("(unknown path)", reason, collector)
            return {"decision": "decline"}

        violations = self._edit_policy.find_disallowed_write_paths(requested_paths)
        if violations:
            for violation in violations:
                self._record_policy_denial(violation.display_path, violation.reason, collector)
            return {"decision": "decline"}
        return {"decision": "accept"}

    def _build_permissions_approval_response(
        self,
        params: dict[str, Any],
        collector: _TurnLogCollector | None,
    ) -> dict[str, Any]:
        permissions = params.get("permissions")
        if not isinstance(permissions, dict):
            return {"permissions": {}, "scope": "turn"}

        if self._edit_policy is None:
            return {"permissions": permissions, "scope": "turn"}

        requested_file_system = permissions.get("fileSystem")
        requested_network = permissions.get("network")

        granted_permissions: dict[str, Any] = {}
        granted_file_system: dict[str, Any] = {}

        if isinstance(requested_file_system, dict):
            requested_read = requested_file_system.get("read")
            if isinstance(requested_read, list):
                granted_read: list[str] = []
                for value in requested_read:
                    if not isinstance(value, str):
                        continue
                    decision = self._edit_policy.evaluate_read_path(value)
                    if decision.allowed:
                        granted_read.append(str(Path(value).expanduser().resolve(strict=False)))
                    else:
                        self._record_policy_denial(decision.display_path, decision.reason, collector)
                if granted_read:
                    granted_file_system["read"] = granted_read

            requested_write = requested_file_system.get("write")
            if isinstance(requested_write, list):
                granted_write: list[str] = []
                for value in requested_write:
                    if not isinstance(value, str):
                        continue
                    decision = self._edit_policy.evaluate_write_path(value)
                    if decision.allowed:
                        granted_write.append(str(Path(value).expanduser().resolve(strict=False)))
                    else:
                        self._record_policy_denial(decision.display_path, decision.reason, collector)
                if granted_write:
                    granted_file_system["write"] = granted_write

        if granted_file_system:
            granted_permissions["fileSystem"] = granted_file_system
        if isinstance(requested_network, dict):
            granted_permissions["network"] = requested_network

        return {"permissions": granted_permissions, "scope": "turn"}

    def _build_command_approval_response(
        self,
        params: dict[str, Any],
        collector: _TurnLogCollector | None,
    ) -> dict[str, str]:
        command = self._extract_requested_command(params, collector)
        if not command:
            return {"decision": "accept"}

        normalized = self._normalize_command_text(command)
        for blocked_command in self._blocked_commands:
            if self._normalize_command_text(blocked_command) in normalized:
                reason = f"command matches blocked command rule `{blocked_command}`"
                self._record_command_denial(command, reason, collector)
                return {"decision": "decline"}
        return {"decision": "accept"}

    def _extract_requested_file_change_paths(
        self,
        params: dict[str, Any],
        collector: _TurnLogCollector | None,
    ) -> list[str]:
        paths: list[str] = []

        item_id = params.get("itemId")
        if isinstance(item_id, str) and collector is not None:
            state = collector.file_change_states.get(item_id)
            if state is not None:
                for change in state.changes:
                    path = change.get("path")
                    if isinstance(path, str) and path:
                        paths.append(path)

        grant_root = params.get("grantRoot")
        if isinstance(grant_root, str) and grant_root:
            paths.append(grant_root)

        deduped_paths: list[str] = []
        seen: set[str] = set()
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            deduped_paths.append(path)
        return deduped_paths

    def _record_policy_denial(
        self,
        path: str,
        reason: str,
        collector: _TurnLogCollector | None,
    ) -> None:
        message = f"Declined access to `{path}`: {reason}."
        if collector is not None:
            collector.note_error(message)
        if self._thread_id is not None:
            self._session_log.append_policy_denial(self._thread_id, path, reason)

    def _record_command_denial(
        self,
        command: str,
        reason: str,
        collector: _TurnLogCollector | None,
    ) -> None:
        message = f"Declined command `{command}`: {reason}."
        if collector is not None:
            collector.note_error(message)
        if self._thread_id is not None:
            self._session_log.append_command_denial(self._thread_id, command, reason)

    def _extract_requested_command(
        self,
        params: dict[str, Any],
        collector: _TurnLogCollector | None,
    ) -> str:
        command = params.get("command")
        if isinstance(command, str) and command:
            return command

        item_id = params.get("itemId")
        if isinstance(item_id, str) and collector is not None:
            state = collector.command_states.get(item_id)
            if state is not None and state.command:
                return state.command
        return ""

    def _normalize_command_text(self, value: str) -> str:
        return value.casefold()

    def _build_dynamic_tool_call_response(
        self,
        params: dict[str, Any],
        collector: _TurnLogCollector | None,
    ) -> dict[str, object]:
        call_id = params.get("callId")
        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        tool_name = params.get("tool")
        arguments = params.get("arguments")

        if not all(isinstance(value, str) and value for value in (call_id, thread_id, turn_id, tool_name)):
            result = DynamicToolCallResult(text="Dynamic tool call was malformed.", success=False)
            return self._dynamic_tool_response_payload(result)

        tool = self._dynamic_tool_map.get(tool_name)
        state = collector.dynamic_tool_state(call_id) if collector is not None else None
        if state is not None:
            state.tool = tool_name
            state.arguments = arguments

        if tool is None:
            result = DynamicToolCallResult(text=f"Unknown dynamic tool `{tool_name}`.", success=False)
            if state is not None:
                state.set_response(result)
            if collector is not None:
                collector.note_error(result.text)
            return self._dynamic_tool_response_payload(result)

        request = DynamicToolCallRequest(
            call_id=call_id,
            thread_id=thread_id,
            turn_id=turn_id,
            tool=tool_name,
            arguments=arguments,
        )
        try:
            result = tool.handler(request)
        except Exception as exc:
            result = DynamicToolCallResult(
                text=f"Dynamic tool `{tool_name}` failed unexpectedly: {exc}",
                success=False,
            )
            if collector is not None:
                collector.note_error(result.text)

        if state is not None:
            state.set_response(result)
        return self._dynamic_tool_response_payload(result)

    def _dynamic_tool_response_payload(self, result: DynamicToolCallResult) -> dict[str, object]:
        return {
            "success": result.success,
            "contentItems": [
                {
                    "type": "inputText",
                    "text": result.text,
                }
            ],
        }
