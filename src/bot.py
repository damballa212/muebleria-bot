"""Bot de Telegram con long-polling directo — sin OpenClaw como intermediario LLM.

Flujo:
  Telegram → bot.py (long-polling) → /v1/process (LangGraph) → Telegram
"""
import asyncio
import logging

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

TELEGRAM_API = f"https://api.telegram.org/bot{settings.telegram_bot_token}"


# ─── Envío de mensajes ──────────────────────────────────────────────────────

async def send_message(chat_id: str, text: str) -> None:
    """Envía un mensaje de texto a Telegram (máx 4096 chars)."""
    text = text.strip()[:4090] + ("..." if len(text) > 4090 else "")
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        )
        if resp.status_code != 200:
            logger.error("Telegram sendMessage failed: %s", resp.text)


async def send_typing(chat_id: str) -> None:
    """Envía el indicador de 'escribiendo...' a Telegram."""
    async with httpx.AsyncClient(timeout=5) as client:
        await client.post(
            f"{TELEGRAM_API}/sendChatAction",
            json={"chat_id": chat_id, "action": "typing"},
        )


# ─── Autorización ───────────────────────────────────────────────────────────

async def is_authorized(chat_id: str) -> bool:
    """Valida que el mensaje viene del dueño autorizado."""
    return str(chat_id) == str(settings.telegram_owner_chat_id)


def format_response(response_text: str) -> str:
    """Formatea la respuesta del backend para Telegram."""
    text = response_text.strip()
    if len(text) > 4096:
        text = text[:4090] + "\n..."
    return text


# ─── Procesamiento de mensajes ──────────────────────────────────────────────

async def process_telegram_message(chat_id: str, text: str, username: str = "") -> None:
    """Procesa un mensaje de Telegram llamando al LangGraph backend."""
    # Mostrar "escribiendo..." mientras procesamos
    await send_typing(chat_id)

    # Llamar al backend LangGraph
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                "http://localhost:8000/v1/process",
                headers={
                    "Authorization": f"Bearer {settings.internal_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "message": text,
                    "chat_id": str(chat_id),
                    "source": "telegram",
                    "username": username,
                },
            )
            data = resp.json()
            response_text = data.get("response", "Error procesando tu solicitud.")
    except Exception as e:
        logger.error("Error calling backend: %s", e)
        response_text = "⚠️ Error de conexión con el sistema. Intenta de nuevo."

    await send_message(chat_id, response_text)


# ─── Long-polling loop ──────────────────────────────────────────────────────

async def telegram_polling_loop() -> None:
    """Long-polling loop — recibe updates de Telegram y los procesa."""
    offset = 0
    logger.info("🤖 Telegram long-polling iniciado")

    while True:
        try:
            async with httpx.AsyncClient(timeout=35) as client:
                resp = await client.get(
                    f"{TELEGRAM_API}/getUpdates",
                    params={"offset": offset, "timeout": 30, "allowed_updates": ["message"]},
                )
                if resp.status_code != 200:
                    logger.warning("getUpdates error %s", resp.status_code)
                    await asyncio.sleep(5)
                    continue

                updates = resp.json().get("result", [])

            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message", {})
                chat_id = str(message.get("chat", {}).get("id", ""))
                text = message.get("text", "").strip()
                username = message.get("from", {}).get("username", "")

                if not text or not chat_id:
                    continue

                # Solo procesar mensajes del dueño autorizado
                if not await is_authorized(chat_id):
                    logger.warning("Mensaje no autorizado de chat_id=%s", chat_id)
                    await send_message(
                        chat_id,
                        "No estoy autorizado para responder mensajes de este chat."
                    )
                    continue

                logger.info("Mensaje de %s: %s", chat_id, text[:60])

                # Procesar de forma asíncrona sin bloquear el loop
                asyncio.create_task(
                    process_telegram_message(chat_id, text, username)
                )

        except asyncio.CancelledError:
            logger.info("Telegram polling detenido.")
            break
        except Exception as e:
            logger.error("Error en polling loop: %s", e)
            await asyncio.sleep(5)
