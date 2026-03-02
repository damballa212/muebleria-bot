"""Configuración centralizada — todas las variables de entorno en un solo lugar."""
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM vía OpenRouter
    openrouter_api_key: str
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openai_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    agent_model: str = "openai/gpt-4.1-mini"
    ocr_model: str = "anthropic/claude-sonnet-4-5"
    audio_input_model: str = "openai/gpt-audio-mini"
    audio_fallback_model: str = "openai/gpt-audio"
    audio_transcribe_provider: str = "gemini"
    audio_transcribe_model: str = "gemini-2.5-flash"
    audio_summary_model: str = "openai/gpt-4.1-mini"

    # Cohere (embeddings + reranker)
    cohere_api_key: str
    cohere_embed_model: str = "embed-multilingual-v3.0"
    cohere_rerank_model: str = "rerank-multilingual-v3.0"

    # Telegram
    telegram_bot_token: str
    telegram_owner_chat_id: str
    telegram_mode: str = "openclaw"  # "openclaw" | "direct"

    # PostgreSQL
    database_url: str

    # Redis
    redis_url: str = "redis://redis:6379/0"
    chat_buffer_size: int = 20

    # Qdrant
    qdrant_host: str = "qdrant"
    qdrant_port: int = 6333
    qdrant_collection: str = "norena_assistant"
    qdrant_max_vectors: int = 100_000
    qdrant_retention_days: int = 365

    # Seguridad
    internal_api_key: str

    # Almacenamiento
    photos_dir: str = "/app/photos"
    openclaw_media_dir: str = "/openclaw-media"

    # App
    timezone: str = "America/Bogota"


# Instancia global — importar desde aquí en todo el proyecto
settings = Settings()
