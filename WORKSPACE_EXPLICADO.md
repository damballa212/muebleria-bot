# Workspace Explicado

Documento maestro del workspace real de Asistente Noreña.

Objetivo:
- explicar qué existe de verdad en el repo;
- explicar cómo se conecta OpenClaw con el backend;
- explicar la lógica de negocio, memoria, OCR, audio, scheduler y Docker;
- servir como referencia técnica para seguir desarrollando sin redescubrir el sistema.

Este documento describe el estado actual del workspace, no el diseño viejo archivado en `Contexto app anterior NO ACTUAL/PRD.md`.

---

## 1. Resumen ejecutivo

Este workspace implementa un backend de negocio para Mueblería Noreña.

El sistema hace las siguientes cosas principales:
- gestionar casos de garantía y cotización;
- consultar clientes, historial y facturas/remisiones;
- crear y despachar recordatorios;
- digitalizar fotos de facturas/remisiones con OCR (dos tipos de planilla física);
- seguimiento de pedidos: estado de entrega, alertas automáticas, confirmación de recepción;
- detectar facturas vencidas (saldo pendiente o entrega sin confirmar);
- entender audios de Telegram/OpenClaw y tratarlos como conversación normal o evidencia de un caso.

La arquitectura real es:

```text
Telegram/OpenClaw
        |
        v
OpenClaw Gateway + agente principal
        |
        v
POST /v1/process   POST /v1/ocr   POST /v1/reset
        |
        v
FastAPI backend
        |
        +--> LangGraph (texto)
        +--> OCR pipeline (foto)
        +--> Audio preprocess (audio)
        |
        +--> PostgreSQL
        +--> Redis
        +--> Qdrant + Cohere
        +--> Celery worker / beat
```

---

## 2. Qué hay en el repo

Estructura principal:

```text
.
├── src/
│   ├── main.py
│   ├── config.py
│   ├── database.py
│   ├── llm.py
│   ├── memory.py
│   ├── audio.py
│   ├── ocr.py
│   ├── bot.py
│   ├── agent/
│   │   ├── graph.py
│   │   ├── state.py
│   │   ├── routing.py
│   │   ├── nodes/
│   │   │   ├── agent.py
│   │   │   ├── recall.py
│   │   │   ├── record.py
│   │   │   ├── router.py
│   │   │   └── tools_executor.py
│   │   └── tools/
│   │       ├── cases.py
│   │       ├── clients.py
│   │       ├── invoices.py
│   │       ├── orders.py
│   │       └── reminders.py
│   ├── models/
│   │   └── models.py
│   └── tasks/
│       ├── celery_app.py
│       └── scheduler.py
├── scripts/
│   ├── send_to_backend.py
│   ├── reset_backend_chat.py
│   └── migrate_*.sql
├── openclaw-config/
│   ├── AGENTS.md
│   ├── openclaw.json
│   └── skills/
├── docker-compose.yml
├── docker-compose.override.yml
└── WORKSPACE_EXPLICADO.md
```

---

## 3. Identidad y rol del sistema

Hay dos capas con personalidad:

### 3.1 OpenClaw

OpenClaw es la capa de entrada en Telegram cuando `TELEGRAM_MODE=openclaw`.

Su función no es resolver negocio por sí solo. Su función correcta es:
- recibir el mensaje;
- detectar si es saludo puro o tema de negocio;
- reenviar el mensaje crudo al backend;
- devolver la respuesta útil al usuario.

La personalidad viva de OpenClaw está en:
- `openclaw-config/AGENTS.md`
- `~/.openclaw/workspace/AGENTS.md`
- `~/.openclaw/workspace/SOUL.md`

### 3.2 Backend

El backend tiene su propio agente LangGraph con prompt de negocio en:
- `src/agent/nodes/agent.py`

Ese agente se identifica como:
- `Noreñita, la asistente operativa de Mueblería Noreña`

---

## 4. Endpoints del backend

Archivo principal: `src/main.py`

### 4.1 `GET /health`

```json
{"status":"ok","version":"1.0.0"}
```

### 4.2 `POST /v1/process`

Entrada:

```json
{
  "message": "...",
  "chat_id": "7166377297",
  "source": "telegram"
}
```

Lógica interna:

