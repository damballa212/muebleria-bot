"""Tools de clientes: buscar_cliente, listar_clientes, crear_cliente, ver_historial_cliente, agregar_nota_cliente."""
import logging

from langchain_core.tools import tool
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from src.database import AsyncSessionFactory
from src.models import Case, Client
from src.utils.phone import normalize_phone

logger = logging.getLogger(__name__)


@tool
async def buscar_cliente(query: str) -> str:
    """Busca un cliente por nombre, teléfono o cédula."""
    normalized = normalize_phone(query) if any(c.isdigit() for c in query) else None

    async with AsyncSessionFactory() as db:
        conditions = [Client.name.ilike(f"%{query}%")]
        # Búsqueda por cédula (solo dígitos)
        if query.replace(" ", "").isdigit():
            conditions.append(Client.cedula == query.replace(" ", ""))
        # Búsqueda por teléfono
        if normalized:
            conditions.append(Client.phone == normalized)
            conditions.append(Client.phone.contains(query.replace(" ", "").replace("-", "")))

        result = await db.execute(
            select(Client).where(or_(*conditions)).limit(5)
        )
        clients = result.scalars().all()

    if not clients:
        return f"No encontré clientes con '{query}'."

    blocks = []
    for c in clients:
        fecha = c.created_at.strftime("%d/%m/%Y") if c.created_at else ""
        direccion = ""
        if c.notes and c.notes.startswith("Dirección:"):
            direccion = c.notes.split("\n")[0].replace("Dirección:", "").strip()
        block = (
            f"👤 {c.name}\n"
            f"   🆔 ID: {c.id}\n"
            f"   📱 {c.phone}"
            + (f"\n   🪪 Cédula: {c.cedula}" if c.cedula else "")
            + (f"\n   📍 {direccion}" if direccion else "")
            + (f"\n   📅 Registrado: {fecha}" if fecha else "")
            + (f"\n   📝 {c.notes}" if c.notes and not c.notes.startswith("Dirección:") else "")
        )
        blocks.append(block)
    header = f"{'Cliente encontrado' if len(clients)==1 else f'Clientes encontrados: {len(clients)}'}:"
    return header + "\n\n" + "\n\n".join(blocks)


@tool
async def crear_cliente(nombre: str, telefono: str, notas: str = "") -> str:
    """Crea un nuevo cliente. El teléfono se normaliza a +57XXXXXXXXXX."""
    phone_normalized = normalize_phone(telefono)

    async with AsyncSessionFactory() as db:
        # Verificar si ya existe
        existing = await db.execute(select(Client).where(Client.phone == phone_normalized))
        if existing.scalar_one_or_none():
            return f"⚠️ Ya existe un cliente con el teléfono {phone_normalized}. Usa buscar_cliente para encontrarlo."

        client = Client(name=nombre, phone=phone_normalized, notes=notas or None)
        db.add(client)
        await db.commit()
        await db.refresh(client)

    return f"✅ Cliente creado\n👤 {nombre}\n📱 {phone_normalized}\nID: {client.id}"


@tool
async def ver_historial_cliente(client_id: str) -> str:
    """Muestra todos los casos y facturas de un cliente."""
    async with AsyncSessionFactory() as db:
        result = await db.execute(
            select(Client)
            .options(
                selectinload(Client.cases).selectinload(Case.invoice),
                selectinload(Client.invoices),
            )
            .where(Client.id == client_id)
        )
        client = result.scalar_one_or_none()

    if not client:
        return f"❌ Cliente {client_id} no encontrado."

    lines = [f"👤 {client.name} | {client.phone}"]

    if client.cases:
        lines.append(f"\nCasos ({len(client.cases)}):")
        for c in client.cases:
            inv_ref = f" [Remisión #{c.invoice.invoice_number}]" if getattr(c, "invoice", None) and c.invoice else ""
            lines.append(f"  #{c.case_number} — {c.type} — {c.status} — {c.product or ''}{inv_ref}")
    else:
        lines.append("Sin casos registrados.")

    if client.invoices:
        lines.append(f"\nRemisiones/Facturas ({len(client.invoices)}):")
        for inv in client.invoices:
            date_str = inv.invoice_date.strftime("%d/%m/%Y") if inv.invoice_date else "sin fecha"
            raw = inv.raw_ocr or {}
            productos = raw.get("productos", "") or ", ".join(
                [i.get("descripcion", "") for i in (inv.items or []) if i.get("descripcion")]
            ) or "sin detalle"
            lines.append(
                f"  📋 #{inv.invoice_number} — {productos[:60]} — ${inv.total:,.0f} — {date_str}"
            )
    else:
        lines.append("Sin facturas registradas.")

    return "\n".join(lines)


@tool
async def agregar_nota_cliente(client_id: str, nota: str) -> str:
    """Agrega una nota al perfil del cliente en la base de datos y en Qdrant."""
    from src.memory import save_to_memory

    async with AsyncSessionFactory() as db:
        client = await db.get(Client, client_id)
        if not client:
            return f"❌ Cliente {client_id} no encontrado."

        existing = client.notes or ""
        client.notes = f"{existing}\n{nota}".strip() if existing else nota
        await db.commit()

    # Guardar también en Qdrant para búsqueda semántica
    try:
        await save_to_memory(f"Nota sobre cliente {client.name}: {nota}", {"client_id": client_id})
    except Exception:
        pass

    return f"✅ Nota agregada al cliente {client.name}"


@tool
async def listar_clientes(limit: int = 20) -> str:
    """Lista todos los clientes registrados en el sistema, ordenados por más reciente.

    Args:
        limit: Máximo de clientes a mostrar (default 20).
    """
    async with AsyncSessionFactory() as db:
        result = await db.execute(
            select(Client).order_by(Client.created_at.desc()).limit(limit)
        )
        clients = result.scalars().all()

    if not clients:
        return "No hay clientes registrados en el sistema."

    lines = [f"📋 Clientes registrados: {len(clients)}"]
    for i, c in enumerate(clients, 1):
        fecha = c.created_at.strftime("%d/%m/%Y") if c.created_at else ""
        direccion = ""
        if c.notes and c.notes.startswith("Dirección:"):
            direccion = c.notes.split("\n")[0].replace("Dirección:", "").strip()
        lines.append(
            f"{i}. {c.name}\n"
            f"   📱 {c.phone}\n"
            + (f"   📍 {direccion}" if direccion else "")
            + (f"\n   📅 {fecha}" if fecha else "")
        )
    return "\n\n".join(lines)

