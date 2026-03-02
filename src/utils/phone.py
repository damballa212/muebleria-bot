"""Normalización de teléfonos colombianos a formato E.164: +57XXXXXXXXXX."""
import re


def normalize_phone(raw: str) -> str:
    """
    Convierte cualquier formato de teléfono colombiano a +57XXXXXXXXXX.

    Ejemplos:
        "3001234567"     → "+573001234567"
        "573001234567"   → "+573001234567"
        "+57 300 123-4567" → "+573001234567"
        "300-123 4567"   → "+573001234567"
        "03001234567"    → "+573001234567"
    """
    if not raw:
        return raw

    # Eliminar todo excepto dígitos y el + inicial
    digits = re.sub(r"[^\d+]", "", raw)

    # Si empieza con +, quitar el +
    digits = digits.lstrip("+")

    # Quitar prefijo 0 inicial (marcación larga distancia local)
    if digits.startswith("0"):
        digits = digits[1:]

    # Quitar prefijo 57 duplicado
    if digits.startswith("57") and len(digits) == 12:
        digits = digits[2:]

    # Validar que quedaron 10 dígitos (número colombiano)
    if len(digits) != 10:
        # Retornar el valor limpio pero sin garantizar formato
        return f"+57{digits}"

    return f"+57{digits}"
