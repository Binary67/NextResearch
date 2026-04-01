from __future__ import annotations


def build_prepare_context_prompt(
    objective: str,
    editable_roots: list[str],
    forbidden_roots: list[str],
) -> str:
    editable_roots_block = "\n".join(f"- {path}" for path in editable_roots)
    forbidden_roots_block = "\n".join(f"- {path}" for path in forbidden_roots) or "- None specified"

    return (
        "You are in the prepare-context phase for an experiment orchestration workflow.\n"
        "Read the codebase in the current working directory and produce a structured context summary only.\n\n"
        "Hard constraints:\n"
        "- Do not create, modify, or delete files.\n"
        "- Do not run experiments, training jobs, or evaluations.\n"
        "- Do not propose implementation patches in this response.\n"
        "- Focus on understanding the repository and what parts are likely relevant to the objective.\n\n"
        f"Objective:\n{objective}\n\n"
        "Editable roots for later phases:\n"
        f"{editable_roots_block}\n\n"
        "Forbidden roots for later phases:\n"
        f"{forbidden_roots_block}\n\n"
        "Return the summary using exactly these section headers:\n"
        "## Repository Purpose\n"
        "## Key Entry Points\n"
        "## Important Modules\n"
        "## Current Execution Flow\n"
        "## Likely Modification Areas\n"
        "## Risks And Unknowns\n"
    )
