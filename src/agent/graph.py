"""Grafo LangGraph completo: START → recall → agent ↔ tools → record → END."""
import functools

from langgraph.graph import END, START, StateGraph

from src.agent.nodes.agent import agent_node
from src.agent.nodes.record import record_node
from src.agent.nodes.recall import recall_node
from src.agent.nodes.router import build_fast_action_node, route_after_router, router_node
from src.agent.nodes.tools_executor import build_tools_node, should_continue
from src.agent.state import AgentState
from src.agent.tools.cases import (
    actualizar_caso,
    adjuntar_evidencia,
    buscar_caso,
    escalar_cotizacion,
    escalar_garantia,
    generar_mensaje_seguimiento,
    listar_casos_pendientes,
    ver_caso,
)
from src.agent.tools.clients import (
    agregar_nota_cliente,
    buscar_cliente,
    crear_cliente,
    listar_clientes,
    ver_historial_cliente,
)
from src.agent.tools.invoices import (
    buscar_abonos_huerfanos,
    buscar_factura,
    listar_facturas,
    listar_facturas_vencidas,
    registrar_remision,
)
from src.agent.tools.orders import (
    actualizar_pedido,
    confirmar_entrega,
    listar_pedidos_pendientes,
    ver_seguimiento_pedido,
)
from src.agent.tools.reminders import (
    cancelar_recordatorio,
    crear_recordatorio,
    get_datetime,
    listar_recordatorios,
)

# Lista de todos los tools disponibles
ALL_TOOLS = [
    # Casos
    escalar_garantia,
    escalar_cotizacion,
    actualizar_caso,
    buscar_caso,
    ver_caso,
    listar_casos_pendientes,
    generar_mensaje_seguimiento,
    adjuntar_evidencia,
    # Clientes
    buscar_cliente,
    listar_clientes,
    crear_cliente,
    ver_historial_cliente,
    agregar_nota_cliente,
    # Facturas / Remisiones
    registrar_remision,
    listar_facturas,
    listar_facturas_vencidas,
    buscar_factura,
    buscar_abonos_huerfanos,
    # Seguimiento de pedidos / entregas
    listar_pedidos_pendientes,
    ver_seguimiento_pedido,
    confirmar_entrega,
    actualizar_pedido,
    # Recordatorios
    get_datetime,
    crear_recordatorio,
    listar_recordatorios,
    cancelar_recordatorio,
]


def build_graph():
    """Construye y compila el grafo de LangGraph."""
    graph = StateGraph(AgentState)

    # Nodos
    graph.add_node("recall", recall_node)
    graph.add_node("router", router_node)
    graph.add_node("fast_action", build_fast_action_node(ALL_TOOLS))
    graph.add_node("agent", functools.partial(agent_node, tools=ALL_TOOLS))
    graph.add_node("tools", build_tools_node(ALL_TOOLS))
    graph.add_node("record", record_node)

    # Edges fijos
    graph.add_edge(START, "recall")
    graph.add_edge("recall", "router")
    graph.add_conditional_edges("router", route_after_router, {"fast_action": "fast_action", "agent": "agent"})
    graph.add_edge("tools", "agent")     # Después de ejecutar tools, vuelve al agente
    graph.add_edge("fast_action", "record")

    # Edge condicional: el agente decide si necesita más tools o ya terminó
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", "record": "record"})
    graph.add_edge("record", END)

    return graph.compile()


# Instancia compilada — singleton, se importa desde main.py
compiled_graph = build_graph()
