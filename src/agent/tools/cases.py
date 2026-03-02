"""Tools de casos: escalar_garantia, escalar_cotizacion, actualizar_caso,
buscar_caso, ver_caso, listar_casos_pendientes, generar_mensaje_seguimiento.
"""
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path

from langchain_core.tools import tool
from sqlalchemy import desc, select
from sqlalchemy.orm import selectinload

from src.database import AsyncSessionFactory
from src.models import Case, CaseUpdate, Client, Invoice

logger = logging.getLogger(__name__)


def _next_case_number(prefix: str, existing: list[str]) -> str:
    """Genera el siguiente número de caso: GAR-0042 / COT-0018."""
    nums = []
    for n in existing:
        match = re.search(r"\d+", n)
        if match:
            nums.append(int(match.group()))
    next_num = (max(nums) + 1) if nums else 1
    return f"{prefix}-{next_num:04d}"


def _sanitize_case_note(note: str) -> str:
    """Oculta rutas locales y detalles internos al mostrar historial."""
    if not note:
        return note

    note = re.sub(r"\s*\(foto:\s*[^)]+\)", " (foto adjunta)", note)
    note = re.sub(r"\s*\(audio:\s*[^)]+\)", " (audio adjunto)", note)
    note = re.sub(r"/Users/[^ ]+", lambda m: Path(m.group(0)).name, note)
    return note


async def attach_case_evidence(
    case_number: str,
    descripcion: str,
    url_foto: str = "",
    media_kind: str = "foto",
    transcript: str = "",
    summary: str = "",
) -> str:
    """Implementación compartida para adjuntar evidencia al caso."""
    async with AsyncSessionFactory() as db:
        result = await db.execute(
            select(Case).where(Case.case_number == case_number)
        )
        case = result.scalar_one_or_none()
        if not case:
            return f"❌ Caso {case_number} no encontrado."

        assets = case.photos or []
        entry = {
            "kind": media_kind,
            "description": descripcion,
            "uploaded_at": datetime.utcnow().isoformat(),
        }
        if url_foto:
            entry["url"] = url_foto
        if transcript:
            entry["transcript"] = transcript
        if summary:
            entry["summary"] = summary
        assets.append(entry)
        case.photos = assets
        case.updated_at = datetime.utcnow()

        if media_kind == "audio":
            nota = f"🎤 Evidencia de audio agregada: {descripcion}"
            if url_foto:
                nota += f" (audio: {url_foto})"
        else:
            nota = f"📎 Evidencia agregada: {descripcion}"
            if url_foto:
                nota += f" (foto: {url_foto})"

        update = CaseUpdate(case_id=case.id, notes=nota)
        db.add(update)
        await db.commit()

    icon = "🎤" if media_kind == "audio" else "📎"
    return f"✅ Evidencia agregada al caso #{case_number}\n{icon} {descripcion}"


@tool
async def escalar_garantia(client_id: str, descripcion: str, producto: str, invoice_number: str = "") -> str:
    """Crea un caso de GARANTÍA asignado a Michelle y activa el timer de alertas.

    Args:
        client_id: UUID del cliente (obtenido de buscar_cliente).
        descripcion: Descripción del problema reportado por el cliente.
        producto: Nombre del producto con falla.
        invoice_number: Número de remisión/factura asociada (opcional pero recomendado
                        cuando el cliente tiene más de una compra).
    """
    async with AsyncSessionFactory() as db:
        # Resolver invoice_id si se proporcionó número de remisión
        invoice_id = None
        invoice_productos = None
        if invoice_number:
            inv_result = await db.execute(
                select(Invoice).where(Invoice.invoice_number == invoice_number)
            )
            inv = inv_result.scalar_one_or_none()
            if inv:
                invoice_id = inv.id
                raw = inv.raw_ocr or {}
                invoice_productos = raw.get("productos") or ", ".join(
                    [i.get("descripcion", "") for i in (inv.items or []) if i.get("descripcion")]
                )
                # Usar productos de la factura si no se especificó producto
                if not producto and invoice_productos:
                    producto = invoice_productos

        # Obtener números existentes para generar el siguiente
        result = await db.execute(select(Case.case_number).where(Case.type == "garantia"))
        existing = [r[0] for r in result.all()]
        case_number = _next_case_number("GAR", existing)

        case = Case(
            case_number=case_number,
            type="garantia",
            client_id=uuid.UUID(client_id),
            invoice_id=invoice_id,
            description=descripcion,
            product=producto,
            status="escalado",
            assigned_to="michelle",
        )
        db.add(case)
        await db.commit()
        await db.refresh(case)

    inv_ref = f"\nRemisión vinculada: #{invoice_number}" if invoice_number and invoice_id else (
        "\n⚠️ Remisión no encontrada — caso creado sin vincular factura" if invoice_number else ""
    )
    return (
        f"✅ Garantía creada — #{case_number}\n"
        f"Producto: {producto}\n"
        f"Asignado a: Michelle{inv_ref}\n"
        f"Te avisaré si no hay respuesta en 24h."
    )


