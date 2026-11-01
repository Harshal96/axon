"""Configurable language-model provider + read-only Cypher guard.

Providers:
  none     - deterministic intents only (fully supported)
  local    - OpenAI-compatible server next to Axon (e.g. `vllm serve
             <hf-model-id>`), reached via LLM_BASE_URL
  external - any OpenAI-compatible URL

The model never answers from memory: it may only draft a read-only
Cypher query. Answers are synthesized from executed result rows.
"""

from __future__ import annotations

import re
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


class LlmClient:
    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    async def chat(self, system: str, user: str) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/v1/chat/completions", json=payload, headers=headers
            )
            resp.raise_for_status()
            data = resp.json()
        return str(data["choices"][0]["message"]["content"])

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


def client_from_settings(settings: Any) -> LlmClient | None:
    provider = (getattr(settings, "llm_provider", "none") or "none").lower()
    if provider == "none":
        return None
    base = getattr(settings, "llm_base_url", "")
    model = getattr(settings, "llm_model", "")
    if not base or not model:
        return None
    return LlmClient(
        base,
        model,
        getattr(settings, "llm_api_key", ""),
        float(getattr(settings, "llm_timeout_seconds", 30.0)),
    )
