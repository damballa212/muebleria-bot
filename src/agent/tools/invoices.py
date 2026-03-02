"""Tools de facturas/remisiones: listar, buscar, registrar."""
import json
import re
import unicodedata
from datetime import datetime, timezone

from langchain_core.tools import tool
from sqlalchemy import String, cast, func, not_, or_, select, text
from sqlalchemy.orm import selectinload

from src.database import AsyncSessionFactory
from src.memory import save_to_memory
from src.models import Client, Invoice
from src.utils.phone import normalize_phone


def _normalize_search_text(value: str) -> str:
    """Normaliza texto para búsquedas tolerantes a acentos y mayúsculas."""
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^a-zA-Z0-9+\s]", " ", value.lower())
    return " ".join(value.split())


def _invoice_search_blob(inv: Invoice) -> str:
    """Consolida texto buscable de una factura para fallback semántico simple."""
    parts = [
        inv.invoice_number or "",
        inv.client.name if inv.client else "",
        inv.client.phone if inv.client else "",
        json.dumps(inv.raw_ocr or {}, ensure_ascii=False),
        json.dumps(inv.items or [], ensure_ascii=False),
    ]
    return _normalize_search_text(" ".join(parts))


def _search_tokens(value: str) -> list[str]:
    tokens = []
    for token in _normalize_search_text(value).split():
        if len(token) < 3:
            continue
        singular = token[:-1] if token.endswith("s") and len(token) > 4 else token
        tokens.append(singular)
    return tokens


def _token_fallback_match(query: str, blob: str) -> bool:
    """Hace matching flexible por tokens para plural/singular y frases cercanas."""
    query_tokens = _search_tokens(query)
    blob_tokens = _search_tokens(blob)
    if not query_tokens or not blob_tokens:
        return False

    return all(
        any(
            candidate == token
            or candidate.startswith(token)
            or token.startswith(candidate)
            for candidate in blob_tokens
        )
        for token in query_tokens
    )


