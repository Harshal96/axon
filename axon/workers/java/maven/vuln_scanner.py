"""
Checks MavenArtifact coordinates against the OSS Index vulnerability database.
https://ossindex.sonatype.org/

Free tier: 128 requests/hour. Batch up to 128 packages per request.
No API key required for the basic tier; configure one in Settings for higher limits.

Results are cached in Neo4j (the is_vulnerable + cve_ids properties on
MavenArtifact nodes). The scanner only re-checks artifacts whose version
has changed or whose last scan was > 7 days ago.
"""

from __future__ import annotations

import logging

import httpx

from axon.workers.java.maven.pom_parser import MavenCoordinate

logger = logging.getLogger(__name__)

_OSS_INDEX_URL = "https://ossindex.sonatype.org/api/v3/component-report"
_BATCH_SIZE = 128


# Package URL scheme for Maven:  pkg:maven/groupId/artifactId@version
def _purl(coord: MavenCoordinate) -> str:
    return f"pkg:maven/{coord.group_id}/{coord.artifact_id}@{coord.version}"


class VulnResult:
    def __init__(
        self,
        coord: MavenCoordinate,
        is_vulnerable: bool,
        cve_ids: list[str],
        cvss_max: float,
    ) -> None:
        self.coord = coord
        self.is_vulnerable = is_vulnerable
        self.cve_ids = cve_ids
        self.cvss_max = cvss_max


async def scan_batch(
    coords: list[MavenCoordinate],
    api_token: str | None = None,
) -> list[VulnResult]:
    """
    Scans a batch of Maven coordinates against OSS Index.
    Returns one VulnResult per coordinate (in the same order).

    Gracefully returns is_vulnerable=False for all if the API is unreachable.
    """
    if not coords:
        return []

    headers = {"Content-Type": "application/json"}
    if api_token:
        headers["Authorization"] = f"Basic {api_token}"

    purls = [_purl(c) for c in coords]
    results_map: dict[str, VulnResult] = {}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            for i in range(0, len(purls), _BATCH_SIZE):
                batch_purls = purls[i : i + _BATCH_SIZE]
                response = await client.post(
                    _OSS_INDEX_URL,
                    headers=headers,
                    json={"coordinates": batch_purls},
                )
                response.raise_for_status()
                data = response.json()

                for item in data:
                    purl = item.get("coordinates", "")
                    vulns = item.get("vulnerabilities", [])
                    is_vuln = len(vulns) > 0
                    cve_ids = [
                        v.get("cve", v.get("id", "")) for v in vulns if v.get("cve") or v.get("id")
                    ]
                    cvss_scores = [float(v.get("cvssScore", 0)) for v in vulns]
                    results_map[purl] = VulnResult(
                        coord=coords[0],  # placeholder; matched by index below
                        is_vulnerable=is_vuln,
                        cve_ids=cve_ids,
                        cvss_max=max(cvss_scores, default=0.0),
                    )

    except (httpx.HTTPError, httpx.TimeoutException) as e:
        logger.warning("OSS Index scan failed: %s — marking all as not scanned", e)
        return [VulnResult(coord=c, is_vulnerable=False, cve_ids=[], cvss_max=0.0) for c in coords]

    # Re-align results to original coord order
    final = []
    for i, coord in enumerate(coords):
        purl = purls[i]
        if purl in results_map:
            r = results_map[purl]
            r.coord = coord
            final.append(r)
        else:
            final.append(VulnResult(coord=coord, is_vulnerable=False, cve_ids=[], cvss_max=0.0))

    return final
