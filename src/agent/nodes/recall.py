"""recall_node — recupera el contexto útil antes de que el agente razone."""
import logging
import re
import uuid

from langchain_core.messages import SystemMessage
from sqlalchemy import or_, select

from src.agent.routing import extract_case_number, normalize_text
from src.agent.state import AgentState
from src.database import AsyncSessionFactory
from src.memory import get_chat_history, search_memory
from src.models import Case, Client

logger = logging.getLogger(__name__)


async def recall_node(state: AgentState) -> dict:
    """Inyecta contexto completo en el estado antes de razonar."""
    chat_id = state.get("chat_id", "")
    user_message = _get_last_user_message(state)

    # 1. Chat buffer (Redis) — conversación reciente
    chat_history = []
    try:
        chat_history = await get_chat_history(chat_id)
    except Exception as exc:
        logger.warning("Redis unavailable, continuing without chat history: %s", exc)

    # 2. Memoria semántica (Qdrant) — historial relevante
    semantic_results = []
    if user_message and _should_load_semantic_memory(user_message):
        try:
            semantic_results = await search_memory(user_message, limit=5)
        except Exception as exc:
            logger.warning("Qdrant unavailable, continuing without semantic memory: %s", exc)

    # 3. Contexto de negocio (PostgreSQL)
    client_context = None
    case_context = []
    if user_message:
        try:
            async with AsyncSessionFactory() as db:
                client_context, case_context = await _fetch_business_context(db, user_message)
        except Exception as exc:
            logger.warning("DB unavailable for context fetch: %s", exc)

    # Construir contexto enriquecido para el agente
    context_summary = _build_context_summary(chat_history, semantic_results, client_context, case_context)

    # Insertar contexto como mensaje de sistema antes del mensaje del usuario
    context_message = SystemMessage(content=f"[CONTEXTO]\n{context_summary}\n[/CONTEXTO]")

    return {
        "messages": [context_message],
        "chat_history": chat_history,
        "client_context": client_context,
        "case_context": case_context,
    }


def _get_last_user_message(state: AgentState) -> str:
    """Extrae el texto del último mensaje del usuario."""
    for msg in reversed(state.get("messages", [])):
        if hasattr(msg, "content") and isinstance(msg.content, str):
            return msg.content
    return ""


async def _fetch_business_context(db, user_message: str) -> tuple[dict | None, list[dict]]:
    """Busca cliente y casos relacionados en PostgreSQL."""
    client = None
    cases = []

    explicit_case = extract_case_number(user_message)
    if explicit_case:
        result = await db.execute(
            select(Case, Client)
            .join(Client, Case.client_id == Client.id)
            .where(Case.case_number == explicit_case)
            .limit(1)
        )
        row = result.first()
        if row:
            case_obj, client_obj = row
            client = {"id": str(client_obj.id), "name": client_obj.name, "phone": client_obj.phone}
            cases.append({
                "id": str(case_obj.id),
                "case_number": case_obj.case_number,
                "type": case_obj.type,
                "status": case_obj.status,
                "product": case_obj.product,
                "assigned_to": case_obj.assigned_to,
            })
            return client, cases

    # 1. Intentar encontrar teléfono en el mensaje
    phone_match = re.search(r"3\d{9}", user_message)
    if phone_match:
        phone_digits = phone_match.group()
        result = await db.execute(
            select(Client).where(Client.phone.contains(phone_digits))
        )
        client_obj = result.scalar_one_or_none()
        if client_obj:
            client = {"id": str(client_obj.id), "name": client_obj.name, "phone": client_obj.phone}

    # 2. Intentar encontrar cédula en el mensaje
    if not client:
        cedula_match = re.search(r"(?:cedula|cédula|cc|c\.c\.)\s*[:\s]?\s*(\d{6,12})", user_message, re.IGNORECASE)
        if cedula_match:
            cedula_digits = cedula_match.group(1)
            result = await db.execute(
                select(Client).where(Client.cedula == cedula_digits)
            )
            client_obj = result.scalar_one_or_none()
            if client_obj:
                client = {"id": str(client_obj.id), "name": client_obj.name, "phone": client_obj.phone}

    # 3. Intentar identificar cliente por nombre si no hubo match estructurado
    if not client:
        name_candidates = _extract_name_candidates(user_message)
        if name_candidates:
            filters = [Client.name.ilike(f"%{candidate}%") for candidate in name_candidates]
            result = await db.execute(
                select(Client).where(or_(*filters)).order_by(Client.created_at.desc()).limit(1)
            )
            client_obj = result.scalar_one_or_none()
            if client_obj:
                client = {"id": str(client_obj.id), "name": client_obj.name, "phone": client_obj.phone}

    # 4. Casos abiertos recientes o del cliente identificado
    query = (
        select(Case)
        .where(Case.status.in_(["abierto", "escalado", "en_proceso"]))
        .order_by(Case.created_at.desc())
        .limit(10)
    )
    if client:
        query = (
            select(Case)
            .where(
                Case.client_id == uuid.UUID(client["id"]),
                Case.status.in_(["abierto", "escalado", "en_proceso"]),
            )
            .order_by(Case.created_at.desc())
            .limit(10)
        )

    result = await db.execute(query)
    for case in result.scalars().all():
        cases.append({
            "id": str(case.id),
            "case_number": case.case_number,
            "type": case.type,
            "status": case.status,
            "product": case.product,
            "assigned_to": case.assigned_to,
        })

    return client, cases