@tool
async def registrar_remision(
    numero_factura: str,
    nombre_cliente: str,
    telefono: str,
    direccion: str,
    productos: str,
    total: float,
    abono: float,
    resto: float,
    acarreo: float = 0,
    tipo_acarreo: str = "llevar",
    ayudantes: int = 0,
    asesor: str = "Marlon",
    persona_recibe: str = "",
    fecha_factura: str = "",
    fecha_entrega: str = "",
    cedula: str = "",
    credito_entidad: str = "",
    credito_cuotas: int = 0,
    credito_valor_cuota: float = 0,
    credito_frecuencia: str = "",
    credito_total: float = 0,
    credito_a_nombre_de: str = "",
    credito_cedula: str = "",
    credito_telefono: str = "",
    notas: str = "",
    tipo_transaccion: str = "venta",
    observaciones: str = "",
    factura_referencia: str = "",
) -> str:
    """Registra una remisión/venta/separé/garantía/cambio en el sistema. Crea el cliente si no existe.

    La misma planilla física se usa para todos los tipos de transacción. El campo
    tipo_transaccion diferencia si es una venta normal, un separé (reserva), un abono
    a un separé anterior, una garantía o un cambio.

    Args:
        numero_factura: Número de factura o remisión (OBLIGATORIO).
        nombre_cliente: Nombre completo del cliente.
        telefono: Teléfono principal del cliente.
        direccion: Dirección de entrega.
        productos: Descripción de los productos (o del problema, en garantías/cambios).
        total: Subtotal de los productos en COP (sin crédito ni acarreo).
        abono: Abono inicial pagado por el cliente.
        resto: Valor restante por pagar.
        acarreo: Costo del flete/acarreo en COP (default 0).
        tipo_acarreo: "llevar" (entregar a domicilio) o "recoger" (cliente recoge en tienda).
        ayudantes: Número de ayudantes contratados (0, 1 o 2). Cada uno vale $20.000.
        asesor: Nombre del asesor que realizó la venta.
        persona_recibe: Nombre de quien recibe. Si no se especifica, es el mismo cliente.
        fecha_factura: Fecha de la factura física (DD/MM/AA o DD/MM/YYYY). Si no se especifica, se usa la fecha actual.
        fecha_entrega: Fecha de entrega (DD/MM/AA o DD/MM/YYYY).
        cedula: Cédula de ciudadanía del cliente (OBLIGATORIO — siempre extraer del documento).
        credito_entidad: Entidad de crédito: "Agaval" (+3.9%), "Addi" (+6.5%), "Sistecredito" (+4.9%).
        credito_cuotas: Número de cuotas del crédito.
        credito_valor_cuota: Valor de cada cuota en COP.
        credito_frecuencia: "mensual" o "quincenal".
        credito_total: Total del crédito (suma de cuotas o monto con porcentaje ya sumado).
        credito_a_nombre_de: Nombre de un tercero si el crédito está a su nombre.
        credito_cedula: Cédula de ese tercero.
        credito_telefono: Teléfono de ese tercero.
        notas: Notas adicionales del operador (no del campo Observaciones de la planilla).
        tipo_transaccion: Tipo de transacción: "venta" (default), "separe" (reserva/layaway),
            "abono" (pago a separé), "garantia" (problema con producto), "cambio" (cambio de producto).
        observaciones: Texto literal del campo Observaciones de la planilla física.
        factura_referencia: Número de la REMISIÓN original a la que este abono/separé está
            vinculado. Solo aplica cuando tipo_transaccion es "abono" o "separe".
            El sistema buscará esa remisión en la BD y la vinculará si existe.
    """
    # Calcular costo de ayudantes
    costo_ayudantes = ayudantes * 20_000

    # Calcular total real del crédito si no lo proporcionaron
    TASAS = {"agaval": 0.039, "addi": 0.065, "sistecredito": 0.049}
    entidad_key = credito_entidad.lower().strip() if credito_entidad else ""
    credito_calculado = 0.0
    if entidad_key in TASAS and not credito_total:
        credito_calculado = round(total * (1 + TASAS[entidad_key]))
    elif credito_total:
        credito_calculado = credito_total
    async with AsyncSessionFactory() as db:
        # 1. Normalizar teléfono
        phone_normalized = normalize_phone(telefono)

        # 2. Buscar o crear cliente
        result = await db.execute(
            select(Client).where(Client.phone == phone_normalized)
        )
        client = result.scalars().first()

        if not client:
            client = Client(
                name=nombre_cliente,
                phone=phone_normalized,
                cedula=cedula.strip() if cedula else None,
                notes=f"Dirección: {direccion}",
            )
            db.add(client)
            await db.flush()  # Obtener client.id
        elif cedula and not client.cedula:
            # Actualizar cédula si el cliente ya existe y no tenía
            client.cedula = cedula.strip()

        # 3. Parsear fechas: factura física y entrega
        def _parse_date(s: str) -> datetime | None:
            if not s:
                return None
            for _fmt in ["%d/%m/%Y", "%d/%m/%y"]:
                try:
                    return datetime.strptime(s.strip(), _fmt).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
            return None

        invoice_date_parsed = _parse_date(fecha_factura) or datetime.now(timezone.utc)
        delivery_date_parsed: datetime | None = None
        if fecha_entrega:
            for _fmt in ["%d/%m/%Y", "%d/%m/%y"]:
                try:
                    delivery_date_parsed = datetime.strptime(fecha_entrega.strip(), _fmt).replace(
                        tzinfo=timezone.utc
                    )
                    break
                except ValueError:
                    continue

        # 4. Normalizar tipo de transacción
        tipo_validos = {"venta", "separe", "abono", "garantia", "cambio"}
        tipo_norm = tipo_transaccion.lower().strip() if tipo_transaccion else "venta"
        if tipo_norm not in tipo_validos:
            tipo_norm = "venta"

        # 4b. Vincular a factura original si se proporcionó factura_referencia
        parent_num = factura_referencia.strip() if factura_referencia else None
        parent_linked = False
        parent_missing = False
        if parent_num:
            parent_check = await db.execute(
                select(Invoice).where(Invoice.invoice_number == parent_num)
            )
            if parent_check.scalars().first():
                parent_linked = True
            else:
                parent_missing = True

        # 5. Crear la factura/remisión
        items_data = [{"descripcion": productos, "total": total}]
        invoice = Invoice(
            invoice_number=numero_factura,
            client_id=client.id,
            items=items_data,
            total=total + acarreo + costo_ayudantes,
            is_signed=True,
            ocr_status="manual",
            invoice_type=tipo_norm,
            parent_invoice_number=parent_num,
            invoice_date=invoice_date_parsed,
            delivery_date=delivery_date_parsed,
            delivery_status="pendiente" if delivery_date_parsed else "sin_fecha",
            raw_ocr={
                "tipo": "remision_manual",
                "tipo_transaccion": tipo_norm,
                "numero_factura": numero_factura,
                "cliente": nombre_cliente,
                "cedula": cedula,
                "telefono": telefono,
                "direccion": direccion,
                "productos": productos,
                "total": total,
                "tipo_acarreo": tipo_acarreo,
                "acarreo": acarreo,
                "ayudantes": ayudantes,
                "costo_ayudantes": costo_ayudantes,
                "abono": abono,
                "resta": resto,
                "fecha_factura": fecha_factura,
                "fecha_entrega": fecha_entrega,
                "persona_recibe": persona_recibe,
                "asesor": asesor,
                "observaciones": observaciones,
                "credito_entidad": credito_entidad,
                "credito_cuotas": credito_cuotas,
                "credito_valor_cuota": credito_valor_cuota,
                "credito_frecuencia": credito_frecuencia,
                "credito_total": credito_calculado,
                "credito_a_nombre_de": credito_a_nombre_de,
                "credito_cedula": credito_cedula,
                "credito_telefono": credito_telefono,
                "notas": notas,
            },
        )
        db.add(invoice)
        await db.commit()

        invoice_id = invoice.id

    # 5. Guardar en memoria semántica para búsquedas futuras
    try:
        await save_to_memory(
            f"Remisión #{numero_factura} - {nombre_cliente} - {productos} - ${total:,.0f}",
            {
                "type": "remision",
                "numero_factura": numero_factura,
                "client_name": nombre_cliente,
                "phone": phone_normalized,
                "total": total,
                "invoice_id": str(invoice_id),
            },
        )
    except Exception:
        pass

    # Si no se especifica quién recibe, es la misma persona a nombre de la factura
    if not persona_recibe:
        persona_recibe = nombre_cliente

    estado = "✅ Pagado" if resto == 0 else f"⏳ Pendiente: ${resto:,.0f} COP"
    cedula_display = cedula.strip() if cedula and cedula.strip() else "No registrada"
    entrega_str = f"\n📅 Entrega: {fecha_entrega}" if fecha_entrega else ""

    # Tipo de transacción
    _TIPO_LABELS = {
        "venta": "🛒 Venta",
        "separe": "🔖 Separé / Reserva",
        "abono": "💵 Abono a separé",
        "garantia": "🔧 Garantía",
        "cambio": "🔄 Cambio",
    }
    tipo_label = _TIPO_LABELS.get(tipo_norm, tipo_norm.capitalize())

    # Acarreo
    acarreo_emoji = "🚚 Llevar" if tipo_acarreo.lower() == "llevar" else "🏬 Recoger"
    acarreo_monto = f"${acarreo:,.0f}" if acarreo else "sin costo"
    # Crédito
    credito_block = ""
    if credito_entidad:
        frec = f" ({credito_frecuencia})" if credito_frecuencia else ""
        cuotas_str = f"{credito_cuotas}x${credito_valor_cuota:,.0f}{frec}" if credito_cuotas else ""
        tercero_str = ""
        if credito_a_nombre_de:
            tercero_str = f"\n   A nombre de: {credito_a_nombre_de}"
            if credito_cedula:
                tercero_str += f" (CC: {credito_cedula})"
            if credito_telefono:
                tercero_str += f"\n   Tel. crédito: {credito_telefono}"
        
        credito_block = (
            f"\n\n💳 Crédito: {credito_entidad}"
            + (f"\n   Cuotas: {cuotas_str}" if cuotas_str else "")
            + (f"\n   Total crédito: ${credito_calculado:,.0f}" if credito_calculado else "")
            + tercero_str
        )

    # Línea de vinculación con remisión original
    if parent_linked:
        parent_line = f"🔗 Vinculado a remisión #{parent_num} ✅"
    elif parent_missing:
        parent_line = f"⚠️ Factura ref. #{parent_num} no está en el sistema aún"
    else:
        parent_line = ""

    lines = [
        f"✅ Remisión #{numero_factura} registrada",
        "─" * 28,
        f"{tipo_label}",
        *([ parent_line ] if parent_line else []),
        f"👤 {nombre_cliente}",
        f"📱 {phone_normalized}",
        f"🪪 Cédula: {cedula_display}",
        "",
        "🛋️ Productos:",
        f"   {productos}",
        "",
        *(
            [f"📝 Observaciones:\n   {observaciones}", ""]
            if observaciones and observaciones.strip()
            else []
        ),
        "💰 Financiero:",
        f"   Subtotal:  ${total:,.0f}",
        f"   {acarreo_emoji}: {acarreo_monto}",
    ]
    if ayudantes:
        lines.append(f"   Ayudantes: {ayudantes} (${costo_ayudantes:,.0f})")
    lines += [
        f"   Abono:     ${abono:,.0f}",
        f"   Resta:     ${resto:,.0f}",
        "",
        estado,
    ]
    if credito_block:
        lines.append(credito_block)
    if entrega_str:
        lines.append(entrega_str.strip())
    lines += [
        f"👤 Recibe: {persona_recibe}",
        f"🧑‍💼 Asesor: {asesor}",
    ]
    return "\n".join(lines)


