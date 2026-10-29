from unittest.mock import MagicMock, patch

from axon.workers.pnpm.lockfile_parser import WorkspacePackage
from axon.workers.pnpm.workspace_resolver import build_workspace_map, read_package_name


def test_read_package_name():
    # relies on tests/fixtures/package.json
    name = read_package_name("tests/fixtures", ".")
    assert name == "@company/ui-components"


@patch("axon.workers.pnpm.workspace_resolver.sync_session")
def test_build_workspace_map(mock_sync_session):
    mock_session = MagicMock()
    mock_sync_session.return_value.__enter__.return_value = mock_session
    mock_session.run.return_value.data.return_value = [
        {"label": "//tests/fixtures:fixtures", "kind": "JS_LIBRARY", "name": "fixtures"}
    ]

    wp = WorkspacePackage(path="tests/fixtures", name="")
    result = build_workspace_map([wp], ".")
    assert "@company/ui-components" in result
    assert result["@company/ui-components"]["bazel_label"] == "//tests/fixtures:fixtures"