def _should_load_semantic_memory(user_message: str) -> bool:
    """Evita memoria semántica en consultas deterministas donde solo añade latencia."""
    normalized = normalize_text(user_message)
    if extract_case_number(user_message):
        return False

    deterministic_signals = [
        "dame detalles", "detalle de", "muestrame", "muestrame las", "muestrame los",
        "cual fue la razon", "cual es la razon", "que paso con", "garantias activas",
        "garantias pendientes", "cotizaciones pendientes", "facturas de", "busca la factura",
        # Consultas de estado global — nunca hay historial relevante, solo tools
        "facturas vencidas", "hay vencidas", "saldos pendientes", "quien debe",
        "cuentas por cobrar", "pedidos pendientes", "hay entregas", "entregas hoy",
        "entregas manana", "hay algo para entregar", "abonos huerfanos",
    ]
    return not any(signal in normalized for signal in deterministic_signals)


def _extract_name_candidates(user_message: str) -> list[str]:
    """Extrae frases cortas que probablemente correspondan al nombre de un cliente."""
    normalized = re.sub(r"[^\w\sáéíóúÁÉÍÓÚñÑ]", " ", user_message)
    patterns = [
        r"(?:cliente|señor|senor|señora|sr|sra)\s+([A-ZÁÉÍÓÚÑ][\wáéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][\wáéíóúñ]+){0,2})",
        r"(?:de|del|para)\s+([A-ZÁÉÍÓÚÑ][\wáéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][\wáéíóúñ]+){0,2})",
    ]

    candidates: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, user_message):
            candidate = match.group(1).strip()
            if candidate and len(candidate) >= 3:
                candidates.append(candidate)

    title_case_sequences = re.findall(
        r"\b([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+){0,2})\b",
        normalized,
    )
    for candidate in title_case_sequences:
        if candidate.casefold() not in {"daniel", "michelle"}:
            candidates.append(candidate.strip())

    # Quitar duplicados preservando orden
    seen = set()
    unique_candidates = []
    for candidate in candidates:
        key = candidate.casefold()
        if key not in seen:
            seen.add(key)
            unique_candidates.append(candidate)
    return unique_candidates[:5]


def _build_context_summary(
    chat_history: list[dict],
    semantic_results: list[dict],
    client: dict | None,
    cases: list[dict],
) -> str:
    """Construye un resumen de contexto legible para el LLM."""
    parts = []

    if chat_history:
        parts.append("Conversación reciente:")
        for msg in chat_history[-5:]:
            role = "Tú" if msg.get("role") == "user" else "Bot"
            parts.append(f"  {role}: {msg.get('content', '')[:200]}")

    if client:
        parts.append(f"Cliente identificado: {client['name']} ({client['phone']}) — ID: {client['id']}")

    if cases:
        parts.append(f"Casos abiertos ({len(cases)}):")
        for c in cases[:5]:
            parts.append(f"  #{c['case_number']} — {c['type']} — {c['status']} — {c.get('product', '')}")

    if semantic_results:
        parts.append("Historial relevante:")
        for r in semantic_results[:3]:
            parts.append(f"  - {r.get('text', '')[:150]}")

    return "\n".join(parts) if parts else "Sin contexto previo disponible."
