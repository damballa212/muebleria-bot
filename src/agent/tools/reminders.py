"""Tools de recordatorios: crear_recordatorio, listar_recordatorios, cancelar_recordatorio, get_datetime."""
from datetime import datetime

import pytz
from langchain_core.tools import tool
from sqlalchemy import select

from src.config import settings
from src.database import AsyncSessionFactory
from src.models import Reminder


def _tz() -> pytz.BaseTzInfo:
    return pytz.timezone(settings.timezone)


@tool
async def get_datetime() -> str:
    """Retorna la fecha y hora actual en Bogotá. Úsalo cuando el usuario diga 'mañana', 'hoy', etc."""
    now = datetime.now(_tz())
    return now.strftime("Fecha actual: %A %d de %B de %Y, %H:%M — Zona horaria: America/Bogota")


@tool
async def crear_recordatorio(texto: str, fecha_hora: str) -> str:
    """
    Crea un recordatorio. fecha_hora debe estar en formato ISO 8601 (2026-03-02T09:00:00).
    Usa get_datetime primero si el usuario dice 'mañana a las 9am'.
    """
    try:
        remind_at = datetime.fromisoformat(fecha_hora)
        if remind_at.tzinfo is None:
            remind_at = _tz().localize(remind_at)
    except ValueError:
        return f"❌ Formato de fecha inválido: '{fecha_hora}'. Usa formato ISO 8601: 2026-03-02T09:00:00"

    async with AsyncSessionFactory() as db:
        reminder = Reminder(text=texto, remind_at=remind_at)
        db.add(reminder)
        await db.commit()
        await db.refresh(reminder)

    formatted = remind_at.strftime("%d/%m/%Y a las %H:%M")
    return f"⏰ Recordatorio creado\n📝 {texto}\n🕐 {formatted}"


@tool
async def listar_recordatorios() -> str:
    """Lista todos los recordatorios pendientes (no enviados)."""
    async with AsyncSessionFactory() as db:
        result = await db.execute(
            select(Reminder)
            .where(Reminder.is_sent == False)  # noqa: E712
            .order_by(Reminder.remind_at.asc())
        )
        reminders = result.scalars().all()

    if not reminders:
        return "No tienes recordatorios pendientes."

    lines = [f"Recordatorios pendientes ({len(reminders)}):"]
    for r in reminders:
        date_str = r.remind_at.astimezone(_tz()).strftime("%d/%m/%Y %H:%M")
        lines.append(f"  📌 {date_str} — {r.text}")
    return "\n".join(lines)


@tool
async def cancelar_recordatorio(reminder_id: str) -> str:
    """Cancela un recordatorio por su ID."""
    async with AsyncSessionFactory() as db:
        reminder = await db.get(Reminder, reminder_id)
        if not reminder:
            return f"❌ Recordatorio {reminder_id} no encontrado."
        if reminder.is_sent:
            return f"⚠️ El recordatorio ya fue enviado y no se puede cancelar."

        await db.delete(reminder)
        await db.commit()

    return f"✅ Recordatorio cancelado: '{reminder.text}'"
