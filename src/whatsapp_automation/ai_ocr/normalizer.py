"""Normalisation des champs extraits (montant, date, ID transaction)."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import Optional

import dateparser


logger = logging.getLogger("whatsapp_automation.ai_ocr.normalizer")

_DASH_TRANSLATION = str.maketrans({"—": "-", "–": "-", "‒": "-", "−": "-"})
_OCR_DIGIT_FIXES = str.maketrans({"O": "0", "o": "0", "Q": "0", "I": "1", "l": "1"})

# Plafond du montant accepté en MRU. 10 000 000 = 10M MRU couvre les cas
# business (factures professionnelles). Au-delà = quasi-certainement une
# erreur OCR (Trs ID confondu avec montant). Configurable via env.
_MAX_AMOUNT_MRU = int(os.environ.get("MAX_AMOUNT_MRU", "10000000"))


def normalize_amount(raw: Optional[str]) -> Optional[int]:
    """Convertit "1`500", "1 500", "MRU 1500.00" en 1500 (entier MRU).

    Retourne None si :
      - entrée vide ou non parseable
      - valeur hors plage (négative, zéro, > _MAX_AMOUNT_MRU)
    Logge un warning sur valeur hors plage pour faciliter le debug en prod
    (avant : ces cas étaient rejetés silencieusement).
    """
    if raw is None:
        return None
    cleaned = re.sub(r"[^\d,.]", "", str(raw))
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        parts = cleaned.split(",")
        if len(parts) == 2 and len(parts[1]) == 3:
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if value <= 0:
        return None
    if value > _MAX_AMOUNT_MRU:
        logger.warning(
            "normalize_amount: valeur %r > plafond %d MRU, rejetée (probable bruit OCR)",
            raw,
            _MAX_AMOUNT_MRU,
        )
        return None
    return int(round(value))


def normalize_txn_id(raw: Optional[str]) -> Optional[str]:
    """Garde uniquement les caractères alphanumériques (les Txn ID sont
    soit purement numériques pour Bankily, soit alphanumériques pour Sedad).
    Refuse les ID < 8 caractères."""
    if raw is None:
        return None
    cleaned = re.sub(r"[^A-Za-z0-9]", "", str(raw))
    if len(cleaned) < 8:
        return None
    return cleaned


def normalize_datetime(raw: Optional[str]) -> Optional[str]:
    """Parse les nombreuses variantes vues sur les reçus :
    - "29—05—25 14:12:02"
    - "26-11-25 13:42:11"
    - "25/11/2025 17:20"
    - "11/26/2025, 4:24 PM"
    - "26 novembre 2025 17:28"
    Retourne ISO 8601 sans fuseau, sinon None.
    """
    if raw is None:
        return None
    text = str(raw).translate(_DASH_TRANSLATION)
    text = text.replace(" ", " ").strip(" .:,;-")
    if not text:
        return None

    candidate = _try_compact_yymmdd(text)
    if candidate is not None:
        return candidate

    parsed = dateparser.parse(
        text,
        languages=["fr", "en"],
        settings={
            "DATE_ORDER": "DMY",
            "PREFER_DAY_OF_MONTH": "first",
            "RETURN_AS_TIMEZONE_AWARE": False,
        },
    )
    if parsed is None:
        return None
    return parsed.replace(microsecond=0).isoformat()


_COMPACT_RE = re.compile(
    r"\b(\d{2})[-/ ](\d{2})[-/ ](\d{2})(?!\d)"
    r"(?:[ T,]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)


def _try_compact_yymmdd(text: str) -> Optional[str]:
    """Parse "DD-MM-YY[ HH:MM:SS]" / "YY-MM-DD[ HH:MM:SS]".

    Quand l'un des deux extrêmes > 31, l'ambiguïté est résolue (cf. règles
    ci-dessous). Sinon l'extracteur appelant DOIT fournir le bon ordre via
    ``layout`` (voir ``_try_compact_yymmdd_layout``) — sans hint, on retourne
    l'interprétation dont l'année est la plus proche de l'année courante,
    pour préserver la compat avec les anciens labels.
    """
    text = text.translate(_OCR_DIGIT_FIXES)
    m = _COMPACT_RE.search(text)
    if not m:
        return None
    a, b, c = m.group(1), m.group(2), m.group(3)
    hh, mm, ss = m.group(4), m.group(5), m.group(6)

    da, db, dc = int(a), int(b), int(c)
    if da > 31 or dc > 31 or not (1 <= db <= 12):
        return None

    today = datetime.now()
    candidates = []
    try:
        candidates.append(datetime(2000 + dc, db, da))  # DD-MM-YY
    except ValueError:
        pass
    try:
        candidates.append(datetime(2000 + da, db, dc))  # YY-MM-DD
    except ValueError:
        pass
    if not candidates:
        return None
    chosen = min(candidates, key=lambda d: abs((d - today).days))
    year, month, day = chosen.year, chosen.month, chosen.day

    try:
        h = int(hh) if hh else 0
        mi = int(mm) if mm else 0
        s = int(ss) if ss else 0
        dt = datetime(year, month, day, h, mi, s)
    except ValueError:
        return None
    return dt.isoformat()


_TIME_RE = re.compile(r"^(\d{1,2})[:.h](\d{2})(?:[:.h](\d{2}))?$")


def parse_yymmdd_explicit(date_str: str, time_str: str = "") -> Optional[str]:
    """Construit une ISO datetime depuis ``date_str`` au format **explicite**
    YY-MM-DD (ex: ``"26-04-27"`` → ``"2026-04-27"``). Stable : ne dépend
    pas du jour courant. Utilisé par les extracteurs qui SAVENT l'ordre
    grâce au contexte de la regex (ex : Bankily layout RTL arabe).
    """
    if not date_str:
        return None
    date_str = date_str.translate(_OCR_DIGIT_FIXES)
    parts = re.split(r"[\-—–/ ]", date_str.strip())
    if len(parts) != 3:
        return None
    try:
        yy, mm, dd = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    if not (0 <= yy <= 99 and 1 <= mm <= 12 and 1 <= dd <= 31):
        return None
    try:
        dt = datetime(2000 + yy, mm, dd)
    except ValueError:
        return None

    if time_str:
        time_clean = time_str.translate(_OCR_DIGIT_FIXES).strip()
        m = _TIME_RE.match(time_clean)
        if m:
            try:
                h = int(m.group(1))
                mi = int(m.group(2))
                s = int(m.group(3) or 0)
                if 0 <= h < 24 and 0 <= mi < 60 and 0 <= s < 60:
                    dt = dt.replace(hour=h, minute=mi, second=s)
            except ValueError:
                pass
    return dt.isoformat()


def fix_ocr_digits(text: str) -> str:
    """Pre-clean a text fragment expected to be numeric (O→0, l→1)."""
    return text.translate(_OCR_DIGIT_FIXES)
