"""Procesamiento de audios entrantes desde OpenClaw/Telegram."""
import json
import logging
import mimetypes
import re
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.agent.routing import extract_case_number
from src.agent.tools.cases import attach_case_evidence
from src.config import settings
from src.llm import LLMError, call_llm, transcribe_audio
from src.memory import get_chat_history, get_chat_state, update_chat_state

logger = logging.getLogger(__name__)

_AUDIO_MARKER_RE = re.compile(
    r"\[media attached:\s*"
    r"(?P<path>[^|\]\s]+?\.(?P<ext>ogg|oga|mp3|wav|m4a))"
    r"(?:\s*\((?P<mime>audio/[^,)\s]+)[^)]*\))?"
    r"(?:\s*\|\s*(?P<altpath>[^\]]+))?"
    r"\]\s*(?P<text>.*?)\s*<media:audio>",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class AudioPreprocessResult:
    rewritten_message: str | None = None
    direct_response: str | None = None


async def preprocess_incoming_message(chat_id: str, raw_message: str) -> AudioPreprocessResult | None:
    """Transcribe audios y decide si se procesan como conversación o evidencia."""
    chat_state = await _safe_get_chat_state(chat_id)
    explicit_case = extract_case_number(raw_message)

    if explicit_case and _fresh_pending_audio_capture(chat_state):
        await update_chat_state(
            chat_id,
            active_case_number=explicit_case,
            pending_evidence_case=explicit_case,
            pending_evidence_requested_at=datetime.now(timezone.utc).isoformat(),
            pending_audio_capture_intent=None,
            pending_audio_capture_requested_at=None,
        )
        return AudioPreprocessResult(
            direct_response=(
                f"Perfecto. Ahora envía el audio con mas detalles de la garantia para el caso {explicit_case}. "
                "Puede ser tuyo o del cliente."
            )
        )

    control_result = await _handle_audio_capture_request(chat_id, raw_message, chat_state)
    if control_result:
        return control_result

    pending_audio = chat_state.get("pending_audio_evidence")

    if pending_audio and explicit_case:
        response = await attach_case_evidence(
            case_number=explicit_case,
            descripcion=pending_audio["summary"],
            url_foto=pending_audio.get("audio_path", ""),
            media_kind="audio",
            transcript=pending_audio.get("transcript", ""),
            summary=pending_audio.get("summary", ""),
        )
        await update_chat_state(
            chat_id,
            active_case_number=explicit_case,
            pending_audio_evidence=None,
            pending_evidence_case=None,
            pending_evidence_requested_at=None,
            pending_audio_capture_intent=None,
            pending_audio_capture_requested_at=None,
        )
        return AudioPreprocessResult(direct_response=response)

    attachment = _extract_audio_attachment(raw_message)
    if not attachment:
        return None

    audio_path = _resolve_audio_path(attachment["path"])
    if not audio_path.exists():
        logger.warning("Audio file not found for transcription: %s", audio_path)
        return AudioPreprocessResult(
            direct_response="No pude leer ese audio desde OpenClaw. Reenvíalo otra vez."
        )

    audio_bytes = audio_path.read_bytes()
    try:
        transcript = await _transcribe_audio(audio_bytes, attachment["format"])
    except LLMError:
        logger.exception("Audio transcription failed")
        return AudioPreprocessResult(
            direct_response="No pude transcribir ese audio. Reenvíalo o escríbeme el mensaje."
        )
    transcript = _clean_text(transcript)
    caption = _clean_text(attachment["text"])
    combined_text = ". ".join(part for part in [caption, transcript] if part)

    spoken_control = await _handle_audio_capture_request(
        chat_id,
        combined_text or transcript,
        chat_state,
    )
    if spoken_control:
        return spoken_control

    active_case = chat_state.get("active_case_number")
    pending_case = _fresh_pending_case(chat_state)
    analysis = await _analyze_audio(
        transcript=transcript,
        caption=caption,
        active_case_number=active_case,
        pending_evidence_case=pending_case,
        recent_history=await _recent_history(chat_id),
    )

    case_number = analysis.get("case_number") or explicit_case or pending_case
    if analysis.get("mode") == "case_evidence" and case_number:
        response = await attach_case_evidence(
            case_number=case_number,
            descripcion=analysis.get("summary") or _fallback_audio_summary(transcript),
            url_foto=attachment["path"],
            media_kind="audio",
            transcript=transcript,
            summary=analysis.get("summary") or "",
        )
        await update_chat_state(
            chat_id,
            active_case_number=case_number,
            pending_audio_evidence=None,
            pending_evidence_case=None,
            pending_evidence_requested_at=None,
            pending_audio_capture_intent=None,
            pending_audio_capture_requested_at=None,
        )
        return AudioPreprocessResult(direct_response=response)

    if analysis.get("mode") == "case_evidence":
        await update_chat_state(
            chat_id,
            pending_audio_evidence={
                "audio_path": attachment["path"],
                "transcript": transcript,
                "summary": analysis.get("summary") or _fallback_audio_summary(transcript),
                "saved_at": datetime.now(timezone.utc).isoformat(),
            },
            pending_evidence_case=pending_case,
            pending_evidence_requested_at=chat_state.get("pending_evidence_requested_at"),
            pending_audio_capture_intent=None,
            pending_audio_capture_requested_at=None,
        )
        return AudioPreprocessResult(
            direct_response=(
                "Recibí el audio y entendí que es evidencia del caso.\n"
                f"📝 Resumen: {analysis.get('summary') or _fallback_audio_summary(transcript)}\n\n"
                "Dime el número de caso para adjuntarlo, por ejemplo `GAR-0001`."
            )
        )

    await update_chat_state(chat_id, last_audio_transcript=transcript)
    return AudioPreprocessResult(rewritten_message=combined_text or transcript)


def _extract_audio_attachment(raw_message: str) -> dict | None:
    match = _AUDIO_MARKER_RE.search(raw_message)
    if not match:
        return None

    ext = (match.group("ext") or "").lower()
    mime = (match.group("mime") or "").lower()
    path = (match.group("altpath") or match.group("path") or "").strip()
    return {
        "path": path,
        "text": _sanitize_attachment_text(match.group("text")),
        "mime": mime,
        "format": _audio_format(ext, mime),
    }


def _resolve_audio_path(host_path: str) -> Path:
    raw_path = Path(host_path)
    if raw_path.exists():
        return raw_path
    return Path(settings.openclaw_media_dir) / raw_path.name


def _audio_format(ext: str, mime: str) -> str:
    if ext in {"ogg", "oga"} or "ogg" in mime:
        return "ogg"
    if ext in {"mp3"} or "mpeg" in mime:
        return "mp3"
    if ext in {"wav"} or "wav" in mime:
        return "wav"
    if ext in {"m4a"} or "mp4" in mime:
        return "mp3"
    guessed, _ = mimetypes.guess_type(f"file.{ext}")
    if guessed and "wav" in guessed:
        return "wav"
    return "ogg"


async def _transcribe_audio(audio_bytes: bytes, audio_format: str) -> str:
    prepared_bytes, prepared_format = _prepare_audio_for_openrouter(audio_bytes, audio_format)
    transcript = await transcribe_audio(
        audio_bytes=prepared_bytes,
        audio_format=prepared_format,
        model=settings.audio_transcribe_model,
        language="es",
    )
    transcript = _clean_text(transcript)
    if _looks_like_assistant_meta_text(transcript):
        raise LLMError("Audio transcript looked like assistant instructions instead of spoken content")
    return transcript


def _prepare_audio_for_openrouter(audio_bytes: bytes, audio_format: str) -> tuple[bytes, str]:
    if audio_format in {"mp3", "wav"}:
        return audio_bytes, audio_format
    return _convert_audio(audio_bytes, audio_format, "mp3"), "mp3"


def _convert_audio(audio_bytes: bytes, source_format: str, target_format: str) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=f".{source_format}", delete=False) as src:
        src.write(audio_bytes)
        src_path = Path(src.name)
    with tempfile.NamedTemporaryFile(suffix=f".{target_format}", delete=False) as dst:
        dst_path = Path(dst.name)

    try:
        completed = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(src_path),
                "-vn",
                str(dst_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise LLMError(f"ffmpeg failed converting {source_format} to {target_format}: {completed.stderr}")
        return dst_path.read_bytes()
    finally:
        src_path.unlink(missing_ok=True)
        dst_path.unlink(missing_ok=True)


async def _analyze_audio(
    transcript: str,
    caption: str,
    active_case_number: str | None,
    pending_evidence_case: str | None,
    recent_history: str,
) -> dict:
    prompt = f"""
Analiza este audio transcrito para Mueblería Noreña y responde JSON válido.

Objetivo:
- decidir si el audio es conversación normal o evidencia/actualización de un caso
- producir un resumen corto útil solo si es evidencia

Reglas:
- mode = "conversation" cuando el audio sea una orden, pregunta o conversación normal
- mode = "case_evidence" cuando el audio describa daño, defecto, inconformidad, evidencia, seguimiento técnico o algo para adjuntar a un caso
- Si hay pending_evidence_case reciente, úsalo como case_number por defecto salvo que el audio indique otro
- NO uses active_case_number por sí solo para convertir un audio conversacional en evidencia
- Si el audio es una pregunta al bot, una consulta de estado o una orden conversacional, es "conversation"
- Solo marca "case_evidence" sin pending_evidence_case cuando el propio audio describa claramente un daño o seguimiento técnico de producto
- Si no hay caso claro, deja case_number en null
- summary debe ser máximo 160 caracteres y útil para historial del caso
- No inventes datos

Contexto:
- active_case_number: {active_case_number or "null"}
- pending_evidence_case: {pending_evidence_case or "null"}
- recent_history: {recent_history or "sin historial útil"}

Caption escrito junto al audio:
{caption or "sin caption"}

Transcripción:
{transcript}

Responde exactamente con este JSON:
{{
  "mode": "conversation" | "case_evidence",
  "case_number": string | null,
  "summary": string | null
}}
""".strip()

    try:
        raw = await call_llm(
            [{"role": "user", "content": prompt}],
            model=settings.audio_summary_model,
            temperature=0,
            max_tokens=250,
        )
        return _parse_json_object(raw)
    except (LLMError, ValueError) as exc:
        logger.warning("Audio analysis fallback triggered: %s", exc)
        if pending_evidence_case:
            return {
                "mode": "case_evidence",
                "case_number": pending_evidence_case,
                "summary": _fallback_audio_summary(transcript),
            }
        return {"mode": "conversation", "case_number": None, "summary": None}


def _parse_json_object(raw: str) -> dict:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in analysis output")
    return json.loads(match.group(0))


def _fallback_audio_summary(transcript: str) -> str:
    clean = _clean_text(transcript)
    if len(clean) <= 160:
        return clean
    return clean[:157].rstrip() + "..."


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _looks_like_assistant_meta_text(text: str) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return True
    suspicious_phrases = (
        "adjunta el archivo",
        "adjunta aqui",
        "pega un enlace",
        "comparte un enlace",
        "reproduce el audio",
        "puedo transcribir",
        "sube el archivo",
        "si ya lo subiste",
        "formatos aceptados",
        "tamano recomendado",
        "marcas de tiempo",
        "separacion por hablantes",
        "proporciona el audio",
        "enlace publico",
        "drive",
        "wetransfer",
        "dropbox",
    )
    return any(phrase in normalized for phrase in suspicious_phrases)


def _sanitize_attachment_text(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""

    cleaned_lines: list[str] = []
    skip_json_block = False
    for line in raw.splitlines():
        stripped = line.strip()
        normalized = _normalize_text(stripped)
        if not stripped:
            continue
        if normalized.startswith("to send an image back"):
            continue
        if normalized.startswith("system:") and "exec completed" in normalized:
            continue
        if normalized.startswith("conversation info"):
            skip_json_block = True
            continue
        if skip_json_block:
            if stripped == "```":
                skip_json_block = False
            continue
        cleaned_lines.append(stripped)
    return "\n".join(cleaned_lines).strip()


def _fresh_pending_case(chat_state: dict) -> str | None:
    case_number = chat_state.get("pending_evidence_case")
    requested_at = chat_state.get("pending_evidence_requested_at")
    if not case_number or not requested_at:
        return None
    try:
        timestamp = datetime.fromisoformat(requested_at)
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - timestamp > timedelta(minutes=15):
        return None
    return case_number


def _fresh_pending_audio_capture(chat_state: dict) -> bool:
    requested_at = chat_state.get("pending_audio_capture_requested_at")
    if not chat_state.get("pending_audio_capture_intent") or not requested_at:
        return False
    try:
        timestamp = datetime.fromisoformat(requested_at)
    except ValueError:
        return False
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - timestamp <= timedelta(minutes=15)


async def _recent_history(chat_id: str) -> str:
    try:
        history = await get_chat_history(chat_id)
    except Exception:
        return ""
    last_messages = history[-4:]
    return " | ".join(f"{msg.get('role')}: {msg.get('content', '')}" for msg in last_messages)


async def _safe_get_chat_state(chat_id: str) -> dict:
    try:
        return await get_chat_state(chat_id)
    except Exception as exc:
        logger.warning("Audio preprocessing without chat_state: %s", exc)
        return {}


async def _handle_audio_capture_request(
    chat_id: str,
    raw_message: str,
    chat_state: dict,
) -> AudioPreprocessResult | None:
    if _extract_audio_attachment(raw_message):
        return None

    normalized = _normalize_text(raw_message)
    if not _is_audio_capture_request(normalized):
        return None

    case_number = extract_case_number(raw_message) or chat_state.get("active_case_number")
    if not case_number:
        await update_chat_state(
            chat_id,
            pending_audio_capture_intent=True,
            pending_audio_capture_requested_at=datetime.now(timezone.utc).isoformat(),
            pending_evidence_case=None,
            pending_evidence_requested_at=None,
            pending_audio_evidence=None,
        )
        return AudioPreprocessResult(
            direct_response=(
                "Dime primero el numero del caso para preparar la carga del audio, "
                "por ejemplo `GAR-0001`."
            )
        )

    await update_chat_state(
        chat_id,
        active_case_number=case_number,
        pending_evidence_case=case_number,
        pending_evidence_requested_at=datetime.now(timezone.utc).isoformat(),
        pending_audio_evidence=None,
        pending_audio_capture_intent=None,
        pending_audio_capture_requested_at=None,
    )
    return AudioPreprocessResult(
        direct_response=(
            f"Por favor envia el audio con mas detalles de la garantia para el caso {case_number}. "
            "Puede ser tuyo o del cliente."
        )
    )


def _is_audio_capture_request(normalized: str) -> bool:
    wants_audio = bool(
        re.search(
            r"\b("
            r"agregar|adjuntar|anadir|subir|mandar|enviar|cargar"
            r")\b(?:\s+\w+){0,4}\s+\baudio\b",
            normalized,
        )
        or re.search(
            r"\bquiero\b(?:\s+\w+){0,3}\s+\b("
            r"agregar|adjuntar|anadir|subir|mandar|enviar|cargar"
            r")\b(?:\s+\w+){0,4}\s+\baudio\b",
            normalized,
        )
    )
    business_target = bool(
        re.search(
            r"\b("
            r"caso|garantia|evidencia|soporte|reclamo"
            r")\b",
            normalized,
        )
    )
    return wants_audio and business_target


def _normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", text).strip().lower()
