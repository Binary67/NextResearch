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
- Inspect the current repository directly to understand how it works.
- Read `{baseline_state_path}`.
- Read `{experiment_history_path}`.

Your job:
1. Analyze the relevant code in the repository.
2. Choose an improvement strategy likely to improve the objective, and pivot to the next plausible hypothesis if the current one is clearly falsified by evaluation feedback.
3. Implement the smallest useful hypothesis-driven change for the current best hypothesis.
4. Call `{eval_tool_name}` after meaningful changes when score feedback would reduce uncertainty.
5. Use the returned score, deltas, and note to refine or pivot while budget remains. Do not ask the human user for approval before editing or evaluating; this is an unattended optimization run and orchestrator-side approvals are handled programmatically.
6. You may edit one or multiple files when those edits are directly connected to the same strategy.
7. If the strategy depends on coordinated changes, make them together instead of stopping at an isolated patch.
8. Keep exploring after a non-improving evaluation while budget remains. Do not spend evaluation budget re-confirming the baseline just to end the run.
9. Stop early if you find a candidate that improves over the current best score; otherwise stop when the eval budget is exhausted.
10. Run sanity-check commands only when they help validate the current hypothesis.

Constraints:
- Hidden evaluation is only available through `{eval_tool_name}`.
- Keep each change hypothesis-focused, but you may switch to a new plausible hypothesis after evaluation feedback clearly rejects the current one.
- Do not bundle unrelated tweaks.
- Avoid speculative refactors.

Reply using exactly this format:
Strategy: <one concise paragraph>
Why this should help: <one concise paragraph>
