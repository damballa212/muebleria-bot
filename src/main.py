"""FastAPI entrypoint — monta todos los endpoints y gestiona el ciclo de vida."""
import base64
import logging
import re
import uuid
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.agent.graph import compiled_graph
from src.audio import preprocess_incoming_message
from src.bot import is_authorized, send_message
from src.config import settings
from src.database import engine
from src.llm import LLMError
from src.memory import clear_chat_context, ensure_collection_exists
from src.models import Base
from src.ocr import process_invoice_photo, save_invoice_photo

logger = logging.getLogger(__name__)


def _is_runtime_startup_message(message: str) -> bool:
    normalized = (message or "").strip()
    return (
        "A new session was started via /new or /reset" in normalized
        or ("System: [" in normalized and "Exec completed" in normalized)
    )


def _generate_placeholder_phone() -> str:
    """Genera un teléfono sintético válido para clientes creados por OCR incompleto."""
    digits = str(uuid.uuid4().int)[-10:]
    return f"+57{digits}"


def _generate_ocr_invoice_number() -> str:
    """Genera un consecutivo técnico para facturas digitalizadas sin número legible."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"OCR-{timestamp}-{uuid.uuid4().hex[:6].upper()}"


# ─── Detección de adjuntos de imagen en mensajes de OpenClaw ─────────────────

# Formato: [media attached: /path/file.jpg (image/jpeg) | /altpath] caption <media:image>
_IMAGE_MARKER_RE = re.compile(
    r"\[media attached:\s*"
    r"(?P<path>[^|\]\s]+?\.(?P<ext>jpg|jpeg|png|webp|heic|heif|gif|bmp|tiff?))"
    r"(?:\s*\((?P<mime>image/[^,)\s]+)[^)]*\))?"
    r"(?:\s*\|\s*(?P<altpath>[^\]]+))?"
    r"\]",
    re.IGNORECASE | re.DOTALL,
)


def _extract_image_path_from_message(message: str) -> str | None:
    """Retorna el path del adjunto de imagen si el mensaje contiene uno, o None."""
    match = _IMAGE_MARKER_RE.search(message)
    if not match:
        return None
    path = (match.group("altpath") or match.group("path") or "").strip()
    return path or None


def _read_image_to_base64(host_path: str) -> str | None:
    """Lee la imagen desde disco (path del host o directorio de medios) y retorna base64."""
    raw = Path(host_path)
    if raw.exists():
        return base64.b64encode(raw.read_bytes()).decode("utf-8")
    # Intentar en el directorio de medios de OpenClaw
    fallback = Path(settings.openclaw_media_dir) / raw.name
    if fallback.exists():
        return base64.b64encode(fallback.read_bytes()).decode("utf-8")
    return None


async def _ocr_and_save(image_base64: str, saved_photo_path: str) -> tuple[str, str]:
    """Ejecuta OCR sobre una foto de factura, persiste en DB y retorna (respuesta, status).

    Raises LLMError si el OCR falla (el llamador debe crear el registro pending_ocr).
    """
    from src.database import AsyncSessionFactory
    from src.models import Invoice, Client
    from src.utils.phone import normalize_phone
    from sqlalchemy import select

    result = await process_invoice_photo(image_base64, photo_path=saved_photo_path)

    client_id = None
    async with AsyncSessionFactory() as db:
        client = None
        normalized_phone = normalize_phone(result.telefono) if result.telefono else None

        if normalized_phone:
            existing = await db.execute(select(Client).where(Client.phone == normalized_phone))
            client = existing.scalar_one_or_none()
            if client:
                if result.nombre and client.name.startswith("Cliente pendiente OCR"):
                    client.name = result.nombre
                if result.cedula and not client.cedula:
                    client.cedula = result.cedula

        if not client and result.nombre:
            existing_by_name = await db.execute(
                select(Client).where(Client.name.ilike(f"%{result.nombre}%")).limit(1)
            )
            client = existing_by_name.scalar_one_or_none()
            if client:
                if normalized_phone and client.name.startswith("Cliente pendiente OCR"):
                    client.phone = normalized_phone
                if result.cedula and not client.cedula:
                    client.cedula = result.cedula

        if not client:
            client = Client(
                name=result.nombre or "Cliente pendiente OCR",
                phone=normalized_phone or _generate_placeholder_phone(),
                cedula=result.cedula or None,
                notes="Creado automáticamente desde OCR. Revisar datos identificatorios.",
            )
            db.add(client)
            await db.flush()

        client_id = client.id

        ocr_invoice_date = datetime.now(timezone.utc)
        if result.fecha_compra:
            try:
                ocr_invoice_date = datetime.strptime(result.fecha_compra, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                pass

        _tipos_validos = {"venta", "separe", "abono", "garantia", "cambio"}
        ocr_tipo = (result.tipo_transaccion or "venta").lower().strip()
        if ocr_tipo not in _tipos_validos:
            ocr_tipo = "venta"

        ocr_delivery_date = None
        if result.fecha_entrega:
            for _fmt in ["%d/%m/%Y", "%d/%m/%y"]:
                try:
                    ocr_delivery_date = datetime.strptime(
                        result.fecha_entrega.strip(), _fmt
                    ).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue

        ocr_invoice_number = result.numero_formulario or _generate_ocr_invoice_number()
        parent_inv_number: str | None = None
        parent_invoice_linked = False
        parent_invoice_missing = False

        if result.form_type == "plan_abonos" and result.numero_factura_ref:
            parent_inv_number = result.numero_factura_ref.strip()
            parent_check = await db.execute(
                select(Invoice).where(Invoice.invoice_number == parent_inv_number)
            )
            if parent_check.scalar_one_or_none():
                parent_invoice_linked = True
            else:
                parent_invoice_missing = True

        if ocr_invoice_number:
            dup_check = await db.execute(
                select(Invoice).where(Invoice.invoice_number == ocr_invoice_number)
            )
            if dup_check.scalar_one_or_none():
                ocr_invoice_number = _generate_ocr_invoice_number()

        invoice = Invoice(
            invoice_number=ocr_invoice_number,
            client_id=client_id,
            items=result.items,
            total=result.total,
            invoice_date=ocr_invoice_date,
            invoice_type=ocr_tipo,
            parent_invoice_number=parent_inv_number,
            delivery_date=ocr_delivery_date,
            delivery_status="pendiente" if ocr_delivery_date else "sin_fecha",
            is_signed=result.firmada,
            photo_path=result.photo_path,
            raw_ocr=result.raw,
            ocr_status="done",
        )
        db.add(invoice)
        await db.commit()

    _FORM_LABELS = {"plan_abonos": "📝 Plan Abonos/Separé", "remision": "📋 Remisión"}
    _TIPO_LABELS = {
        "venta": "🛒 Venta", "separe": "🔖 Separé", "abono": "💵 Abono",
        "garantia": "🔧 Garantía", "cambio": "🔄 Cambio",
    }
    form_label = _FORM_LABELS.get(result.form_type, "📋 Documento")
    tipo_label = _TIPO_LABELS.get(ocr_tipo, ocr_tipo.capitalize())

    items_text = ", ".join(
        [f"{item.get('descripcion', 'item')}" for item in result.items[:4]]
    ) or (result.observaciones[:60] if result.observaciones else "sin detalle")

    total_text = f"${result.total:,.0f}" if result.total else "no legible"
    abono_text = f"${result.abono:,.0f}" if result.abono else "—"
    resta_text = f"${result.resta:,.0f}" if result.resta else "—"
    num_text = f"#{ocr_invoice_number}" if ocr_invoice_number else "sin número"

    if parent_invoice_linked:
        parent_line = f"🔗 Vinculado a remisión #{parent_inv_number} ✅\n"
    elif parent_invoice_missing:
        parent_line = f"⚠️ Factura ref. #{parent_inv_number} no está en el sistema (otro vendedor)\n"
    else:
        parent_line = ""

    response = (
        f"{form_label} digitalizada — {tipo_label}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📄 {num_text}\n"
        + parent_line
        + f"👤 {result.nombre or 'Cliente pendiente de identificar'}\n"
        f"📱 {result.telefono or 'No legible'}\n"
        + (f"🪪 CC: {result.cedula}\n" if result.cedula else "")
        + (f"📍 {result.direccion}\n" if result.direccion else "")
        + f"\n🛋️ {items_text}\n"
        f"💰 Total: {total_text}  |  Abono: {abono_text}  |  Resta: {resta_text}\n"
        + (f"📅 Entrega: {result.fecha_entrega}\n" if result.fecha_entrega else "")
        + (f"📝 Obs: {result.observaciones[:80]}\n" if result.observaciones else "")
        + f"{'✍️ Firmada' if result.firmada else '🔲 Sin firma'}\n\n"
        "¿Qué hago con este documento?\n"
        "1️⃣ Crear caso de garantía\n"
        "2️⃣ Crear cotización\n"
        "3️⃣ Solo guardar (ya quedó registrado)"
    )

    return response, "success"


# ─── Schemas de request/response ─────────────────────────────────────────────

class ProcessRequest(BaseModel):
    message: str
    chat_id: str
    source: str = "openclaw"
    timestamp: str = ""
    send_direct: bool = False  # True → backend envía a Telegram; devuelve solo ACK


class OcrRequest(BaseModel):
    image_base64: str
    chat_id: str
    source: str = "openclaw"
    timestamp: str = ""
    send_direct: bool = False  # True → backend envía a Telegram; devuelve solo ACK


class ResetRequest(BaseModel):
    chat_id: str
    source: str = "openclaw"


# ─── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicialización al arrancar: crea tablas, colección Qdrant y polling de Telegram."""
    import asyncio
    from src.bot import telegram_polling_loop

    logger.info("Arrancando Asistente Noreña...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_collection_exists()

    # Arrancar polling de Telegram si modo directo
    polling_task = None
    if settings.telegram_mode == "direct":
        logger.info("🤖 Modo directo — iniciando Telegram long-polling...")
        polling_task = asyncio.create_task(telegram_polling_loop())

    logger.info("✅ Backend listo en puerto 8000")
    yield

    # Apagar polling al detener el server
    if polling_task:
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass
    logger.info("Apagando backend...")


# ─── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Asistente Noreña",
    version="1.0.0",
    description="Backend API para el asistente personal de Mueblería Noreña",
    lifespan=lifespan,
)


