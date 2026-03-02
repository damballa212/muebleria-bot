"""router_node y fast_action_node para resolver intenciones obvias sin pasar por el LLM completo."""
from __future__ import annotations

import logging
import re

from langchain_core.messages import AIMessage

from src.agent.routing import (
    count_family_matches,
    extract_case_number,
    has_family_match,
    latest_case_number_from_history,
    normalize_text,
    tokenize,
)
from src.agent.state import AgentState

logger = logging.getLogger(__name__)

DETAIL_ROOTS = [
    "detalle", "detalles", "razon", "motivo", "estado", "historial", "info",
    "informacion", "que", "paso", "muestra", "muestre", "ver", "mostrar",
    "consultar", "revision", "revisar", "descripcion",
]
LIST_ROOTS = ["lista", "listar", "muestre", "mostrar", "muestra", "dame", "hay", "ver"]
CASE_ROOTS = ["caso", "garantia", "cotizacion", "reclamo", "pendiente", "pendientes", "activa", "activas", "abierto", "abiertos"]
GUARANTEE_ROOTS = ["garantia", "garantias", "reclamo", "reclamos", "posventa"]
QUOTE_ROOTS = ["cotizacion", "cotizaciones", "precio", "precios", "presupuesto"]
INVOICE_ROOTS = ["factura", "facturas", "remision", "remisiones", "venta", "ventas", "compra", "compras"]
VENCIDAS_ROOTS = ["vencida", "vencidas", "vencido", "vencidos", "deben", "debe", "pendiente", "pendientes", "saldo", "saldos", "cobrar", "cobro"]
SEARCH_ROOTS = ["buscar", "busca", "buscarme", "encontrar", "hay", "tiene", "muestra", "ver", "dame", "consulta"]
DEMONSTRATIVE_ROOTS = ["ese", "esa", "este", "esta", "aquel", "aquella"]


def router_node(state: AgentState) -> dict:
    """Clasifica fast-paths seguros y deja el resto al agente completo."""
    user_message = _get_last_user_message(state)
    route = detect_fast_route(
        user_message=user_message,
        case_context=state.get("case_context", []),
        chat_history=state.get("chat_history", []),
    )
    return {"route": route}


def route_after_router(state: AgentState) -> str:
    return "fast_action" if state.get("route") else "agent"


def build_fast_action_node(tools: list):
    tools_by_name = {tool.name: tool for tool in tools}

    async def fast_action_node(state: AgentState) -> dict:
        route = state.get("route")
        if not route:
            return {}

        tool_name = route["tool_name"]
        args = route["args"]
        tool = tools_by_name.get(tool_name)
        if not tool:
            logger.warning("Fast route requested unknown tool: %s", tool_name)
            return {"route": None}

        try:
            result = await tool.ainvoke(args)
            return {"messages": [AIMessage(content=result)], "route": None}
        except Exception as exc:
            logger.exception("Fast action failed for %s: %s", tool_name, exc)
            return {
                "messages": [AIMessage(content="⚠️ Hubo un error procesando la solicitud.")],
                "route": None,
            }

    return fast_action_node


def detect_fast_route(user_message: str, case_context: list[dict], chat_history: list[dict]) -> dict | None:
    normalized = normalize_text(user_message)
    tokens = tokenize(user_message)
    explicit_case = extract_case_number(user_message)
    resolved_case = explicit_case or _resolve_contextual_case(normalized, tokens, case_context, chat_history)

    detail_score = count_family_matches(tokens, DETAIL_ROOTS)
    list_score = count_family_matches(tokens, LIST_ROOTS)
    case_score = count_family_matches(tokens, CASE_ROOTS)
    invoice_score = count_family_matches(tokens, INVOICE_ROOTS)
    search_score = count_family_matches(tokens, SEARCH_ROOTS)

    if resolved_case and _is_case_detail_request(normalized, tokens, detail_score):
        return {
            "kind": "tool",
            "tool_name": "ver_caso",
            "args": {"case_number": resolved_case},
        }

    if case_score and list_score:
        tipo = ""
        if has_family_match(tokens, GUARANTEE_ROOTS):
            tipo = "garantia"
        elif has_family_match(tokens, QUOTE_ROOTS):
            tipo = "cotizacion"
        assigned_to = ""
        if "michelle" in normalized:
            assigned_to = "michelle"
        elif "daniel" in normalized:
            assigned_to = "daniel"
        return {
            "kind": "tool",
            "tool_name": "listar_casos_pendientes",
            "args": {"tipo": tipo, "assigned_to": assigned_to},
        }

    # Facturas vencidas / saldos pendientes — ANTES del search genérico
    if invoice_score and has_family_match(tokens, VENCIDAS_ROOTS):
        return {
            "kind": "tool",
            "tool_name": "listar_facturas_vencidas",
            "args": {},
        }

    if invoice_score and search_score:
        query = _extract_invoice_query(user_message)
        if query:
            return {
                "kind": "tool",
                "tool_name": "buscar_factura",
                "args": {"query": query},
            }

    return None


def _get_last_user_message(state: AgentState) -> str:
    for msg in reversed(state.get("messages", [])):
        if hasattr(msg, "content") and isinstance(msg.content, str) and not msg.content.startswith("[CONTEXTO]"):
            return msg.content
    return ""


def _resolve_contextual_case(normalized: str, tokens: list[str], case_context: list[dict], chat_history: list[dict]) -> str | None:
    has_demonstrative = has_family_match(tokens, DEMONSTRATIVE_ROOTS)
    if case_context and len(case_context) == 1 and has_demonstrative:
        return case_context[0]["case_number"]
    if "la razon" in normalized or "el motivo" in normalized or "ese caso" in normalized or "esa garantia" in normalized:
        if case_context:
            return case_context[0]["case_number"]
        return latest_case_number_from_history(chat_history)
    return None


def _is_case_detail_request(normalized: str, tokens: list[str], detail_score: int) -> bool:
    if detail_score >= 1:
        return True
    if "que paso" in normalized or "cual fue" in normalized:
        return True
    return has_family_match(tokens, DEMONSTRATIVE_ROOTS) and ("caso" in tokens or "garantia" in tokens)


def _extract_invoice_query(user_message: str) -> str:
    query = re_sub_invoice_noise(user_message)
    query = query.strip(" ?!.")
    return query if len(query) >= 3 else ""


def re_sub_invoice_noise(text: str) -> str:
    normalized = normalize_text(text)
    patterns = [
        r"^(hay|tiene|busca(?:r)?|buscar|muestr(?:a|ame)?|ver|dame|consulta(?:r)?)\s+",
        r"\b(alguna|algun|una|unas|unas?)\b",
        r"\b(factura|facturas|remision|remisiones|venta|ventas|compra|compras)\b",
        r"\b(con|de|del|la|las|el|los|sobre|relacionada?s?)\b",
    ]
    query = normalized
    for pattern in patterns:
        query = re.sub(pattern, " ", query)
    return " ".join(query.split())
