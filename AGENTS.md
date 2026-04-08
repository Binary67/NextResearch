## Project Rules

1. Use `uv run` to run any python file
2. Use `uv add` to add any python dependencies
3. You are not required to create any unit tests
4. If you need to run `uv run Main.py` ask user to run it as it is taking a long time to run. 

## Source Map

Keep this map focused on files and directories that matter for navigation.
Update it when adding, removing, renaming, or moving core modules.

### Entrypoints
- `Main.py`
  - Manual local runner. Loads `config.toml`, builds `ExperimentRunConfig`, and runs the orchestrator.
- `ResetExperiments.py`
  - Local helper for resetting experiment state for one objective.

### Core Orchestration
- `src/Orchestration/ExperimentOrchestrator.py`
  - Top-level coordinator for bootstrap, iteration execution, scoring, ledger logging, and best-branch promotion.
- `src/Orchestration/ExperimentIterationRunner.py`
  - Single-iteration flow: create worktrees, run Codex, apply patch, evaluate, log result, and clean up.
- `src/Orchestration/Models.py`
  - Shared dataclasses and core types, including `ExperimentRunConfig` and iteration results.
- `src/Orchestration/RunConfigFile.py`
  - Loads and validates repo-root `config.toml`.
- `src/Orchestration/GitWorkspace.py`
  - Git worktree, branch, patch, and commit operations.
- `src/Orchestration/EvaluationRunner.py`
  - Runs the external evaluator and parses the score.
- `src/Orchestration/ExperimentLedger.py`
  - Appends and reads the JSONL experiment ledger.
- `src/Orchestration/ExperimentVisualization.py`
  - Regenerates the per-objective SVG progress chart from ledger history.

### Prompting And Run Documents
- `PromptTemplates.md`
  - Prompt source text used by the orchestrator.
- `src/Orchestration/ExperimentPrompts.py`
  - Builds the experiment prompt sent to Codex.
- `src/Orchestration/ExperimentBootstrap.py`
  - Generates bootstrap artifacts like `RUNNING_INSTRUCTIONS.md` and `EVALUATION_SPEC.md`.
- `src/Orchestration/ExperimentRunDocs.py`
  - Builds per-run docs such as baseline and history context.
- `src/Orchestration/ExperimentRunSupport.py`
  - Helper logic for edit policy, sparse checkout patterns, cleanup, and post-run review.

### Codex Integration
- `src/Agents/Codex/Agent.py`
  - Low-level wrapper around `codex app-server`.
- `src/Agents/Codex/SessionRunner.py`
  - Runs a Codex session and enforces edit-policy checks against resulting changes.
- `src/Agents/Codex/SessionLog.py`
  - Markdown session logging.

### Edit Restrictions
- `src/EditPolicy.py`
  - Read/write policy model used to restrict what Codex can access or modify.

### Examples And Reference Docs
- `ExampleUsage/`
  - Small examples for direct Codex usage.
- `Documentations/`
  - Local reference docs, including Codex app-server notes.

### Generated And Runtime Artifacts
- `config.toml`
  - Local run configuration created on first `uv run Main.py`.
- `Logs/`
  - Runtime logs, session logs, experiment ledger, progress SVGs, and temporary worktrees.
- `Cache/`
  - Cached generated artifacts.
- `__pycache__/`, `.venv/`, `.pytest_cache/`
  - Ignore unless debugging environment issues.

### Where To Start
- If changing experiment flow, start in `src/Orchestration/ExperimentOrchestrator.py` and `src/Orchestration/ExperimentIterationRunner.py`.
- If changing config shape or validation, start in `src/Orchestration/Models.py` and `src/Orchestration/RunConfigFile.py`.
- If changing Codex session behavior, start in `src/Agents/Codex/Agent.py` and `src/Agents/Codex/SessionRunner.py`.
- If changing file access restrictions, start in `src/EditPolicy.py` and `src/Orchestration/ExperimentRunSupport.py`.
- If changing prompt wording, start in `PromptTemplates.md` and `src/Orchestration/ExperimentPrompts.py`.
