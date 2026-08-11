from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings


class WorkerSettings(BaseSettings):
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "bankqa"
    postgres_user: str = "bankqa"
    postgres_password: str = "changeme"

    redis_url: str = "redis://redis:6378/0"

    minio_endpoint: str = "minio:4003"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "changeme123"
    minio_bucket_transcripts: str = "transcripts"
    minio_bucket_results: str = "results"
    minio_bucket_campaigns: str = "campaigns"
    minio_bucket_documents: str = "documents"

    # llm_base_url: str = "http://host.docker.internal:11444/v1"
    # llm_api_key: str = "dummy"
    # llm_model: str = "gpt-oss-120b"
    # llm_temperature: float = 1.0
    # llm_seed: int = 42
    # llm_reasoning_effort: str = "medium"
    
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "gpt-5.4-mini"
    llm_temperature: float = 1.0
    llm_seed: int = 42
    llm_reasoning_effort: str = "medium"

    llm_api_version: str = "2025-04-01-preview"   # ← field BARU, wajib ditambahkan
    

    llm_timeout: float = 1800.0


    # OCR uses its own (possibly different) LLM endpoint/model. Any value left
    # as None is simply omitted from the OCR call (so the function default or the
    # provider default applies).
    ocr_base_url: str | None = None
    ocr_api_key: str | None = None
    ocr_model: str | None = None
    ocr_temperature: float | None = None
    ocr_seed: int | None = None
    ocr_reasoning_effort: str | None = None

    # An empty env value (e.g. OCR_SEED=) means "unset" -> None, so it is omitted
    # from the OCR call instead of failing float/int parsing on "".
    @field_validator(
        "ocr_base_url",
        "ocr_api_key",
        "ocr_model",
        "ocr_temperature",
        "ocr_seed",
        "ocr_reasoning_effort",
        mode="before",
    )
    @classmethod
    def _empty_str_to_none(cls, v):
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    class Config:
        env_file = ".env"
        extra = "ignore"

    @property
    def database_url(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache()
def get_worker_settings() -> WorkerSettings:
    return WorkerSettings()
