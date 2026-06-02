"""Pytest fixtures shared across test modules."""

import sys
from pathlib import Path

# Make `aibuilder/` importable as the source root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
