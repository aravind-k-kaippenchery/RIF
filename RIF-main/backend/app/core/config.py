"""Settings loaded from environment variables and the local .env file."""

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Application settings. Values in .env override the defaults below."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "B2B Product Intelligence Assistant API"
    app_version: str = "0.15.0"
    app_env: str = "development"
    app_debug: bool = False
    log_level: str = "INFO"
    cors_origins: str = "http://localhost:3000,http://localhost:5173,http://127.0.0.1:5173,http://127.0.0.1:5500"

    # PostgreSQL was connected in Phase 2.
    postgres_host: str = "127.0.0.1"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str = "b2b_assistant"
    postgres_user: str = "b2b_app"
    postgres_password: str = ""
    postgres_connect_timeout: int = Field(default=5, ge=1, le=30)
    database_echo: bool = False

    # Phase 5 local Ollama adapter settings. The adapter rejects non-local URLs.
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "llama3:8b"
    ollama_request_timeout_seconds: int = Field(default=300, ge=5, le=600)
    ollama_keep_alive: str = "10m"
    ollama_temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    # Maximum total model calls for one request. A single correction retry is allowed.
    ollama_max_attempts: int = Field(default=2, ge=1, le=2)

    # Optional local fallback model. Same loopback Ollama server, a second pulled model.
    # Used only when the primary model is unreachable, not installed, times out, or
    # returns an HTTP error. Never a cloud endpoint -- the local-only guarantee stays
    # intact for the fallback model too. Leave blank to disable fallback entirely.
    ollama_fallback_model: str = "llama3.2:3b"
    ollama_fallback_enabled: bool = True

    # Phase 8 local document ingestion. OCR is only loaded when a scanned PDF/image is uploaded.
    document_max_upload_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)
    document_native_text_min_characters: int = Field(default=24, ge=0, le=1000)
    document_ocr_language: str = "en"

    # Phase 9 local vector store and embedding settings. The embedding model is
    # downloaded once to the local cache, then reused without a cloud API.
    chroma_persist_directory: str = "./data/chroma"
    chroma_collection_name: str = "b2b_document_chunks"
    embedding_model_name: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_cache_directory: str = "./data/embedding_models"
    rag_chunk_size_characters: int = Field(default=900, ge=200, le=4000)
    rag_chunk_overlap_characters: int = Field(default=160, ge=0, le=1000)
    rag_top_k: int = Field(default=4, ge=1, le=10)
    rag_min_similarity: float = Field(default=0.25, ge=0.0, le=1.0)
    rag_max_evidence_characters: int = Field(default=6500, ge=500, le=20000)

    # Phase 11 hybrid evidence fusion limits. The hybrid tool accepts no raw SQL;
    # it only retrieves vendor/product mappings linked to explicit document identities.
    hybrid_max_result_rows: int = Field(default=20, ge=1, le=100)
    upload_directory: str = "./uploads"
    log_directory: str = "./logs"

    # Phase 4 SQL guardrail settings. These are server-controlled limits.
    sql_read_max_rows: int = Field(default=100, ge=1, le=500)
    sql_statement_timeout_ms: int = Field(default=3000, ge=250, le=30000)
    mcp_server_name: str = "B2B Controlled Database Tools"

    # Phase 14 audit rollback guardrails. Rollback remains admin-only and is stored
    # as a confirmation-gated pending action before any database restore occurs.
    rollback_max_records: int = Field(default=25, ge=1, le=100)
    rollback_pending_ttl_minutes: int = Field(default=30, ge=1, le=120)

    # Phase 15 benchmark guardrails. Benchmarking persists only operational metrics
    # and never authorizes business-table writes or arbitrary SQL.
    benchmark_max_repeats: int = Field(default=3, ge=1, le=3)

    @property
    def cors_origins_list(self) -> list[str]:
        """Return safely trimmed origins for CORSMiddleware."""

        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def database_url(self) -> URL:
        """Build a password-safe PostgreSQL URL without string concatenation."""

        return URL.create(
            drivername="postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )

    @property
    def resolved_upload_directory(self) -> Path:
        """Return an absolute upload directory path and create it when needed."""

        path = Path(self.upload_directory)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def resolved_chroma_persist_directory(self) -> Path:
        """Return the local disk directory used by the persistent Chroma client."""

        directory = Path(self.chroma_persist_directory)
        if not directory.is_absolute():
            directory = PROJECT_ROOT / directory
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @property
    def resolved_embedding_cache_directory(self) -> Path:
        """Return the local cache used by the sentence-transformer embedding model."""

        directory = Path(self.embedding_cache_directory)
        if not directory.is_absolute():
            directory = PROJECT_ROOT / directory
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @property
    def resolved_log_directory(self) -> Path:
        """Return an absolute log directory path and create it if needed."""

        directory = Path(self.log_directory)
        if not directory.is_absolute():
            directory = PROJECT_ROOT / directory
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @property
    def is_local_ollama_endpoint(self) -> bool:
        """Ensure Phase 5 remains local-first and never silently calls a cloud API."""

        parsed = urlparse(self.ollama_base_url)
        return parsed.scheme in {"http", "https"} and parsed.hostname in {"127.0.0.1", "localhost", "::1"}


@lru_cache
def get_settings() -> Settings:
    """Create settings once per process."""

    return Settings()


def clear_settings_cache() -> None:
    """Useful for tests that need settings reloaded after environment changes."""

    get_settings.cache_clear()
