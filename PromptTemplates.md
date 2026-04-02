# Running Instructions Prompt

Inspect the current codebase and produce the complete markdown for a file named `RUNNING_INSTRUCTIONS.md`.

Requirements:
- Do not modify any files.
- Reply with the markdown document only. Do not wrap the entire response in a code fence.
- Focus on how a future Codex session should understand, run, and sanity-check this codebase.
- Include concrete commands when they can be inferred safely.
- Keep the document concise and practical.

The document must contain these sections:
# Running Instructions
## Overview
## Important Paths
## Setup And Execution
## Allowed Sanity Checks
## Constraints

# Evaluation Spec Prompt

Read the evaluation entrypoint at `{evaluation_relative_path}` and produce the complete markdown for a file named `EVALUATION_SPEC.md`.

Requirements:
- Do not modify any files.
- Reply with the markdown document only. Do not wrap the entire response in a code fence.
- Summarize only the public contract an optimizer needs.
- Do not reveal the exact scoring formula or exploitable implementation details.
- Mention that the orchestrator will run the evaluation command `{evaluation_command}`.

The document must contain these sections:
# Evaluation Spec
## Objective
## Interfaces The Evaluator Depends On
## Evaluation Command
## Constraints

# Experiment Prompt

You are running one automated optimization attempt for the objective "{objective_name}".

Before making changes:
- Read `{running_instructions_path}`.
- Read `{evaluation_spec_path}`.

Your job:
1. Analyze the relevant code.
2. Choose one concrete improvement likely to improve the objective.
3. Implement only that improvement.
4. If needed, run only sanity-check commands that are allowed by `RUNNING_INSTRUCTIONS.md`.

Constraints:
- Do not read or modify the real evaluator implementation.
- Do not edit files under `.nextresearch`.
- Keep the change scoped.
- Avoid speculative refactors.

Reply with a concise summary of what you changed and why it should improve the objective.
