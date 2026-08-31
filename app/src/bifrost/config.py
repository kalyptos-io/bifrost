"""runtime settings from env (prefix BIFROST_)."""

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BIFROST_")

    database_dsn: str  # required; no default keeps prod off the dev password
    # overrides dsn host for the serve pool only (creds stay in dsn); that pool never writes and a
    # generation is immutable once registered, so aiming this at a read-replica service is safe
    database_host: str | None = None
    workers: int = 4  # uvicorn processes; total db conns = workers * db_pool_max
    db_pool_min: int = 2
    db_pool_max: int = 10
    db_max_connections: int = 200  # mirror postgres max_connections (compose)
    db_reserved: int = 10  # BIFROST_DB_RESERVED; conns held back for superuser + loader
    cache_url: str | None = None  # BIFROST_CACHE_URL; none disables the response cache (fail-open)
    cache_ttl: int = 86400  # BIFROST_CACHE_TTL secs; registry is static between loads
    log_level: str = "INFO"  # BIFROST_LOG_LEVEL; root handler level (main.py basicConfig)
    cors_origins: str = "*"  # BIFROST_CORS_ORIGINS; comma-separated, * = any (public read api)
    max_body_bytes: int = 2_000_000  # BIFROST_MAX_BODY_BYTES; request body ceiling, 413 past it
    request_timeout: float = 30.0  # BIFROST_REQUEST_TIMEOUT secs; end-to-end deadline, 504 past it
    version: str = "0.0.0"  # BIFROST_VERSION; openapi only, stamped from the chart appVersion

    @model_validator(mode="after")
    def _pool_within_budget(self) -> "Settings":
        # fail fast on a scale-up that would exhaust postgres; the chart renders the same reserve
        if self.workers * self.db_pool_max > self.db_max_connections - self.db_reserved:
            raise ValueError(
                f"[!] workers*db_pool_max={self.workers * self.db_pool_max} exceeds the "
                f"db_max_connections={self.db_max_connections} budget "
                f"(keep {self.db_reserved} reserved)"
            )
        return self
