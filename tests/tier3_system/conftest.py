"""Tier 3 tests require an explicitly supplied real workspace."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session")
def system_workspace() -> str:
    workspace = os.getenv("CONNECTOME_WORKSPACE") or os.getenv("AXON_WORKSPACE")
    if not workspace:
        pytest.skip("Set CONNECTOME_WORKSPACE or AXON_WORKSPACE to run tier3 tests")
    return workspace