@tool
async def buscar_abonos_huerfanos() -> str:
    """Lista abonos/separés cuya factura de venta original no está en el sistema.

    Útil para cruzar pagos registrados por otros vendedores cuya remisión aún
    no se ha subido. Una vez subida la remisión original, los huérfanos quedan
    vinculables manualmente.
    """
    async with AsyncSessionFactory() as db:
        result = await db.execute(
            select(Invoice)
            .options(selectinload(Invoice.client))
            .where(
                Invoice.parent_invoice_number.isnot(None),
                Invoice.invoice_type.in_(["abono", "separe"]),
            )
            .order_by(Invoice.created_at.desc())
        )
        candidates = result.scalars().all()

        if not candidates:
            return "✅ No hay abonos/separés con factura de venta pendiente."

        # Filtrar solo los que su remisión original realmente no existe en el sistema
        parent_numbers = {inv.parent_invoice_number for inv in candidates}
        found_result = await db.execute(
            select(Invoice.invoice_number).where(
                Invoice.invoice_number.in_(parent_numbers)
            )
        )
        found_numbers = {row[0] for row in found_result.all()}

    orphans = [inv for inv in candidates if inv.parent_invoice_number not in found_numbers]
    linked = [inv for inv in candidates if inv.parent_invoice_number in found_numbers]

    if not orphans and not linked:
        return "✅ No hay abonos/separés con factura de venta pendiente."

    blocks = []
    if orphans:
        blocks.append(f"⚠️ Abonos/separés SIN remisión original en el sistema ({len(orphans)}):")
        for inv in orphans:
            cliente = inv.client.name if inv.client else "Sin cliente"
            raw = inv.raw_ocr or {}
            abono_val = raw.get("abono", 0) or 0
            obs = raw.get("observaciones", "") or ""
            blocks.append(
                f"\n📋 #{inv.invoice_number}  ({inv.invoice_type})\n"
                f"   👤 {cliente}  📱 {inv.client.phone if inv.client else ''}\n"
                f"   🔗 Ref. remisión: #{inv.parent_invoice_number}  (no encontrada)\n"
                f"   💵 Abono: ${abono_val:,.0f}"
                + (f"\n   📝 {obs[:70]}" if obs else "")
            )

    if linked:
        blocks.append(f"\n✅ Abonos/separés YA vinculados ({len(linked)}):")
        for inv in linked:
            cliente = inv.client.name if inv.client else "Sin cliente"
            blocks.append(
                f"   #{inv.invoice_number} → remisión #{inv.parent_invoice_number}  |  {cliente}"
            )

    return "\n".join(blocks)


