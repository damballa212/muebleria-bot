"""OCR de facturas manuscritas via Claude Vision (OpenRouter)."""
import base64
import logging
import uuid
from datetime import datetime
from pathlib import Path

from src.config import settings
from src.llm import LLMError, call_llm_with_vision

logger = logging.getLogger(__name__)

OCR_PROMPT = """Eres un experto en leer los documentos físicos de Mueblería Noreña S.A.S (Caldas, Antioquia).
Existen DOS tipos de planilla distintas. Primero identifica cuál es:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PLANILLA TIPO A — "PLAN ABONOS / SEPARÉ"
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Reconócela porque dice "PLAN ABONOS" y/o "SEPARÉ" en la parte superior.
Es una planilla sencilla, SIN tabla de productos ni sección de acarreos.

ATENCIÓN — esta planilla tiene DOS números distintos:

  1. numero_formulario: El número impreso en la esquina superior derecha
     (Ej: "N° 0694"). Es el ID físico de este formulario específico.
     Este número va en el campo "numero_formulario".

  2. numero_factura_ref: El número escrito a mano en el campo "N° Factura:"
     (Ej: "4156"). Este es el número de la REMISIÓN/venta original
     a la cual se está haciendo este pago o separé.
     Este número va en el campo "numero_factura_ref".

Ambos números son importantes. No los confundas.

Campos a extraer:
- Nombre del cliente (campo "Nombre:")
- Celular (campo "Celular:")
- numero_formulario: número impreso del formulario (esquina superior derecha)
- numero_factura_ref: número escrito en campo "N° Factura:" (referencia a la venta)
- Abono (monto pagado + método de pago, ej: "50.000 Transferencia")
- Resta (monto pendiente)
- Fecha de Entrega (campo "Fecha de Entrega")
- Asesor
- C.C. (cédula del cliente)
- OBSERVACIONES: texto libre escrito a mano. Aquí se anotan los productos
  y el motivo (separé, garantía, cambio, etc.)
- Checkboxes al pie: RECOGER □ LLEVAR □ — cuál tiene la X marcada

tipo_transaccion: inferir del texto en Observaciones:
  - Si dice "separé", "separ" o menciona productos que el cliente va a retirar después → "separe"
  - Si dice "abono", "pago", "cuota" → "abono"
  - Si dice "garantía", "daño", "problema" → "garantia"
  - Si dice "cambio", "devolu" → "cambio"
  - Default → "separe" (es la planilla más usada para reservas)

Los "items" de la planilla tipo A se extraen de OBSERVACIONES (no hay tabla):
  - Separa los productos mencionados en la lista de observaciones
  - precio_unitario = 0 (no aparece en esta planilla)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PLANILLA TIPO B — "REMISIÓN"
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Reconócela porque dice "REMISIÓN" en la parte superior derecha, con un N° de remisión.
Es la planilla completa para ventas, con tabla de productos y secciones detalladas.

Campos a extraer:
- N° de remisión (número grande en la esquina)
- Fecha de Contrato (fecha de la venta) → fecha_compra
- Fecha de Entrega
- Nombre del cliente
- C.C./NIT
- Dirección
- Tel/Cel
- Tabla de productos: cada fila tiene CANT | DESCRIPCIÓN | Vr. TOTAL
- ACARREOS: monto del flete + cuál checkbox tiene X:
    Llevar □ = entrega a domicilio → tipo_acarreo: "llevar"
    Recoger □ = cliente recoge → tipo_acarreo: "recoger"
- AYUDANTES: cuántos tiene marcados (Uno/Dos) → cada uno vale $20.000
- Crédito: entidad (Agaval/Addi/Sistecredito/otra), # cuotas, valor cuota,
  frecuencia (Mes/Quince), Total crédito
  - Si "Crédito Inicial" tiene un nombre distinto al cliente → credito_a_nombre_de
- SUB TOTAL → total (suma de productos, sin acarreo ni ayudantes)
- ABONO
- RESTA
- Firma del cliente → firmada: true si hay firma visible
- Al final: "plan separé X meses..." si está marcado → tipo_transaccion: "separe", si no → "venta"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RESPUESTA
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Responde SOLO con este JSON (usa null para campos no visibles):
{
  "form_type": "plan_abonos o remision",
  "numero_formulario": "número impreso del formulario (esquina superior derecha). En plan_abonos: el N° del talonario. En remision: el N° de remisión.",
  "numero_factura_ref": "SOLO para plan_abonos: el número escrito a mano en el campo 'N° Factura' (referencia a la venta original). En remision: null.",
  "nombre": "nombre completo del cliente",
  "telefono": "solo dígitos, sin espacios",
  "cedula": "cédula del cliente o null",
  "direccion": "dirección de entrega o null",
  "items": [
    {"descripcion": "nombre del producto", "cantidad": 1, "precio_unitario": 0}
  ],
  "total": 0,
  "abono": 0,
  "resta": 0,
  "acarreo": 0,
  "ayudantes": 0,
  "fecha_compra": "YYYY-MM-DD o null",
  "fecha_entrega": "DD/MM/YYYY o null",
  "firmada": false,
  "tipo_acarreo": "llevar o recoger",
  "asesor": "nombre del asesor o null",
  "persona_recibe": "nombre de quien recibe si es distinto al cliente, o null",
  "tipo_transaccion": "venta, separe, abono, garantia o cambio",
  "observaciones": "texto literal del campo Observaciones, o null",
  "credito_entidad": "entidad de crédito o null",
  "credito_cuotas": 0,
  "credito_valor_cuota": 0,
  "credito_frecuencia": "mensual o quincenal o null",
  "credito_total": 0,
  "credito_a_nombre_de": "nombre del titular si es tercero, o null",
  "credito_cedula": "cédula del tercero, o null",
  "credito_telefono": "teléfono del tercero, o null"
}

Responde ÚNICAMENTE con el JSON, sin texto adicional."""


