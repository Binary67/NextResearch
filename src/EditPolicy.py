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
class EditPolicyExtraRoot:
    path: Path
    normalized_path: str

    @property
    def display_path(self) -> str:
        return _ensure_trailing_separator(str(self.path))

    def matches(self, normalized_path: str) -> bool:
        if normalized_path == self.normalized_path:
            return True
        return normalized_path.startswith(f"{self.normalized_path}{os.sep}")


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
    extra_writable_roots: tuple[EditPolicyExtraRoot, ...]

    @classmethod
    def from_paths(
        cls,
        repo_root: Path | str,
        session_cwd: Path | str | None = None,
        editable_paths: tuple[str, ...] = (),
        blocked_write_paths: tuple[str, ...] = (),
        extra_writable_roots: tuple[Path | str, ...] = (),
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
        extra_root_rules = cls._build_extra_root_rules(root, extra_writable_roots)
        return cls(
            repo_root=root,
            session_cwd=cwd,
            editable_rules=editable_rules,
            blocked_write_rules=blocked_write_rules,
            extra_writable_roots=extra_root_rules,
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

    def writable_scope_paths(self) -> tuple[str, ...]:
        entries: list[str] = []
        if self.editable_rules:
            entries.extend(rule.display_path for rule in self.editable_rules)
        else:
            entries.append("all repo paths")
        entries.extend(root.display_path for root in self.extra_writable_roots)
        return tuple(entries)

    def prompt_prefix(self) -> str:
        lines = [
            "Writable scope for this run:",
            f"- Allowed repo write paths: {self._repo_writable_scope_text()}",
        ]
        if self.extra_writable_roots:
            extra_root_text = ", ".join(root.display_path for root in self.extra_writable_roots)
            lines.append(f"- Allowed extra write paths: {extra_root_text}")
        lines.append("- Only modify files under the allowed write paths for this run.")
        return "\n".join(lines)

    def writable_scope_summary(self) -> str:
        entries = [f"repo={self._repo_writable_scope_text()}"]
        if self.extra_writable_roots:
            extra_root_text = ", ".join(root.display_path for root in self.extra_writable_roots)
            entries.append(f"extra={extra_root_text}")
        return "; ".join(entries)

    def resolve_path(self, path: str | Path) -> Path | None:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.session_cwd / candidate

        resolved = candidate.expanduser().resolve(strict=False)
        try:
            resolved.relative_to(self.repo_root)
            return resolved
        except ValueError:
            return resolved if self._match_extra_root(resolved) is not None else None

    def evaluate_read_path(self, path: str | Path) -> EditPolicyDecision:
        requested_path = str(path)
        classification = self._classify_candidate_path(path)
        if classification is None:
            return EditPolicyDecision(
                requested_path=requested_path,
                normalized_path=requested_path,
                display_path_value=requested_path,
                allowed=False,
                reason="path is outside the repository root and allowed extra writable roots",
            )

        normalized_path, display_path, extra_root = classification
        if extra_root is not None:
            return EditPolicyDecision(
                requested_path=requested_path,
                normalized_path=normalized_path,
                display_path_value=display_path,
                allowed=True,
                reason=f"path is readable because it is under allowed extra root `{extra_root.display_path}`",
            )

        return EditPolicyDecision(
            requested_path=requested_path,
            normalized_path=normalized_path,
            display_path_value=display_path,
            allowed=True,
            reason="path is readable",
        )

    def evaluate_write_path(self, path: str | Path) -> EditPolicyDecision:
        requested_path = str(path)
        classification = self._classify_candidate_path(path)
        if classification is None:
            return EditPolicyDecision(
                requested_path=requested_path,
                normalized_path=requested_path,
                display_path_value=requested_path,
                allowed=False,
                reason="path is outside the repository root and allowed extra writable roots",
            )

        normalized_path, display_path, extra_root = classification
        if extra_root is not None:
            return EditPolicyDecision(
                requested_path=requested_path,
                normalized_path=normalized_path,
                display_path_value=display_path,
                allowed=True,
                reason=f"path is editable because it is under allowed extra root `{extra_root.display_path}`",
            )

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

    @classmethod
    def _build_extra_root_rules(
        cls,
        repo_root: Path,
        raw_paths: tuple[Path | str, ...],
    ) -> tuple[EditPolicyExtraRoot, ...]:
        if isinstance(raw_paths, (str, Path)):
            raise TypeError(
                "extra_writable_roots must be a tuple[Path | str, ...] or list[Path | str], not a single path."
            )

        rules: list[EditPolicyExtraRoot] = []
        seen_paths: set[str] = set()
        for raw_path in raw_paths:
            resolved = Path(raw_path).expanduser().resolve()
            if not resolved.exists():
                raise ValueError(f"extra_writable_root does not exist: {raw_path}")
            if not resolved.is_dir():
                raise ValueError(f"extra_writable_root is not a directory: {raw_path}")
            try:
                resolved.relative_to(repo_root)
            except ValueError:
                pass
            else:
                raise ValueError(f"extra_writable_root must stay outside repo_root: {raw_path}")

            normalized_path = _normalize_absolute_path(resolved)
            if normalized_path in seen_paths:
                continue
            seen_paths.add(normalized_path)
            rules.append(
                EditPolicyExtraRoot(
                    path=resolved,
                    normalized_path=normalized_path,
                )
            )

        return tuple(rules)

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

    @staticmethod
    def _resolve_candidate_path(session_cwd: Path, path: str | Path) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = session_cwd / candidate
        return candidate.expanduser().resolve(strict=False)

    def _classify_candidate_path(
        self,
        path: str | Path,
    ) -> tuple[str, str, EditPolicyExtraRoot | None] | None:
        candidate = self._resolve_candidate_path(self.session_cwd, path)
        try:
            relative = candidate.relative_to(self.repo_root)
        except ValueError:
            extra_root = self._match_extra_root(candidate)
            if extra_root is None:
                return None
            return _normalize_absolute_path(candidate), str(candidate), extra_root

        display_path = self._display_path_from_relative(relative)
        return self._fold_case(display_path), display_path, None

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

    def _match_extra_root(self, candidate: Path) -> EditPolicyExtraRoot | None:
        normalized_candidate = _normalize_absolute_path(candidate)
        for root in self.extra_writable_roots:
            if root.matches(normalized_candidate):
                return root
        return None

    def _repo_writable_scope_text(self) -> str:
        editable_text = ", ".join(self.editable_rule_paths())
        if editable_text:
            return editable_text
        return "all repo paths"


def _normalize_absolute_path(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _ensure_trailing_separator(value: str) -> str:
    if value.endswith(("/", "\\")):
        return value
    return f"{value}{os.sep}"
