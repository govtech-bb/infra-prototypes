"""Pytest fixtures shared across test modules."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Make `aibuilder/` importable as the source root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _isolate_live_pricing():
    """Disable the live AWS Pricing API in tests by default + clear the
    per-process cache between tests.

    Two problems this solves:
    1. If the dev runs `make check` with AWS SSO active locally, the live
       resolvers would actually call AWS and any test asserting
       `is_fallback is True` could flip to False non-deterministically.
    2. The `_LIVE_PRICE_CACHE` is process-global; without clearing,
       earlier tests would pollute later ones.

    Tests that explicitly exercise the live path (e.g. mock boto3 to
    return a valid response) use their own `with patch("pricing.boto3.client", ...)`
    block — nested patches stack with the inner one winning, so those
    tests work fine.
    """
    import pricing

    pricing._LIVE_PRICE_CACHE.clear()
    with patch("pricing.boto3.client", side_effect=Exception("live pricing disabled in tests")):
        yield
    pricing._LIVE_PRICE_CACHE.clear()
