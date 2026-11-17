from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    """All configuration comes from environment variables (or a local .env file).

    Postgres and Redis run outside the app (on the host locally, RDS/ElastiCache on
    AWS), so connections are configured as parts: switching environments — e.g.
    docker compose pointing at host.docker.internal — only overrides the *_HOST values.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_NAME: str = "Expense Tracker API"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False

    # No defaults for credentials or the database name: a missing value should fail
    # at startup, not silently connect somewhere unexpected.
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_USER: str
    DB_PASSWORD: SecretStr  # SecretStr keeps it out of reprs/logs
    DB_NAME: str
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 10
    DB_ECHO: bool = False

    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: SecretStr | None = None

    # Upper bound for each dependency probe in /ready, so a hung DB or Redis
    # makes the probe fail fast instead of hanging the load balancer check.
    READINESS_TIMEOUT_SECONDS: float = 2.0

    @property
    def database_url(self) -> URL:
        # URL.create escapes special characters in the password, which string
        # formatting would not.
        return URL.create(
            drivername="postgresql+asyncpg",
            username=self.DB_USER,
            password=self.DB_PASSWORD.get_secret_value(),
            host=self.DB_HOST,
            port=self.DB_PORT,
            database=self.DB_NAME,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
