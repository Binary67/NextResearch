from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections import deque
from pathlib import Path
from typing import Any


class CodexAgentError(RuntimeError):
    """Raised when the Codex app-server session cannot complete a request."""


class CodexAgent:
    def __init__(
        self,
        codex_executable: str | None = None,
        client_name: str = "nextresearch",
        client_title: str = "NextResearch",
        client_version: str = "0.1.0",
    ) -> None:
        self._codex_executable = codex_executable or self._resolve_codex_executable()
        self._client_name = client_name
        self._client_title = client_title
        self._client_version = client_version
        self._process: subprocess.Popen[str] | None = None
        self._next_request_id = 1
        self._pending_messages: deque[dict[str, Any]] = deque()
        self._thread_id: str | None = None

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

        try:
            self._thread_id = result["thread"]["id"]
        except (KeyError, TypeError) as exc:
            raise CodexAgentError("Codex app-server returned an invalid thread/start response.") from exc

    def end_session(self) -> None:
        if self._thread_id is None:
            return

        thread_id = self._thread_id
        self._thread_id = None
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
        return self._consume_turn(turn_id)

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

    def _consume_turn(self, expected_turn_id: str) -> str:
        message_buffers: dict[str, str] = {}
        last_message_text = ""
        final_answer_text: str | None = None

        while True:
            message = self._read_message()
            self._raise_for_server_request(message)

            if "id" in message:
                raise CodexAgentError(f"Unexpected JSON-RPC response while waiting for turn events: {message!r}")

            method = message.get("method")
            params = message.get("params", {})

            if method == "item/agentMessage/delta":
                item_id = params["itemId"]
                message_buffers[item_id] = message_buffers.get(item_id, "") + params["delta"]
                last_message_text = message_buffers[item_id]
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
                    raise CodexAgentError(error.get("message", "Codex turn failed."))
                if status == "interrupted":
                    raise CodexAgentError("Codex turn was interrupted.")
                if status != "completed":
                    raise CodexAgentError(f"Unexpected Codex turn status: {status!r}")
                return final_answer_text or last_message_text

            if method in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
                "item/tool/requestUserInput",
                "item/tool/call",
            }:
                raise CodexAgentError(f"Unexpected approval or tool request from Codex: {method}.")

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
