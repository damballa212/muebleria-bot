"""Cliente HTTP para OpenRouter con retry automático (exponential backoff)."""
import asyncio
import base64
import io
import logging
from typing import Any

import requests
from openai import AsyncOpenAI
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.config import settings

logger = logging.getLogger(__name__)

# Cliente OpenRouter compatible con la API de OpenAI
_client = AsyncOpenAI(
    api_key=settings.openrouter_api_key,
    base_url=settings.openrouter_base_url,
)
_openai_client: AsyncOpenAI | None = None


def get_openai_client() -> AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        if not settings.openai_api_key:
            raise LLMError("OPENAI_API_KEY no está configurada para transcripción de audio")
        _openai_client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _openai_client


def _gemini_headers() -> dict[str, str]:
    if not settings.gemini_api_key:
        raise LLMError("GEMINI_API_KEY no está configurada para transcripción de audio")
    return {
        "x-goog-api-key": settings.gemini_api_key,
    }


def _gemini_mime_type(audio_format: str) -> str:
    mapping = {
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "ogg": "audio/ogg",
        "m4a": "audio/mp4",
    }
    return mapping.get(audio_format.lower(), "audio/mpeg")


def _extract_gemini_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        raise LLMError(f"Gemini no devolvió candidatos: {payload}")
    parts = (((candidates[0] or {}).get("content") or {}).get("parts")) or []
    text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
    if not text:
        raise LLMError(f"Gemini no devolvió texto de transcripción: {payload}")
    return text


def _gemini_upload_file(audio_bytes: bytes, audio_format: str) -> str:
    mime_type = _gemini_mime_type(audio_format)
    start_resp = requests.post(
        "https://generativelanguage.googleapis.com/upload/v1beta/files",
        headers={
            **_gemini_headers(),
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(len(audio_bytes)),
            "X-Goog-Upload-Header-Content-Type": mime_type,
            "Content-Type": "application/json",
        },
        json={"file": {"display_name": f"audio.{audio_format}", "mime_type": mime_type}},
        timeout=120,
    )
    start_resp.raise_for_status()
    upload_url = start_resp.headers.get("X-Goog-Upload-URL")
    if not upload_url:
        raise LLMError(f"Gemini upload no devolvió URL de subida: {start_resp.text}")

    upload_resp = requests.post(
        upload_url,
        headers={
            "Content-Length": str(len(audio_bytes)),
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
        },
        data=audio_bytes,
        timeout=300,
    )
    upload_resp.raise_for_status()
    file_uri = (upload_resp.json().get("file") or {}).get("uri")
    if not file_uri:
        raise LLMError(f"Gemini upload no devolvió file URI: {upload_resp.text}")
    return file_uri


def _gemini_generate_content(model: str, parts: list[dict[str, Any]]) -> str:
    resp = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={**_gemini_headers(), "Content-Type": "application/json"},
        json={
            "contents": [{"parts": parts}],
            "generationConfig": {"temperature": 0},
        },
        timeout=300,
    )
    resp.raise_for_status()
    return _extract_gemini_text(resp.json())


def _gemini_delete_file(file_uri: str) -> None:
    try:
        file_name = file_uri.rsplit("/", 1)[-1]
        requests.delete(
            f"https://generativelanguage.googleapis.com/v1beta/files/{file_name}",
            headers=_gemini_headers(),
            timeout=60,
        ).raise_for_status()
    except Exception:
        logger.warning("No se pudo borrar archivo temporal de Gemini", exc_info=True)


def _transcribe_audio_with_gemini(audio_bytes: bytes, audio_format: str, model: str) -> str:
    prompt = (
        "Generate a verbatim transcript of the speech in this audio in Spanish. "
        "Return only the transcript text."
    )
    if len(audio_bytes) <= 20 * 1024 * 1024:
        parts = [
            {"text": prompt},
            {
                "inline_data": {
                    "mime_type": _gemini_mime_type(audio_format),
                    "data": base64.b64encode(audio_bytes).decode("ascii"),
                }
            },
        ]
        return _gemini_generate_content(model, parts)

    file_uri = _gemini_upload_file(audio_bytes, audio_format)
    try:
        parts = [
            {"text": prompt},
            {"file_data": {"mime_type": _gemini_mime_type(audio_format), "file_uri": file_uri}},
        ]
        return _gemini_generate_content(model, parts)
    finally:
        _gemini_delete_file(file_uri)


