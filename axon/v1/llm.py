"""Configurable language-model provider + read-only Cypher guard.

Providers:
  none     - deterministic intents only (fully supported)
  local    - OpenAI-compatible server next to Axon (e.g. `vllm serve
             <hf-model-id>`), reached via LLM_BASE_URL
  external - any OpenAI-compatible URL

Preference order: the primary endpoint (LLM_BASE_URL/LLM_MODEL, meant
for a self-hosted RLCD-trained generative model) is tried first; the
optional fallback endpoint (LLM_FALLBACK_*) is used only when the
primary is unreachable, times out, is rate-limited, or returns 5xx.
A bad draft never triggers fallback; it goes through the repair loop
in ask.py instead. LLM_ALLOWED_MODELS (comma-separated) restricts
which model IDs may be configured at either position; Axon cannot
verify a checkpoint's training method over HTTP, so the allowlist is
an operator attestation, enforced exactly as configured.

Decision models such as Laya never generate text and therefore cannot
draft Cypher; they are not part of this chain (see docs/v1.md).

The model never answers from memory: it may only draft a read-only
Cypher query. Answers are synthesized from executed result rows.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

WRITE_KEYWORDS = (
    "CREATE",
    "MERGE",
    "DELETE",
    "DETACH",
    "SET",
    "REMOVE",
    "DROP",
    "LOAD CSV",
    "CALL DBMS",
    "CALL APOC",
    "FOREACH",
)

SCHEMA_HINT = """Nodes: Repo, Service, SourceFile, Symbol, ApiEndpoint, ApiSpec,
ApiClient, ExternalApi.
Common props: id, name, repo_id, service_id, file, path, method, qualified.
Relationships: CONTAINS, DEFINES, EXPOSES, HANDLES, CALLS, CALLS_API,
GENERATED_FROM, IMPORTS."""


def validate_readonly_cypher(cypher: str) -> tuple[bool, str]:
    c = cypher.strip().rstrip(";")
    if not c:
        return False, "empty query"
    if ";" in c:
        return False, "multiple statements are not allowed"
    upper = re.sub(r"'[^']*'", "''", c).upper()
    for kw in WRITE_KEYWORDS:
        if re.search(rf"\b{kw}\b", upper):
            return False, f"write keyword not allowed: {kw}"
    if not upper.lstrip().startswith(("MATCH", "OPTIONAL", "RETURN", "WITH")):
        return False, "query must start with MATCH/RETURN/WITH"
    if "RETURN" not in upper:
        return False, "query must RETURN rows"
    return True, "ok"


@dataclass(frozen=True)
class LlmEndpoint:
    base_url: str
    model: str
    api_key: str = ""


class LlmClient:
    """One logical model client over an ordered endpoint chain."""

    def __init__(
        self,
        base_url: str = "",
        model: str = "",
        api_key: str = "",
        timeout: float = 30.0,
        *,
        endpoints: Sequence[LlmEndpoint | tuple[str, str, str]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        raw = endpoints if endpoints is not None else [(base_url, model, api_key)]
        self.endpoints: list[LlmEndpoint] = [
            e if isinstance(e, LlmEndpoint) else LlmEndpoint(e[0].rstrip("/"), e[1], e[2])
            for e in raw
        ]
        first = self.endpoints[0] if self.endpoints else LlmEndpoint("", "")
        self.base_url = first.base_url
        self.model = first.model
        self.api_key = first.api_key
        self.timeout = timeout
        self.transport = transport
        self.used_model = ""

    async def _chat_once(self, endpoint: LlmEndpoint, system: str, user: str) -> str:
        headers = {"Content-Type": "application/json"}
        if endpoint.api_key:
            headers["Authorization"] = f"Bearer {endpoint.api_key}"
        payload = {
            "model": endpoint.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
        }
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            resp = await client.post(
                f"{endpoint.base_url}/v1/chat/completions", json=payload, headers=headers
            )
            resp.raise_for_status()
            data = resp.json()
        return str(data["choices"][0]["message"]["content"])

    async def chat(self, system: str, user: str) -> str:
        last_exc: Exception | None = None
        for endpoint in self.endpoints:
            try:
                content = await self._chat_once(endpoint, system, user)
            except httpx.HTTPStatusError as exc:
                # Retryable only for rate limits and server errors; a 4xx
                # (bad key, unknown model) would fail identically downstream.
                if exc.response.status_code != 429 and exc.response.status_code < 500:
                    raise
                last_exc = exc
            except httpx.TransportError as exc:
                last_exc = exc
            else:
                self.used_model = endpoint.model
                return content
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("no LLM endpoints configured")

    async def draft_cypher(self, question: str) -> str:
        raw = await self.chat(
            "You translate questions into ONE read-only Cypher MATCH query. "
            "Reply with only the query, no fences, no prose.\n" + SCHEMA_HINT,
            question,
        )
        raw = raw.strip()
        raw = re.sub(r"^```(?:cypher)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return raw.strip()


def _allowed_models(settings: Any) -> set[str] | None:
    raw = getattr(settings, "llm_allowed_models", "") or ""
    allowed = {m.strip() for m in raw.replace("\n", ",").split(",") if m.strip()}
    return allowed or None


def client_from_settings(settings: Any) -> LlmClient | None:
    provider = (getattr(settings, "llm_provider", "none") or "none").lower()
    if provider == "none":
        return None
    allowed = _allowed_models(settings)
    candidates = [
        LlmEndpoint(
            (getattr(settings, "llm_base_url", "") or "").rstrip("/"),
            getattr(settings, "llm_model", "") or "",
            getattr(settings, "llm_api_key", "") or "",
        ),
        LlmEndpoint(
            (getattr(settings, "llm_fallback_base_url", "") or "").rstrip("/"),
            getattr(settings, "llm_fallback_model", "") or "",
            getattr(settings, "llm_fallback_api_key", "") or "",
        ),
    ]
    endpoints = [
        e for e in candidates if e.base_url and e.model and (allowed is None or e.model in allowed)
    ]
    if not endpoints:
        return None
    return LlmClient(
        endpoints=endpoints,
        timeout=float(getattr(settings, "llm_timeout_seconds", 30.0)),
    )
