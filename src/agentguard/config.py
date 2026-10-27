from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./agentguard.db"
    redis_url: str = "redis://localhost:6379/0"
    environment: str = "development"
    approval_default_expiry_seconds: int = 900

    # Resilience and runtime controls (docs/THREAT_MODEL.md #5, #6, #14)
    kill_switch: bool = False  # deny every tool call, for all agents
    allow_reads_when_policy_unavailable: bool = False  # the only permitted fail-open, and off by default
    broker_timeout_seconds: float = 5.0
    breaker_threshold: int = 5  # denials within the window that open an agent's circuit
    breaker_window_seconds: int = 300

    # Required for the operator-only /v1/stats endpoint; unset means the endpoint is disabled.
    operator_token: str | None = None


settings = Settings()