```text
verify_api_key
    ->
is_authorized(chat_id)
    ->
startup/reset guard
    ->
preprocess_incoming_message(...)
    ->
si hay direct_response: devolverla
si hay rewritten_message: usarla como mensaje efectivo
    ->
compiled_graph.ainvoke(...)
    ->
extraer AIMessage final
```

### 4.3 `POST /v1/reset`

Borra `chat:{chat_id}` y `chat_state:{chat_id}` en Redis.

### 4.4 `POST /v1/ocr`

Recibe foto base64, ejecuta OCR, crea cliente e `Invoice`, responde resumen.

---

## 5. Flujo completo de texto

```text
START
  -> recall
  -> router
     -> fast_action      si la intención es segura
     -> agent            si requiere razonamiento completo
  -> tools               cuando el agente llama tools
  -> record
END
```

Edges del grafo:

```text
START -> recall -> router
router -> fast_action -> record -> END
router -> agent
agent -> tools -> agent
agent -> record -> END
```

---

## 6. Recall y contexto

Archivo: `src/agent/nodes/recall.py`

Recupera contexto desde:
- Redis chat history;
- PostgreSQL (cliente + casos relacionados);
- Qdrant semantic memory cuando aplica.

El recall NO responde por sí solo. Solo inyecta un `SystemMessage [CONTEXTO]...[/CONTEXTO]` antes del mensaje del usuario.

### 6.1 Señales determinísticas (sin búsqueda semántica)

Para consultas de estado global donde la memoria semántica no aporta y solo añade latencia, la búsqueda en Qdrant se omite:

```python
deterministic_signals = [
    "dame detalles", "detalle de", "muestrame", ...,
    "facturas de", "busca la factura",
    # Estado global — solo tools resuelven esto
    "facturas vencidas", "hay vencidas", "saldos pendientes", "quien debe",
    "cuentas por cobrar", "pedidos pendientes", "hay entregas", "entregas hoy",
    "entregas manana", "hay algo para entregar", "abonos huerfanos",
]
```

---

## 7. Router y fast-paths

Archivos:
- `src/agent/nodes/router.py`
- `src/agent/routing.py`

El router detecta intenciones obvias y despacha `fast_action` directo, sin pasar por el LLM completo.

### 7.1 Familias léxicas actuales

```python
LIST_ROOTS    = ["lista", "listar", "muestre", "mostrar", "muestra", "dame", "hay", "ver"]
CASE_ROOTS    = ["caso", "garantia", "cotizacion", "reclamo", "pendiente", ...]
INVOICE_ROOTS = ["factura", "facturas", "remision", "remisiones", "venta", "ventas", ...]
VENCIDAS_ROOTS= ["vencida", "vencidas", "vencido", "vencidos", "deben", "debe",
                  "pendiente", "pendientes", "saldo", "saldos", "cobrar", "cobro"]
SEARCH_ROOTS  = ["buscar", "busca", "encontrar", "hay", "tiene", ...]
```

### 7.2 Fast-paths activos

| Condición | Tool destino |
|-----------|-------------|
| Caso explícito + señal de detalle | `ver_caso` |
| `case_score > 0` + `list_score > 0` | `listar_casos_pendientes` |
| `invoice_score > 0` + match VENCIDAS | `listar_facturas_vencidas` |
| `invoice_score > 0` + `search_score > 0` | `buscar_factura` |

El check de vencidas va **antes** del search genérico para evitar que "hay facturas vencidas" termine en `buscar_factura("vencidas")`.

---

## 8. Agente principal

Archivo: `src/agent/nodes/agent.py`

Responsabilidad:
- decidir cuándo usar tools;
- componer respuestas de negocio;
- aplicar reglas de garantía/cotización/evidencia/remisión.

Reglas clave del prompt:
1. No decide garantías — eso es Michelle.
2. No decide cotizaciones/precios — eso es Daniel.
3. Usa tools antes de responder.
4. No agrega coletillas de seguimiento.
5. `listar_casos_pendientes()` siempre para listas de casos.
6. `listar_facturas_vencidas()` siempre para vencidas/saldos/cobros.
7. `ver_caso(case_number)` siempre para detalles de caso.

---

## 9. Tools de negocio

### 9.1 Casos — `src/agent/tools/cases.py`

