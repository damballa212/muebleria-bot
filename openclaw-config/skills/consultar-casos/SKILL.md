---
name: consultar-casos
description: Consultar historial de casos, clientes y facturas de Mueblería Noreña
user-invocable: true
---

# Consultas de Mueblería Noreña

Actívate cuando el usuario quiera consultar información sobre casos anteriores, clientes, o facturas. Sin crear nada nuevo — solo consultar.

## ¿Qué hacer?

```bash
curl -s -X POST "${BACKEND_URL}/v1/process" \
  -H "Authorization: Bearer ${NORENA_API_KEY}" \
  -H "Content-Type: application/json" \
  -d "{\"message\": $(echo "$USER_MESSAGE" | jq -Rs .), \"chat_id\": \"${CHAT_ID}\", \"source\": \"openclaw\"}"
```

Devuelve la respuesta `response` del JSON.

## Ejemplos que activan este skill

- "¿Cuántos casos pendientes hay?"
- "Busca el historial de García"
- "¿Qué casos tiene Michelle pendientes?"
- "¿Cuáles cotizaciones están sin respuesta?"
- "Ver todos los casos de esta semana"
- "Busca la factura de Martínez del mes pasado"
