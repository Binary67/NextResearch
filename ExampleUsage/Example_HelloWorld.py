from __future__ import annotations

from pathlib import Path

from src.Agents.Codex import CodexAgent


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    target_file = project_root / "hello_world.py"

    agent = CodexAgent()

    try:
        agent.start_session(str(project_root))
        response = agent.run_instruction(
            'Create or overwrite a file named "hello_world.py" in the current working directory. '
            'The file content must be exactly: print("hello world"). '
            "Do not modify any other files. Reply with a short confirmation after the file is written."
        )
        agent.end_session()
    finally:
        agent.close()

    print(response.response_text)
    print(target_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
