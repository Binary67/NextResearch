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
    editable_rules: tuple[EditPolicyRule, ...]
    blocked_write_rules: tuple[EditPolicyRule, ...]

    @classmethod
    def from_paths(
        cls,
        repo_root: Path | str,
        session_cwd: Path | str | None = None,
        editable_paths: tuple[str, ...] = (),
        blocked_write_paths: tuple[str, ...] = (),
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
        blocked_candidates = cls._validate_rule_paths("blocked_write_paths", blocked_write_paths)

        editable_rules = tuple(cls._build_rule(root, raw_path) for raw_path in editable_candidates)
        blocked_write_rules = tuple(cls._build_rule(root, raw_path) for raw_path in blocked_candidates)
        return cls(
            repo_root=root,
            session_cwd=cwd,
            editable_rules=editable_rules,
            blocked_write_rules=blocked_write_rules,
        )

    @classmethod
    def validate_config_paths(
        cls,
        repo_root: Path | str,
        editable_paths: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        root = Path(repo_root).expanduser().resolve()
        if not root.exists():
            raise ValueError(f"repo_root does not exist: {repo_root}")
        if not root.is_dir():
            raise ValueError(f"repo_root is not a directory: {repo_root}")

        return cls._validate_rule_targets(
            root,
            "editable_paths",
            editable_paths,
            allow_missing_leaf=True,
        )

    @property
    def has_editable_paths(self) -> bool:
        return bool(self.editable_rules)

    def editable_rule_paths(self) -> tuple[str, ...]:
        return tuple(rule.display_path for rule in self.editable_rules)

    def prompt_prefix(self) -> str:
        if not self.editable_rules:
            return ""

        editable_text = ", ".join(self.editable_rule_paths())
        return "\n".join(
            (
                "Writable scope for this run:",
                f"- Allowed write paths: {editable_text}",
                "- Only modify files under the allowed write paths for this run.",
            )
        )

    def writable_scope_summary(self) -> str:
        editable_text = ", ".join(self.editable_rule_paths())
        if editable_text:
            return editable_text
        return "all repo paths"

    def resolve_path(self, path: str | Path) -> Path | None:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.session_cwd / candidate

        resolved = candidate.expanduser().resolve(strict=False)
        try:
            resolved.relative_to(self.repo_root)
        except ValueError:
            return None
        return resolved

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

        blocked_rule = self._match_rule(normalized_path, self.blocked_write_rules)
        if blocked_rule is not None:
            return EditPolicyDecision(
                requested_path=requested_path,
                normalized_path=normalized_path,
                display_path_value=display_path,
                allowed=False,
                reason=f"path matches internal blocked path `{blocked_rule.display_path}`",
            )

        if not self.editable_rules:
            return EditPolicyDecision(
                requested_path=requested_path,
                normalized_path=normalized_path,
                display_path_value=display_path,
                allowed=True,
                reason="path is editable because editable_paths is not configured",
            )

        editable_rule = self._match_rule(normalized_path, self.editable_rules)
        if editable_rule is None:
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
            reason=f"path matches editable_paths rule `{editable_rule.display_path}`",
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
        candidate = cls._resolve_candidate_path(session_cwd, path)
        try:
            relative = candidate.relative_to(repo_root)
        except ValueError:
            return None
        display_path = cls._display_path_from_relative(relative)
        return cls._fold_case(display_path), display_path

    @staticmethod
    def _resolve_candidate_path(session_cwd: Path, path: str | Path) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = session_cwd / candidate
        return candidate.expanduser().resolve(strict=False)

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
