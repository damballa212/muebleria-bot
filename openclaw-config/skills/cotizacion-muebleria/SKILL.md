---
name: cotizacion-muebleria
description: Gestionar cotizaciones de Mueblería Noreña — escalar a Daniel, consultar precios y seguimiento
user-invocable: true
---

# Cotizaciones de Mueblería Noreña

Actívate cuando el usuario mencione: cotización, cotizacion, Daniel, Don Daniel, precio, presupuesto, cliente quiere saber el precio, consulta de precio, solicitud de producto.

## ¿Qué hacer?

1. Extrae el mensaje completo del usuario
2. Ejecuta el siguiente comando:

```bash
curl -s -X POST "${BACKEND_URL}/v1/process" \
  -H "Authorization: Bearer ${NORENA_API_KEY}" \
  -H "Content-Type: application/json" \
  -d "{\"message\": $(echo "$USER_MESSAGE" | jq -Rs .), \"chat_id\": \"${CHAT_ID}\", \"source\": \"openclaw\"}"
```

3. Devuelve la respuesta `response` del JSON al usuario.

## Ejemplos que activan este skill

- "Daniel cotizó el juego de sala en 2.5 millones"
- "Crea una cotización para Martínez, quiere una cama doble"
- "¿Cuál es el estado de la cotización COT-0031?"
- "Don Daniel va a responder mañana sobre el precio del sofá"
