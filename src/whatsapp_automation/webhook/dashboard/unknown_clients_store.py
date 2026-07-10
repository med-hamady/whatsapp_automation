"""Table SQLite `numeros_introuvable` : préserve les paiements reçus d'un
numéro non reconnu (client_not_found) pour reprise/rattachement manuel futur.

Phase 1 uniquement : on se contente d'écrire les données structurées dispo au
moment du webhook (sample_id, montant, txn_id, téléphones...). Pas de création
de Job, pas d'appel UCRM/MikroTik/UltraMsg ici — le comportement client_not_found
existant (skip + notif support) n'est pas modifié, cette table est un
enregistrement en plus, best-effort.

Base dédiée (pas events.db, pas PostgreSQL) : `status`/`job_id`/`ucrm_payment_id`
sont prévus pour un rattachement ultérieur (phases suivantes), ce qui ne
correspond ni à un cache de logs en lecture seule (events.db) ni au schéma
clients PostgreSQL existant.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

from ... import config

logger = logging.getLogger("whatsapp_automation.webhook.dashboard")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS numeros_introuvable (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id        TEXT NOT NULL,
    sample_date      TEXT,
    original_phone   TEXT,
    body_phone       TEXT,
    group_id         TEXT,
    txn_id           TEXT,
    amount           INTEGER,
    date_heure       TEXT,
    operator         TEXT,
    raw_text         TEXT,
    status           TEXT NOT NULL DEFAULT 'pending',
    job_id           TEXT,
    ucrm_payment_id  TEXT,
    error_message    TEXT,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_numeros_introuvable_sample
    ON numeros_introuvable(sample_id);
CREATE INDEX IF NOT EXISTS idx_numeros_introuvable_status
    ON numeros_introuvable(status);
CREATE INDEX IF NOT EXISTS idx_numeros_introuvable_txn
    ON numeros_introuvable(txn_id);
"""

_INSERT = """
INSERT OR IGNORE INTO numeros_introuvable
    (sample_id, sample_date, original_phone, body_phone, group_id, txn_id,
     amount, date_heure, operator, raw_text, status, created_at, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
"""


def _path(db_path: Optional[str]) -> str:
    return db_path or config.UNKNOWN_CLIENTS_DB_PATH


def _connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(_path(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(db_path: Optional[str] = None) -> None:
    Path(_path(db_path)).parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)


def _sample_date(sample_id: str) -> Optional[str]:
    """Extrait la date 'YYYY-MM-DD' préfixe du sample_id (cf. ai_ocr
    dataset/writer.save_sample : sample_id = "<today>/<uuid hex>")."""
    if not sample_id or "/" not in sample_id:
        return None
    return sample_id.split("/", 1)[0]


def insert_unknown_client(
    *,
    sample_id: str,
    txn_id: Optional[str] = None,
    amount: Optional[int] = None,
    date_heure: Optional[str] = None,
    operator: Optional[str] = None,
    original_phone: Optional[str] = None,
    body_phone: Optional[str] = None,
    group_id: Optional[str] = None,
    raw_text: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Optional[int]:
    """Enregistre un paiement reçu d'un client introuvable, de façon idempotente
    par `sample_id` (une même image/OCR ne crée jamais deux lignes).

    Retourne l'id de la ligne (existante ou nouvellement créée), ou None si
    l'insertion a échoué (erreur loggée, jamais levée — best-effort, ne doit
    pas casser le pipeline webhook appelant)."""
    if not sample_id:
        logger.warning("insert_unknown_client appelé sans sample_id, ignoré")
        return None
    now = time.time()
    try:
        with _connect(db_path) as conn:
            conn.execute(
                _INSERT,
                (
                    sample_id, _sample_date(sample_id), original_phone, body_phone,
                    group_id, txn_id or None, amount, date_heure, operator, raw_text,
                    now, now,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT id FROM numeros_introuvable WHERE sample_id = ?",
                (sample_id,),
            ).fetchone()
            return row["id"] if row else None
    except Exception as exc:
        logger.error(
            "échec insert_unknown_client sample_id=%s : %s: %r",
            sample_id, type(exc).__name__, exc,
        )
        return None


def _row_to_dict(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "sample_id": r["sample_id"],
        "sample_date": r["sample_date"],
        "original_phone": r["original_phone"],
        "body_phone": r["body_phone"],
        "group_id": r["group_id"],
        "txn_id": r["txn_id"],
        "amount": r["amount"],
        "date_heure": r["date_heure"],
        "operator": r["operator"],
        "raw_text": r["raw_text"],
        "status": r["status"],
        "job_id": r["job_id"],
        "ucrm_payment_id": r["ucrm_payment_id"],
        "error_message": r["error_message"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def get_by_sample_id(sample_id: str, db_path: Optional[str] = None) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM numeros_introuvable WHERE sample_id = ?",
            (sample_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_recent(limit: int = 50, db_path: Optional[str] = None) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM numeros_introuvable ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]
