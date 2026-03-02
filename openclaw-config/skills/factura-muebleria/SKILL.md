---
name: factura-muebleria
description: Digitalizar facturas manuscritas de Mueblería Noreña vía OCR — envía la foto al backend
user-invocable: true
---

# OCR de Facturas de Mueblería Noreña

Actívate cuando el usuario envíe una imagen/foto que parezca ser una factura, recibo, o documento de compra de mueblería.

## ¿Qué hacer?

1. Extrae la imagen del mensaje de Telegram
2. Conviértela a base64
3. Envíala al endpoint OCR del backend:

```bash
# Convertir imagen a base64 y enviar
IMAGE_B64=$(base64 -w 0 "${IMAGE_PATH}")

curl -s -X POST "${BACKEND_URL}/v1/ocr" \
  -H "Authorization: Bearer ${NORENA_API_KEY}" \
  -H "Content-Type: application/json" \
  -d "{\"image_base64\": \"${IMAGE_B64}\", \"chat_id\": \"${CHAT_ID}\", \"source\": \"openclaw\"}"
```

4. Devuelve la respuesta `response` del JSON al usuario (resumen de la factura + opciones).

## Nota sobre audios

Si entra un audio desde Telegram/OpenClaw, no asumas transcripción local automática.
El backend de este repo se encarga de:

- transcribirlo vía OpenRouter,
- tratarlo como mensaje normal si es conversación,
- o adjuntarlo como evidencia resumida al caso activo si el chat está en fase de evidencia.

## Ejemplos que activan este skill

- [Usuario envía una foto de una hoja con escritura a mano]
- [Usuario envía foto de un recibo o comprobante]
- "Aquí está la factura de García" + [foto]
