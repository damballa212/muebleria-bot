# Asistente Noreña

Backend de negocio para Mueblería Noreña. Gestiona clientes, garantías, cotizaciones, remisiones y recordatorios; recibe texto o fotos desde OpenClaw o desde Telegram en modo directo.

## Estado real del repo

- La integración incluida en este workspace con OpenClaw es por `skills` en `openclaw-config/skills/`.
- No existe carpeta `openclaw-config/plugins/` en este repo. Si en el futuro se crea un plugin, será otra capa adicional, no la actual.
- El backend expone dos endpoints internos:
  - `POST /v1/process`: mensajes de texto.
  - `POST /v1/ocr`: fotos de facturas/remisiones para OCR.
  - `POST /v1/reset`: limpia historial y estado conversacional persistido del chat.

## Stack

| Capa | Tecnología |
|---|---|
| API | FastAPI + Python 3.12 |
| Orquestación agente | LangGraph |
| LLM | OpenRouter |
| STT de audio | Gemini Audio Understanding |
| OCR | Claude Sonnet Vision vía OpenRouter |
| Base de datos | PostgreSQL 17 |
| Buffer de chat | Redis 7 |
| Memoria semántica | Qdrant + Cohere |
| Tareas programadas | Celery worker + Celery beat |
| Integración externa | OpenClaw por skills o Telegram directo |

## Estructura

```text
.
├── src/
│   ├── main.py                 # /health, /v1/process, /v1/ocr
│   ├── config.py               # Variables de entorno
│   ├── database.py             # Engine y sesiones async
│   ├── llm.py                  # Cliente OpenRouter con retry
│   ├── memory.py               # Redis + Qdrant + Cohere
│   ├── ocr.py                  # OCR de facturas manuscritas
│   ├── bot.py                  # Modo Telegram directo
│   ├── models/                 # ORM SQLAlchemy
│   ├── agent/
│   │   ├── graph.py            # recall -> agent -> tools -> record
│   │   ├── nodes/
│   │   └── tools/
│   └── tasks/                  # Celery y scheduler
├── openclaw-config/
│   ├── AGENTS.md               # Prompt del agente OpenClaw
│   ├── openclaw.json           # Env vars para skills
│   └── skills/                 # Integración actual con OpenClaw
├── docker-compose.yml
├── docker-compose.override.yml
└── .env.example
```

## Flujo principal

### Texto

1. OpenClaw o Telegram directo envía un mensaje al backend.
2. `src/main.py` valida `INTERNAL_API_KEY` y `chat_id`.
3. El grafo `src/agent/graph.py` ejecuta:
   - `recall`: recupera contexto de Redis, Qdrant y PostgreSQL.
   - `agent`: el LLM decide si responde o llama tools.
   - `tools`: ejecuta operaciones de negocio.
   - `record`: persiste chat, memoria semántica e interaction log.

### Reset de sesión

1. OpenClaw crea una sesión nueva con `/reset`.
2. Antes de saludar, debe llamar a `POST /v1/reset` para borrar `chat_history` y `chat_state` en Redis para ese `chat_id`.
3. Eso evita que el siguiente mensaje herede `active_case_number`, `pending_evidence_case` u otro contexto viejo del backend.

### OCR

1. OpenClaw envía una foto a `POST /v1/ocr`.
2. `src/ocr.py` guarda la imagen y llama al modelo de visión.
3. El backend crea o vincula cliente, registra una `Invoice` y devuelve un resumen.
4. Si el OCR falla temporalmente, la foto se guarda con `ocr_status="pending_ocr"` y Celery la reintenta.

## Modelo de datos

Las tablas principales están en `src/models/models.py`:

- `Client`
- `Case`
- `CaseUpdate`
- `Invoice`
- `Reminder`
- `InteractionLog`

## Variables de entorno

Usa `.env.example` como plantilla. Las variables mínimas para arrancar bien son:

- `OPENROUTER_API_KEY`
- `GEMINI_API_KEY`
- `OPENAI_API_KEY` (opcional, solo si quieres fallback OpenAI)
- `COHERE_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_OWNER_CHAT_ID`
- `DATABASE_URL`
- `INTERNAL_API_KEY`

Para audios, el backend usa Gemini con `AUDIO_TRANSCRIBE_PROVIDER=gemini` y `AUDIO_TRANSCRIBE_MODEL=gemini-2.5-flash`. Si quieres fallback alterno, puedes configurar OpenAI aparte.

## Arranque local

```bash
cp .env.example .env
docker compose up -d --build
curl http://localhost:8000/health
```

En local, `docker-compose.override.yml` agrega Qdrant y elimina la dependencia de `dokploy-network`.

## OpenClaw

La configuración viva para OpenClaw está en:

- `openclaw-config/AGENTS.md`
- `openclaw-config/openclaw.json`
- `openclaw-config/skills/garantia-muebleria/SKILL.md`
- `openclaw-config/skills/cotizacion-muebleria/SKILL.md`
- `openclaw-config/skills/factura-muebleria/SKILL.md`
- `openclaw-config/skills/consultar-casos/SKILL.md`
- `openclaw-config/skills/recordatorio-muebleria/SKILL.md`

## Modo directo sin OpenClaw

Si OpenClaw no está disponible, cambia:

```env
TELEGRAM_MODE=direct
```

y reinicia `backend`. En ese modo, `src/bot.py` hace long-polling directo con Telegram.
