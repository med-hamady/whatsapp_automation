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

# Phase 3 : colonnes d'association client (ajoutées via migration ALTER TABLE
# pour ne pas casser les bases SQLite déjà déployées en Phase 1/2).
# `client_id` en TEXT (pas INTEGER) : PostgreSQL stocke `client.idclient` en
# VARCHAR(250), cf. db/postgres.py — on garde le même type ici pour éviter
# toute conversion avec perte entre les deux bases.
_NEW_COLUMNS = {
    "entered_phone": "TEXT",
    "subscription_phone": "TEXT",
    "client_id": "TEXT",
    "mac_address": "TEXT",
    "associated_at": "REAL",
}


def _migrate_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(numeros_introuvable)")}
    for name, col_type in _NEW_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE numeros_introuvable ADD COLUMN {name} {col_type}")


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
        _migrate_columns(conn)
        conn.commit()


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
        "entered_phone": r["entered_phone"],
        "subscription_phone": r["subscription_phone"],
        "client_id": r["client_id"],
        "mac_address": r["mac_address"],
        "associated_at": r["associated_at"],
    }


def get_by_sample_id(sample_id: str, db_path: Optional[str] = None) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM numeros_introuvable WHERE sample_id = ?",
            (sample_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_by_id(id: int, db_path: Optional[str] = None) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM numeros_introuvable WHERE id = ?",
            (id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_recent(
    limit: int = 50, status: Optional[str] = None, db_path: Optional[str] = None
) -> list[dict]:
    with _connect(db_path) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM numeros_introuvable WHERE status = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM numeros_introuvable ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


def associate_unknown_client(
    id: int,
    *,
    entered_phone: str,
    subscription_phone: Optional[str],
    client_id: str,
    mac_address: Optional[str],
    db_path: Optional[str] = None,
) -> Optional[dict]:
    """Phase 3 : rattache l'enregistrement au client PostgreSQL trouvé par le
    numéro saisi par l'admin. N'écrit que dans cette base SQLite dédiée — ne
    crée aucun paiement/Job, n'appelle ni UCRM ni MikroTik ni UltraMsg.

    status pending -> associated. `error_message` est effacé (une nouvelle
    tentative réussie annule un échec précédent éventuel)."""
    now = time.time()
    with _connect(db_path) as conn:
        conn.execute(
            """UPDATE numeros_introuvable
               SET entered_phone = ?, subscription_phone = ?, client_id = ?,
                   mac_address = ?, status = 'associated', associated_at = ?,
                   updated_at = ?, error_message = NULL
               WHERE id = ?""",
            (entered_phone, subscription_phone, client_id, mac_address, now, now, id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM numeros_introuvable WHERE id = ?", (id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def mark_unknown_client_error(
    id: int, error_message: str, db_path: Optional[str] = None
) -> Optional[dict]:
    """Enregistre un message d'erreur (numéro invalide, client introuvable...)
    sans changer le statut : l'enregistrement reste 'pending' pour une
    nouvelle tentative d'association."""
    now = time.time()
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE numeros_introuvable SET error_message = ?, updated_at = ? WHERE id = ?",
            (error_message, now, id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM numeros_introuvable WHERE id = ?", (id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


# Phase 4B-2 : transitions atomiques compare-and-set (CAS) autour de la
# confirmation dashboard. Chaque helper utilise `WHERE status = ...` et
# vérifie `rowcount` — c'est ce qui ferme la fenêtre de course entre deux
# confirmations concurrentes du même enregistrement (un seul appel gagne la
# transition ; l'autre voit rowcount=0 et sait qu'il a perdu la course, sans
# transaction explicite nécessaire côté appelant : chaque UPDATE est atomique
# en SQLite en autocommit).
#
# Pas de nouvelle valeur de `status` en dur dans un CHECK constraint (colonne
# TEXT libre) : aucune migration de schéma requise pour introduire
# 'confirming' et 'queued' en plus de 'pending'/'associated' existants.


def reserve_for_confirmation(id: int, db_path: Optional[str] = None) -> bool:
    """CAS : associated -> confirming. Retourne True si CET appel a gagné la
    réservation (rowcount == 1). False si le statut n'était pas 'associated'
    au moment de l'UPDATE (déjà en confirmation, déjà en file, ou pas encore
    associé) — l'appelant doit alors refuser la confirmation (409) plutôt que
    de continuer, pour ne jamais construire deux Jobs pour le même reçu."""
    now = time.time()
    with _connect(db_path) as conn:
        cur = conn.execute(
            """UPDATE numeros_introuvable
               SET status = 'confirming', updated_at = ?
               WHERE id = ? AND status = 'associated'""",
            (now, id),
        )
        conn.commit()
        return cur.rowcount == 1


def mark_queued(id: int, job_id: str, db_path: Optional[str] = None) -> bool:
    """CAS : confirming -> queued. Persiste `job_id`, efface `error_message`
    (un succès annule tout échec précédent). Retourne True si la transition a
    eu lieu (rowcount == 1) — False si l'enregistrement n'était plus en
    'confirming' (ex : appelé deux fois pour le même id)."""
    now = time.time()
    with _connect(db_path) as conn:
        cur = conn.execute(
            """UPDATE numeros_introuvable
               SET status = 'queued', job_id = ?, error_message = NULL, updated_at = ?
               WHERE id = ? AND status = 'confirming'""",
            (job_id, now, id),
        )
        conn.commit()
        return cur.rowcount == 1


def release_confirmation(id: int, error_message: str, db_path: Optional[str] = None) -> bool:
    """CAS : confirming -> associated (échec récupérable : PostgreSQL/UCRM
    indisponible, validation refusée, doublon introuvable en queue...).
    Enregistre `error_message` pour affichage dashboard. Retourne True si la
    transition a eu lieu (rowcount == 1).

    Réservé à la requête qui DÉTIENT la réservation courante (celle qui a
    elle-même appelé `reserve_for_confirmation()` puis échoué avant
    `enqueue()`) : elle sait avec certitude qu'aucun Job n'a été empilé, donc
    la libération immédiate est sûre, sans condition d'âge. Pour libérer un
    enregistrement 'confirming' constaté par une AUTRE requête (qui ne sait
    pas si l'original est toujours en cours), utiliser
    `release_stale_confirmation` à la place."""
    now = time.time()
    with _connect(db_path) as conn:
        cur = conn.execute(
            """UPDATE numeros_introuvable
               SET status = 'associated', error_message = ?, updated_at = ?
               WHERE id = ? AND status = 'confirming'""",
            (error_message, now, id),
        )
        conn.commit()
        return cur.rowcount == 1


def release_stale_confirmation(
    id: int, error_message: str, min_age_seconds: float, db_path: Optional[str] = None,
) -> bool:
    """CAS : confirming -> associated, mais UNIQUEMENT si la réservation est
    plus vieille que `min_age_seconds` (comparé à `updated_at`, posé comme
    horodatage de réservation par `reserve_for_confirmation`).

    Entre la réservation et l'enqueue, la route confirm effectue de vraies
    lectures PostgreSQL et UCRM (I/O réseau, pas microsecondes) : un
    enregistrement 'confirming' récent peut donc appartenir à une requête
    encore légitimement en cours. On ne le libère JAMAIS sans vérifier l'âge
    — la condition `updated_at <= now - min_age_seconds` est évaluée dans le
    MÊME UPDATE atomique que le changement de statut, donc deux tentatives de
    récupération concurrentes sur le même enregistrement stale ne peuvent pas
    toutes les deux réussir (rowcount == 1 pour une seule des deux).

    Retourne True si la transition a eu lieu (donc si l'appelant a bien
    gagné/effectué la récupération), False si l'enregistrement n'était pas
    'confirming', ou pas encore assez vieux, ou déjà récupéré par un autre
    appel concurrent."""
    now = time.time()
    cutoff = now - min_age_seconds
    with _connect(db_path) as conn:
        cur = conn.execute(
            """UPDATE numeros_introuvable
               SET status = 'associated', error_message = ?, updated_at = ?
               WHERE id = ? AND status = 'confirming' AND updated_at <= ?""",
            (error_message, now, id, cutoff),
        )
        conn.commit()
        return cur.rowcount == 1
