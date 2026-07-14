"""Table SQLite `whatsapp_crm_mappings` : mémoire persistante des associations
numéro WhatsApp → idclient CRM apprises via le dashboard (reçus "client
introuvable" rattachés manuellement à un client par son identifiant CRM).

La correspondance n'est écrite qu'une fois le paiement confirmé EN FILE
(statut 'queued' de `numeros_introuvable`, cf. routes._ensure_whatsapp_mapping)
— jamais à la simple association : une association abandonnée ou erronée ne
doit pas router silencieusement les paiements futurs vers un mauvais client.

Rôle : réduire les futurs cas client_not_found. Le pipeline webhook consulte
cette table UNIQUEMENT en repli, quand le lookup PostgreSQL par téléphone
(from_phone puis body_phone) n'a rien donné. Si une correspondance active
existe, le client est rechargé par pg.get_client_by_id() et le flux normal
continue à l'identique (job_builder, queue, worker) — sinon le comportement
client_not_found historique s'applique tel quel.

Règles :
- une seule correspondance ACTIVE par numéro (index UNIQUE partiel sur
  is_active=1) ;
- ré-associer le même numéro à un AUTRE idclient désactive l'ancienne ligne
  (historique conservé, jamais supprimé) et en insère une nouvelle ;
- ré-associer au MÊME idclient rafraîchit juste updated_at (idempotent).

Base dédiée (pas unknown_clients.db) : la correspondance survit aux
enregistrements `numeros_introuvable` qui l'ont créée — c'est une donnée de
référence long-terme, pas un état de workflow. Toutes les fonctions sont
best-effort côté pipeline : une erreur SQLite est loggée, jamais propagée
(le webhook ne doit jamais planter à cause de cette table).
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

from .. import config

logger = logging.getLogger("whatsapp_automation.webhook.crm_mappings")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS whatsapp_crm_mappings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    whatsapp_phone  TEXT NOT NULL,
    crm_client_id   TEXT NOT NULL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    created_by      TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_wcm_phone_active
    ON whatsapp_crm_mappings(whatsapp_phone) WHERE is_active = 1;
CREATE INDEX IF NOT EXISTS idx_wcm_phone ON whatsapp_crm_mappings(whatsapp_phone);
"""


def _path(db_path: Optional[str]) -> str:
    return db_path or config.WHATSAPP_CRM_MAPPINGS_DB_PATH


def _connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(
        _path(db_path),
        isolation_level=None,  # autocommit ; BEGIN IMMEDIATE explicite si besoin
        timeout=10.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(db_path: Optional[str] = None) -> None:
    Path(_path(db_path)).parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)


def _row_to_dict(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "whatsapp_phone": r["whatsapp_phone"],
        "crm_client_id": r["crm_client_id"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "created_by": r["created_by"],
        "is_active": bool(r["is_active"]),
    }


def get_active_mapping(whatsapp_phone: str, db_path: Optional[str] = None) -> Optional[dict]:
    """Retourne la correspondance ACTIVE pour ce numéro WhatsApp, ou None.

    Best-effort : toute erreur SQLite (base absente, verrou, corruption) est
    loggée et renvoie None — l'appelant (pipeline webhook) retombe alors sur
    le comportement client_not_found existant, jamais sur une exception."""
    if not whatsapp_phone:
        return None
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                """SELECT * FROM whatsapp_crm_mappings
                   WHERE whatsapp_phone = ? AND is_active = 1
                   LIMIT 1""",
                (whatsapp_phone,),
            ).fetchone()
        return _row_to_dict(row) if row else None
    except Exception as exc:
        logger.error(
            "échec get_active_mapping phone=%s : %s: %r",
            whatsapp_phone, type(exc).__name__, exc,
        )
        return None


def upsert_mapping(
    *,
    whatsapp_phone: str,
    crm_client_id: str,
    created_by: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Optional[dict]:
    """Crée ou met à jour la correspondance active pour ce numéro WhatsApp.

    - Aucune correspondance active → INSERT d'une ligne active.
    - Même idclient déjà actif → rafraîchit updated_at (idempotent).
    - Idclient DIFFÉRENT actif → désactive l'ancienne ligne (is_active=0,
      historique conservé) puis insère la nouvelle — le tout dans une
      transaction BEGIN IMMEDIATE (l'index UNIQUE partiel garantit en dernier
      recours qu'il n'y a jamais deux lignes actives pour le même numéro).

    Retourne la ligne active résultante, ou None si entrée invalide ou erreur
    SQLite (loggée, jamais levée — l'association dashboard qui nous appelle ne
    doit pas échouer à cause de cette mémoire)."""
    if not whatsapp_phone or not crm_client_id:
        logger.warning(
            "upsert_mapping ignoré : phone=%r crm_client_id=%r",
            whatsapp_phone, crm_client_id,
        )
        return None
    now = time.time()
    crm_client_id = str(crm_client_id)
    try:
        with _connect(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current = conn.execute(
                    """SELECT * FROM whatsapp_crm_mappings
                       WHERE whatsapp_phone = ? AND is_active = 1
                       LIMIT 1""",
                    (whatsapp_phone,),
                ).fetchone()
                if current is not None and current["crm_client_id"] == crm_client_id:
                    conn.execute(
                        "UPDATE whatsapp_crm_mappings SET updated_at = ? WHERE id = ?",
                        (now, current["id"]),
                    )
                    active_id = current["id"]
                else:
                    if current is not None:
                        conn.execute(
                            """UPDATE whatsapp_crm_mappings
                               SET is_active = 0, updated_at = ?
                               WHERE id = ?""",
                            (now, current["id"]),
                        )
                    cur = conn.execute(
                        """INSERT INTO whatsapp_crm_mappings
                           (whatsapp_phone, crm_client_id, created_at, updated_at,
                            created_by, is_active)
                           VALUES (?, ?, ?, ?, ?, 1)""",
                        (whatsapp_phone, crm_client_id, now, now, created_by),
                    )
                    active_id = cur.lastrowid
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            row = conn.execute(
                "SELECT * FROM whatsapp_crm_mappings WHERE id = ?", (active_id,)
            ).fetchone()
        return _row_to_dict(row) if row else None
    except Exception as exc:
        logger.error(
            "échec upsert_mapping phone=%s crm_client_id=%s : %s: %r",
            whatsapp_phone, crm_client_id, type(exc).__name__, exc,
        )
        return None
