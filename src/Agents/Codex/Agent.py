from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .SessionLog import CodexSessionLog, CommandLogEntry, FileChangeLogEntry, TurnLogEntry


class CodexAgentError(RuntimeError):
    """Raised when the Codex app-server session cannot complete a request."""


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
class _TurnLogCollector:
    user_request: str
    command_states: dict[str, _CommandLogState] = field(default_factory=dict)
    file_change_states: dict[str, _FileChangeState] = field(default_factory=dict)
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

    def to_entry(self, codex_response: str) -> TurnLogEntry:
        commands = [state.to_entry() for state in self.command_states.values()]
        file_changes: list[FileChangeLogEntry] = []
        for state in self.file_change_states.values():
            file_changes.extend(state.to_entries())

        return TurnLogEntry(
            user_request=self.user_request,
            codex_response=codex_response,
            commands=commands,
            file_changes=file_changes,
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
    ) -> None:
        self._codex_executable = codex_executable or self._resolve_codex_executable()
        self._client_name = client_name
        self._client_title = client_title
        self._client_version = client_version
        self._session_log = CodexSessionLog(logs_root)
        self._process: subprocess.Popen[str] | None = None
        self._next_request_id = 1
        self._pending_messages: deque[dict[str, Any]] = deque()
        self._thread_id: str | None = None
        self._session_cwd: str | None = None

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
        )

        initialize_result = self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": self._client_name,
                    "title": self._client_title,
                    "version": self._client_version,
                }
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
            {
                "approvalPolicy": "never",
                "cwd": normalized_cwd,
                "sandbox": "danger-full-access",
            },
        )

        self._thread_id = self._extract_thread_id_from_session_result(result, "thread/start")
        self._session_cwd = normalized_cwd
        self._session_log.append_session_started(self._thread_id, normalized_cwd)

    def resume_session(self, thread_id: str) -> None:
        if not thread_id or not thread_id.strip():
            raise ValueError("thread_id must be a non-empty string.")

        self.start()

        if self._thread_id == thread_id:
            return

        if self._thread_id is not None:
            self.end_session()

        result = self._request("thread/resume", {"threadId": thread_id})
        self._thread_id = self._extract_thread_id_from_session_result(result, "thread/resume")
        self._session_log.append_session_resumed(self._thread_id, self._session_cwd)

    def end_session(self) -> None:
        if self._thread_id is None:
            return

        thread_id = self._thread_id
        self._thread_id = None
        self._session_cwd = None
        self._request("thread/unsubscribe", {"threadId": thread_id})

    def run_instruction(self, instruction: str) -> str:
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
        self._session_cwd = None

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

    def _consume_turn(self, expected_turn_id: str, instruction: str) -> str:
        message_buffers: dict[str, str] = {}
        last_message_text = ""
        final_answer_text: str | None = None
        collector = _TurnLogCollector(user_request=instruction)
        did_write_log = False

        def finalize_turn_log(response_text: str, error_message: str | None = None) -> None:
            nonlocal did_write_log
            if did_write_log:
                return
            if error_message:
                collector.note_error(error_message)
            thread_id = self._require_thread_id()
            self._session_log.append_turn(thread_id, collector.to_entry(response_text))
            did_write_log = True

        try:
            while True:
                message = self._read_message()
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
                    elif item.get("type") == "fileChange":
                        item_id = item.get("id")
                        if isinstance(item_id, str) and item_id:
                            file_change_state = collector.file_change_state(item_id)
                            file_change_state.update_from_item(item)
                            if file_change_state.status in {"failed", "declined"}:
                                collector.note_error(f"File change item ended with status {file_change_state.status}.")
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
                        finalize_turn_log(final_answer_text or last_message_text, error_message)
                        raise CodexAgentError(error_message)
                    if status == "interrupted":
                        error_message = "Codex turn was interrupted."
                        finalize_turn_log(final_answer_text or last_message_text, error_message)
                        raise CodexAgentError(error_message)
                    if status != "completed":
                        error_message = f"Unexpected Codex turn status: {status!r}"
                        finalize_turn_log(final_answer_text or last_message_text, error_message)
                        raise CodexAgentError(error_message)
                    response_text = final_answer_text or last_message_text
                    finalize_turn_log(response_text)
                    return response_text

                if method == "item/commandExecution/requestApproval":
                    error_message = "Unexpected approval request from Codex: item/commandExecution/requestApproval."
                    finalize_turn_log(final_answer_text or last_message_text, error_message)
                    raise CodexAgentError(error_message)

                if method == "item/fileChange/requestApproval":
                    error_message = "Unexpected approval request from Codex: item/fileChange/requestApproval."
                    finalize_turn_log(final_answer_text or last_message_text, error_message)
                    raise CodexAgentError(error_message)

                if method == "item/tool/requestUserInput":
                    error_message = "Unexpected approval request from Codex: item/tool/requestUserInput."
                    finalize_turn_log(final_answer_text or last_message_text, error_message)
                    raise CodexAgentError(error_message)

                if method == "item/tool/call":
                    error_message = "Unexpected approval request from Codex: item/tool/call."
                    finalize_turn_log(final_answer_text or last_message_text, error_message)
                    raise CodexAgentError(error_message)
        except Exception as exc:
            finalize_turn_log(final_answer_text or last_message_text, str(exc))
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
                raise CodexAgentError(error.get("message", f"Codex request failed for {method}."))
            if "result" not in message:
                raise CodexAgentError(f"Codex response for {method} did not include a result.")
            if deferred_messages:
                self._pending_messages.extendleft(reversed(deferred_messages))
            return message["result"]

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._write_message({"method": method, "params": params})

    def _write_message(self, message: dict[str, Any]) -> None:
        process = self._require_process()
        if process.stdin is None:
            raise CodexAgentError("Codex app-server stdin is not available.")

        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def _read_message(self) -> dict[str, Any]:
        if self._pending_messages:
            return self._pending_messages.popleft()

        process = self._require_process()
        if process.stdout is None:
            raise CodexAgentError("Codex app-server stdout is not available.")

        line = process.stdout.readline()
        if line == "":
            exit_code = process.poll()
            raise CodexAgentError(
                "Codex app-server closed the connection unexpectedly."
                if exit_code is None
                else f"Codex app-server exited unexpectedly with code {exit_code}."
            )

        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CodexAgentError(f"Codex app-server returned invalid JSON: {line!r}") from exc

        if not isinstance(payload, dict):
            raise CodexAgentError(f"Codex app-server returned an unexpected message: {payload!r}")
        return payload

    def _extract_turn_id(self, result: dict[str, Any]) -> str:
        try:
            return result["turn"]["id"]
        except (KeyError, TypeError) as exc:
            raise CodexAgentError("Codex app-server returned an invalid turn/start response.") from exc

    def _extract_thread_id_from_session_result(self, result: dict[str, Any], operation: str) -> str:
        try:
            return result["thread"]["id"]
        except (KeyError, TypeError) as exc:
            raise CodexAgentError(f"Codex app-server returned an invalid {operation} response.") from exc

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
            raise CodexAgentError(
                f"Codex requested client-side handling for {message['method']}, which this wrapper does not support."
            )

    def _require_process(self) -> subprocess.Popen[str]:
        if self._process is None:
            raise CodexAgentError("Codex agent is not started.")
        return self._process

    def _resolve_codex_executable(self) -> str:
        if sys.platform.startswith("win"):
            return shutil.which("codex.cmd") or shutil.which("codex") or "codex"
        return shutil.which("codex") or "codex"

    def _require_thread_id(self) -> str:
        if self._thread_id is None:
            raise CodexAgentError("Codex thread is not initialized.")
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