- `escalar_garantia`
- `escalar_cotizacion`
- `actualizar_caso`
- `buscar_caso`
- `ver_caso`
- `listar_casos_pendientes`
- `generar_mensaje_seguimiento`
- `adjuntar_evidencia`

Números de caso: `GAR-XXXX` (Michelle) / `COT-XXXX` (Daniel).

### 9.2 Clientes — `src/agent/tools/clients.py`

- `listar_clientes`
- `buscar_cliente`
- `crear_cliente`
- `ver_historial_cliente`
- `agregar_nota_cliente`

### 9.3 Facturas / Remisiones — `src/agent/tools/invoices.py`

- `registrar_remision` — crea venta/separé/abono/garantía/cambio con todos los datos
- `listar_facturas` — facturas de un cliente por `client_id`
- `listar_facturas_vencidas` — facturas con saldo pendiente (`resta > 0`) O entregas con fecha pasada sin confirmar
- `buscar_factura` — búsqueda tolerante a acentos por cliente, número, producto
- `buscar_abonos_huerfanos` — abonos/separés cuya remisión original no está en el sistema

#### `registrar_remision` — parámetros clave

| Parámetro | Descripción |
|-----------|-------------|
| `tipo_transaccion` | `venta`, `separe`, `abono`, `garantia`, `cambio` |
| `factura_referencia` | Número de la REMISIÓN original que este abono/separé referencia |
| `fecha_entrega` | Fecha de entrega (DD/MM/AA). Activa el seguimiento de pedido. |
| `cedula` | OBLIGATORIA — campo CC/NIT de la planilla |
| `tipo_acarreo` | `llevar` / `recoger` — la casilla marcada con X en la planilla |

#### `listar_facturas_vencidas` — dos secciones

1. **Entregas vencidas sin confirmar**: `delivery_date < hoy` y `delivery_status ≠ confirmado/cancelado`
2. **Saldos pendientes**: `raw_ocr->resta > 0`

### 9.4 Seguimiento de pedidos — `src/agent/tools/orders.py`

- `listar_pedidos_pendientes(fecha="")` — lista pedidos pendientes/en_ruta/demorados; acepta `fecha="hoy|mañana|pasado|DD/MM/YYYY"`
- `ver_seguimiento_pedido(numero_factura)` — estado detallado de un pedido
- `confirmar_entrega(numero_factura, notas="")` — marca como confirmado
- `actualizar_pedido(numero_factura, nuevo_status, notas, nueva_fecha_entrega)` — cambia estado

Estados de delivery: `sin_fecha | pendiente | en_ruta | entregado | confirmado | demorado | cancelado`

### 9.5 Recordatorios — `src/agent/tools/reminders.py`

- `get_datetime` — fecha/hora actual en Bogotá
- `crear_recordatorio`
- `listar_recordatorios`
- `cancelar_recordatorio`

---

## 10. Tools totales registradas

**28 tools en `ALL_TOOLS`** (graph.py):

```
casos:      escalar_garantia, escalar_cotizacion, actualizar_caso, buscar_caso,
            ver_caso, listar_casos_pendientes, generar_mensaje_seguimiento, adjuntar_evidencia
clientes:   buscar_cliente, listar_clientes, crear_cliente, ver_historial_cliente, agregar_nota_cliente
facturas:   registrar_remision, listar_facturas, listar_facturas_vencidas,
            buscar_factura, buscar_abonos_huerfanos
pedidos:    listar_pedidos_pendientes, ver_seguimiento_pedido, confirmar_entrega, actualizar_pedido
recorda.:   get_datetime, crear_recordatorio, listar_recordatorios, cancelar_recordatorio
```

---

## 11. Plugin MCP (OpenClaw)

Archivos (siempre mantener sincronizados):
- `~/.openclaw/plugins/norena-mcp/index.js`
- `~/.openclaw/extensions/norena-mcp/index.js`

**25 tools nativas registradas:**

