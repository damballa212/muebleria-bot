"""AgentState — estado compartido entre todos los nodos del grafo LangGraph."""
from typing import Annotated

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class AgentState(TypedDict):
    # Mensajes de la sesión actual (LangGraph los acumula automáticamente)
    messages: Annotated[list[BaseMessage], add_messages]

    # Últimos N mensajes de conversaciones anteriores (inyectados por recall_node)
    chat_history: list[dict]

    # Contexto de negocio inyectado por recall_node
    client_context: dict | None    # Datos del cliente identificado
    case_context: list[dict]       # Casos abiertos relacionados
    route: dict | None             # Fast-path propuesto por router_node

    # Metadata del request
    chat_id: str                   # ID del chat de Telegram
    source: str                    # "openclaw" | "direct"