# ─── Autenticación ────────────────────────────────────────────────────────────

async def verify_api_key(authorization: str = Header(...)) -> str:
    """Verifica INTERNAL_API_KEY en el header Authorization: Bearer <key>."""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or token != settings.internal_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key inválida")
    return token


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


@app.post("/v1/process")
async def process_message(body: ProcessRequest, _: str = Depends(verify_api_key)):
    """Recibe un mensaje de texto desde OpenClaw y ejecuta el agente LangGraph."""
    if not await is_authorized(body.chat_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Chat no autorizado")

    if _is_runtime_startup_message(body.message):
        await clear_chat_context(body.chat_id)
        return {
            "response": "Listo. Soy Noreñita, la asistente de Mueblería Noreña. ¿Qué necesitas que haga hoy?",
            "status": "success",
        }

    # ── Fotos de facturas: redirigir a OCR antes de que el agente las toque ──
    image_path = _extract_image_path_from_message(body.message)
    if image_path:
        logger.info("Imagen detectada en /v1/process — enrutando a OCR: %s", image_path)
        image_b64 = _read_image_to_base64(image_path)
        if not image_b64:
            msg = "📷 No pude leer la foto. ¿Puedes reenviarla?"
            if settings.telegram_mode == "direct":
                await send_message(body.chat_id, msg)
            return {"response": msg, "status": "error"}
        saved_path = save_invoice_photo(image_b64)
        try:
            ocr_response, ocr_status = await _ocr_and_save(image_b64, saved_path)
        except LLMError:
            from src.database import AsyncSessionFactory
            from src.models import Invoice, Client
            async with AsyncSessionFactory() as db:
                pending_client = Client(
                    name="Cliente pendiente OCR",
                    phone=_generate_placeholder_phone(),
                    notes="Creado automáticamente porque el OCR quedó pendiente.",
                )
                db.add(pending_client)
                await db.flush()
                db.add(Invoice(
                    invoice_number=_generate_ocr_invoice_number(),
                    client_id=pending_client.id,
                    items=[],
                    invoice_date=datetime.now(timezone.utc),
                    photo_path=saved_path,
                    raw_ocr={"status": "pending_ocr"},
                    ocr_status="pending_ocr",
                ))
                await db.commit()
            ocr_response = "📷 Foto recibida. El OCR está tardando, te aviso cuando esté lista."
            ocr_status = "pending"
        if body.send_direct or settings.telegram_mode == "direct":
            await send_message(body.chat_id, ocr_response)
            if body.send_direct:
                return {"response": "✅ Factura registrada.", "status": ocr_status}
        return {"response": ocr_response, "status": ocr_status}

    from langchain_core.messages import AIMessage, HumanMessage

    preprocessed = await preprocess_incoming_message(body.chat_id, body.message)
    if preprocessed and preprocessed.direct_response:
        if settings.telegram_mode == "direct":
            await send_message(body.chat_id, preprocessed.direct_response)
        return {"response": preprocessed.direct_response, "status": "success"}

    effective_message = (
        preprocessed.rewritten_message
        if preprocessed and preprocessed.rewritten_message
        else body.message
    )

    initial_state = {
        "messages": [HumanMessage(content=effective_message)],
        "chat_history": [],
        "client_context": None,
        "case_context": [],
        "chat_id": body.chat_id,
        "source": body.source,
    }

    try:
        final_state = await compiled_graph.ainvoke(initial_state)
        # Extraer la última respuesta del agente:
        # - Solo AIMessage (no HumanMessage de contexto, no ToolMessage)
        # - Sin tool_calls pendientes (esas son llamadas intermedias, no la respuesta final)
        response_text = ""
        for msg in reversed(final_state["messages"]):
            if isinstance(msg, AIMessage) and msg.content and not msg.tool_calls:
                response_text = msg.content
                break

        if body.send_direct or settings.telegram_mode == "direct":
            await send_message(body.chat_id, response_text)
            if body.send_direct:
                return {"response": "✅", "status": "success"}

        return {"response": response_text, "status": "success"}

    except LLMError:
        error_msg = "⚠️ El servicio de IA no está disponible temporalmente. Intenta en unos minutos."
        if body.send_direct or settings.telegram_mode == "direct":
            await send_message(body.chat_id, error_msg)
        return JSONResponse(status_code=503, content={"response": error_msg, "status": "error"})

    except Exception as exc:
        logger.exception("Error inesperado en /v1/process: %s", exc)
        raise HTTPException(status_code=500, detail="Error interno")


@app.post("/v1/reset")
async def reset_chat(body: ResetRequest, _: str = Depends(verify_api_key)):
    """Limpia historial y estado conversacional persistido del chat."""
    if not await is_authorized(body.chat_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Chat no autorizado")

    await clear_chat_context(body.chat_id)
    return {"status": "success", "response": "Contexto del chat reiniciado."}


@app.get("/v1/orders")
async def list_orders(
    status: str | None = None,
    overdue: bool | None = None,
    _: str = Depends(verify_api_key),
):
    """
    Lista pedidos con fecha de entrega programada.

    Query params:
    - status: filtrar por delivery_status (pendiente, en_ruta, demorado, entregado, confirmado)
    - overdue: true → solo pedidos vencidos sin confirmar
    """
    from src.database import AsyncSessionFactory
    from src.models import Invoice
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    now_utc = datetime.now(timezone.utc)

    async with AsyncSessionFactory() as db:
        stmt = (
            select(Invoice)
            .options(selectinload(Invoice.client))
            .where(Invoice.delivery_status != "sin_fecha")
            .order_by(Invoice.delivery_date.asc())
        )
        if status:
            stmt = stmt.where(Invoice.delivery_status == status)
        if overdue:
            stmt = stmt.where(
                Invoice.delivery_date < now_utc,
                Invoice.delivery_status.notin_(["confirmado", "entregado", "cancelado"]),
            )
        result = await db.execute(stmt)
        invoices = result.scalars().all()

    orders = []
    for inv in invoices:
        raw = inv.raw_ocr or {}
        orders.append({
            "invoice_number": inv.invoice_number,
            "client_name": inv.client.name if inv.client else None,
            "client_phone": inv.client.phone if inv.client else None,
            "productos": raw.get("productos", ""),
            "direccion": raw.get("direccion", ""),
            "tipo_acarreo": raw.get("tipo_acarreo", "llevar"),
            "delivery_date": inv.delivery_date.isoformat() if inv.delivery_date else None,
            "delivery_status": inv.delivery_status,
            "delivery_notes": inv.delivery_notes,
            "total": float(inv.total) if inv.total else None,
            "abono": raw.get("abono", 0),
            "resta": raw.get("resta", 0),
            "asesor": raw.get("asesor", ""),
            "persona_recibe": raw.get("persona_recibe", ""),
            "is_overdue": bool(inv.delivery_date and inv.delivery_date < now_utc),
            "created_at": inv.created_at.isoformat(),
        })

    return {"orders": orders, "total": len(orders)}


@app.post("/v1/orders/{invoice_number}/confirm")
async def confirm_order(invoice_number: str, _: str = Depends(verify_api_key)):
    """Confirma la entrega de un pedido directamente via REST (para CRM/integración)."""
    from src.database import AsyncSessionFactory
    from src.models import Invoice
    from sqlalchemy import select

    async with AsyncSessionFactory() as db:
        result = await db.execute(
            select(Invoice).where(Invoice.invoice_number == invoice_number)
        )
        inv = result.scalar_one_or_none()

        if not inv:
            raise HTTPException(status_code=404, detail=f"Factura '{invoice_number}' no encontrada")

        prev_status = inv.delivery_status
        inv.delivery_status = "confirmado"
        timestamp = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M")
        existing = inv.delivery_notes or ""
        inv.delivery_notes = f"{existing}\n[{timestamp}] Confirmado via API".strip()
        await db.commit()

    return {
        "invoice_number": invoice_number,
        "prev_status": prev_status,
        "delivery_status": "confirmado",
        "status": "success",
    }


@app.get("/v1/orders/stats")
async def order_stats(_: str = Depends(verify_api_key)):
    """Estadísticas de pedidos por estado (para dashboard futuro del mini-CRM)."""
    from src.database import AsyncSessionFactory
    from src.models import Invoice
    from sqlalchemy import func, select

    now_utc = datetime.now(timezone.utc)

    async with AsyncSessionFactory() as db:
        # Contar por estado
        count_result = await db.execute(
            select(Invoice.delivery_status, func.count(Invoice.id))
            .where(Invoice.delivery_status != "sin_fecha")
            .group_by(Invoice.delivery_status)
        )
        by_status = {row[0]: row[1] for row in count_result.all()}

        # Vencidos sin confirmar
        overdue_result = await db.execute(
            select(func.count(Invoice.id)).where(
                Invoice.delivery_date < now_utc,
                Invoice.delivery_status.notin_(["confirmado", "entregado", "cancelado"]),
            )
        )
        overdue_count = overdue_result.scalar() or 0

    return {
        "by_status": by_status,
        "overdue": overdue_count,
        "total_tracked": sum(by_status.values()),
    }


@app.post("/v1/ocr")
async def process_ocr(body: OcrRequest, _: str = Depends(verify_api_key)):
    """Recibe una foto de factura en base64, hace OCR y guarda en DB."""
    if not await is_authorized(body.chat_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Chat no autorizado")

    from src.database import AsyncSessionFactory
    from src.models import Invoice, Client

    saved_photo_path = save_invoice_photo(body.image_base64)

    try:
        response, _ = await _ocr_and_save(body.image_base64, saved_photo_path)
        if body.send_direct or settings.telegram_mode == "direct":
            await send_message(body.chat_id, response)
            if body.send_direct:
                return {"response": "✅ Factura registrada.", "status": "success"}
        return {"response": response, "status": "success"}

    except LLMError:
        async with AsyncSessionFactory() as db:
            pending_client = Client(
                name="Cliente pendiente OCR",
                phone=_generate_placeholder_phone(),
                notes="Creado automáticamente porque el OCR quedó pendiente.",
            )
            db.add(pending_client)
            await db.flush()
            db.add(Invoice(
                invoice_number=_generate_ocr_invoice_number(),
                client_id=pending_client.id,
                items=[],
                invoice_date=datetime.now(timezone.utc),
                photo_path=saved_photo_path,
                raw_ocr={"status": "pending_ocr"},
                ocr_status="pending_ocr",
            ))
            await db.commit()

        msg = "📷 Foto recibida. El OCR está tardando, te aviso cuando esté lista."
        if settings.telegram_mode == "direct":
            await send_message(body.chat_id, msg)
        return {"response": msg, "status": "pending"}