@tool
async def escalar_cotizacion(client_id: str, descripcion: str) -> str:
    """Crea un caso de COTIZACIÓN asignado a Daniel Noreña."""
    async with AsyncSessionFactory() as db:
        result = await db.execute(select(Case.case_number).where(Case.type == "cotizacion"))
        existing = [r[0] for r in result.all()]
        case_number = _next_case_number("COT", existing)

        case = Case(
            case_number=case_number,
            type="cotizacion",
            client_id=uuid.UUID(client_id),
            description=descripcion,
            status="escalado",
            assigned_to="daniel",
        )
        db.add(case)
        await db.commit()

    return f"✅ Cotización creada — #{case_number}\nSolicitud: {descripcion}\nAsignado a: Daniel Noreña\nTe avisaré si no hay respuesta en 24h."


@tool
async def actualizar_caso(case_number: str, estado: str, notas: str, decision: str = "") -> str:
    """Actualiza el estado de un caso y registra las notas en el historial.

    Acepta el número humano del caso (ej. GAR-0001) y, por compatibilidad,
    también tolera un UUID interno si alguna integración lo envía así.
    """
    async with AsyncSessionFactory() as db:
        case = None

        # Flujo principal: número visible del caso.
        result = await db.execute(
            select(Case).where(Case.case_number == case_number)
        )
        case = result.scalar_one_or_none()

        # Compatibilidad con integraciones antiguas que pudieran mandar UUID.
        if not case:
            try:
                case = await db.get(Case, uuid.UUID(case_number))
            except ValueError:
                case = None

        if not case:
            return f"❌ Caso {case_number} no encontrado."

        last_update_result = await db.execute(
            select(CaseUpdate)
            .where(CaseUpdate.case_id == case.id)
            .order_by(desc(CaseUpdate.created_at))
            .limit(1)
        )
        last_update = last_update_result.scalar_one_or_none()

        same_status = case.status == estado
        same_decision = (case.decision or "") == (decision or "")
        same_note = bool(last_update and last_update.new_status == estado and last_update.notes.strip() == notas.strip())
        if same_status and same_decision and same_note:
            return (
                f"ℹ️ Caso #{case.case_number} sin cambios\n"
                f"Estado: {estado}\n"
                f"Notas: {notas}"
            )

        case.status = estado
        if decision:
            case.decision = decision
        case.updated_at = datetime.utcnow()
        if estado in ("resuelto", "cerrado"):
            case.resolved_at = datetime.utcnow()

        update = CaseUpdate(case_id=case.id, notes=notas, new_status=estado)
        db.add(update)
        await db.commit()

    return f"✅ Caso #{case.case_number} actualizado\nEstado: {estado}\n{f'Decisión: {decision}' if decision else ''}\nNotas: {notas}"


@tool
async def buscar_caso(query: str) -> str:
    """Busca casos por nombre de cliente, número de caso o descripción."""
    from src.memory import search_memory
    results = await search_memory(query, limit=5)
    if not results:
        return "No encontré casos relacionados con esa búsqueda."
    lines = ["Casos encontrados:"]
    for r in results:
        lines.append(f"  - {r.get('text', '')[:200]}")
    return "\n".join(lines)


