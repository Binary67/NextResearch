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
- Review the orchestrator-provided optimization context below. It is runtime context, not part of the candidate repository.

{baseline_state}

{experiment_history}

Your job:
1. Analyze the relevant code in the repository.
2. Choose an improvement strategy likely to improve the objective, and pivot to the next plausible hypothesis if the current one is clearly falsified by evaluation feedback.
3. Implement the smallest useful hypothesis-driven change for the current best hypothesis.
4. Treat `{eval_tool_name}` as the primary optimization signal and call it regularly after meaningful changes.
5. Use the returned score, deltas, and note to refine or pivot while budget remains. Do not ask the human user for approval before editing or evaluating; this is an unattended optimization run and orchestrator-side approvals are handled programmatically.
6. You may edit one or multiple files when those edits are directly connected to the same strategy.
7. If the strategy depends on coordinated changes, make them together instead of stopping at an isolated patch.
8. Keep exploring after a non-improving evaluation while budget remains. Do not spend evaluation budget re-confirming the baseline just to end the run.
9. Stop early if you find a candidate that improves over the current best score; otherwise stop when the eval budget is exhausted.
10. Run sanity-check commands and local evaluation commands only when they help validate the current hypothesis.
11. If the current hypothesis needs an additional Python dependency, you may add it yourself with `uv add <package>`. Keep dependency changes minimal and directly tied to the current strategy.
12. Treat local evaluation as a supporting signal only. Do not optimize against local evaluation alone.
13. After at most 2 local evaluation runs for the current hypothesis, call `{eval_tool_name}` before continuing with further optimization work.
14. If you complete a candidate patch that you would otherwise continue iterating on, call `{eval_tool_name}` before making another substantial round of changes.
15. Do not go long stretches of local-only iteration when `{eval_tool_name}` budget remains.

Constraints:
- Hidden evaluation is only available through `{eval_tool_name}` and is the true optimization signal for this run.
- Local evaluation may be used for quick checks, but it must not replace regular use of `{eval_tool_name}`.
- Do not add speculative packages or unrelated tooling; only add dependencies that are necessary for the current hypothesis.
- Keep each change hypothesis-focused, but you may switch to a new plausible hypothesis after evaluation feedback clearly rejects the current one.
- Do not bundle unrelated tweaks.
- Avoid speculative refactors.

Reply using exactly this format:
Strategy: <one concise paragraph>
Why this should help: <one concise paragraph>
