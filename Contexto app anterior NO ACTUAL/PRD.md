# ARCHIVADO — No corresponde al sistema actual de este repo

Este documento describe una arquitectura anterior basada en Odoo, Chatwoot y Evolution API.
Se conserva solo como referencia histórica. El sistema vigente del workspace está documentado en
`README.md` y `plan.md`.

# PRD – MVP Sistema Comercial y Garantías
## Mueblería Noreña – Infraestructura Local
**Versión:** 2.0  
**Fecha:** 18 de febrero de 2026  
**Stack Principal:** Python 3.12+, PostgreSQL 16+, Docker  
**Arquitectura:** VPS (Hostinger KVM2 + Dokploy)

---

# 1. Resumen Ejecutivo

Mueblería Noreña actualmente opera con:

- WhatsApp como canal principal de comunicación
- Registros físicos/manuales de ventas y clientes
- Reclamos no estructurados (verbales, por WhatsApp, sin tracking)
- Seguimiento comercial informal

Problemas críticos:

- Reclamos olvidados o sin respuesta
- Técnicos no asignados a visitas
- Leads sin seguimiento ni conversión medida
- Cero métricas estructuradas
- Falta de trazabilidad histórica por cliente
- Sin evidencia fotográfica de garantías

Este MVP implementará:

- Centralización de conversaciones de WhatsApp
- CRM básico estructurado con pipeline de ventas
- Sistema formal de garantías y reclamos con SLA
- Métricas en tiempo real
- Almacenamiento de evidencia multimedia
- Motor de automatizaciones con tareas programadas
- Infraestructura en VPS con despliegue vía Dokploy

---

# 2. Arquitectura del Sistema

## 2.1 Componentes

### WhatsApp Gateway
- Evolution API (self-hosted)
- Conexión vía QR (WhatsApp Web)
- Webhooks hacia Chatwoot
- Filtrado: solo mensajes individuales (ignora grupos)

### Inbox
- Chatwoot (self-hosted)
- Gestión de conversaciones
- Asignación a agentes
- Etiquetas (incluyendo "garantia")
- Webhooks hacia middleware
- **Requiere:** Redis (obligatorio)

### Backend CRM + Helpdesk
- Odoo Community 18 (self-hosted, LGPL)
- Módulos nativos:
  - CRM (Leads y Pipeline)
  - Contacts
- Módulos OCA (gratuitos, open-source):
  - `helpdesk_mgmt` — Gestión de tickets
  - `helpdesk_mgmt_sla` — SLAs con tiempos de respuesta/resolución
  - `helpdesk_mgmt_crm` — Vinculación tickets ↔ leads
- Integración vía: **XML-RPC** (protocolo nativo de Odoo)

### Middleware (Desarrollo propio)
- Python 3.12+
- FastAPI
- SQLAlchemy
- Celery + Redis (tareas programadas y alertas)
- Servicio HTTP stateless + workers async
- Webhooks entrantes/salientes
- Lógica de negocio y orquestación

### Almacenamiento Multimedia
- Directorio local montado en Docker: `/data/attachments/`
- Fotos de evidencia de garantías (muebles dañados)
- Límite: 10 MB por archivo, formatos: JPG, PNG, PDF
- Referencia en ticket vía path relativo

### Base de Datos
- PostgreSQL 16+
- Bases separadas:
  - `chatwoot_db` — Propiedad de Chatwoot
  - `odoo_db` — Propiedad de Odoo (fuente de verdad para Leads, Tickets, Contactos)
  - `middleware_db` — Cache, logs, configuración del middleware
- **Fuente de verdad:** Odoo (`odoo_db`) para toda entidad de negocio
- `middleware_db` solo almacena: logs de webhooks, cola de tareas, configuración interna

---

# 2.2 Diagrama de Flujo

```
Cliente
  → WhatsApp (mensaje individual)
    → Evolution API
      → Chatwoot (inbox, etiquetas, agente)
        → Webhook POST /webhook/chatwoot
          → Middleware (FastAPI)
            → Odoo (XML-RPC)
              ├── CRM: crear/actualizar Lead
              └── Helpdesk: crear Ticket (si aplica)
            → Celery Worker (tareas async)
              ├── Alertas por inactividad
              ├── Notificaciones a gerencia
              └── Sincronización de estados
```

