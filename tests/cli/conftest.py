import pytest


@pytest.fixture(scope="session", autouse=True)
def initialize_schema():
    yield


@pytest.fixture(autouse=True)
def clean_graph():
    yield
