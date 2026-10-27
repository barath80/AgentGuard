from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./agentguard.db"
    redis_url: str = "redis://localhost:6379/0"
    environment: str = "development"
    approval_default_expiry_seconds: int = 900


settings = Settings()
