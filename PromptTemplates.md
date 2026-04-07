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
You may call the dynamic tool `{eval_tool_name}` up to {agent_eval_budget} times during this attempt.
That tool evaluates your current candidate state through the orchestrator and returns sanitized JSON fields:
- `status`
- `score`
- `delta_vs_best`
- `delta_vs_start`
- `budget_remaining`
- `note`

Before making changes:
- Read `{running_instructions_path}`.
- Read `{evaluation_spec_path}`.
- Read `{baseline_state_path}`.
- Read `{experiment_history_path}`.

Your job:
1. Analyze the relevant code across the editable files.
2. Choose an improvement strategy likely to improve the objective, and pivot to the next plausible hypothesis if the current one is clearly falsified by evaluation feedback.
3. Implement the smallest useful hypothesis-driven change for the current best hypothesis.
4. Call `{eval_tool_name}` after meaningful changes when score feedback would reduce uncertainty.
5. Use the returned score, deltas, and note to refine or pivot while budget remains. Do not ask the human user for approval before editing or evaluating; this is an unattended optimization run and orchestrator-side approvals are handled programmatically.
6. You may edit one or multiple allowed files when those edits are directly connected to the same strategy.
7. If the strategy depends on coordinated feature and training changes, make those changes together instead of stopping at an isolated patch.
8. Keep exploring after a non-improving evaluation while budget remains. Do not spend evaluation budget re-confirming the baseline just to end the run.
9. Stop early if you find a candidate that improves over the current best score; otherwise stop when the eval budget is exhausted.
10. If needed, run only sanity-check commands that are allowed by `RUNNING_INSTRUCTIONS.md`.

Constraints:
- Do not read or modify the real evaluator implementation.
- Do not edit files under `.nextresearch`.
- Keep each change hypothesis-focused, but you may switch to a new plausible hypothesis after evaluation feedback clearly rejects the current one.
- Do not bundle unrelated tweaks.
- Avoid speculative refactors.

Reply using exactly this format:
Strategy: <one concise paragraph>
Why this should help: <one concise paragraph>
