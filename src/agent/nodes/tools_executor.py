"""tools_executor_node — ejecuta los tools que el agente seleccionó.
Usa ToolNode de LangGraph para manejar la iteración automáticamente.
"""
from langchain_core.messages import AIMessage
from langgraph.prebuilt import ToolNode

from src.agent.state import AgentState


def should_continue(state: AgentState) -> str:
    """
    Conditional edge: decide si continuar ejecutando tools o ir al record_node.
    Retorna "tools" si el último mensaje tiene tool_calls, "record" si es respuesta final.
    """
    last_message = state["messages"][-1]
    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        return "tools"
    return "record"


def build_tools_node(tools: list) -> ToolNode:
    """Construye el ToolNode de LangGraph con todos los tools registrados."""
    return ToolNode(tools)