@tool
async def ver_caso(case_number: str) -> str:
    """Muestra el historial completo de un caso con todas sus actualizaciones."""
    async with AsyncSessionFactory() as db:
        result = await db.execute(
            select(Case)
            .options(selectinload(Case.updates), selectinload(Case.client))
            .where(Case.case_number == case_number)
        )
        case = result.scalar_one_or_none()
        if not case:
            return f"❌ Caso {case_number} no encontrado."

        tipo_label = {"garantia": "Garantía", "cotizacion": "Cotización"}.get(case.type, case.type.capitalize())
        status_label = {"abierto": "Abierto", "escalado": "Escalado", "en_proceso": "En proceso", "resuelto": "Resuelto", "cerrado": "Cerrado"}.get(case.status, case.status)
        assignee_label = {"michelle": "Michelle", "daniel": "Daniel"}.get(case.assigned_to, case.assigned_to)
        lines = [
            f"📋 Caso #{case.case_number} — {tipo_label}",
            f"👤 {case.client.name} ({case.client.phone})" if case.client else "",
            f"🛋 Producto: {case.product or 'No especificado'}",
            f"📝 Descripción: {case.description or 'Sin descripción'}",
            f"📌 Estado: {status_label} · Asignado a: {assignee_label}",
            f"📅 Creado: {case.created_at.strftime('%d/%m/%Y %H:%M')}",
        ]
        if case.decision:
            lines.append(f"✅ Decisión: {case.decision}")
        if case.updates:
            lines.append("\nActualizaciones:")
            for upd in case.updates:
                lines.append(f"  [{upd.created_at.strftime('%d/%m %H:%M')}] {_sanitize_case_note(upd.notes)}")
        return "\n".join(filter(None, lines))


@tool
async def listar_casos_pendientes(tipo: str = "", assigned_to: str = "") -> str:
    """Lista los casos abiertos, filtrados por tipo o responsable."""
    async with AsyncSessionFactory() as db:
        query = select(Case).options(selectinload(Case.client)).where(
            Case.status.in_(["abierto", "escalado", "en_proceso"])
        ).order_by(Case.created_at.asc())
        if tipo:
            query = query.where(Case.type == tipo.lower())
        if assigned_to:
            query = query.where(Case.assigned_to == assigned_to.lower())

        result = await db.execute(query)
        cases = result.scalars().all()

    if not cases:
        return "No hay casos pendientes."

    tipo_label = {"garantia": "Garantía", "cotizacion": "Cotización"}.get
    status_label = {"abierto": "Abierto", "escalado": "Escalado", "en_proceso": "En proceso"}.get
    assignee_label = {"michelle": "Michelle", "daniel": "Daniel"}.get

    lines = [f"📋 *Casos pendientes* ({len(cases)})\n"]
    for c in cases:
        client_name = c.client.name if c.client else "Sin cliente"
        t = tipo_label(c.type, c.type.capitalize())
        s = status_label(c.status, c.status)
        a = assignee_label(c.assigned_to, c.assigned_to)
        created = c.created_at.strftime("%d/%m/%Y") if c.created_at else ""
        lines.append(
            f"• #{c.case_number} — {t}\n"
            f"  👤 {client_name}\n"
            f"  🛋 {c.product or 'Producto no especificado'}\n"
            f"  📌 {s} · {a} · {created}\n"
        )
    return "\n".join(lines)


@tool
async def generar_mensaje_seguimiento(case_number: str) -> str:
    """Genera un mensaje sugerido para hacerle seguimiento a Michelle o Daniel sobre el caso."""
    from src.llm import call_llm

    async with AsyncSessionFactory() as db:
        result = await db.execute(
            select(Case).options(selectinload(Case.client)).where(Case.case_number == case_number)
        )
        case = result.scalar_one_or_none()
        if not case:
            return f"❌ Caso {case_number} no encontrado."

        client_name = case.client.name if case.client else "el cliente"
        responsible = "Michelle" if case.assigned_to == "michelle" else "Don Daniel"
        case_type = "garantía" if case.type == "garantia" else "cotización"

    prompt = f"""Escribe un mensaje corto y profesional (máx 3 líneas) para hacerle seguimiento a {responsible} sobre la {case_type} del cliente {client_name}.
Producto: {case.product or 'mueble'}.
El mensaje debe ser cordial y concreto. No uses saludos largos."""

    msg = await call_llm([{"role": "user", "content": prompt}], temperature=0.7)
    return f"📝 Mensaje sugerido para {responsible}:\n─────────────\n{msg}\n─────────────"


@tool
async def adjuntar_evidencia(case_number: str, descripcion: str, url_foto: str = "") -> str:
    """Agrega evidencia (descripción y/o foto) al historial de un caso existente.

    Args:
        case_number: Número del caso, ej: GAR-0001.
        descripcion: Descripción de la evidencia (qué muestra la foto, qué dijo el cliente, etc).
        url_foto: URL o ruta de la foto adjunta (opcional).
    """
    return await attach_case_evidence(
        case_number=case_number,
        descripcion=descripcion,
        url_foto=url_foto,
        media_kind="foto",
    )
