"""Repo analyzer: classify a cloned repo into a RepoProfile.

Pure Python, no LLM calls. Walks the directory tree and reads manifest
files (package.json, requirements.txt, Dockerfile, etc.) to figure out
what kind of app this is.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class RepoProfile:
    app_type: str
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    has_dockerfile: bool = False
    has_compose: bool = False
    has_database_hints: bool = False
    entry_points: list[str] = field(default_factory=list)
    build_command: str | None = None
    summary: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def analyze_repo(path: str) -> RepoProfile:
    root = Path(path)
    if not root.exists() or not root.is_dir():
        return RepoProfile(app_type="unknown", summary=f"Path not found: {path}")

    files = [p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts]
    if not files:
        return RepoProfile(app_type="unknown", summary="Empty repository — nothing to analyze.")

    return RepoProfile(app_type="unknown", summary="Unable to determine app type yet.")