@tool
async def listar_facturas_vencidas() -> str:
    """Lista facturas/remisiones vencidas en dos sentidos:
    1. Entregas vencidas: fecha de entrega pasada y el cliente NO confirmó recepción.
    2. Saldos pendientes: el cliente aún debe dinero (resta > 0).

    Úsalo cuando pregunten por facturas vencidas, saldos pendientes, clientes que deben,
    entregas no confirmadas, deudas activas, cuentas por cobrar o seguimiento de cobros.
    """
    from datetime import date as date_type

    today = datetime.now(timezone.utc).date()

    async with AsyncSessionFactory() as db:
        result = await db.execute(
            select(Invoice)
            .options(selectinload(Invoice.client))
            .where(Invoice.invoice_type.notin_(["garantia", "cambio"]))
            .order_by(Invoice.invoice_date.asc())
        )
        all_invoices = result.scalars().all()

    tipo_labels = {
        "venta": "🛒", "separe": "🔖", "abono": "💵",
        "garantia": "🔧", "cambio": "🔄",
    }

    def _icon(inv): return tipo_labels.get(inv.invoice_type or "venta", "📋")
    def _edad(inv):
        if inv.invoice_date:
            return f"{(today - inv.invoice_date.astimezone(timezone.utc).date()).days}d"
        return "—"

    # ── 1. Entregas vencidas (fecha pasada, no confirmadas) ──────────────────
    entregas_vencidas = []
    for inv in all_invoices:
        if not inv.delivery_date:
            continue
        delivery_day = inv.delivery_date.astimezone(timezone.utc).date()
        if delivery_day >= today:
            continue
        if inv.delivery_status in ("confirmado", "cancelado"):
            continue
        dias_vencido = (today - delivery_day).days
        entregas_vencidas.append((inv, dias_vencido))

    # ── 2. Saldos pendientes (resta > 0) ─────────────────────────────────────
    saldos_pendientes = []
    for inv in all_invoices:
        raw = inv.raw_ocr or {}
        try:
            resta = float(raw.get("resta") or 0)
        except (ValueError, TypeError):
            resta = 0
        if resta > 0:
            saldos_pendientes.append((inv, resta))

    if not entregas_vencidas and not saldos_pendientes:
        return "✅ No hay facturas vencidas ni saldos pendientes."

    sections = []

    if entregas_vencidas:
        block = [f"🚚 Entregas vencidas sin confirmar ({len(entregas_vencidas)}):"]
        for inv, dias in sorted(entregas_vencidas, key=lambda x: -x[1]):
            raw = inv.raw_ocr or {}
            cliente = inv.client.name if inv.client else "Sin cliente"
            tel = inv.client.phone if inv.client else ""
            num = inv.invoice_number or str(inv.id)[:8]
            prods = raw.get("productos", "") or ", ".join(
                i.get("descripcion", "") for i in (inv.items or []) if i.get("descripcion")
            ) or "sin detalle"
            fecha_str = inv.delivery_date.astimezone(timezone.utc).strftime("%d/%m/%Y")
            block.append(
                f"\n{_icon(inv)} #{num} — {cliente}\n"
                f"   📱 {tel}\n"
                f"   🛋️ {prods[:70]}\n"
                f"   📅 Debía entregarse el {fecha_str}  |  ⚠️ Hace {dias} día(s)"
            )
        sections.append("\n".join(block))

    if saldos_pendientes:
        total_deuda = sum(r for _, r in saldos_pendientes)
        block = [
            f"💸 Saldos pendientes de pago ({len(saldos_pendientes)})",
            f"   Total adeudado: ${total_deuda:,.0f} COP",
        ]
        for inv, resta in saldos_pendientes:
            raw = inv.raw_ocr or {}
            cliente = inv.client.name if inv.client else "Sin cliente"
            tel = inv.client.phone if inv.client else ""
            num = inv.invoice_number or str(inv.id)[:8]
            prods = raw.get("productos", "") or ", ".join(
                i.get("descripcion", "") for i in (inv.items or []) if i.get("descripcion")
            ) or "sin detalle"
            abono = float(raw.get("abono") or 0)
            block.append(
                f"\n{_icon(inv)} #{num} — {cliente}\n"
                f"   📱 {tel}\n"
                f"   🛋️ {prods[:70]}\n"
                f"   💵 Abono: ${abono:,.0f}  |  ⏳ Resta: ${resta:,.0f}  |  🕐 {_edad(inv)}"
            )
        sections.append("\n".join(block))

    return ("\n\n" + "─" * 32 + "\n\n").join(sections)


