"""Path-boundary and file-integrity helpers for Project B."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable


PROJECT_B_ROOT = Path(__file__).resolve().parents[2]


def _reject_project_a_component(path: str | Path) -> None:
    """Reject any raw Project_A component before resolving the path."""
    if any(part == "Project_A" for part in Path(path).parts):
        raise PermissionError("Project_A paths are forbidden")


def canonical(path: str | Path) -> Path:
    """Return a normalized absolute path without requiring that it exists."""
    _reject_project_a_component(path)
    return Path(path).expanduser().resolve(strict=False)


def assert_within_project_b(path: str | Path) -> Path:
    """Return *path* only if it resolves under the Project B workspace."""
    candidate = canonical(path)
    project_root = canonical(PROJECT_B_ROOT)
    try:
        candidate.relative_to(project_root)
    except ValueError as exc:
        raise PermissionError(f"Path is outside Project_B: {candidate}") from exc
    return candidate


def assert_allowed_source(path: str | Path, whitelist: Iterable[str | Path]) -> Path:
    """Return *path* only when it exactly matches a canonical whitelist member."""
    candidate = canonical(path)
    if candidate not in {canonical(item) for item in whitelist}:
        raise PermissionError(f"Source is not whitelisted: {candidate}")
    return candidate


def sha256_file(path: str | Path) -> str:
    """Calculate the SHA-256 digest of a file without loading it all at once."""
    _reject_project_a_component(path)
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
