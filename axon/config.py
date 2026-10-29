from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
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
    api_secret_key: str = "change_me_in_production"
    api_auth_mode: str = "none"
    api_dev_token: str = ""

    # Logging
    log_level: str = "INFO"

    # Graph behaviour
    graph_staleness_threshold_seconds: int = 300
    graph_default_blast_radius_depth: int = 5
    graph_max_blast_radius_depth: int = 10
    graph_default_subgraph_depth: int = 2
    graph_default_callers_depth: int = 1
    graph_default_callees_depth: int = 3
    graph_search_limit_default: int = 20
    graph_search_limit_max: int = 100

    # Slack integration
    slack_webhook_url: str = ""

    # v2 graph store: memory | neo4j
    axon_store: str = "memory"

    # Language model (ask endpoint): none | local | external.
    # local/external both talk to an OpenAI-compatible /v1/chat/completions
    # endpoint at llm_base_url serving llm_model (e.g. `vllm serve <hf-id>`).
    llm_provider: str = "none"
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_timeout_seconds: float = 30.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
