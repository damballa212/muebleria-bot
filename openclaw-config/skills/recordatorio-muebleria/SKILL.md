---
name: recordatorio-muebleria
description: Crear, listar y cancelar recordatorios personales para Mueblería Noreña
user-invocable: true
---

# Recordatorios de Mueblería Noreña

Actívate cuando el usuario quiera crear, ver o cancelar recordatorios laborales o personales relacionados con la mueblería.

## ¿Qué hacer?

```bash
curl -s -X POST "${BACKEND_URL}/v1/process" \
  -H "Authorization: Bearer ${NORENA_API_KEY}" \
  -H "Content-Type: application/json" \
  -d "{\"message\": $(echo "$USER_MESSAGE" | jq -Rs .), \"chat_id\": \"${CHAT_ID}\", \"source\": \"openclaw\"}"
```

Devuelve la respuesta `response` del JSON.

## Ejemplos que activan este skill

- "Recuérdame llamar a García mañana a las 10am"
- "Pon un recordatorio para el lunes de revisar el caso COT-0031"
- "¿Qué recordatorios tengo pendientes?"
- "Cancela el recordatorio de mañana a las 3pm"
- "Recuérdame a las 9am que hay reunión con Michelle"