@tool
async def listar_facturas(client_id: str) -> str:
    """Lista todas las facturas/remisiones de un cliente por su ID."""
    async with AsyncSessionFactory() as db:
        result = await db.execute(
            select(Invoice)
            .options(selectinload(Invoice.client))
            .where(Invoice.client_id == client_id)
            .order_by(Invoice.created_at.desc())
        )
        invoices = result.scalars().all()

    if not invoices:
        return "No hay facturas/remisiones registradas para este cliente."

    _TIPO_LABELS_LISTAR = {
        "venta": "🛒 Venta",
        "separe": "🔖 Separé",
        "abono": "💵 Abono",
        "garantia": "🔧 Garantía",
        "cambio": "🔄 Cambio",
    }

    blocks = [f"📄 Registros del cliente ({len(invoices)}):"]
    for inv in invoices:
        num = inv.invoice_number or str(inv.id)[:8]
        raw = inv.raw_ocr or {}
        tipo_str = _TIPO_LABELS_LISTAR.get(inv.invoice_type or "venta", "🛒 Venta")
        total_str = f"${inv.total:,.0f}" if inv.total else "monto no disponible"
        abono = raw.get('abono', 0)
        resta = raw.get('resta', 0)
        productos = raw.get('productos', '') or ', '.join(
            [i.get('descripcion','') for i in (inv.items or []) if i.get('descripcion')]
        ) or 'sin detalle'
        fecha_factura_str = raw.get('fecha_factura', '')
        if not fecha_factura_str and inv.invoice_date:
            fecha_factura_str = inv.invoice_date.strftime("%d/%m/%Y")
        fecha_entrega = raw.get('fecha_entrega', '')
        obs = raw.get('observaciones', '')
        estado = "✅ Pagado" if resta == 0 else f"⏳ Pendiente: ${resta:,.0f}"
        parent_str = ""
        if inv.parent_invoice_number:
            parent_str = f"\n   🔗 Ref. remisión: #{inv.parent_invoice_number}"
        block = (
            f"\n📋 #{num}  {tipo_str}\n"
            + (f"   🗓️ Fecha factura: {fecha_factura_str}\n" if fecha_factura_str else "")
            + f"   🛋️ {productos[:80]}\n"
            f"   💰 Total: {total_str}  |  Abono: ${abono:,.0f}  |  Resta: ${resta:,.0f}\n"
            f"   {estado}"
            + parent_str
            + (f"\n   📅 Entrega: {fecha_entrega}" if fecha_entrega else "")
            + (f"\n   📝 {obs[:80]}" if obs else "")
        )
        blocks.append(block)
    return "\n".join(blocks)


