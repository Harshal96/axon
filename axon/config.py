from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # Retired settings in existing .env files do not prevent startup.
    )

    # Neo4j
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: str = "axon_dev"
    neo4j_database: str = "neo4j"

    # Connection pool
    neo4j_max_connection_pool_size: int = 50
    neo4j_connection_timeout_seconds: int = 30
    neo4j_max_transaction_retry_time_seconds: int = 30

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8080
    api_debug: bool = False
    api_auth_mode: str = "none"
    api_dev_token: str = ""

    # Logging
    log_level: str = "INFO"

    # v1 graph store: memory | neo4j
    axon_store: str = "memory"

    # Language model (ask endpoint): none | local | external.
    # local/external both talk to an OpenAI-compatible /v1/chat/completions
    # endpoint at llm_base_url serving llm_model (e.g. `vllm serve <hf-id>`).
    llm_provider: str = "none"
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_timeout_seconds: float = 30.0
    # Fallback generative endpoint, used only when the primary is
    # unreachable, times out, is rate-limited, or returns 5xx.
    llm_fallback_base_url: str = ""
    llm_fallback_model: str = ""
    llm_fallback_api_key: str = ""
    # Comma-separated model IDs allowed at either endpoint position
    # (empty = allow any configured model). Operator attestation:
    # a checkpoint's training method cannot be verified over HTTP.
    llm_allowed_models: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