```
norena_listar_clientes       norena_buscar_cliente        norena_crear_cliente
norena_registrar_remision    norena_escalar_garantia      norena_escalar_cotizacion
norena_ver_caso              norena_listar_casos          norena_actualizar_caso
norena_adjuntar_evidencia    norena_generar_seguimiento   norena_buscar_caso
norena_ver_historial_cliente norena_agregar_nota_cliente  norena_crear_recordatorio
norena_listar_recordatorios  norena_cancelar_recordatorio norena_buscar_factura
norena_listar_facturas       norena_facturas_vencidas     norena_abonos_huerfanos
norena_listar_pedidos_pendientes  norena_ver_seguimiento  norena_confirmar_entrega
norena_actualizar_pedido
```

Todas llaman internamente a `POST /v1/process` con un mensaje de lenguaje natural. El LangGraph del backend ejecuta la lógica real.

Al modificar el plugin, **siempre copiar** a extensions:
```bash
cp ~/.openclaw/plugins/norena-mcp/index.js ~/.openclaw/extensions/norena-mcp/index.js
```

---

## 12. Memoria y estado conversacional

Archivo: `src/memory.py`

### 12.1 Redis

Dos claves por chat:

```text
chat:{chat_id}        — buffer conversacional corto
chat_state:{chat_id}  — estado operativo
```

Campos de `chat_state` relevantes:
- `active_case_number`
- `pending_evidence_case`
- `pending_evidence_requested_at`
- `pending_audio_evidence`
- `pending_audio_capture_intent`
- `pending_audio_capture_requested_at`
- `last_audio_transcript`

`/v1/reset` borra ambas claves.

### 12.2 Qdrant + Cohere

- Qdrant guarda memoria semántica vectorial.
- Cohere genera embeddings y hace rerank.
- Si Qdrant falla, el sistema sigue degradado pero no cae.

---

## 13. Audio

Archivo: `src/audio.py`

### 13.1 Proveedor STT

- **Proveedor**: Gemini
- **Modelo**: `gemini-2.5-flash`
- Motivo: OpenRouter `chat.completions + input_audio` obedecía el audio en vez de transcribirlo.

### 13.2 Flujo de decisión en `preprocess_incoming_message()`

```text
1. cargar chat_state
2. si espero número de caso para audio y llega GAR-0001 → guardar pending_evidence_case
3. si el texto pide captura de audio → pedir número o audio según estado
4. si hay pending_audio_evidence y llega GAR-0001 → adjuntar audio al caso
5. si no trae audio → no hacer nada especial
6. si trae audio → transcribir → reclasificar → responder/adjuntar
```

---

## 14. OCR

Archivo: `src/ocr.py`

### 14.1 Dos tipos de planilla física

El sistema distingue dos formularios reales de la mueblería:

| Tipo | `form_type` | Descripción |
|------|-------------|-------------|
| **PLAN ABONOS** | `"plan_abonos"` | Sin tabla de productos. Usado para separé, abono, garantía, cambio. Tiene campo "N° Factura" para referenciar la venta original. |
| **REMISIÓN** | `"remision"` | Con tabla de productos, acarreos, ayudantes, sección de crédito. Usado para ventas directas. |

### 14.2 Números en PLAN ABONOS

El formulario tiene **dos números distintos**:
- `numero_formulario` — ID propio del talonario (esquina superior derecha, impreso)
- `numero_factura_ref` — N° Factura escrito a mano, referencia a la REMISIÓN de venta original

### 14.3 Tipo de transacción

`tipo_transaccion`: `venta | separe | abono | garantia | cambio`

- En REMISIÓN: default `"venta"` salvo que el campo "Plan Separé" esté marcado.
- En PLAN ABONOS: se infiere del campo Observaciones.

### 14.4 Vinculación abono → remisión original

Cuando se sube un PLAN ABONOS:
1. OCR extrae `numero_factura_ref`.
2. El sistema busca en DB si existe una `Invoice` con ese número.
3. Si existe → `parent_invoice_number` apunta a ella (vinculada ✅).
4. Si no existe → se guarda igual como "huérfana" ⚠️ (puede ser de otro vendedor).

---

## 15. Base de datos

Modelos: `src/models/models.py`

Entidades:
- `Client`
- `Case`
- `CaseUpdate`
- `Invoice`
- `Reminder`
- `InteractionLog`

### 15.1 Invoice — campos de seguimiento de entrega

