from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="RHYTHM_SERVICE_",
        extra="ignore",
    )

    app_name: str = "rhythm-service"
    app_host: str = "0.0.0.0"
    app_port: int = 8001
    database_url: str = "postgresql+psycopg://rhythm:rhythm@localhost:5433/rhythm"
    rabbitmq_url: str = "amqp://bvc:bvc-secret@localhost:5672/"
    rhythm_task_queue: str = "rhythm.evaluate.v1"
    rhythm_dead_letter_queue: str = "rhythm.evaluate.dlq"
    template_task_queue: str = "rhythm.template.analyze.v1"
    template_dead_letter_queue: str = "rhythm.template.analyze.dlq"
    artifact_root: str = "/data/rhythm-artifacts"
    internal_api_key: str = "change-me"
    callback_secret: str = ""
    callback_timeout_seconds: float = 5.0
    callback_max_retries: int = Field(default=2, ge=0, le=5)
    callback_allowed_hosts: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_timeout_seconds: float = 30.0
    max_file_bytes: int = 25 * 1024 * 1024
    download_timeout_seconds: float = 60.0
    max_retries: int = Field(default=3, ge=0)
    temp_directory: str = "/tmp/rhythm-service"
    allowed_hosts: str = ""

    @field_validator("rhythm_task_queue", "rhythm_dead_letter_queue", "template_task_queue", "template_dead_letter_queue", "artifact_root")
    @classmethod
    def require_nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Tên hàng đợi và thư mục lưu trữ không được để trống.")
        return value

    @model_validator(mode="after")
    def require_distinct_queues(self) -> "Settings":
        queues = [self.rhythm_task_queue, self.rhythm_dead_letter_queue, self.template_task_queue, self.template_dead_letter_queue]
        if len(set(queues)) != len(queues):
            raise ValueError("Các hàng đợi so sánh, template và dead-letter phải có tên khác nhau.")
        return self

    @property
    def allowed_host_values(self) -> set[str]:
        return {host.strip().lower() for host in self.allowed_hosts.split(",") if host.strip()}

    @property
    def callback_allowed_host_values(self) -> set[str]:
        return {host.strip().lower() for host in self.callback_allowed_hosts.split(",") if host.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
