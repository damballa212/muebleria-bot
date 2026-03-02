# Plan Técnico Actual

Documento de arquitectura y operación del workspace tal como existe hoy.

## Objetivo

Centralizar en Telegram la operación de Mueblería Noreña:

- registrar clientes y remisiones,
- escalar garantías y cotizaciones,
- digitalizar facturas manuscritas,
- recordar tareas y hacer seguimiento,
- conservar memoria operativa entre conversaciones.

## Arquitectura actual

```text
Usuario
  -> OpenClaw o Telegram directo
  -> FastAPI (/v1/process, /v1/ocr)
  -> LangGraph
       recall -> agent -> tools -> record
  -> PostgreSQL / Redis / Qdrant / Cohere / OpenRouter
  -> respuesta al usuario
```

## Componentes

### Backend HTTP

- `src/main.py`
- Protege endpoints con `Authorization: Bearer <INTERNAL_API_KEY>`.
- Autoriza un solo chat por `TELEGRAM_OWNER_CHAT_ID`.

### Grafo del agente

- `src/agent/graph.py`
- Flujo fijo:
  - `recall`: contexto de chat, memoria semántica y negocio.
  - `agent`: LLM con system prompt de operación.
  - `tools`: ejecución de tools LangChain.
  - `record`: persistencia de interacción.

### Tools de negocio

- `src/agent/tools/clients.py`
- `src/agent/tools/cases.py`
- `src/agent/tools/invoices.py`
- `src/agent/tools/reminders.py`

### Persistencia

- PostgreSQL: datos estructurados.
- Redis: buffer conversacional e idempotencia de tareas.
- Qdrant + Cohere: memoria semántica.

### OCR

- `src/ocr.py`
- Guarda la imagen en disco.
- Llama al modelo de visión.
- Si el OCR queda pendiente, crea una `Invoice` técnica con `ocr_status="pending_ocr"` para reintento posterior.

### Scheduler

- `src/tasks/celery_app.py`
- `src/tasks/scheduler.py`
- Tareas:
  - resumen matutino,
  - alertas por casos estancados,
  - despacho de recordatorios,
  - reintento de OCR pendiente.

## Integración con OpenClaw

La integración presente en este repo es por skills:

- `openclaw-config/skills/garantia-muebleria/SKILL.md`
- `openclaw-config/skills/cotizacion-muebleria/SKILL.md`
- `openclaw-config/skills/factura-muebleria/SKILL.md`
- `openclaw-config/skills/consultar-casos/SKILL.md`
- `openclaw-config/skills/recordatorio-muebleria/SKILL.md`

Cada skill hace `curl` al backend local. No hay plugin OpenClaw versionado dentro de este workspace.

## Decisiones importantes

- `Invoice.invoice_number` siempre se llena, incluso para OCR sin consecutivo legible, usando un ID técnico `OCR-...`.
- Si el OCR no identifica al cliente, se crea un cliente placeholder para no romper integridad referencial.
- El contexto del agente se inyecta como `SystemMessage`, no como mensaje del usuario.
- La búsqueda de facturas se hace en SQL sobre toda la base relevante, no filtrando solo las últimas 10 en memoria.

## Variables mínimas

Ver `.env.example`.

Obligatorias:

- `OPENROUTER_API_KEY`
- `COHERE_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_OWNER_CHAT_ID`
- `DATABASE_URL`
- `INTERNAL_API_KEY`

## Fuera de alcance en este repo

- Plugin `norena-mcp` de OpenClaw.
- Infraestructura anterior basada en Odoo/Chatwoot/Evolution.

Ese contexto viejo se conserva solo como referencia histórica en `Contexto app anterior NO ACTUAL/PRD.md`.
