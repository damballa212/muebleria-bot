---
name: garantia-muebleria
description: Gestionar garantías de Mueblería Noreña — escalar casos a Michelle, actualizar estados, ver historial
user-invocable: true
---

# Garantías de Mueblería Noreña

Actívate cuando el usuario mencione: garantía, garantia, Michelle, reclamo, problema con mueble, caso de servicio, producto dañado, daño, defecto, posventa.

## ¿Qué hacer?

1. Extrae el mensaje completo del usuario
2. Ejecuta el siguiente comando para enviarlo al backend:

```bash
curl -s -X POST "${BACKEND_URL}/v1/process" \
  -H "Authorization: Bearer ${NORENA_API_KEY}" \
  -H "Content-Type: application/json" \
  -d "{\"message\": $(echo "$USER_MESSAGE" | jq -Rs .), \"chat_id\": \"${CHAT_ID}\", \"source\": \"openclaw\"}"
```

3. Extrae el campo `response` del JSON retornado y envíalo al usuario en Telegram.

## Ejemplos que activan este skill

- "Michelle aprobó la garantía de García"
- "El caso GAR-0012 ya fue resuelto"
- "El sofá de Pérez tiene el resorte roto, escala a Michelle"
- "¿En qué estado está el caso de Rodríguez?"
- "Actualiza el GAR-0008 como resuelto, Michelle dijo que sí"
