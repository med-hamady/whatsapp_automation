"""Parsing des numéros de téléphone mauritaniens (indicatif +222)."""

from __future__ import annotations

import re


def parse_from_field(from_value: str) -> str:
    """Convertit "22237697850@c.us" → "37697850" (retire @c.us puis l'indicatif 222)."""
    if not from_value:
        return ""
    clean = from_value.replace("@c.us", "").replace("@s.whatsapp.net", "")
    digits = re.sub(r"[^0-9]", "", clean)
    if digits.startswith("222") and len(digits) > 8:
        return digits[3:]
    return digits


def parse_body_number(body: str) -> str:
    """Récupère les chiffres consécutifs du corps du message (fallback : client
    paie pour quelqu'un d'autre, mentionne son numéro dans le texte)."""
    if not body:
        return ""
    digits = re.sub(r"[^0-9]", "", body)
    if digits.startswith("222") and len(digits) > 8:
        return digits[3:]
    return digits


def normalize_mr_phone(raw: str) -> str:
    """Normalise un numéro mauritanien saisi manuellement (dashboard : association
    Phase 3) : accepte "37697850", "+22237697850" ou "22237697850" → 8 chiffres.
    Retourne "" si le résultat n'est pas exactement 8 chiffres (invalide)."""
    if not raw:
        return ""
    digits = re.sub(r"[^0-9]", "", raw)
    if digits.startswith("222") and len(digits) > 8:
        digits = digits[3:]
    return digits if re.fullmatch(r"\d{8}", digits) else ""
