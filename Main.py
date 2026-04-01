from __future__ import annotations

from pathlib import Path

from src.Agents.Codex import CodexAgent


EXPLORE_TURN = (
    "Explore the repository in the current working directory and identify the code paths most relevant "
    "to improving the coding-agent workflow. Keep the answer concise and do not modify any files."
)

PROPOSE_TURN = (
    "Based on the repository exploration, propose one concrete improvement to the coding-agent workflow. "
    "Explain why it is high leverage, keep the answer concise, and do not modify any files."
)


def main() -> None:
    project_root = Path(__file__).resolve().parent
    agent = CodexAgent()

    try:
        agent.start_session(str(project_root))
        explore_response = agent.run_instruction(EXPLORE_TURN)
        propose_response = agent.run_instruction(PROPOSE_TURN)
        log_path = agent.session_log_path
        agent.end_session()
    finally:
        agent.close()

    print("Explore Turn:\n")
    print(explore_response)
    print("\nPropose Turn:\n")
    print(propose_response)
    if log_path is not None:
        print(f"\nSession log: {log_path}")


if __name__ == "__main__":
    main()
