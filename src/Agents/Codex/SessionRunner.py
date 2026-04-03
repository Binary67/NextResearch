from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.EditPolicy import EditPolicy

from .Agent import CodexAgent, CodexTurnResult


@dataclass(frozen=True)
class CodexSessionRunResult:
    turn_result: CodexTurnResult
    session_log_path: Path | None


class CodexSessionRunner:
    def __init__(
        self,
        codex_executable: str | None = None,
        logs_root: Path | str | None = None,
    ) -> None:
        self._codex_executable = codex_executable
        self._logs_root = logs_root

    def run(
        self,
        cwd: Path,
        instruction: str,
        edit_policy: EditPolicy | None = None,
    ) -> CodexSessionRunResult:
        agent = CodexAgent(
            codex_executable=self._codex_executable,
            logs_root=self._logs_root,
            edit_policy=edit_policy,
        )
        try:
            agent.start_session(str(cwd))
            turn_result = agent.run_instruction(instruction)
            session_log_path = agent.session_log_path
            if edit_policy is not None:
                violations = edit_policy.find_disallowed_paths([entry.path for entry in turn_result.file_changes])
                if violations:
                    violation_message = "; ".join(
                        f"{entry.display_path}: {entry.reason}" for entry in violations
                    )
                    agent.append_policy_violation(violation_message)
                    raise agent.build_error(f"Codex modified disallowed paths: {violation_message}")
            agent.end_session()
        finally:
            agent.close()

        return CodexSessionRunResult(turn_result=turn_result, session_log_path=session_log_path)
