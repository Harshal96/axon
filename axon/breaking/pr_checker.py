import argparse
import os
import sys

import httpx


def cli_main() -> int:
    parser = argparse.ArgumentParser(description="Axon PR Checker for breaking changes")
    parser.add_argument("--base-sha", required=True, help="Base commit SHA")
    parser.add_argument("--head-sha", required=True, help="Head commit SHA")
    parser.add_argument(
        "--fail-on", default="CRITICAL,HIGH", help="Comma-separated severities to fail on"
    )
    parser.add_argument("--dry-run", action="store_true", help="Always exit 0")
    args = parser.parse_args()

    api_url = os.environ.get("CONNECTOME_API_URL", "http://localhost:8080")
    token = os.environ.get("CONNECTOME_TOKEN", "")

    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    fail_severities = [s.strip().upper() for s in args.fail_on.split(",") if s.strip()]

    try:
        # First trigger diff
        r_diff = httpx.get(
            f"{api_url}/api/v1/ops/graph-diff",
            params={"sha1": args.base_sha, "sha2": args.head_sha},
            headers=headers,
            timeout=30.0,
        )
        r_diff.raise_for_status()

        # Then fetch alerts
        r_alerts = httpx.get(
            f"{api_url}/api/v1/breaking/alerts",
            params={"commit": args.head_sha, "limit": 100},
            headers=headers,
            timeout=10.0,
        )
        r_alerts.raise_for_status()
        alerts = r_alerts.json()

        failed = False
        print(f"Axon PR Check: {len(alerts)} unacknowledged alerts found.")
        for a in alerts:
            sev = a.get("severity")
            print(f"  [{sev}] {a.get('alert_type')} on {a.get('node_id')}")
            if sev in fail_severities:
                failed = True

        if failed and not args.dry_run:
            print(f"\nExiting 1: Breaking changes of severity {args.fail_on} found.")
            return 1

        print("\nPass: No blocking breaking changes found.")
        return 0

    except Exception as e:
        print(f"Error during PR check: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(cli_main())
