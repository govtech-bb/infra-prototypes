"""Repo analyzer: classify a cloned repo into a RepoProfile.

Pure Python, no LLM calls. Walks the directory tree and reads manifest
files (package.json, requirements.txt, Dockerfile, etc.) to figure out
what kind of app this is.
"""

from __future__ import annotations

import json
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


_LANGUAGE_EXTS = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".rb": "ruby",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".cs": "csharp",
    ".php": "php",
    ".html": "html",
    ".css": "css",
}

_NODE_API_FRAMEWORKS = {"express", "fastify", "koa", "@hapi/hapi", "@nestjs/core"}
_NODE_FRONTEND_FRAMEWORKS = {"react", "vue", "svelte", "next", "@angular/core"}
_PYTHON_API_FRAMEWORKS = {"fastapi", "flask", "django"}
_NODE_WORKER_FRAMEWORKS = {"bull", "bullmq", "agenda"}
_PYTHON_WORKER_FRAMEWORKS = {"celery", "rq", "huey"}

_DB_HINT_KEYWORDS = (
    "postgres",
    "postgresql",
    "mysql",
    "mongodb",
    "redis",
    "DATABASE_URL",
    "DB_HOST",
)

_ENTRY_POINT_NAMES = (
    "index.html",
    "main.py",
    "app.py",
    "server.js",
    "server.ts",
    "index.js",
)


def analyze_repo(path: str) -> RepoProfile:
    root = Path(path)
    if not root.exists() or not root.is_dir():
        return RepoProfile(app_type="unknown", summary=f"Path not found: {path}")

    files = [p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts]
    if not files:
        return RepoProfile(app_type="unknown", summary="Empty repository — nothing to analyze.")

    file_names = {p.name for p in files}
    languages = sorted({_LANGUAGE_EXTS[p.suffix] for p in files if p.suffix in _LANGUAGE_EXTS})

    has_dockerfile = "Dockerfile" in file_names
    has_compose = any(
        n in file_names for n in ("docker-compose.yml", "docker-compose.yaml", "compose.yml")
    )

    pkg_deps, pkg_scripts = _parse_package_json(root / "package.json")
    py_deps = _parse_python_deps(root)

    all_deps = set(pkg_deps) | set(py_deps)
    framework_set = {
        d
        for d in all_deps
        if d
        in _NODE_API_FRAMEWORKS
        | _NODE_FRONTEND_FRAMEWORKS
        | _PYTHON_API_FRAMEWORKS
        | _NODE_WORKER_FRAMEWORKS
        | _PYTHON_WORKER_FRAMEWORKS
    }
    frameworks = sorted(framework_set)

    has_database_hints = _detect_db_hints(root, all_deps)

    entry_points = [n for n in _ENTRY_POINT_NAMES if n in file_names]
    build_command = "npm run build" if pkg_scripts.get("build") else None

    app_type = _classify(file_names, pkg_deps, py_deps, has_dockerfile, has_database_hints)
    summary = _build_summary(app_type, languages, frameworks, has_dockerfile, has_database_hints)

    return RepoProfile(
        app_type=app_type,
        languages=languages,
        frameworks=frameworks,
        has_dockerfile=has_dockerfile,
        has_compose=has_compose,
        has_database_hints=has_database_hints,
        entry_points=entry_points,
        build_command=build_command,
        summary=summary,
    )


def _parse_package_json(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    if not path.exists():
        return {}, {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}, {}
    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
    scripts = data.get("scripts", {})
    return deps, scripts


def _parse_python_deps(root: Path) -> dict[str, str]:
    deps: dict[str, str] = {}
    req = root / "requirements.txt"
    if req.exists():
        for line in req.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name = line.split("==")[0].split(">=")[0].split("<=")[0].split("~=")[0].split("[")[0]
            deps[name.strip().lower()] = ""
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        # Minimal parser: look for "fastapi" / "flask" / "django" tokens in the file.
        text = pyproject.read_text().lower()
        for token in _PYTHON_API_FRAMEWORKS | _PYTHON_WORKER_FRAMEWORKS:
            if token in text:
                deps[token] = ""
    return deps


def _detect_db_hints(root: Path, deps: set[str]) -> bool:
    if any(d.lower() in {"pg", "psycopg2", "psycopg", "pymongo", "mysql2", "mysql"} for d in deps):
        return True
    candidates = [
        root / "requirements.txt",
        root / "package.json",
        root / "docker-compose.yml",
        root / "docker-compose.yaml",
        root / ".env.example",
        root / ".env.sample",
    ]
    for c in candidates:
        if c.exists():
            try:
                text = c.read_text()
            except OSError:
                continue
            if any(kw in text for kw in _DB_HINT_KEYWORDS):
                return True
    return False


def _classify(
    file_names: set[str],
    pkg_deps: dict[str, str],
    py_deps: dict[str, str],
    has_dockerfile: bool,
    has_database_hints: bool,
) -> str:
    pkg_set = set(pkg_deps)
    py_set = set(py_deps)

    has_node_api = bool(pkg_set & _NODE_API_FRAMEWORKS)
    has_python_api = bool(py_set & _PYTHON_API_FRAMEWORKS)
    has_node_frontend = bool(pkg_set & _NODE_FRONTEND_FRAMEWORKS)
    has_worker = bool(
        (pkg_set & _NODE_WORKER_FRAMEWORKS) or (py_set & _PYTHON_WORKER_FRAMEWORKS)
    )

    if has_node_frontend and (has_node_api or has_python_api):
        return "fullstack_with_db" if has_database_hints else "spa_with_api"
    if "index.html" in file_names and not (pkg_set or py_set):
        return "static_site"
    if has_node_frontend:
        return "static_site"
    if has_node_api:
        return "node_api"
    if has_python_api:
        return "python_api"
    if has_worker:
        return "worker"
    if has_dockerfile:
        return "dockerized_web"
    return "unknown"


def _build_summary(
    app_type: str,
    languages: list[str],
    frameworks: list[str],
    has_dockerfile: bool,
    has_database_hints: bool,
) -> str:
    if app_type == "unknown":
        return "I couldn't tell what kind of app this is from the files in the repo."

    type_phrase = {
        "static_site": "a static website (HTML/CSS/JS, no backend)",
        "spa_with_api": "a single-page app frontend with a backend API",
        "node_api": "a Node.js backend API",
        "python_api": "a Python backend API",
        "dockerized_web": "a Dockerized web service",
        "fullstack_with_db": "a full-stack web app that uses a database",
        "worker": "a background worker / scheduled job",
    }[app_type]

    fw = f" built with {', '.join(frameworks)}" if frameworks else ""
    docker = " It includes a Dockerfile." if has_dockerfile else ""
    db = " It looks like it talks to a database." if has_database_hints else ""
    langs = f" Languages detected: {', '.join(languages)}." if languages else ""
    return f"This looks like {type_phrase}{fw}.{langs}{docker}{db}".strip()