---

# 3. Infraestructura Técnica

## 3.1 Servidor (Hostinger VPS KVM2)

- **Plan:** KVM2
- **SO:** Ubuntu Server 24.04 LTS
- **RAM:** 8 GB
- **CPU:** 2 vCPU
- **Storage:** 100 GB NVMe SSD
- **Bandwidth:** 8 TB/mes
- **Deploy:** Dokploy (gestión de contenedores)
- Docker + Docker Compose (gestionado por Dokploy)
- IP dedicada incluida

## 3.2 Servicios Docker

| Servicio | Imagen | Puerto |
|----------|--------|--------|
| evolution-api | evolution-api | 8080 |
| chatwoot | chatwoot/chatwoot | 3000 |
| odoo | odoo:18 | 8069 |
| postgresql | postgres:16 | 5432 |
| middleware | custom (FastAPI) | 8000 |
| celery-worker | custom (mismo imagen middleware) | — |
| celery-beat | custom (scheduler) | — |
| redis | redis:7-alpine | 6379 |
| nginx | nginx:alpine | 80/443 |

## 3.3 Backups

- `pg_dump` automático diario para cada base (chatwoot_db, odoo_db, middleware_db)
- Rotación de 14 días
- Backup incremental de `/data/attachments/`
- Script de backup en cron, salida registrada en logs
- **Limitación MVP:** No incluye WAL archiving ni PITR

## 3.4 Monitoreo (MVP)

- Healthcheck HTTP en cada contenedor Docker
- Script cron cada 5 min que verifica:
  - Estado de cada contenedor (`docker ps`)
  - Conectividad de Evolution API (sesión WhatsApp activa)
  - Conectividad PostgreSQL
- Alerta por correo o WhatsApp (vía Evolution API) si un servicio cae
- Logs estructurados (JSON) en todos los servicios

---

# 4. Integración Middleware ↔ Odoo

## 4.1 Protocolo

- **XML-RPC** (nativo de Odoo, no requiere módulos adicionales)
- Endpoints Odoo: `http://odoo:8069/xmlrpc/2/common` y `http://odoo:8069/xmlrpc/2/object`
- Librería Python: `xmlrpc.client` (stdlib)

## 4.2 Autenticación

- Usuario de servicio dedicado en Odoo: `middleware_bot`
- API Key o contraseña almacenada en variable de entorno `ODOO_API_PASSWORD`
- Permisos: lectura/escritura en CRM, Helpdesk y Contacts

## 4.3 Modelos Odoo Consumidos

| Modelo Odoo | Uso | Operaciones |
|-------------|-----|-------------|
| `res.partner` | Contactos/Clientes | search, create, write |
| `crm.lead` | Leads comerciales | search, create, write |
| `helpdesk.ticket` (OCA) | Tickets de garantía | search, create, write |
| `helpdesk.ticket.team` (OCA) | Equipos técnicos | search, read |
| `helpdesk.ticket.stage` (OCA) | Estados de tickets | search, read |
| `helpdesk.ticket.category` (OCA) | Categorías de issue | search, read |

## 4.4 Mapeo de Campos

### Contacto (`res.partner`)

| Campo PRD | Campo Odoo | Tipo |
|-----------|------------|------|
| phone | `phone` o `mobile` | char |
| name | `name` | char |
| source | `comment` o custom field | char |

### Lead (`crm.lead`)

| Campo PRD | Campo Odoo | Tipo |
|-----------|------------|------|
| contact_id | `partner_id` (FK) | many2one |
| phone | `phone` | char |
| product_interest | `description` o custom field | text |
| estimated_budget | `expected_revenue` | float |
| source | `source_id` | many2one |
| assigned_user | `user_id` | many2one |
| stage | `stage_id` | many2one |
| created_at | `create_date` (auto) | datetime |
| updated_at | `write_date` (auto) | datetime |

### Ticket (`helpdesk.ticket` — OCA)

| Campo PRD | Campo Odoo | Tipo |
|-----------|------------|------|
| customer_id | `partner_id` (FK) | many2one |
| related_lead_id | Vía `helpdesk_mgmt_crm` | many2one |
| phone | `partner_id.phone` | related |
| product | `description` o custom field | char |
| issue_type | `category_id` | many2one |
| priority | `priority` | selection (0-3) |
| technician_id | `user_id` | many2one |
| visit_date | Custom field o actividad programada | date |
| status | `stage_id` | many2one |
| description | `description` | html |
| resolution_notes | Campo custom o nota interna | text |

