from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EditPolicyRule:
    normalized_path: str
    display_path_value: str
    is_directory: bool

    @property
    def display_path(self) -> str:
        if not self.display_path_value:
            return "./"
        if self.is_directory:
            return f"{self.display_path_value}/"
        return self.display_path_value

    def matches(self, normalized_path: str) -> bool:
        if not self.normalized_path:
            return True
        if normalized_path == self.normalized_path:
            return True
        return self.is_directory and normalized_path.startswith(f"{self.normalized_path}/")


@dataclass(frozen=True)
class EditPolicyDecision:
    requested_path: str
    normalized_path: str
    display_path_value: str
    allowed: bool
    reason: str

    @property
    def display_path(self) -> str:
        return self.display_path_value or "./"


@dataclass(frozen=True)
class EditPolicy:
    repo_root: Path
    session_cwd: Path
    allow_rules: tuple[EditPolicyRule, ...]
    deny_rules: tuple[EditPolicyRule, ...]
    hidden_rules: tuple[EditPolicyRule, ...]

    @classmethod
    def from_paths(
        cls,
        repo_root: Path | str,
        session_cwd: Path | str | None = None,
        editable_paths: tuple[str, ...] = (),
        non_editable_paths: tuple[str, ...] = (),
        non_readable_paths: tuple[str, ...] = (),
    ) -> EditPolicy:
        root = Path(repo_root).expanduser().resolve()
        if not root.exists():
            raise ValueError(f"repo_root does not exist: {repo_root}")
        if not root.is_dir():
            raise ValueError(f"repo_root is not a directory: {repo_root}")
        cwd = root if session_cwd is None else Path(session_cwd).expanduser().resolve()
        try:
            cwd.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"session_cwd must stay within repo_root: {session_cwd}") from exc

        editable_candidates = cls._validate_rule_paths("editable_paths", editable_paths)
        non_editable_candidates = cls._validate_rule_paths("non_editable_paths", non_editable_paths)
        non_readable_candidates = cls._validate_rule_paths("non_readable_paths", non_readable_paths)

        allow_rules = tuple(
            cls._build_rule(root, raw_path)
            for raw_path in editable_candidates
        )
        deny_rules = tuple(
            cls._build_rule(root, raw_path)
            for raw_path in non_editable_candidates
        )
        hidden_rules = tuple(
            cls._build_rule(root, raw_path)
            for raw_path in non_readable_candidates
        )
        return cls(
            repo_root=root,
            session_cwd=cwd,
            allow_rules=allow_rules,
            deny_rules=deny_rules,
            hidden_rules=hidden_rules,
        )

    @classmethod
    def validate_config_paths(
        cls,
        repo_root: Path | str,
        editable_paths: tuple[str, ...] = (),
        non_editable_paths: tuple[str, ...] = (),
        non_readable_paths: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        root = Path(repo_root).expanduser().resolve()
        if not root.exists():
            raise ValueError(f"repo_root does not exist: {repo_root}")
        if not root.is_dir():
            raise ValueError(f"repo_root is not a directory: {repo_root}")

        errors = [
            *cls._validate_rule_targets(root, "editable_paths", editable_paths, allow_missing_leaf=True),
            *cls._validate_rule_targets(root, "non_editable_paths", non_editable_paths, allow_missing_leaf=False),
            *cls._validate_rule_targets(root, "non_readable_paths", non_readable_paths, allow_missing_leaf=False),
        ]
        return tuple(errors)

    @property
    def mode_label(self) -> str:
        if self.allow_rules:
            return "only-editable-minus-non-editable"
        return "all-except-non-editable"

    def editable_rule_paths(self) -> tuple[str, ...]:
        return tuple(rule.display_path for rule in self.allow_rules)

    def non_editable_rule_paths(self) -> tuple[str, ...]:
        return tuple(rule.display_path for rule in self.deny_rules)

    def non_readable_rule_paths(self) -> tuple[str, ...]:
        return tuple(rule.display_path for rule in self.hidden_rules)

    def prompt_prefix(self) -> str:
        editable_text = ", ".join(self.editable_rule_paths()) or "all repo paths"
        non_editable_text = ", ".join(self.non_editable_rule_paths()) or "none"
        lines = [
            "Edit policy for this run:",
            f"- Mode: {self.mode_label}",
            f"- Editable paths: {editable_text}",
            f"- Non-editable paths: {non_editable_text}",
        ]
        if self.hidden_rules:
            lines.append("- Some repository paths are hidden from this run and unavailable to read or modify.")
        lines.append("- If a needed change falls outside the editable set, explain it instead of editing it.")
        return "\n".join(lines)

    def evaluate_read_path(self, path: str | Path) -> EditPolicyDecision:
        requested_path = str(path)
        candidate_info = self._normalize_candidate_path(self.repo_root, self.session_cwd, path)
        if candidate_info is None:
            return EditPolicyDecision(
                requested_path=requested_path,
                normalized_path=requested_path,
                display_path_value=requested_path,
                allowed=False,
                reason="path is outside the repository root",
            )
        normalized_path, display_path = candidate_info

        hidden_rule = self._match_rule(normalized_path, self.hidden_rules)
        if hidden_rule is not None:
            return EditPolicyDecision(
                requested_path=requested_path,
                normalized_path=normalized_path,
                display_path_value=display_path,
                allowed=False,
                reason=f"path matches non_readable_paths rule `{hidden_rule.display_path}`",
            )

        return EditPolicyDecision(
            requested_path=requested_path,
            normalized_path=normalized_path,
            display_path_value=display_path,
            allowed=True,
            reason="path is readable",
        )

    def evaluate_write_path(self, path: str | Path) -> EditPolicyDecision:
        read_decision = self.evaluate_read_path(path)
        if not read_decision.allowed:
            return read_decision

        requested_path = str(path)
        normalized_path = read_decision.normalized_path
        display_path = read_decision.display_path_value
        deny_rule = self._match_rule(normalized_path, self.deny_rules)
        if deny_rule is not None:
            return EditPolicyDecision(
                requested_path=requested_path,
                normalized_path=normalized_path,
                display_path_value=display_path,
                allowed=False,
                reason=f"path matches non_editable_paths rule `{deny_rule.display_path}`",
            )

        if not self.allow_rules:
            return EditPolicyDecision(
                requested_path=requested_path,
                normalized_path=normalized_path,
                display_path_value=display_path,
                allowed=True,
                reason="path is editable because editable_paths is empty",
            )

        allow_rule = self._match_rule(normalized_path, self.allow_rules)
        if allow_rule is None:
            return EditPolicyDecision(
                requested_path=requested_path,
                normalized_path=normalized_path,
                display_path_value=display_path,
                allowed=False,
                reason="path is outside editable_paths",
            )

        return EditPolicyDecision(
            requested_path=requested_path,
            normalized_path=normalized_path,
            display_path_value=display_path,
            allowed=True,
            reason=f"path matches editable_paths rule `{allow_rule.display_path}`",
        )

    def find_disallowed_write_paths(self, paths: list[str]) -> list[EditPolicyDecision]:
        decisions: list[EditPolicyDecision] = []
        seen: set[str] = set()
        for path in paths:
            decision = self.evaluate_write_path(path)
            key = f"{decision.display_path}|{decision.reason}"
            if decision.allowed or key in seen:
                continue
            seen.add(key)
            decisions.append(decision)
        return decisions

    @staticmethod
    def _validate_rule_paths(field_name: str, raw_paths: tuple[str, ...]) -> tuple[str, ...]:
        if isinstance(raw_paths, str):
            raise TypeError(
                f"{field_name} must be a tuple[str, ...] or list[str], not a string."
            )

        normalized: list[str] = []
        for raw_path in raw_paths:
            if not isinstance(raw_path, str):
                raise TypeError(f"{field_name} entries must be strings; got {type(raw_path).__name__}.")
            stripped = raw_path.strip()
            if not stripped:
                raise ValueError(f"{field_name} entries must be non-empty strings.")
            normalized.append(stripped)
        return tuple(normalized)

    @classmethod
    def _validate_rule_targets(
        cls,
        repo_root: Path,
        field_name: str,
        raw_paths: tuple[str, ...],
        *,
        allow_missing_leaf: bool,
    ) -> tuple[str, ...]:
        normalized_paths = cls._validate_rule_paths(field_name, raw_paths)
        errors: list[str] = []
        for raw_path in normalized_paths:
            try:
                cls._build_rule(repo_root, raw_path)
            except ValueError as exc:
                errors.append(f"{field_name} `{raw_path}` is invalid: {exc}")
                continue

            candidate = Path(raw_path)
            source_path = repo_root / candidate
            if source_path.exists():
                continue

            is_directory_rule = raw_path.endswith(("/", "\\"))
            if allow_missing_leaf and not is_directory_rule and source_path.parent.is_dir():
                continue

            errors.append(
                cls._missing_path_error(
                    repo_root,
                    field_name,
                    raw_path,
                    source_path,
                    allow_missing_leaf=allow_missing_leaf,
                )
            )
        return tuple(errors)

    @classmethod
    def _missing_path_error(
        cls,
        repo_root: Path,
        field_name: str,
        raw_path: str,
        source_path: Path,
        *,
        allow_missing_leaf: bool,
    ) -> str:
        if not allow_missing_leaf:
            return f"{field_name} `{raw_path}` does not exist."

        if raw_path.endswith(("/", "\\")):
            return f"{field_name} `{raw_path}` does not exist."

        parent_relative = cls._display_path_from_relative(
            source_path.parent.resolve(strict=False).relative_to(repo_root)
        )
        parent_display = parent_relative or "./"
        return (
            f"{field_name} `{raw_path}` does not exist, and its parent directory "
            f"`{parent_display}` does not exist."
        )

    @classmethod
    def _build_rule(cls, repo_root: Path, raw_path: str) -> EditPolicyRule:
        stripped = raw_path.strip()
        if not stripped:
            raise ValueError("edit policy paths must be non-empty strings")

        candidate = Path(stripped)
        if candidate.is_absolute():
            raise ValueError(f"edit policy paths must be repo-relative: {raw_path}")

        resolved = (repo_root / candidate).resolve(strict=False)
        try:
            relative = resolved.relative_to(repo_root)
        except ValueError as exc:
            raise ValueError(f"edit policy path escapes the repository root: {raw_path}") from exc

        display_path = cls._display_path_from_relative(relative)
        normalized_path = cls._fold_case(display_path)
        source_path = repo_root / candidate
        is_directory = stripped.endswith(("/", "\\")) or source_path.is_dir()
        return EditPolicyRule(
            normalized_path=normalized_path,
            display_path_value=display_path,
            is_directory=is_directory,
        )

    @classmethod
    def _normalize_candidate_path(
        cls,
        repo_root: Path,
        session_cwd: Path,
        path: str | Path,
    ) -> tuple[str, str] | None:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = session_cwd / candidate

        resolved = candidate.expanduser().resolve(strict=False)
        try:
            relative = resolved.relative_to(repo_root)
        except ValueError:
            return None
        display_path = cls._display_path_from_relative(relative)
        return cls._fold_case(display_path), display_path

    @classmethod
    def _display_path_from_relative(cls, relative_path: Path) -> str:
        normalized = relative_path.as_posix()
        if normalized == ".":
            normalized = ""
        return normalized.strip("/")


    @staticmethod
    def _fold_case(value: str) -> str:
        if os.name == "nt":
            return value.casefold()
        return value

    @staticmethod
    def _match_rule(normalized_path: str, rules: tuple[EditPolicyRule, ...]) -> EditPolicyRule | None:
        for rule in rules:
            if rule.matches(normalized_path):
                return rule
        return None
