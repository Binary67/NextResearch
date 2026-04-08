# Running Instructions Prompt

Inspect the current codebase to understand how it works, then produce the complete markdown for a file named `RUNNING_INSTRUCTIONS.md`.

You have two roles:
1. Inspect the repository to understand how it is run.
2. Publish a sanitized brief for a lower-privilege future Codex session.

Role 1 does not authorize publishing everything you discover during inspection.

Requirements:
- Do not modify any files.
- Reply with the markdown document only. Do not wrap the entire response in a code fence.
- Focus on operational guidance for how a future Codex session should understand, run, and sanity-check this codebase.
- Include concrete commands only when they can be inferred safely from visible or public information.
- Visible-source facts may be stated concretely.
- Hidden-source facts may influence high-level wording, but must not be emitted as concrete identifiers or implementation specifics.
- Do not publish details learned only from hidden or non-readable paths as concrete hidden filenames, module names, symbol/function/class names, artifact names, dataset/schema details, tuple/return shapes, scoring details, or inferred private pipeline structure.
- Prefer command-first guidance over internal pipeline narration. Do not include a step-by-step execution-flow description of helper modules or internal data flow.
- In `Important Paths`, list only operator-facing configuration files, public documentation, and directly runnable entrypoints that a future agent would need to open or execute on purpose. Do not enumerate helper modules, data-loading modules, intermediate artifacts, or evaluator-adjacent implementation files merely because they exist.
- In `Allowed Sanity Checks`, prefer top-level entrypoints and generic smoke checks. Do not suggest importing helper functions or private/internal modules directly.
- Do not mention concrete dataset files or generated artifact filenames in the published document. Refer to them generically only when needed for operational guidance.
- Prefer wording like `preserve evaluator-facing behavior` or `use the documented evaluation flow` over hidden symbol names or private implementation details.
- If a detail cannot be stated safely, omit it and keep the document generic, operational, and actionable.
- Before finalizing, self-sanitize the document and remove anything that would teach the lower-privilege agent something sourced only from hidden or non-readable paths.
- Keep the document concise and practical.

The document must contain these sections:
# Running Instructions
## Overview
## Important Paths
## Setup And Execution
## Allowed Sanity Checks
## Constraints

# Evaluation Spec Prompt

Read the evaluation entrypoint at `{evaluation_relative_path}` to understand the evaluator, then produce the complete markdown for a file named `EVALUATION_SPEC.md`.

You are publishing a sanitized public contract for a lower-privilege optimizer agent, not a full evaluator walkthrough.

Requirements:
- Do not modify any files.
- Reply with the markdown document only. Do not wrap the entire response in a code fence.
- Summarize only the public contract an optimizer needs.
- Visible-source facts may be stated concretely.
- Hidden-source facts may influence high-level wording, but must not be emitted as concrete identifiers or implementation specifics.
- Do not reveal the exact scoring formula or exploitable implementation details.
- Mention that the orchestrator will run the evaluation command `{evaluation_command}`.
- Do not emit hidden file/module names, concrete hidden interface names, exact artifact paths/names, exact return shapes, data schema, or evaluator implementation details unless that exact detail is already present in visible/public sources the experiment agent can read.
- Apart from the evaluation command itself, avoid concrete filenames, module names, function names, and artifact names. Prefer capability-level language over implementation-level labels.
- Prefer contract language like `preserve the evaluation-facing training/prediction interface` over exact hidden function names, tuple shapes, or private artifact details.
- If a requirement cannot be stated safely, omit it or restate it at a higher level without concrete hidden identifiers.
- Before finalizing, self-sanitize the document and remove anything that would teach the lower-privilege agent something sourced only from hidden or non-readable paths.

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