---

# 5. Alcance Funcional MVP

---

# 5.1 Gestión de Leads (CRM Básico)

## Objetivo
Registrar automáticamente cada contacto nuevo de WhatsApp y permitir seguimiento estructurado en Odoo CRM.

## Creación automática

Cuando un contacto escribe por primera vez:

Middleware debe (vía XML-RPC a Odoo):

1. Buscar contacto en `res.partner` por `phone` normalizado
2. Si no existe → `create` en `res.partner`
3. Crear Lead en `crm.lead` asociado al `partner_id`

## Normalización de teléfono

- Formato estándar: `+57XXXXXXXXXX` (Colombia)
- Remover espacios, guiones, paréntesis
- Función `normalize_phone()` ejecutada antes de cualquier búsqueda

## Pipeline CRM (Stages en Odoo)

| Stage | Nombre Display | Descripción |
|-------|---------------|-------------|
| new | Nuevo | Contacto recién recibido |
| contacted | Contactado | Se ha respondido al cliente |
| quotation_sent | Cotización Enviada | Se envió presupuesto |
| follow_up | Seguimiento | Requiere recontacto |
| won | Ganado | Venta cerrada |
| lost | Perdido | Oportunidad descartada |

## Automatizaciones (ejecutadas por Celery Beat)

| Condición | Acción | Timer |
|-----------|--------|-------|
| stage = `quotation_sent` + 3 días sin `write_date` | Notificación al vendedor | Cada 24h |
| Cualquier stage + 7 días sin `write_date` | Mover a `follow_up` | Cada 24h |
| Cualquier stage + 30 días sin `write_date` | Marcar como `lost` | Cada 24h |

---

# 5.2 Gestión de Garantías y Reclamos (CRÍTICO)

## Objetivo
Eliminar reclamos olvidados y garantizar trazabilidad total con SLA medibles.

## Creación de Ticket

Se crea ticket en Odoo (`helpdesk.ticket`) cuando:

1. Chatwoot envía webhook con etiqueta: `garantia`, **O**
2. Middleware detecta intención de reclamo en el mensaje

## Detección de Intención (MVP)

Método: Regex + co-ocurrencia de keywords (no keyword individual).

Reglas:
- Match si el mensaje contiene **2+ keywords** de la lista: `garantia, reclamo, dañado, roto, defecto, falla, arreglo, visita técnica, se rompió, no sirve, no funciona`
- Match si contiene **1 keyword** + contexto de queja: `tengo un|mi .* (está|tiene|quedó) .* (dañado|roto|malo)`
- **NO** match si solo aparece una keyword aislada (ej: "no tengo problema" no genera ticket)
- Probabilidad baja → marcar conversación en Chatwoot como "revisar" para validación humana

Preparado para extender con LLM en fase 2.

# 5.3 Digitalización de Ventas (Facturas y Remisiones) - FASE 2

## Objetivo
Digitalizar el flujo de ventas físicas mediante OCR de las facturas/remisiones manuscritas, creando automáticamente órdenes de venta y movimientos contables en Odoo.

## Tipos de Documentos Identificados
1. **Remisión / Factura Horizontal**: Documento principal de entrega y venta final.
2. **Recibo Vertical (Plan Separe)**: Comprobante de abonos parciales.

## Estructura de Datos a Extraer

### Encabezado
- **Consecutivo (Nº)**: Identificador único (ej: 4100, 3552).
- **Fechas**: Fecha Contrato vs Fecha Entrega.
- **Cliente**: Nombre, C.C/NIT, Dirección, Teléfono.

### Detalle de Productos
- Cantidad, Descripción, Valor Total.
- **Notas manuscritas**.

### Logística y Totales
- **Acarreo**: Llevar/Recoger, Costo.
- **Ayudantes**: Cantidad, Costo, Nombres.
- **Totales**: Subtotal, Abonos, Resta.

## Integración Odoo
- **Remisión** → `sale.order`.
- **Abono** → `account.payment`.

---