@tool
async def buscar_factura(query: str) -> str:
    """Busca facturas/remisiones por nombre de cliente, número de remisión o producto."""
    cleaned_query = query.strip()
    normalized_query = _normalize_search_text(cleaned_query)

    async with AsyncSessionFactory() as db:
        query_pattern = f"%{cleaned_query}%"
        stmt = (
            select(Invoice)
            .join(Client, Invoice.client_id == Client.id)
            .options(selectinload(Invoice.client))
            .where(
                or_(
                    Invoice.invoice_number.ilike(query_pattern),
                    Client.name.ilike(query_pattern),
                    Client.phone.ilike(query_pattern),
                    cast(Invoice.raw_ocr, String).ilike(query_pattern),
                    cast(Invoice.items, String).ilike(query_pattern),
                )
            )
            .order_by(Invoice.created_at.desc())
            .limit(10)
        )
        result = await db.execute(stmt)
        matches = result.scalars().all()

        if not matches and normalized_query:
            # Fallback tolerante a acentos para consultas como
            # "restauracion" -> "restauración".
            fallback_stmt = (
                select(Invoice)
                .join(Client, Invoice.client_id == Client.id)
                .options(selectinload(Invoice.client))
                .order_by(Invoice.created_at.desc())
            )
            fallback_result = await db.execute(fallback_stmt)
            candidates = fallback_result.scalars().all()
            matches = [
                inv for inv in candidates
                if (
                    normalized_query in _invoice_search_blob(inv)
                    or _token_fallback_match(normalized_query, _invoice_search_blob(inv))
                )
            ][:10]

    if not matches:
        return f"No encontré facturas relacionadas con '{cleaned_query}'."

    blocks = [f"🔍 Resultados para '{cleaned_query}' ({len(matches)}):"]
    for inv in matches:
        num = inv.invoice_number or str(inv.id)[:8]
        cliente = inv.client.name if inv.client else 'Sin cliente'
        raw = inv.raw_ocr or {}
        total_str = f"${inv.total:,.0f}" if inv.total else "monto no disponible"
        resta = raw.get('resta', 0)
        estado = "✅ Pagado" if resta == 0 else f"⏳ Pendiente: ${resta:,.0f}"
        blocks.append(
            f"\n📋 #{num} — {cliente}\n"
            f"   💰 {total_str}  {estado}"
        )
    return "\n".join(blocks)
