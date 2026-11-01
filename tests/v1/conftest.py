from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "v1"


@pytest.fixture(scope="session")
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture()
def monorepo_snap(fixtures: Path):
    from axon.v1.indexer import index_repo

    snap, _ = index_repo(str(fixtures / "monorepo"))
    return snap