# 6. Fase 3: Inteligencia Operativa (Agentes + Memoria)

## Concepto
Implementación de una arquitectura agéntica (**LangGraph**) con memoria semántica (**Memora/Qdrant**) para actuar como un "Secretario Virtual Operativo".

> [!IMPORTANT]
> **Regla de Oro**: El agente **NUNCA** responde automáticamente al cliente. Su función es preparar borradores, crear registros y alertar al humano.

## Arquitectura
1.  **Orquestador (LangGraph)**:
    - Recibe webhooks de Chatwoot.
    - Ciclo: `Input` → `Clasificación` → `Consulta Memoria` → `Ejecución Herramientas` → `Fin`.
    - **No** requiere loop conversacional complejo.

2.  **Memoria (Memora + Qdrant)**:
    - Almacena historial semántico del cliente (ej: "Cliente frecuente con problemas de garantía").
    - Permite priorización basada en el "sentimiento" histórico.

3.  **Herramientas (Tools)**:
    - `ChatwootAPI`: Etiquetar, añadir notas privadas.
    - `OdooAPI`: Crear Leads, Tickets, buscar info.
    - `OCR`: Digitalizar documentos adjuntos.

## Flujo de Trabajo
1.  **Recepción**: Mensaje entra a Chatwoot.
2.  **Análisis**: Agente clasifica intención (Venta, Garantía, Duda).
3.  **Contexto**: Consulta Memora (¿Es reincidente? ¿VIP?).
4.  **Acción**: Ejecuta en Odoo (crea ticket) y Chatwoot (etiqueta "Garantía", nota interna "Cliente molesto, 3er reclamo").
5.  **Entrega**: Asesor humano recibe el caso "masticado" y responde.

---

# 7. Consideraciones Técnicas Adicionales


| Stage | Nombre Display | ¿Cerrado? |
|-------|---------------|:---------:|
| new | Nuevo | No |
| under_review | En Revisión | No |
| visit_scheduled | Visita Programada | No |
| repairing | En Reparación | No |
| resolved | Resuelto | No |
| closed | Cerrado | Sí |

## SLA (vía `helpdesk_mgmt_sla`)

| Regla | Tiempo | Acción |
|-------|--------|--------|
| Ticket sin cambio de stage | 48 horas | Alerta automática al técnico y supervisor |
| Ticket abierto | 5 días | Alerta a gerencia |
| Ticket `resolved` sin `resolution_notes` | — | Bloquear paso a `closed` (validación en middleware) |

## Reglas Obligatorias

- Todo ticket **debe** tener `user_id` (técnico) asignado
- Asignación automática: método **balanced** (al técnico con menos tickets abiertos)
- No se puede cerrar sin `resolution_notes`
- Evidencia fotográfica obligatoria para `issue_type` = daño físico

---

# 6. Middleware – Especificación Técnica

## 6.1 Endpoints Entrantes

| Método | Ruta | Fuente | Rate Limit |
|--------|------|--------|:----------:|
| POST | `/webhook/chatwoot` | Chatwoot | 100 req/min |
| POST | `/webhook/evolution` | Evolution API | 100 req/min |
| GET | `/health` | Monitoreo | — |

## 6.2 Rate Limiting

- Implementado con `slowapi` (basado en límites por IP)
- Si un webhook excede el límite → responder `429 Too Many Requests`
- Protección contra loops de mensajes entre servicios

## 6.3 Funciones Principales

| Función | Descripción |
|---------|-------------|
| `normalize_phone(raw)` | Normaliza teléfono a formato `+57XXXXXXXXXX` |
| `get_or_create_contact(phone, name)` | Busca/crea `res.partner` en Odoo vía XML-RPC |
| `create_lead(partner_id, data)` | Crea `crm.lead` en Odoo |
| `detect_intent(message)` | Regex + co-ocurrencia para detectar reclamo |
| `create_ticket(partner_id, lead_id, data)` | Crea `helpdesk.ticket` en Odoo |
| `assign_technician(ticket_id)` | Asigna técnico con menos carga (balanced) |
| `sync_status(entity, odoo_id, status)` | Actualiza estado en Odoo |
| `upload_attachment(ticket_id, file)` | Guarda foto en `/data/attachments/` y referencia en ticket |

## 6.4 Filtrado de Mensajes

