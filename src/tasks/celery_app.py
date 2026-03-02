"""Celery app con configuración de Redis, timezone Bogotá, y Redis locks para idempotencia."""
import uuid

import redis
from celery import Celery

from src.config import settings

# App Celery
app = Celery("asistente-norena", broker=settings.redis_url, backend=settings.redis_url)

app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone=settings.timezone,
    enable_utc=True,
    # Autodiscover de tasks en el módulo scheduler
    include=["src.tasks.scheduler"],
    # Beat schedule (se define en scheduler.py)
    beat_schedule_filename="/app/celerybeat-schedule",
)

# Beat schedule
app.conf.beat_schedule = {
    "morning-digest": {
        "task": "src.tasks.scheduler.morning_digest",
        "schedule": 28800,  # 8am Bogotá → 13:00 UTC (28800s = 8h desde UTC-5)
    },
    "check-stale-cases": {
        "task": "src.tasks.scheduler.check_stale_cases",
        "schedule": 3600,  # Cada hora
    },
    "dispatch-reminders": {
        "task": "src.tasks.scheduler.dispatch_reminders",
        "schedule": 60,  # Cada minuto
    },
    "retry-pending-ocr": {
        "task": "src.tasks.scheduler.retry_pending_ocr",
        "schedule": 300,  # Cada 5 minutos
    },
    # Seguimiento de pedidos / entregas
    "check-upcoming-deliveries": {
        "task": "src.tasks.scheduler.check_upcoming_deliveries",
        "schedule": 21600,  # Cada 6 horas — alerta 3 días hábiles antes
    },
    "check-overdue-deliveries": {
        "task": "src.tasks.scheduler.check_overdue_deliveries",
        "schedule": 21600,  # Cada 6 horas — alerta pedidos vencidos
    },
    "check-delivery-day": {
        "task": "src.tasks.scheduler.check_delivery_day",
        "schedule": 3600,   # Cada hora — notifica cuando HOY es el día de entrega
    },
    "check-delivery-followup": {
        "task": "src.tasks.scheduler.check_delivery_followup",
        "schedule": 7200,   # Cada 2 horas — pregunta si llegó el pedido de ayer
    },
}

# ─── Redis Locks para idempotencia ───────────────────────────────────────────

_redis_sync: redis.Redis | None = None


def _get_redis() -> redis.Redis:
    global _redis_sync
    if _redis_sync is None:
        _redis_sync = redis.from_url(settings.redis_url)
    return _redis_sync


def acquire_lock(key: str, ttl_seconds: int = 300) -> bool:
    """
    Adquiere un lock Redis. Retorna True si lo obtuvo, False si ya existe.
    Previene que tareas Celery duplicadas procesen el mismo registro.
    """
    r = _get_redis()
    lock_key = f"celery_lock:{key}"
    return bool(r.set(lock_key, "1", nx=True, ex=ttl_seconds))


def release_lock(key: str) -> None:
    """Libera el lock Redis manualmente."""
    r = _get_redis()
    r.delete(f"celery_lock:{key}")