```python
delivery_date              # Fecha prometida de entrega
delivery_status            # sin_fecha | pendiente | en_ruta | entregado | confirmado | demorado | cancelado
delivery_notes             # Notas de seguimiento (timestampeadas)
delivery_alert_sent_at     # Timestamp: alerta enviada 3 días hábiles antes
delivery_day_notified_at   # Timestamp: notificación enviada el día de entrega
delivery_followup_sent_at  # Timestamp: seguimiento enviado al día siguiente
delivery_overdue_alert_at  # Timestamp: alerta de vencimiento enviada
```

### 15.2 Invoice — campos de tipo y vinculación

```python
invoice_type           # venta | separe | abono | garantia | cambio
parent_invoice_number  # Número de la REMISIÓN original (para abonos/separés)
```

### 15.3 Migraciones disponibles

```
scripts/migrate_add_invoice_type.sql         — columna invoice_type
scripts/migrate_add_parent_invoice.sql       — columna parent_invoice_number
scripts/migrate_add_order_tracking.sql       — columnas delivery_*
scripts/migrate_delivery_notifications.sql   — columnas delivery_day_notified_at + delivery_followup_sent_at
```

---

## 16. Scheduler y Celery

Archivos:
- `src/tasks/celery_app.py` — app + beat schedule
- `src/tasks/scheduler.py` — lógica de cada tarea

Worker síncrono: usa `SyncSessionFactory` (no `asyncpg`).

### 16.1 Tareas y frecuencias

| Beat key | Task | Frecuencia | Descripción |
|----------|------|-----------|-------------|
| `morning-digest` | `morning_digest` | 8h (28800s) | Resumen diario 8am Bogotá: casos activos, entregas próximas, vencidas, recordatorios del día |
| `check-stale-cases` | `check_stale_cases` | 1h (3600s) | Alerta casos sin actividad |
| `dispatch-reminders` | `dispatch_reminders` | 1min (60s) | Despacha recordatorios del usuario que ya vencieron |
| `retry-pending-ocr` | `retry_pending_ocr` | 5min (300s) | Reintenta OCR de facturas con status `pending_ocr` |
| `check-upcoming-deliveries` | `check_upcoming_deliveries` | 6h (21600s) | Alerta pedidos con entrega en los próximos 3 días hábiles |
| `check-overdue-deliveries` | `check_overdue_deliveries` | 6h (21600s) | Alerta pedidos cuya fecha de entrega ya venció |
| `check-delivery-day` | `check_delivery_day` | 1h (3600s) | Notifica cuando HOY es el día de entrega de un pedido |
| `check-delivery-followup` | `check_delivery_followup` | 2h (7200s) | Al día siguiente de la entrega, pregunta si el cliente recibió |

### 16.2 Idempotencia

Cada alerta usa un campo timestamp en `Invoice` para no enviarse dos veces:
- `delivery_alert_sent_at` — guard de `check_upcoming_deliveries`
- `delivery_day_notified_at` — guard de `check_delivery_day`
- `delivery_followup_sent_at` — guard de `check_delivery_followup`
- `delivery_overdue_alert_at` — guard de `check_overdue_deliveries`

### 16.3 Qué hace `check_delivery_followup`

Al día siguiente de la fecha prometida, envía por Telegram:
```
❓ Verificar entrega — #XXXX

👤 Cliente  📱 Tel
🛋️ Producto
📅 Fecha programada: DD/MM/YYYY

¿El cliente ya recibió el pedido?
• Si SÍ recibió → confirmar entrega #XXXX
• Si NO recibió → actualizar #XXXX demorado [motivo]
```

---

## 17. Docker y runtime local

Archivos:
- `docker-compose.yml`
- `docker-compose.override.yml`

Servicios:

| Servicio | Descripción |
|----------|-------------|
| `backend` | FastAPI + LangGraph |
| `postgres` | Base de datos principal |
| `redis` | Chat buffer + Celery broker |
| `qdrant` | Memoria semántica vectorial |
| `celery-worker` | Ejecuta tasks |
| `celery-beat` | Dispara tasks según schedule |
| `pg-backup` | Backup automático de PostgreSQL |

---

## 18. OpenClaw: cómo se conecta todo

### 18.1 Flujo modo OpenClaw

```text
Usuario Telegram
  -> OpenClaw canal Telegram
  -> agente principal OpenClaw
  -> scripts/send_to_backend.py
  -> POST /v1/process
  -> backend LangGraph
  -> OpenClaw
  -> Telegram
```

