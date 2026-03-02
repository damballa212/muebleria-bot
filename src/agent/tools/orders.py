"""Tools de seguimiento de pedidos: listar pendientes, confirmar entrega, actualizar estado."""
from datetime import date, datetime, timedelta, timezone

from langchain_core.tools import tool
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.database import AsyncSessionFactory
from src.models import Invoice


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _parse_fecha(fecha_str: str) -> datetime | None:
    """Parsea fecha en formatos DD/MM/YY o DD/MM/YYYY. Retorna datetime UTC o None."""
    if not fecha_str:
        return None
    for fmt in ["%d/%m/%Y", "%d/%m/%y"]:
        try:
            return datetime.strptime(fecha_str.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _business_days_between(start: date, end: date) -> int:
    """Cuenta días hábiles (lunes-viernes) desde start (exclusive) hasta end (inclusive)."""
    if end <= start:
        return 0
    count = 0
    current = start + timedelta(days=1)
    while current <= end:
        if current.weekday() < 5:  # 0=lunes … 4=viernes
            count += 1
        current += timedelta(days=1)
    return count


_STATUS_LABELS = {
    "sin_fecha": "📅 Sin fecha programada",
    "pendiente": "⏳ Pendiente",
    "en_ruta":   "🚚 En ruta",
    "entregado": "📦 Entregado (sin confirmar)",
    "confirmado": "✅ Confirmado por cliente",
    "demorado":  "⚠️ Demorado",
    "cancelado": "❌ Cancelado",
}

_VALID_STATUSES = set(_STATUS_LABELS.keys()) - {"sin_fecha"}


def _status_label(status: str) -> str:
    return _STATUS_LABELS.get(status, status)


def _timing_label(delivery_date: datetime, today: date) -> str:
    """Devuelve texto con urgencia relativa respecto a hoy."""
    delivery_day = delivery_date.astimezone(timezone.utc).date()
    days_diff = (delivery_day - today).days
    bdays = _business_days_between(today, delivery_day) if days_diff > 0 else 0

    if days_diff < 0:
        return f"⚠️ VENCIDO hace {abs(days_diff)} día(s)"
    if days_diff == 0:
        return "🔴 HOY"
    if bdays <= 3:
        return f"🟡 {bdays} día(s) hábil(es)"
    return f"🟢 {days_diff} día(s) naturales ({bdays} hábiles)"


# ─── Tools ────────────────────────────────────────────────────────────────────

@tool
async def listar_pedidos_pendientes(
    incluir_demorados: bool = True,
    fecha: str = "",
) -> str:
    """Lista todos los pedidos con fecha de entrega pendiente, en ruta o demorados.

    Muestra días restantes, urgencia y datos del cliente para gestión operativa.
    Úsalo cuando pregunten por pedidos pendientes, entregas programadas o encargos.
    También responde "¿qué hay para entregar hoy/mañana/pasado?" si se pasa la fecha.

    Args:
        incluir_demorados: Incluir también pedidos marcados como demorados (default True).
        fecha: Filtra por fecha de entrega específica. Acepta:
               - "hoy" / "today"
               - "mañana" / "manana" / "tomorrow"
               - "pasado" (pasado mañana)
               - Fecha exacta en formato DD/MM/YYYY o DD/MM/YY
               Si se omite, devuelve todos los pedidos pendientes.
    """
    statuses = ["pendiente", "en_ruta"]
    if incluir_demorados:
        statuses.append("demorado")

    today = datetime.now(timezone.utc).date()

    # Resolver fecha de filtro
    filter_date: date | None = None
    fecha_lower = fecha.strip().lower() if fecha else ""
    if fecha_lower in ("hoy", "today"):
        filter_date = today
    elif fecha_lower in ("mañana", "manana", "tomorrow"):
        filter_date = today + timedelta(days=1)
    elif fecha_lower in ("pasado", "pasado mañana", "pasado manana"):
        filter_date = today + timedelta(days=2)
    elif fecha_lower:
        parsed = _parse_fecha(fecha.strip())
        filter_date = parsed.date() if parsed else None

    async with AsyncSessionFactory() as db:
        stmt = (
            select(Invoice)
            .options(selectinload(Invoice.client))
            .where(Invoice.delivery_status.in_(statuses))
            .order_by(Invoice.delivery_date.asc())
        )
        result = await db.execute(stmt)
        invoices = result.scalars().all()

    # Filtrar por fecha si se especificó
    if filter_date is not None:
        invoices = [
            inv for inv in invoices
            if inv.delivery_date
            and inv.delivery_date.astimezone(timezone.utc).date() == filter_date
        ]
        fecha_label = filter_date.strftime("%d/%m/%Y")
        if not invoices:
            return f"✅ No hay entregas programadas para el {fecha_label}."
        lines = [f"📦 Entregas para el {fecha_label} ({len(invoices)}):"]
    else:
        if not invoices:
            return "✅ No hay pedidos pendientes de entrega."
        lines = [f"📦 Pedidos pendientes de entrega ({len(invoices)}):"]

    for inv in invoices:
        raw = inv.raw_ocr or {}
        cliente = inv.client.name if inv.client else "Sin cliente"
        telefono = inv.client.phone if inv.client else ""
        productos = raw.get("productos", "") or ", ".join(
            i.get("descripcion", "") for i in (inv.items or []) if i.get("descripcion")
        ) or "sin detalle"
        resta = raw.get("resta", 0) or 0
        pago_str = "✅ Pagado" if resta == 0 else f"⏳ Resta: ${resta:,.0f}"

        if inv.delivery_date:
            fecha_str = inv.delivery_date.astimezone(timezone.utc).strftime("%d/%m/%Y")
            timing = _timing_label(inv.delivery_date, today)
        else:
            fecha_str = "—"
            timing = ""

        lines.append(
            f"\n📋 #{inv.invoice_number} — {cliente}\n"
            f"   📱 {telefono}\n"
            f"   🛋️ {productos[:80]}\n"
            f"   📅 {fecha_str}  {timing}\n"
            f"   {_status_label(inv.delivery_status)}  |  {pago_str}"
        )

    return "\n".join(lines)


@tool
async def ver_seguimiento_pedido(numero_factura: str) -> str:
    """Muestra el estado detallado de seguimiento de un pedido específico.

    Incluye fecha de entrega, días restantes, tipo de entrega, quién recibe y notas.

    Args:
        numero_factura: Número de la factura o remisión (ej: 0042, 1275).
    """
    async with AsyncSessionFactory() as db:
        result = await db.execute(
            select(Invoice)
            .options(selectinload(Invoice.client))
            .where(Invoice.invoice_number == numero_factura)
        )
        inv = result.scalar_one_or_none()

    if not inv:
        return f"No encontré la factura/remisión #{numero_factura}."

    raw = inv.raw_ocr or {}
    cliente = inv.client.name if inv.client else "Sin cliente"
    telefono = inv.client.phone if inv.client else ""
    cedula = raw.get("cedula", "") or (inv.client.cedula if inv.client else "") or "No registrada"
    direccion = raw.get("direccion", "—")
    productos = raw.get("productos", "") or ", ".join(
        i.get("descripcion", "") for i in (inv.items or []) if i.get("descripcion")
    ) or "sin detalle"
    abono = raw.get("abono", 0) or 0
    resta = raw.get("resta", 0) or 0
    tipo_acarreo = raw.get("tipo_acarreo", "llevar")
    asesor = raw.get("asesor", "—")
    persona_recibe = raw.get("persona_recibe", "") or cliente

    today = datetime.now(timezone.utc).date()

    if inv.delivery_date:
        fecha_str = inv.delivery_date.astimezone(timezone.utc).strftime("%d/%m/%Y")
        timing = _timing_label(inv.delivery_date, today)
    else:
        fecha_str = "No especificada"
        timing = ""

    acarreo_str = "🚚 A domicilio" if tipo_acarreo == "llevar" else "🏬 Recoge en tienda"
    pago_str = "✅ Pagado" if resta == 0 else f"⏳ Pendiente: ${resta:,.0f} COP"

    lines = [
        f"📋 Seguimiento #{inv.invoice_number}",
        "─" * 30,
        f"👤 {cliente}",
        f"📱 {telefono}",
        f"🪪 CC: {cedula}",
        f"📍 {direccion}",
        "",
        f"🛋️ {productos[:120]}",
        "",
        f"📅 Entrega: {fecha_str}",
    ]
    if timing:
        lines.append(f"   {timing}")
    lines += [
        f"🚛 {acarreo_str}",
        f"👤 Recibe: {persona_recibe}",
        "",
        f"Estado: {_status_label(inv.delivery_status)}",
        f"💰 Pago: {pago_str}",
        f"🧑‍💼 Asesor: {asesor}",
    ]
    if inv.delivery_notes:
        lines += ["", f"📝 Notas:\n{inv.delivery_notes}"]

    return "\n".join(lines)


@tool
async def confirmar_entrega(numero_factura: str, notas: str = "") -> str:
    """Marca un pedido como confirmado: el cliente recibió el producto correctamente.

    Úsalo cuando el cliente confirme la recepción o cuando la tienda lo verifique.

    Args:
        numero_factura: Número de la factura o remisión.
        notas: Observaciones de la entrega (quién recibió, novedades, etc.).
    """
    async with AsyncSessionFactory() as db:
        result = await db.execute(
            select(Invoice)
            .options(selectinload(Invoice.client))
            .where(Invoice.invoice_number == numero_factura)
        )
        inv = result.scalar_one_or_none()

        if not inv:
            return f"No encontré la factura/remisión #{numero_factura}."

        cliente = inv.client.name if inv.client else "el cliente"
        prev_status = inv.delivery_status

        inv.delivery_status = "confirmado"
        if notas:
            timestamp = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M")
            existing = inv.delivery_notes or ""
            inv.delivery_notes = f"{existing}\n[{timestamp}] {notas}".strip()

        await db.commit()

    return (
        f"✅ Entrega confirmada — Remisión #{numero_factura}\n"
        f"👤 Cliente: {cliente}\n"
        f"Estado: {_status_label(prev_status)} → {_status_label('confirmado')}"
        + (f"\n📝 {notas}" if notas else "")
    )


@tool
async def actualizar_pedido(
    numero_factura: str,
    nuevo_status: str,
    notas: str = "",
    nueva_fecha_entrega: str = "",
) -> str:
    """Actualiza el estado de seguimiento de un pedido/remisión.

    Úsalo cuando el encargado informe que el pedido va en camino, se demoró,
    se canceló o cualquier cambio de estado en la entrega.

    Args:
        numero_factura: Número de la factura o remisión.
        nuevo_status: Estado nuevo. Opciones: "pendiente", "en_ruta", "entregado",
                      "confirmado", "demorado", "cancelado".
        notas: Descripción del cambio (ej: "proveedor avisó retraso de 3 días").
        nueva_fecha_entrega: Nueva fecha prometida si cambió (DD/MM/YYYY o DD/MM/YY).
    """
    if nuevo_status not in _VALID_STATUSES:
        return (
            f"Estado inválido: '{nuevo_status}'.\n"
            f"Opciones válidas: {', '.join(sorted(_VALID_STATUSES))}"
        )

    async with AsyncSessionFactory() as db:
        result = await db.execute(
            select(Invoice)
            .options(selectinload(Invoice.client))
            .where(Invoice.invoice_number == numero_factura)
        )
        inv = result.scalar_one_or_none()

        if not inv:
            return f"No encontré la factura/remisión #{numero_factura}."

        cliente = inv.client.name if inv.client else "el cliente"
        prev_status = inv.delivery_status

        inv.delivery_status = nuevo_status

        if nueva_fecha_entrega:
            parsed = _parse_fecha(nueva_fecha_entrega)
            if parsed:
                inv.delivery_date = parsed
                # Resetear alerta de 3 días para que se vuelva a enviar en la nueva fecha
                inv.delivery_alert_sent_at = None
            else:
                return f"No pude interpretar la fecha '{nueva_fecha_entrega}'. Usa formato DD/MM/YYYY."

        if notas:
            timestamp = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M")
            existing = inv.delivery_notes or ""
            inv.delivery_notes = f"{existing}\n[{timestamp}] {notas}".strip()

        await db.commit()

    fecha_str = ""
    if inv.delivery_date:
        fecha_str = f"\n📅 Fecha entrega: {inv.delivery_date.astimezone(timezone.utc).strftime('%d/%m/%Y')}"

    return (
        f"✅ Pedido #{numero_factura} actualizado\n"
        f"👤 {cliente}\n"
        f"Estado: {_status_label(prev_status)} → {_status_label(nuevo_status)}"
        + fecha_str
        + (f"\n📝 {notas}" if notas else "")
    )