- **Ignorar:** mensajes de grupo, mensajes del propio bot, mensajes vacíos
- **Procesar:** solo mensajes individuales entrantes con contenido

---

# 7. Roles y Permisos

| Permiso | Administrador | Vendedor | Técnico |
|---------|:-------------:|:--------:|:-------:|
| Acceso total al sistema | ✅ | ❌ | ❌ |
| Configuración del sistema | ✅ | ❌ | ❌ |
| Métricas completas | ✅ | ❌ | ❌ |
| Gestionar leads propios | ✅ | ✅ | ❌ |
| Ver tickets de sus clientes | ✅ | ✅ | ❌ |
| Ver tickets asignados | ✅ | ❌ | ✅ |
| Cambiar estado de ticket | ✅ | ❌ | ✅ |
| Añadir notas a ticket | ✅ | ✅ | ✅ |
| Cerrar tickets técnicos | ✅ | ❌ | ✅ |
| Eliminar registros | ✅ | ❌ | ❌ |

---

# 8. Métricas Obligatorias

## Comerciales (Odoo CRM Dashboard o custom)

- Leads por mes
- Tasa de cierre (won / total)
- Leads por vendedor
- Tiempo promedio de conversión (new → won)
- Leads por fuente (WhatsApp default)

## Garantías (OCA Helpdesk Dashboard)

- Tickets abiertos (por stage)
- Tickets por técnico
- Tiempo promedio de resolución (new → closed)
- Tickets por categoría de producto
- Clientes reincidentes (>1 ticket en 6 meses)
- Cumplimiento de SLA (% tickets dentro del tiempo)

---

# 9. Seguridad

- Autenticación JWT en endpoints del middleware
- HTTPS obligatorio (nginx reverse proxy con certificado local o Let's Encrypt)
- Logs estructurados (JSON) con rotación
- Backups automáticos diarios con rotación 14 días
- Control de acceso por rol en Odoo
- Variables sensibles en `.env` (nunca en código)
- Rate limiting en webhooks
- **Cumplimiento Ley 1581 de 2012** (Protección de datos personales - Colombia):
  - Consentimiento del cliente para almacenar conversaciones
  - Política de privacidad visible
  - Mecanismo de eliminación de datos bajo solicitud

---

# 10. No Incluido en MVP

- Inventario avanzado
- Módulo de producción
- Facturación electrónica (DIAN)
- Plan separe
- Créditos externos (Addi, Sistecrédito, Agaval)
- Automatización IA avanzada (LLM)
- Multi-sucursal
- App móvil custom
- WAL archiving / PITR para backups
- Integración contable

---

# 11. Criterios de Aceptación

Se considera exitoso el MVP si:

- [x] 100% de reclamos detectados se crean como ticket en Odoo
- [x] Ningún ticket queda sin técnico asignado (asignación automática)
- [x] Dashboard muestra métricas en tiempo real
- [x] Se puede consultar historial completo por cliente (leads + tickets)
- [x] No existen contactos duplicados por teléfono (normalización)
- [x] Alertas automáticas funcionan a 48h y 5 días
- [x] Middleware responde webhooks en < 2 segundos (p95)
- [x] Sistema se recupera tras reinicio de Docker sin pérdida de datos

---

# 12. Plan de Rollback

Si el sistema falla en producción:

1. Los vendedores continúan operando vía WhatsApp directo (sin Chatwoot)
2. Reclamos se registran manualmente en hoja de cálculo compartida
3. Restaurar desde backup más reciente de PostgreSQL
4. Chatwoot y Odoo se reinician vía `docker compose restart`
5. Si Evolution API pierde sesión → re-escanear QR

---

# 13. Roadmap Posterior

## Fase 2:

- Plan Separe (apartados con abonos)
- Créditos (Addi, Sistecrédito, Agaval)
- Inventario básico
- Integración contable
- IA para clasificación automática avanzada (LLM)
- Automatización de mensajes salientes programados
- App móvil para técnicos

---

# 14. Control de Versiones

Repositorio Git:

- `main` — producción estable
- `staging` — pruebas pre-producción
- `develop` — desarrollo activo

Deploy mediante Docker Compose.

CI básico: lint + tests antes de merge a `main`.

---

# Fin del Documento