### 18.2 Skills de OpenClaw

Carpeta: `openclaw-config/skills/`

Cada SKILL.md usa `curl` o el script `send_to_backend.py` contra el backend.

### 18.3 Scripts auxiliares

- `scripts/send_to_backend.py` — reenvía mensaje crudo a `/v1/process` (evita errores de quoting con media)
- `scripts/reset_backend_chat.py` — alinea `/reset` de OpenClaw con el estado en Redis

---

## 19. Flujos de negocio estabilizados

### 19.1 Ver caso

```text
"dame detalles de ese caso"
  -> router: fast_action ver_caso
  -> ficha completa del caso
```

### 19.2 Buscar factura

```text
"hay facturas de García"
  -> router: fast_action buscar_factura("García")
  -> SQL tolerante a acentos
```

### 19.3 Facturas vencidas

```text
"hay facturas vencidas?"
  -> recall: salta búsqueda semántica (señal determinística)
  -> router: fast_action listar_facturas_vencidas
  -> Sección 1: entregas sin confirmar con fecha pasada
  -> Sección 2: facturas con resta > 0
```

### 19.4 Seguimiento de pedido

```text
"hay algo para entregar mañana?"
  -> agente: listar_pedidos_pendientes(fecha="mañana")
  -> lista filtrada por fecha exacta

"confirmar entrega #3600"
  -> agente: confirmar_entrega(numero_factura="3600")
  -> delivery_status = "confirmado"
```

### 19.5 Abono con vinculación

```text
Foto de PLAN ABONOS #0694 con "N° Factura: 4156"
  -> OCR extrae numero_formulario=0694, numero_factura_ref=4156
  -> Busca Invoice #4156 en DB
  -> Si existe: parent_invoice_number="4156" ✅
  -> Si no:     parent_invoice_number="4156" ⚠️ huérfano
```

### 19.6 OCR remisión completa

```text
Foto de REMISIÓN
  -> form_type="remision"
  -> Extrae productos, acarreo, ayudantes, crédito, fecha entrega
  -> Crea Invoice con delivery_date y delivery_status="pendiente"
```

### 19.7 Crear recordatorio

```text
"recuérdame en 2 minutos..."
  -> tool crear_recordatorio
  -> DB Reminder
  -> Celery beat dispatch_reminders (corre cada minuto)
  -> Telegram
```

### 19.8 Audio conversacional

```text
audio: "cuántas garantías hay"
  -> Gemini transcribe
  -> texto entra al grafo normal
  -> respuesta
```

### 19.9 Audio de evidencia

```text
"quiero agregar un audio al caso"
  -> pending_audio_capture_intent
usuario: "GAR-0001"
  -> pending_evidence_case = GAR-0001
  -> bot pide audio real
audio real
  -> Gemini transcribe
  -> resumir
  -> adjuntar_evidencia(GAR-0001, ...)
```

---

## 20. Convenciones de desarrollo

- `SyncSessionFactory` para Celery (tasks síncronas).
- `AsyncSessionFactory` para FastAPI/tools (async).
- Números de caso: `GAR-XXXX` (garantía) / `COT-XXXX` (cotización).
- Nunca auto-commit git sin pedirlo.
- Al modificar el plugin MCP, siempre copiar a extensions también.
- `INTERNAL_API_KEY` protege todos los endpoints internos.

---

## 21. Limitaciones actuales

1. Qdrant puede degradar — el sistema sigue sin memoria semántica rica.
2. `resta` vive en `raw_ocr` (JSONB), no es columna directa — filtrar en Python.
3. No hay festivos colombianos en el cálculo de días hábiles.
4. El flujo de audio puede mejorarse: aceptar "quiero agregar audio al caso GAR-0001" en un solo paso.
5. No hay tests automatizados.

---

## 22. Archivos clave para moverse rápido

1. `WORKSPACE_EXPLICADO.md`
2. `src/main.py`
3. `src/agent/graph.py`
4. `src/agent/nodes/agent.py`
5. `src/agent/nodes/router.py`
6. `src/agent/tools/invoices.py`
7. `src/agent/tools/orders.py`
8. `src/ocr.py`
9. `src/tasks/scheduler.py`
10. `src/models/models.py`