class OCRResult:
    """Resultado estructurado del OCR."""

    def __init__(self, data: dict, photo_path: str):
        # Tipo de planilla detectado por el OCR
        self.form_type: str = data.get("form_type") or "remision"  # "plan_abonos" | "remision"
        # numero_formulario: ID propio de este documento (lo que va en invoice_number)
        self.numero_formulario: str | None = data.get("numero_formulario")
        # numero_factura_ref: referencia a la REMISIÓN original (solo en plan_abonos)
        self.numero_factura_ref: str | None = data.get("numero_factura_ref")

        self.nombre: str | None = data.get("nombre")
        self.telefono: str | None = data.get("telefono")
        self.cedula: str | None = data.get("cedula")
        self.direccion: str | None = data.get("direccion")
        self.items: list = data.get("items") or []
        self.total: float | None = data.get("total")
        self.abono: float | None = data.get("abono")
        self.resta: float | None = data.get("resta")
        self.acarreo: float = float(data.get("acarreo") or 0)
        self.ayudantes: int = int(data.get("ayudantes") or 0)
        self.fecha_compra: str | None = data.get("fecha_compra")
        self.fecha_entrega: str | None = data.get("fecha_entrega")
        self.firmada: bool = bool(data.get("firmada", False))
        self.tipo_acarreo: str = data.get("tipo_acarreo") or "llevar"
        self.asesor: str | None = data.get("asesor")
        self.persona_recibe: str | None = data.get("persona_recibe")
        self.tipo_transaccion: str = data.get("tipo_transaccion") or "venta"
        self.observaciones: str | None = data.get("observaciones")

        # Crédito (solo Remisión)
        self.credito_entidad: str | None = data.get("credito_entidad")
        self.credito_cuotas: int | None = data.get("credito_cuotas")
        self.credito_valor_cuota: float | None = data.get("credito_valor_cuota")
        self.credito_frecuencia: str | None = data.get("credito_frecuencia")
        self.credito_total: float | None = data.get("credito_total")
        self.credito_a_nombre_de: str | None = data.get("credito_a_nombre_de")
        self.credito_cedula: str | None = data.get("credito_cedula")
        self.credito_telefono: str | None = data.get("credito_telefono")

        self.photo_path: str = photo_path
        self.raw: dict = data


async def process_invoice_photo(image_base64: str, photo_path: str | None = None) -> OCRResult:
    """
    Procesa una foto de factura:
    1. Guarda la imagen en disco
    2. Llama a Claude Vision para extraer datos
    3. Retorna OCRResult estructurado

    Lanza LLMError si el OCR falla (el caller debe guardar como pending_ocr).
    """
    import json

    # Guardar foto en disco si el caller no lo hizo antes
    if not photo_path:
        photo_path = save_invoice_photo(image_base64)

    # Llamar Claude Vision con retry
    raw_response = await call_llm_with_vision(
        prompt=OCR_PROMPT,
        image_base64=image_base64,
        model=settings.ocr_model,
    )

    # Parsear JSON de respuesta
    try:
        # Limpiar markdown code blocks si los hay
        cleaned = raw_response.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        data = json.loads(cleaned.strip())
    except (json.JSONDecodeError, IndexError) as exc:
        logger.error("OCR response is not valid JSON: %s", raw_response[:200])
        raise LLMError(f"OCR JSON inválido: {exc}") from exc

    return OCRResult(data=data, photo_path=photo_path)


def save_invoice_photo(image_base64: str) -> str:
    """Guarda la imagen base64 en disco y retorna el path."""
    photos_dir = Path(settings.photos_dir)
    photos_dir.mkdir(parents=True, exist_ok=True)

    # Limpiar prefijo data URL si existe
    if "," in image_base64:
        image_base64 = image_base64.split(",")[1]

    filename = f"factura_{uuid.uuid4().hex}_{int(datetime.now().timestamp())}.jpg"
    path = photos_dir / filename

    with open(path, "wb") as f:
        f.write(base64.b64decode(image_base64))

    return str(path)
