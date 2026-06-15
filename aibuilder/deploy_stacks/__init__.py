"""Pattern → tofu stack registry.

Each catalog pattern that can be deployed registers a StackSpec here.
The registry is the source of truth for "what can aibuilder deploy
today" — `not_deployable_message` enumerates supported patterns from
the registry so the message can't drift from reality.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from deploy_stacks import _registry


@dataclass(frozen=True)
class StackSpec:
    pattern: str
    stack_dir: str
    build_vars: Callable[[dict], dict]
    allowed_knobs: list[str]


def register(spec: StackSpec) -> None:
    _registry.STACK_REGISTRY[spec.pattern] = spec


def get_spec(pattern: str) -> StackSpec | None:
    return _registry.STACK_REGISTRY.get(pattern)


def list_supported_patterns() -> list[str]:
    return list(_registry.STACK_REGISTRY.keys())


def not_deployable_message(pattern: str) -> str:
    supported = sorted(list_supported_patterns())
    if not supported:
        return (
            f"`{pattern}` is not yet deployable — no patterns are wired up yet. "
            "This is a setup error; ask the maintainer."
        )
    return (
        f"`{pattern}` is not yet deployable by aibuilder. "
        f"Currently supported: {', '.join(f'`{p}`' for p in supported)}. "
        "Other patterns are coming in later waves."
    )


# Register built-in patterns. New patterns add a sibling module here.
from deploy_stacks import static_website  # noqa: E402,F401