class LLMError(Exception):
    """Error al llamar al LLM — usado para activar el fallback."""


@retry(
    retry=retry_if_exception_type(Exception),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
async def call_llm(
    messages: list[dict[str, str]],
    model: str | None = None,
    temperature: float = 0.3,
    max_tokens: int = 2048,
) -> str:
    """
    Llama al LLM vía OpenRouter con retry automático (3 intentos, backoff 1s→2s→4s).
    Retorna el texto de la respuesta.
    Lanza LLMError si todos los reintentos fallan.
    """
    effective_model = model or settings.agent_model
    try:
        response = await _client.chat.completions.create(
            model=effective_model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content or ""
    except Exception as exc:
        logger.warning("LLM call failed (will retry if attempts remain): %s", exc)
        raise LLMError(str(exc)) from exc


async def call_llm_with_vision(
    prompt: str,
    image_base64: str,
    model: str | None = None,
) -> str:
    """
    Llama al modelo de visión (Claude) con una imagen en base64.
    Usado por el pipeline de OCR.
    """
    effective_model = model or settings.ocr_model
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"},
                },
            ],
        }
    ]

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _call() -> str:
        response = await _client.chat.completions.create(
            model=effective_model,
            messages=messages,
            max_tokens=1024,
        )
        return response.choices[0].message.content or ""

    try:
        return await _call()
    except Exception as exc:
        raise LLMError(f"Vision LLM failed after 3 retries: {exc}") from exc


async def call_llm_with_audio(
    prompt: str,
    audio_bytes: bytes,
    audio_format: str = "ogg",
    model: str | None = None,
    max_tokens: int = 1024,
    temperature: float = 0,
) -> str:
    """Llama a OpenRouter con `input_audio` y devuelve salida de texto."""
    effective_model = model or settings.audio_input_model
    audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "input_audio",
                    "input_audio": {"data": audio_b64, "format": audio_format},
                },
            ],
        }
    ]

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _call() -> str:
        response = await _client.chat.completions.create(
            model=effective_model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            modalities=["text"],
        )
        return response.choices[0].message.content or ""

    try:
        return await _call()
    except Exception as exc:
        logger.warning("Audio LLM call failed (will retry if attempts remain): %s", exc)
        raise LLMError(f"Audio LLM failed after 3 retries: {exc}") from exc


async def transcribe_audio(
    audio_bytes: bytes,
    audio_format: str = "mp3",
    model: str | None = None,
    language: str = "es",
) -> str:
    """Transcribe audio con el proveedor configurado."""
    effective_model = model or settings.audio_transcribe_model
    provider = (settings.audio_transcribe_provider or "gemini").lower()

    if provider == "gemini":
        @retry(
            retry=retry_if_exception_type(Exception),
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            reraise=True,
        )
        async def _call_gemini() -> str:
            return await asyncio.to_thread(
                _transcribe_audio_with_gemini,
                audio_bytes,
                audio_format,
                effective_model,
            )

        try:
            return await _call_gemini()
        except Exception as exc:
            logger.warning("Gemini audio transcription failed (will retry if attempts remain): %s", exc)
            raise LLMError(f"Gemini audio transcription failed: {exc}") from exc

    if not settings.openai_api_key:
        raise LLMError("OPENAI_API_KEY no está configurada para transcripción de audio")

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _call() -> str:
        file_obj = io.BytesIO(audio_bytes)
        file_obj.name = f"audio.{audio_format}"
        response = await get_openai_client().audio.transcriptions.create(
            model=effective_model,
            file=file_obj,
            language=language,
        )
        return getattr(response, "text", "") or ""

    try:
        return await _call()
    except Exception as exc:
        logger.warning("Audio transcription failed (will retry if attempts remain): %s", exc)
        raise LLMError(f"Audio transcription failed after 3 retries: {exc}") from exc
