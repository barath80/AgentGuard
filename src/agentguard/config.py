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
    # How far the broker's clock may disagree with the gateway's before it's flagged (THREAT_MODEL #17).
    clock_skew_tolerance_seconds: float = 1.0
    broker_timeout_seconds: float = 5.0

    # Where tools actually run (broker/executors.py): "inprocess" test double, or the real "helpdesk".
    target_backend: str = "inprocess"
    helpdesk_url: str = "http://127.0.0.1:8200"
    # Set to run tool calls through the separate broker service (broker/server.py).
    broker_url: str | None = None
    broker_service_token: str | None = None
    breaker_threshold: int = 5  # denials within the window that open an agent's circuit
    breaker_window_seconds: int = 300

    # Required for the operator-only /v1/stats endpoint; unset means the endpoint is disabled.
    # Also signs a read-all "operator" principal into the console (see console/auth.py).
    operator_token: str | None = None

    # Console sign-in: JSON list of {"name", "roles", "tenants", "token_sha256"}. Only token
    # hashes are configured here; the tokens themselves are handed to people out of band.
    console_principals: str | None = None
    proposed_policies_dir: str = "policies/proposed"
    # Static console tokens (above) are for local dev and break-glass; turn off once OIDC is set up.
    console_static_tokens: bool = True

    # OIDC sign-in for the console (e.g. Keycloak). Unset issuer = OIDC disabled.
    oidc_issuer: str | None = None  # must equal the tokens' `iss`, e.g. http://localhost:8081/realms/agentguard
    oidc_client_id: str = "agentguard-console"  # also the required audience
    oidc_jwks_url: str | None = None  # defaults to the issuer's Keycloak certs endpoint
    # Only these IdP roles mean anything here; IdP defaults (offline_access, ...) are dropped.
    oidc_allowed_roles: str = "operator,viewer,support-lead"

    # Behavioural anomaly model (agentguard.ml). Advisory only; never consulted for decisions.
    anomaly_model_path: str = "models/anomaly.joblib"


settings = Settings()
