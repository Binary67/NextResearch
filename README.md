# NextResearch

NextResearch is a Python experiment orchestrator for running Codex-guided optimization attempts against another Git repository. Each attempt runs in an isolated Git worktree, is scored by an external evaluation command, and can be promoted to a `best/<objective>` branch when it improves the score.

## Status

This repository is currently closer to a library/prototype than a polished end-user application.

- The main workflow is exposed as Python classes in `src/Orchestration`.
- `Main.py` is a local example runner, not a general-purpose entrypoint.
- The target repository and evaluator are expected to exist outside this repository.

## What It Does

- Resolves the target repository and verifies it is a clean Git working tree.
- Uses Codex to generate bootstrap artifacts for the target repository:
  - `RUNNING_INSTRUCTIONS.md`
  - `EVALUATION_SPEC.md`
- Builds deterministic per-run context documents:
  - `BASELINE_STATE.md`
  - `EXPERIMENT_HISTORY.md`
- Creates an isolated worktree and experiment branch for each iteration.
- Asks Codex to make one coherent improvement attempt, which may include coordinated edits across the allowed files.
- Runs an external evaluation command and parses the score.
- Keeps the best result on `best/<objective>`.
- Writes a JSONL experiment ledger and markdown session logs under `Logs/`.

## Requirements

- Python `>=3.13`
- `uv`
- `git`
- `codex` CLI available on `PATH`
- a target repository that is already a Git repository
- a clean working tree in the target repository before experiments start
- an evaluation command whose last non-empty stdout line is a numeric score

## Installation

Install dependencies with `uv`:

```bash
uv sync
```

## Core Concepts

### `ExperimentOrchestrator`

The main entrypoint for running optimization attempts. It handles:

- bootstrap document generation
- Git worktree lifecycle
- Codex session execution
- evaluation
- promotion of improved results
- ledger logging

### `ExperimentRunConfig`

The run configuration includes:

- `target_repo_path`
- `objective_name`
- `evaluation_command`
- `iteration_count`
- `optimization_direction`
- optional `evaluation_file_path`
- optional `baseline_branch`
- optional `editable_paths`
- optional `non_editable_paths`
- optional `non_readable_paths`

Prompt wording is loaded from the top-level `PromptTemplates.md` file.

### Evaluation Contract

The evaluator is treated as an external command and must follow this contract:

- it is executed with the configured `evaluation_command`
- a non-zero exit code fails the run
- the last non-empty line of stdout must be parseable as a `float`
- `optimization_direction` decides whether lower or higher scores are better

If the evaluator file path cannot be inferred from the command, you must provide `evaluation_file_path` explicitly.

## Minimal Usage

Use the Python API directly:

```python
from src.Orchestration import ExperimentOrchestrator, ExperimentRunConfig

config = ExperimentRunConfig(
    target_repo_path="D:/path/to/target-repo",
    objective_name="maximize-evaluation-score",
    evaluation_command="uv run evaluation.py",
    iteration_count=3,
    optimization_direction="minimize",
)

orchestrator = ExperimentOrchestrator()
results = orchestrator.run_iterations(config)

for result in results:
    print(
        f"{result.run_id} status={result.status} improved={result.improved} "
        f"score={result.score} delta={result.score_delta}"
    )
```

## How A Run Works

1. Resolve the target repository root.
2. Verify the repository is clean.
3. Bootstrap repo-specific instructions using Codex.
4. Compute the current best score from the starting reference.
5. Create an isolated experiment branch and full orchestrator worktree.
6. Create a restricted sparse worktree for the Codex agent.
7. Ask Codex to make one coherent improvement in the restricted worktree, including coordinated multi-file edits when one strategy needs them.
8. Apply the resulting patch to the full orchestrator worktree.
9. Run the evaluator in the full orchestrator worktree.
10. If the score improves, commit the change and update `best/<objective>`.
11. Append the run result to the experiment ledger.
12. Clean up the temporary worktrees and branch.

## Outputs

NextResearch writes runtime artifacts under `Logs/`:

- `Logs/codex_experiments.jsonl`
  - append-only experiment ledger
- `Logs/codex_session_*.md`
  - human-readable Codex session logs
- `Logs/Worktrees/`
  - temporary worktrees used during scoring and experiment runs

## Prompt Templates

Prompt templates for:

- `RUNNING_INSTRUCTIONS.md` generation
- `EVALUATION_SPEC.md` generation
- experiment execution

are stored in the top-level `PromptTemplates.md` file. The orchestrator reads the required prompt sections from that file and fails fast if a required section is missing or empty.

## Project Structure

- `src/Orchestration/`
  - orchestration workflow, Git workspace management, evaluation runner, and ledger
- `src/Agents/Codex/`
  - wrapper around `codex app-server` and markdown session logging
- `ExampleUsage/`
  - minimal examples for direct Codex interaction
- `Documentations/`
  - reference material, including a local copy of Codex app-server documentation
- `Main.py`
  - local example runner with hardcoded values

## Examples In This Repo

Run the local examples with `uv run`:

```bash
uv run ExampleUsage/Example_HelloWorld.py
uv run ExampleUsage/Example_Logging.py
```

`Main.py` is also runnable:

```bash
uv run Main.py
```

However, the current file contains environment-specific hardcoded values and should be treated as a local example, not the primary usage path.

`ResetExperiments.py` is a local reset helper for starting fresh on a single objective:

```bash
uv run ResetExperiments.py
```

Set `TARGET_REPO_PATH`, `OBJECTIVE_NAME`, and optionally `DELETE_ALL_LOGS` inside the script before running it.

## Limitations

- The target repository must be clean before a run starts.
- This repository does not include a built-in evaluator.
- `Main.py` is currently hardcoded to a local path and example evaluation command.
- There is no test suite in this repository.
