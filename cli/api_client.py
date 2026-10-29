"""HTTP client for a configured Axon API server.

Config lives in ~/.axon/config.json ({"api_url": ..., "token": ...}),
the same file axon-mcp uses. Resolution order for the URL and token:
explicit flag, AXON_API_URL / AXON_API_TOKEN env, config file, default
http://localhost:8080. `configured` reports whether any source named a
host explicitly (flag, env, or file), which the CLI uses to decide
that API mode is mandatory rather than opportunistic.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

DEFAULT_API_URL = "http://localhost:8080"


def config_path() -> Path:
    return Path(os.path.expanduser("~/.axon/config.json"))


def load_config_file() -> dict[str, str]:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: str(v) for k, v in data.items() if isinstance(v, str)}


def save_config(api_url: str, token: str = "") -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    current = load_config_file()
    current["api_url"] = api_url
    if token or "token" not in current:
        current["token"] = token
    path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    return path


@dataclass
class ApiConfig:
    api_url: str
    token: str = ""
    configured: bool = False  # an explicit source named a host


def resolve_config(api_url: str | None = None, token: str | None = None) -> ApiConfig:
    file_cfg = load_config_file()
    env_url = os.environ.get("AXON_API_URL", "")
    url = api_url or env_url or file_cfg.get("api_url") or DEFAULT_API_URL
    tok = token or os.environ.get("AXON_API_TOKEN", "") or file_cfg.get("token", "")
    configured = bool(api_url or env_url or file_cfg.get("api_url"))
    return ApiConfig(api_url=url.rstrip("/"), token=tok, configured=configured)


class ApiError(RuntimeError):
    """The API server is unreachable or returned an error."""


class ApiClient:
    def __init__(self, config: ApiConfig | None = None) -> None:
        self.config = config or resolve_config()
        headers = {}
        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"
        self._client = httpx.Client(
            base_url=self.config.api_url, headers=headers, timeout=httpx.Timeout(30.0)
        )

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ApiError(
                f"cannot reach Axon API at {self.config.api_url}: {exc}. "
                "Start it with `uv run axon-server` or point Axon at a hosted "
                "one: `axon configure --api-url <url>`."
            ) from exc
        if resp.status_code >= 400:
            raise ApiError(f"{method} {path} -> {resp.status_code}: {resp.text.strip()}")
        data = resp.json()
        return data if isinstance(data, dict) else {"data": data}

    def create_scan_job(self, repo_path: str, write: bool = False) -> str:
        body = self._request(
            "POST", "/api/v2/onboard/scan/jobs", json={"repo_path": repo_path, "write": write}
        )
        return str(body["job_id"])

    def create_index_job(self, repo_path: str, commit: str = "", force: bool = False) -> str:
        body = self._request(
            "POST",
            "/api/v2/index/jobs",
            json={"repo_path": repo_path, "commit": commit, "force": force},
        )
        return str(body["job_id"])

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v2/jobs/{job_id}")

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/v2/jobs/{job_id}/cancel")

    def poll_job(
        self,
        job_id: str,
        on_update: Any,
        interval_s: float = 0.4,
    ) -> dict[str, Any]:
        """Poll until terminal; on_update(job, elapsed_s) per change.

        KeyboardInterrupt propagates (caller decides; the job keeps
        running on the server and can be reattached).
        """
        started = time.monotonic()
        last = ""
        while True:
            job = self.get_job(job_id)
            stamp = f"{job.get('status')}|{job.get('phase')}|{job.get('done')}|{job.get('total')}"
            if stamp != last:
                last = stamp
                on_update(job, time.monotonic() - started)
            if job.get("status") in ("done", "error", "cancelled"):
                return job
            time.sleep(interval_s)
