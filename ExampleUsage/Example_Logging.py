from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.Agents.Codex import CodexAgent


FIRST_TURN = (
    "Inspect the current repository and summarize the purpose of the main top-level folders. "
    "Keep the answer short and do not modify any files."
)

SECOND_TURN = (
    "Now focus on src/Agents/Codex and explain how the Codex session logging works today. "
    "Mention the main sections that appear in the human-readable log. Keep the answer short and do not modify any files."
)


def main() -> None:
    agent = CodexAgent()

    try:
        agent.start_session(str(PROJECT_ROOT))

        first_response = agent.run_instruction(FIRST_TURN)
        second_response = agent.run_instruction(SECOND_TURN)

        log_path = agent.session_log_path
        agent.end_session()
    finally:
        agent.close()

    if log_path is None:
        raise RuntimeError("Codex session did not expose a log path.")


if __name__ == "__main__":
    main()
