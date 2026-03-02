"""record_node — persiste la interacción al finalizar.
1. Guarda en Redis: chat buffer (mensaje usuario + respuesta del bot)
2. Guarda en Qdrant: si la interacción tiene contenido de negocio relevante
3. Guarda en PostgreSQL: interaction_log
"""
import logging
import re
from datetime import datetime, timezone

from langchain_core.messages import AIMessage, HumanMessage

from src.agent.routing import extract_case_number
from src.agent.state import AgentState
from src.database import AsyncSessionFactory
from src.memory import save_to_chat_buffer, save_to_memory, update_chat_state
from src.models import InteractionLog

logger = logging.getLogger(__name__)

# Palabras clave que indican contenido de negocio worth saving en Qdrant
BUSINESS_KEYWORDS = {
    "garantía", "garantia", "caso", "factura", "cliente", "michelle",
    "daniel", "cotización", "cotizacion", "colchón", "mueble", "producto",
    "reclamo", "aprobó", "rechazó", "proveedor", "resolución",
}


async def record_node(state: AgentState) -> dict:
    """Persiste la interacción en Redis, Qdrant y PostgreSQL."""
    chat_id = state.get("chat_id", "")
    client_id = state.get("client_context", {}) or {}
    case_context = state.get("case_context", [])

    user_message = _extract_user_message(state)
    bot_response = _extract_bot_response(state)

    if not user_message or not bot_response:
        return {}

    # 1. Chat buffer (Redis)
    try:
        await save_to_chat_buffer(chat_id, "user", user_message)
        await save_to_chat_buffer(chat_id, "assistant", bot_response)
        await _update_conversation_state(chat_id, user_message, bot_response, case_context)
    except Exception as exc:
        logger.warning("Redis chat buffer save failed: %s", exc)

    # 2. Qdrant — solo si tiene contenido de negocio relevante
    if _is_business_relevant(user_message + " " + bot_response):
        try:
            metadata = {
                "chat_id": chat_id,
                "client_id": client_id.get("id") if client_id else None,
                "case_id": case_context[0]["id"] if case_context else None,
            }
            combined = f"Usuario: {user_message}\nAsistente: {bot_response}"
            await save_to_memory(combined, metadata)
        except Exception as exc:
            logger.warning("Qdrant save failed: %s", exc)

    # 3. interaction_log (PostgreSQL)
    try:
        async with AsyncSessionFactory() as db:
            client_uuid = client_id.get("id") if isinstance(client_id, dict) else None
            case_uuid = case_context[0]["id"] if case_context else None

            db.add(InteractionLog(
                client_id=client_uuid,
                case_id=case_uuid,
                role="user",
                content=user_message,
            ))
            db.add(InteractionLog(
                client_id=client_uuid,
                case_id=case_uuid,
                role="assistant",
                content=bot_response,
            ))
            await db.commit()
    except Exception as exc:
        logger.warning("PostgreSQL interaction_log save failed: %s", exc)

    return {}


def _extract_user_message(state: AgentState) -> str:
    for msg in state.get("messages", []):
        if isinstance(msg, HumanMessage) and not msg.content.startswith("[CONTEXTO]"):
            return msg.content
    return ""


def _extract_bot_response(state: AgentState) -> str:
    for msg in reversed(state.get("messages", [])):
        if isinstance(msg, AIMessage) and msg.content and not msg.tool_calls:
            return msg.content
    return ""


def _is_business_relevant(text: str) -> bool:
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in BUSINESS_KEYWORDS)


async def _update_conversation_state(chat_id: str, user_message: str, bot_response: str, case_context: list[dict]) -> None:
    active_case_number = None
    if case_context:
        active_case_number = case_context[0].get("case_number")
    active_case_number = active_case_number or extract_case_number(user_message) or extract_case_number(bot_response)

    updates: dict = {}
    if active_case_number:
        updates["active_case_number"] = active_case_number

    if _is_evidence_request(bot_response) and active_case_number:
        updates["pending_evidence_case"] = active_case_number
        updates["pending_evidence_requested_at"] = datetime.now(timezone.utc).isoformat()

    if _is_evidence_confirmation(bot_response):
        updates["pending_evidence_case"] = None
        updates["pending_evidence_requested_at"] = None
        updates["pending_audio_evidence"] = None

    if updates:
        await update_chat_state(chat_id, **updates)


def _is_evidence_request(text: str) -> bool:
    normalized = text.lower()
    evidence_words = ("evidencia", "foto", "adjunta", "adjunto", "audio", "archivo")
    send_words = ("envia", "enví", "manda", "adjunta", "sube", "comparte")
    return any(word in normalized for word in evidence_words) and any(word in normalized for word in send_words)


def _is_evidence_confirmation(text: str) -> bool:
    normalized = text.lower()
    return bool(re.search(r"evidencia agregada al caso|he adjuntado la evidencia|audio agregado al caso", normalized))
