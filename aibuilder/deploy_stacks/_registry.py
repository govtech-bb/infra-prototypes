"""Mutable global. Split from __init__.py so tests can monkeypatch it."""

from __future__ import annotations

STACK_REGISTRY: dict = {}
