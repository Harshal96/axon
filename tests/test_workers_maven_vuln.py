from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from axon.workers.java.maven.pom_parser import MavenCoordinate
from axon.workers.java.maven.vuln_scanner import scan_batch


@pytest.mark.asyncio
@patch("axon.workers.java.maven.vuln_scanner.httpx.AsyncClient.post")
async def test_scan_batch(mock_post):
    mock_response = MagicMock()
    mock_response.json.return_value = [
        {
            "coordinates": "pkg:maven/org.apache.commons/commons-collections@3.2.1",
            "vulnerabilities": [{"id": "CVE-2015-7501", "cvssScore": 9.8}],
        }
    ]
    mock_post.return_value = mock_response

    coord = MavenCoordinate("org.apache.commons", "commons-collections", "3.2.1")
    results = await scan_batch([coord])

    assert len(results) == 1
    assert results[0].is_vulnerable is True
    assert "CVE-2015-7501" in results[0].cve_ids
    assert results[0].cvss_max == 9.8
