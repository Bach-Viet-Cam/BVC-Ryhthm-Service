from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "rhythm-service"
    app_host: str = "0.0.0.0"
    app_port: int = 8001
    database_url: str = "postgresql+psycopg://rhythm:rhythm@localhost:5433/rhythm"
    rabbitmq_url: str = "amqp://bvc:bvc-secret@localhost:5672/"
    rhythm_task_queue: str = "rhythm.evaluate.v1"
    rhythm_dead_letter_queue: str = "rhythm.evaluate.dlq"
    internal_api_key: str = "change-me"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_timeout_seconds: float = 30.0
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_timeout_seconds: float = 30.0
    max_file_bytes: int = 25 * 1024 * 1024
    download_timeout_seconds: float = 60.0
    max_retries: int = 3
    temp_directory: str = "/tmp/rhythm-service"
    allowed_hosts: str = ""

    @property
    def allowed_host_values(self) -> set[str]:
        return {host.strip().lower() for host in self.allowed_hosts.split(",") if host.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
