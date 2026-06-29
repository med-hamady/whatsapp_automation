"""Retrouve la capture (image) et l'OCR d'un reçu archivé par ai_ocr.

Chaque reçu passé par l'OCR est stocké dans
``{AI_OCR_DATASET_PATH}/store/{YYYY-MM-DD}/{hex32}/`` avec ``image.jpg`` +
``prediction.json`` (qui contient ``extracted.txn_id``). Les refus du dashboard
portent le ``txn_id`` : on relie donc un événement à sa capture en cherchant le
``prediction.json`` dont le txn_id correspond, dans le dossier du jour.

L'archivage a lieu AVANT la décision de refus, donc l'image existe pour tout
reçu qui a passé l'OCR (notamment les sur-paiements et doublons).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Optional

from ... import config

logger = logging.getLogger("whatsapp_automation.webhook.dashboard")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HEX_RE = re.compile(r"^[0-9a-f]{32}$")


def _store_dir() -> str:
    return os.path.join(config.AI_OCR_DATASET_PATH, "store")


def _day_dir(date: str) -> str:
    return os.path.join(_store_dir(), date)


# Index {txn_id: hex} par jour, construit à la demande puis caché. Les samples
# sont immuables ; on garde un TTL court pour capter ceux ajoutés aujourd'hui.
_TTL = 60.0
_index_cache: dict[str, tuple[float, dict[str, str]]] = {}


def _txn_index_for_day(date: str) -> dict[str, str]:
    now = time.time()
    cached = _index_cache.get(date)
    if cached and (now - cached[0]) < _TTL:
        return cached[1]

    index: dict[str, str] = {}
    day_dir = _day_dir(date)
    try:
        entries = os.listdir(day_dir)
    except OSError:
        entries = []
    for hex_id in entries:
        if not _HEX_RE.match(hex_id):
            continue
        pred = os.path.join(day_dir, hex_id, "prediction.json")
        try:
            with open(pred, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        txn = (data.get("extracted") or {}).get("txn_id")
        if txn:
            index[str(txn)] = hex_id
    _index_cache[date] = (now, index)
    return index


def _candidate_dates(date_hint: Optional[str]) -> list[str]:
    """Jour indiqué + veille/lendemain (décalage minuit / fuseau)."""
    if not date_hint or not _DATE_RE.match(date_hint):
        # Repli : aujourd'hui et hier.
        base = datetime.now()
    else:
        base = datetime.strptime(date_hint, "%Y-%m-%d")
    days = [base, base - timedelta(days=1), base + timedelta(days=1)]
    return [d.strftime("%Y-%m-%d") for d in days]


def find_sample_by_txn(txn_id: str, date_hint: Optional[str] = None) -> Optional[dict]:
    """Retrouve la capture d'un reçu par son txn_id. None si introuvable."""
    if not txn_id:
        return None
    for date in _candidate_dates(date_hint):
        hex_id = _txn_index_for_day(date).get(str(txn_id))
        if hex_id:
            return _load_sample(date, hex_id)
    return None


def _load_sample(date: str, hex_id: str) -> Optional[dict]:
    pred_path = os.path.join(_day_dir(date), hex_id, "prediction.json")
    try:
        with open(pred_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return {
        "found": True,
        "date": date,
        "sid": hex_id,
        "extracted": data.get("extracted") or {},
        "template": data.get("template"),
        "raw_text": data.get("raw_text") or "",
        "saved_at": data.get("saved_at"),
    }


def image_path(date: str, sid: str) -> Optional[str]:
    """Chemin de l'image d'un sample, après validation stricte (anti-traversal)."""
    if not _DATE_RE.match(date or "") or not _HEX_RE.match(sid or ""):
        return None
    path = os.path.join(_day_dir(date), sid, "image.jpg")
    return path if os.path.isfile(path) else None
