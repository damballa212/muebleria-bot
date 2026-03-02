"""Todas las tareas Celery: morning_digest, check_stale_cases, dispatch_reminders,
retry_pending_ocr, check_upcoming_deliveries, check_overdue_deliveries."""
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

import httpx
import pytz
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.config import settings
from src.database import SyncSessionFactory
from src.models import Case, Client, Invoice, Reminder
from src.tasks.celery_app import acquire_lock, app, release_lock

logger = logging.getLogger(__name__)

_TZ = pytz.timezone(settings.timezone)
_TELEGRAM_API = f"https://api.telegram.org/bot{settings.telegram_bot_token}"


def _send_tg(text: str) -> None:
    """Helper síncrono para enviar mensajes a Telegram."""
    try:
        with httpx.Client(timeout=10) as client:
            client.post(
                f"{_TELEGRAM_API}/sendMessage",
                json={"chat_id": settings.telegram_owner_chat_id, "text": text, "parse_mode": "HTML"},
            )
    except Exception as exc:
        logger.warning("Telegram send failed in task: %s", exc)


# ─── Task 1: Morning Digest ──────────────────────────────────────────────────

@app.task(name="src.tasks.scheduler.morning_digest")
def morning_digest():
    """Resumen de las 8am: casos pendientes + recordatorios del día."""
    if not acquire_lock("morning_digest", ttl_seconds=3600):
        return  # Ya se ejecutó hoy — idempotencia

    try:
        with SyncSessionFactory() as db:
            # Casos pendientes
            result = db.execute(
                select(Case)
                .options(selectinload(Case.client))
                .where(Case.status.in_(["abierto", "escalado", "en_proceso"]))
                .order_by(Case.created_at.asc())
            )
            cases = result.scalars().all()

            # Recordatorios de hoy
            today_start = datetime.now(_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
            today_end = today_start + timedelta(days=1)
            rem_result = db.execute(
                select(Reminder).where(
                    Reminder.is_sent == False,  # noqa: E712
                    Reminder.remind_at >= today_start,
                    Reminder.remind_at < today_end,
                )
            )
            reminders = rem_result.scalars().all()

            # Pedidos con entrega en los próximos 3 días hábiles
            today_dt = datetime.now(_TZ)
            today_date = today_dt.date()
            threshold_date = _add_business_days(today_date, 3)
            threshold_utc = datetime.combine(threshold_date, datetime.min.time()).replace(tzinfo=timezone.utc)
            now_utc = datetime.now(timezone.utc)

            inv_result = db.execute(
                select(Invoice)
                .options(selectinload(Invoice.client))
                .where(
                    Invoice.delivery_status.in_(["pendiente", "en_ruta", "demorado"]),
                    Invoice.delivery_date.isnot(None),
                    Invoice.delivery_date >= now_utc,
                    Invoice.delivery_date <= threshold_utc,
                )
                .order_by(Invoice.delivery_date.asc())
            )
            upcoming_orders = inv_result.scalars().all()

            # Pedidos vencidos sin confirmar
            overdue_result = db.execute(
                select(Invoice)
                .options(selectinload(Invoice.client))
                .where(
                    Invoice.delivery_status.in_(["pendiente", "en_ruta", "demorado"]),
                    Invoice.delivery_date.isnot(None),
                    Invoice.delivery_date < now_utc,
                )
                .order_by(Invoice.delivery_date.asc())
            )
            overdue_orders = overdue_result.scalars().all()

        lines = ["☀️ <b>Buenos días — Resumen Mueblería Noreña</b>"]

        if cases:
            lines.append(f"\n📋 <b>Casos pendientes ({len(cases)})</b>")
            for c in cases[:10]:
                name = c.client.name if c.client else "Sin cliente"
                lines.append(f"  #{c.case_number} — {name} — {c.status}")

        if upcoming_orders:
            lines.append(f"\n📦 <b>Entregas próximas — 3 días hábiles ({len(upcoming_orders)})</b>")
            for inv in upcoming_orders:
                cliente = inv.client.name if inv.client else "Sin cliente"
                fecha_str = inv.delivery_date.astimezone(_TZ).strftime("%d/%m")
                raw = inv.raw_ocr or {}
                productos = (raw.get("productos", "") or "—")[:50]
                lines.append(f"  #{inv.invoice_number} — {cliente} — {fecha_str} — {productos}")

        if overdue_orders:
            lines.append(f"\n🚨 <b>Pedidos VENCIDOS sin confirmar ({len(overdue_orders)})</b>")
            for inv in overdue_orders:
                cliente = inv.client.name if inv.client else "Sin cliente"
                fecha_str = inv.delivery_date.astimezone(_TZ).strftime("%d/%m")
                days_late = (now_utc - inv.delivery_date).days
                lines.append(f"  #{inv.invoice_number} — {cliente} — debía: {fecha_str} (hace {days_late}d)")

        if reminders:
            lines.append(f"\n⏰ <b>Recordatorios de hoy ({len(reminders)})</b>")
            for r in reminders:
                t = r.remind_at.astimezone(_TZ).strftime("%H:%M")
                lines.append(f"  {t} — {r.text}")

        if not cases and not reminders and not upcoming_orders and not overdue_orders:
            lines.append("\n✅ Todo al día — no hay pendientes.")

        msg = "\n".join(lines)
        _send_tg(msg)

    finally:
        pass  # Lock expira automáticamente en 1 hora


# ─── Task 2: Check Stale Cases (escalación progresiva) ───────────────────────

@app.task(name="src.tasks.scheduler.check_stale_cases")
def check_stale_cases():
    """
    Revisa casos sin actualización y envía alertas progresivas:
    - 24h → primera alerta
    - 48h → segunda alerta (más urgente)
    - 5d  → alerta crítica
    """
    now = datetime.now(_TZ)
    with SyncSessionFactory() as db:
        result = db.execute(
            select(Case)
            .options(selectinload(Case.client))
            .where(Case.status.in_(["escalado", "en_proceso"]))
        )
        cases = result.scalars().all()

    for case in cases:
        lock_key = f"stale_{case.id}"
        hours_open = (now - case.created_at.astimezone(_TZ)).total_seconds() / 3600

        # Determinar nivel de alerta
        if hours_open >= 120 and not acquire_lock(f"{lock_key}_5d", 86400):
            continue
        elif hours_open >= 120:
            level, urgency = "5d", "⚠️⚠️ CRÍTICO"
        elif hours_open >= 48 and not acquire_lock(f"{lock_key}_48h", 86400):
            continue
        elif hours_open >= 48:
            level, urgency = "48h", "⚠️ Urgente"
        elif hours_open >= 24 and not acquire_lock(f"{lock_key}_24h", 86400):
            continue
        elif hours_open >= 24:
            level, urgency = "24h", "🔔"
        else:
            continue

        responsible = "Michelle" if case.assigned_to == "michelle" else "Daniel"
        client_name = case.client.name if case.client else "el cliente"
        msg = (
            f"{urgency} Caso #{case.case_number} sin respuesta ({level})\n"
            f"Tipo: {case.type} | Cliente: {client_name}\n"
            f"Responsable: {responsible}"
        )
        _send_tg(msg)



# ─── Task 3: Dispatch Reminders ──────────────────────────────────────────────

@app.task(name="src.tasks.scheduler.dispatch_reminders")
def dispatch_reminders():
    """Envía recordatorios que ya vencieron y los marca como enviados."""
    now = datetime.now(_TZ)
    with SyncSessionFactory() as db:
        result = db.execute(
            select(Reminder).where(
                Reminder.is_sent == False,  # noqa: E712
                Reminder.remind_at <= now,
            )
        )
        reminders = result.scalars().all()

        for reminder in reminders:
            lock_key = f"reminder_{reminder.id}"
            if not acquire_lock(lock_key, ttl_seconds=120):
                continue  # Otro worker ya lo está procesando

            try:
                _send_tg(f"⏰ Recordatorio\n📝 {reminder.text}")
                reminder.is_sent = True
            finally:
                release_lock(lock_key)

        db.commit()


# ─── Helpers para días hábiles ───────────────────────────────────────────────

def _is_business_day(d: date) -> bool:
    return d.weekday() < 5  # 0=lunes … 4=viernes


def _add_business_days(start: date, n: int) -> date:
    """Suma n días hábiles a una fecha."""
    current = start
    added = 0
    while added < n:
        current += timedelta(days=1)
        if _is_business_day(current):
            added += 1
    return current


# ─── Task 5: Check Upcoming Deliveries ───────────────────────────────────────

@app.task(name="src.tasks.scheduler.check_upcoming_deliveries")
def check_upcoming_deliveries():
    """
    Alerta sobre pedidos con fecha de entrega en los próximos 3 días hábiles.
    Se ejecuta cada 6 horas. Solo envía una alerta por pedido (idempotente).
    """
    now = datetime.now(_TZ)
    today = now.date()
    threshold = _add_business_days(today, 3)  # 3 días hábiles desde hoy

    with SyncSessionFactory() as db:
        result = db.execute(
            select(Invoice)
            .options(selectinload(Invoice.client))
            .where(
                Invoice.delivery_status.in_(["pendiente", "en_ruta"]),
                Invoice.delivery_date.isnot(None),
                Invoice.delivery_date <= datetime.combine(threshold, datetime.min.time()).replace(tzinfo=timezone.utc),
                Invoice.delivery_date >= datetime.now(timezone.utc),
                Invoice.delivery_alert_sent_at.is_(None),  # Solo si no se ha alertado
            )
        )
        invoices = result.scalars().all()

        if not invoices:
            return

        lines = [f"📦 <b>Entregas próximas ({len(invoices)}) — próximos 3 días hábiles</b>"]
        for inv in invoices:
            cliente = inv.client.name if inv.client else "Sin cliente"
            telefono = inv.client.phone if inv.client else ""
            raw = inv.raw_ocr or {}
            productos = raw.get("productos", "") or "—"
            direccion = raw.get("direccion", "—")
            fecha_str = inv.delivery_date.astimezone(_TZ).strftime("%d/%m/%Y")
            tipo = "🚚 A domicilio" if raw.get("tipo_acarreo", "llevar") == "llevar" else "🏬 Recoge en tienda"

            lines.append(
                f"\n📋 #{inv.invoice_number}\n"
                f"  👤 {cliente}  📱 {telefono}\n"
                f"  🛋️ {productos[:60]}\n"
                f"  📅 {fecha_str}  {tipo}\n"
                f"  📍 {direccion}"
            )
            inv.delivery_alert_sent_at = datetime.now(timezone.utc)

        db.commit()

    _send_tg("\n".join(lines))


# ─── Task 6: Check Overdue Deliveries ────────────────────────────────────────

@app.task(name="src.tasks.scheduler.check_overdue_deliveries")
def check_overdue_deliveries():
    """
    Alerta sobre pedidos cuya fecha de entrega ya pasó y no han sido confirmados.
    - Primer vencimiento (1-3 días): recordatorio suave.
    - Vencimiento grave (>3 días): alerta urgente.
    Se ejecuta cada 6 horas. Idempotente por pedido.
    """
    now_utc = datetime.now(timezone.utc)

    with SyncSessionFactory() as db:
        result = db.execute(
            select(Invoice)
            .options(selectinload(Invoice.client))
            .where(
                Invoice.delivery_status.in_(["pendiente", "en_ruta", "demorado"]),
                Invoice.delivery_date.isnot(None),
                Invoice.delivery_date < now_utc,  # Ya venció
                Invoice.delivery_overdue_alert_at.is_(None),  # No alertado aún
            )
        )
        invoices = result.scalars().all()

        if not invoices:
            return

        soft_lines = [f"⚠️ <b>Pedidos vencidos sin confirmar ({len(invoices)})</b>"]
        urgent_lines = []

        for inv in invoices:
            cliente = inv.client.name if inv.client else "Sin cliente"
            telefono = inv.client.phone if inv.client else ""
            raw = inv.raw_ocr or {}
            productos = raw.get("productos", "") or "—"
            fecha_str = inv.delivery_date.astimezone(_TZ).strftime("%d/%m/%Y")
            days_overdue = (now_utc - inv.delivery_date).days

            entry = (
                f"\n📋 #{inv.invoice_number}\n"
                f"  👤 {cliente}  📱 {telefono}\n"
                f"  🛋️ {productos[:60]}\n"
                f"  📅 Debía entregarse: {fecha_str}  (hace {days_overdue} día(s))"
            )

            if days_overdue > 3:
                urgent_lines.append(entry)
            else:
                soft_lines.append(entry)

            inv.delivery_overdue_alert_at = now_utc

        db.commit()

    if len(soft_lines) > 1:
        _send_tg("\n".join(soft_lines) + "\n\n❓ ¿Ya fueron entregados? Confirmar con: confirmar_entrega #factura")
    if urgent_lines:
        msg = "🚨 <b>PEDIDOS VENCIDOS — más de 3 días sin confirmar</b>\n" + "\n".join(urgent_lines)
        _send_tg(msg)


# ─── Task 7: Delivery Day Notification ───────────────────────────────────────

@app.task(name="src.tasks.scheduler.check_delivery_day")
def check_delivery_day():
    """
    Notifica cuando HOY es el día de entrega programado de un pedido.
    Se ejecuta cada hora. Idempotente: solo envía una vez por pedido.
    """
    now_utc = datetime.now(timezone.utc)
    today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end   = now_utc.replace(hour=23, minute=59, second=59, microsecond=999999)

    with SyncSessionFactory() as db:
        result = db.execute(
            select(Invoice)
            .options(selectinload(Invoice.client))
            .where(
                Invoice.delivery_status.in_(["pendiente", "en_ruta"]),
                Invoice.delivery_date.isnot(None),
                Invoice.delivery_date >= today_start,
                Invoice.delivery_date <= today_end,
                Invoice.delivery_day_notified_at.is_(None),
            )
        )
        invoices = result.scalars().all()

        if not invoices:
            return

        lines = [f"📦 <b>Entregas programadas para HOY ({len(invoices)})</b>"]
        for inv in invoices:
            cliente = inv.client.name if inv.client else "Sin cliente"
            telefono = inv.client.phone if inv.client else ""
            raw = inv.raw_ocr or {}
            productos = (raw.get("productos", "") or "—")[:60]
            direccion = raw.get("direccion", "—")
            persona_recibe = raw.get("persona_recibe", "") or cliente
            tipo = "🚚 A domicilio" if raw.get("tipo_acarreo", "llevar") == "llevar" else "🏬 Recoge en tienda"
            resta = float(raw.get("resta", 0) or 0)
            pago_str = "✅ Pagado" if resta == 0 else f"⏳ Falta: ${resta:,.0f}"

            lines.append(
                f"\n📋 #{inv.invoice_number}\n"
                f"  👤 {cliente}  📱 {telefono}\n"
                f"  🛋️ {productos}\n"
                f"  {tipo}  |  Recibe: {persona_recibe}\n"
                f"  📍 {direccion}\n"
                f"  {pago_str}"
            )
            inv.delivery_day_notified_at = now_utc

        db.commit()

    _send_tg("\n".join(lines))


# ─── Task 8: Delivery Follow-up (día siguiente) ───────────────────────────────

@app.task(name="src.tasks.scheduler.check_delivery_followup")
def check_delivery_followup():
    """
    Al día siguiente de la fecha de entrega, pregunta si el cliente recibió el pedido.
    Solo aplica a pedidos que no están confirmados ni cancelados.
    Se ejecuta cada 2 horas. Idempotente por pedido.
    """
    now_utc = datetime.now(timezone.utc)
    # Ventana: ayer (hace 24h a hace 48h)
    window_end   = now_utc - timedelta(hours=24)
    window_start = now_utc - timedelta(hours=48)

    with SyncSessionFactory() as db:
        result = db.execute(
            select(Invoice)
            .options(selectinload(Invoice.client))
            .where(
                Invoice.delivery_status.in_(["pendiente", "en_ruta", "entregado"]),
                Invoice.delivery_date.isnot(None),
                Invoice.delivery_date >= window_start,
                Invoice.delivery_date < window_end,
                Invoice.delivery_followup_sent_at.is_(None),
            )
        )
        invoices = result.scalars().all()

        if not invoices:
            return

        for inv in invoices:
            cliente = inv.client.name if inv.client else "Sin cliente"
            telefono = inv.client.phone if inv.client else ""
            raw = inv.raw_ocr or {}
            productos = (raw.get("productos", "") or "—")[:60]
            fecha_str = inv.delivery_date.astimezone(_TZ).strftime("%d/%m/%Y")

            msg = (
                f"❓ <b>Verificar entrega — #{inv.invoice_number}</b>\n\n"
                f"👤 {cliente}  📱 {telefono}\n"
                f"🛋️ {productos}\n"
                f"📅 Fecha programada: {fecha_str}\n\n"
                f"¿El cliente ya recibió el pedido?\n"
                f"• Si SÍ recibió → responde: <code>confirmar entrega #{inv.invoice_number}</code>\n"
                f"• Si NO recibió → responde: <code>actualizar #{inv.invoice_number} demorado [motivo]</code>"
            )
            _send_tg(msg)
            inv.delivery_followup_sent_at = now_utc

        db.commit()


# ─── Task 4: Retry Pending OCR ───────────────────────────────────────────────

@app.task(name="src.tasks.scheduler.retry_pending_ocr")
def retry_pending_ocr():
    """Reintenta el OCR de facturas que fallaron (status=pending_ocr)."""
    from src.ocr import process_invoice_photo

    with SyncSessionFactory() as db:
        result = db.execute(
            select(Invoice).where(Invoice.ocr_status == "pending_ocr").limit(5)
        )
        invoices = result.scalars().all()

        for invoice in invoices:
            if not invoice.photo_path:
                continue
            lock_key = f"ocr_retry_{invoice.id}"
            if not acquire_lock(lock_key, ttl_seconds=60):
                continue

            try:
                import base64
                from src.utils.phone import normalize_phone

                with open(invoice.photo_path, "rb") as f:
                    img_b64 = base64.b64encode(f.read()).decode()

                ocr_result = asyncio.run(process_invoice_photo(img_b64, photo_path=invoice.photo_path))
                client = db.get(Client, invoice.client_id)
                if client:
                    is_placeholder_client = client.name.startswith("Cliente pendiente OCR")
                    if ocr_result.nombre and is_placeholder_client:
                        client.name = ocr_result.nombre
                    if ocr_result.telefono and is_placeholder_client:
                        client.phone = normalize_phone(ocr_result.telefono)
                invoice.items = ocr_result.items
                invoice.total = ocr_result.total
                invoice.invoice_date = invoice.invoice_date or datetime.now(timezone.utc)
                invoice.is_signed = ocr_result.firmada
                invoice.raw_ocr = ocr_result.raw
                invoice.ocr_status = "done"
                db.commit()
                _send_tg(f"✅ OCR completado para factura {invoice.id}")
            except Exception as exc:
                logger.warning("OCR retry failed for %s: %s", invoice.id, exc)
                db.rollback()
            finally:
                release_lock(lock_key)
