"""Utilidades de routing robusto para fast-paths sin depender de texto exacto."""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher


def normalize_text(text: str) -> str:
    """Normaliza texto para matching tolerante a acentos, signos y mayúsculas."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9#\-\s]", " ", text)
    return " ".join(text.split())


def tokenize(text: str) -> list[str]:
    return normalize_text(text).split()


def extract_case_number(text: str) -> str | None:
    """Extrae un número de caso y lo normaliza a GAR-0001 / COT-0003."""
    match = re.search(r"\b(gar|cot)\s*[- ]?\s*(\d{1,4})\b", text or "", flags=re.IGNORECASE)
    if not match:
        return None
    prefix = match.group(1).upper()
    number = int(match.group(2))
    return f"{prefix}-{number:04d}"


def latest_case_number_from_history(chat_history: list[dict]) -> str | None:
    """Busca el último número de caso mencionado en el buffer conversacional."""
    for item in reversed(chat_history or []):
        content = item.get("content", "")
        case_number = extract_case_number(content)
        if case_number:
            return case_number
    return None


def token_matches_root(token: str, root: str) -> bool:
    """Matching tolerante a errores pequeños y variaciones morfológicas."""
    token = normalize_text(token)
    root = normalize_text(root)
    if not token or not root:
        return False
    if token.startswith(root) or root.startswith(token):
        return True
    if len(token) >= 4 and len(root) >= 4 and token[:4] == root[:4]:
        return True
    ratio = SequenceMatcher(a=token, b=root).ratio()
    return ratio >= 0.82


def count_family_matches(tokens: list[str], roots: list[str]) -> int:
    count = 0
    for token in tokens:
        if any(token_matches_root(token, root) for root in roots):
            count += 1
    return count


def has_family_match(tokens: list[str], roots: list[str]) -> bool:
    return count_family_matches(tokens, roots) > 0

