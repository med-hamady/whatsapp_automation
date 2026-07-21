"""Table SQLite `numeros_introuvable` : ticket de travail pour un paiement reçu
d'un numéro WhatsApp non reconnu (client_not_found), en attente de rattachement
manuel à un client via le dashboard — et mémoire de référence pour que les
PROCHAINS paiements du même numéro n'aient plus besoin de ce rattachement
manuel.

Cycle de vie d'un ticket : pending -> associated -> confirming -> queued.
- pending     : paiement reçu, aucun client identifié (créé par le webhook).
- associated  : un admin a rattaché le ticket à un client CRM (`client_id`),
                aucun paiement/Job créé — c'est réversible (ré-association
                possible tant qu'on n'a pas encore confirmé).
- confirming  : confirmation en cours (réservation CAS, cf. `_transition`).
- queued      : un Job a été empilé en queue ; le worker prend le relais
                (UCRM, MikroTik, UltraMsg) exactement comme un paiement webhook
                normal.

Auto-résolution des paiements futurs : une fois un ticket `queued`, la paire
(`whatsapp_phone`, `client_id`) qu'il porte devient consultable par
`find_client_id_for_phone()`. Le pipeline webhook l'appelle EN REPLI, quand le
lookup PostgreSQL par téléphone échoue : si ce numéro a déjà un ticket
`queued`, le paiement est routé directement vers ce client, sans repasser par
le dashboard (cf. pipeline.process). Seuls les tickets `queued` comptent : un
ticket abandonné en `associated` (mauvais identifiant CRM saisi, jamais
confirmé) ne doit jamais router silencieusement un paiement futur vers le
mauvais client.

Base SQLite dédiée (pas events.db, pas PostgreSQL) : ce ticket a un cycle de
vie et des transitions d'état propres qui ne correspondent ni à un cache de
logs en lecture seule, ni au schéma clients PostgreSQL existant.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

from ... import config

logger = logging.getLogger("whatsapp_automation.webhook.dashboard")

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS numeros_introuvable (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id       TEXT NOT NULL,
    sample_date     TEXT,
    whatsapp_phone  TEXT,
    body_phone      TEXT,
    group_id        TEXT,
    txn_id          TEXT,
    amount          INTEGER,
    date_heure      TEXT,
    operator        TEXT,
    raw_text        TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    job_id          TEXT,
    error_message   TEXT,
    client_id       TEXT,
    associated_at   REAL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
"""

# Créés APRÈS _migrate_schema (pas dans _CREATE_TABLE) : idx_..._phone porte
# sur `whatsapp_phone`, qui n'existe pas encore tant qu'une base héritée n'a
# pas été renommée depuis `original_phone` — créer l'index avant la migration
# ferait échouer l'ouverture d'une base existante.
_CREATE_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_numeros_introuvable_sample
    ON numeros_introuvable(sample_id);
CREATE INDEX IF NOT EXISTS idx_numeros_introuvable_status
    ON numeros_introuvable(status);
CREATE INDEX IF NOT EXISTS idx_numeros_introuvable_txn
    ON numeros_introuvable(txn_id);
CREATE INDEX IF NOT EXISTS idx_numeros_introuvable_phone
    ON numeros_introuvable(whatsapp_phone);
"""

_INSERT = """
INSERT OR IGNORE INTO numeros_introuvable
    (sample_id, sample_date, whatsapp_phone, body_phone, group_id, txn_id,
     amount, date_heure, operator, raw_text, status, created_at, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
"""

# Colonnes d'une version antérieure du schéma, jamais relues par aucune étape
# du workflow (l'association ne pilote que `client_id` ; la confirmation
# relit PostgreSQL/UCRM à chaud, jamais ces valeurs figées) : supprimées à la
# première ouverture d'une base qui les contient encore.
_LEGACY_DROP_COLUMNS = ("entered_phone", "subscription_phone", "mac_address", "ucrm_payment_id")


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Aligne une base créée par une version antérieure sur le schéma actuel.
    Chaque étape vérifie l'état avant d'agir : no-op sur une base à jour."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(numeros_introuvable)")}
    if "client_id" not in existing:
        conn.execute("ALTER TABLE numeros_introuvable ADD COLUMN client_id TEXT")
    if "associated_at" not in existing:
        conn.execute("ALTER TABLE numeros_introuvable ADD COLUMN associated_at REAL")
    if "original_phone" in existing and "whatsapp_phone" not in existing:
        conn.execute(
            "ALTER TABLE numeros_introuvable RENAME COLUMN original_phone TO whatsapp_phone"
        )

    existing = {row["name"] for row in conn.execute("PRAGMA table_info(numeros_introuvable)")}
    for name in _LEGACY_DROP_COLUMNS:
        if name in existing:
            conn.execute(f"ALTER TABLE numeros_introuvable DROP COLUMN {name}")


def _path(db_path: Optional[str]) -> str:
    return db_path or config.UNKNOWN_CLIENTS_DB_PATH


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
        conn.executescript(_CREATE_TABLE)
        _migrate_schema(conn)
        conn.executescript(_CREATE_INDEXES)


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
    whatsapp_phone: Optional[str] = None,
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
                    sample_id, _sample_date(sample_id), whatsapp_phone, body_phone,
                    group_id, txn_id or None, amount, date_heure, operator, raw_text,
                    now, now,
                ),
            )
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
        "whatsapp_phone": r["whatsapp_phone"],
        "body_phone": r["body_phone"],
        "group_id": r["group_id"],
        "txn_id": r["txn_id"],
        "amount": r["amount"],
        "date_heure": r["date_heure"],
        "operator": r["operator"],
        "raw_text": r["raw_text"],
        "status": r["status"],
        "job_id": r["job_id"],
        "error_message": r["error_message"],
        "client_id": r["client_id"],
        "associated_at": r["associated_at"],
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


def find_client_id_for_phone(whatsapp_phone: str, db_path: Optional[str] = None) -> Optional[str]:
    """Résout le `client_id` déjà confirmé pour ce numéro WhatsApp, s'il en
    existe un (cf. docstring de module : auto-résolution des paiements
    futurs). Ne considère que les tickets `queued` — jamais `associated` ou
    `pending`, pour ne jamais router un paiement futur sur une association
    abandonnée ou pas encore confirmée.

    S'il existe plusieurs tickets `queued` pour ce numéro (rare : le client a
    été introuvable à plusieurs reprises), le plus récent gagne.

    Best-effort : appelé sur le chemin chaud du webhook, ne doit jamais faire
    planter le pipeline — toute erreur SQLite est loggée et traitée comme
    "aucune correspondance"."""
    if not whatsapp_phone:
        return None
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                """SELECT client_id FROM numeros_introuvable
                   WHERE whatsapp_phone = ? AND status = 'queued'
                   ORDER BY created_at DESC LIMIT 1""",
                (whatsapp_phone,),
            ).fetchone()
        return row["client_id"] if row else None
    except Exception as exc:
        logger.error(
            "échec find_client_id_for_phone phone=%s : %s: %r",
            whatsapp_phone, type(exc).__name__, exc,
        )
        return None


def associate_unknown_client(
    id: int, *, client_id: str, db_path: Optional[str] = None,
) -> Optional[dict]:
    """Rattache le ticket `id` au client CRM `client_id`, choisi par l'admin.
    N'écrit que dans cette base SQLite dédiée — ne crée aucun paiement/Job,
    n'appelle ni UCRM ni MikroTik ni UltraMsg.

    `whatsapp_phone` n'est pas touché ici : il est déjà connu depuis la
    création du ticket (`insert_unknown_client`) — c'est le numéro qui a
    envoyé le paiement, il ne change jamais. Cette étape n'ajoute que
    `client_id` ; c'est la paire (whatsapp_phone, client_id) qui deviendra
    consultable par `find_client_id_for_phone` une fois `queued` (cf.
    docstring du module).

    status -> associated. `error_message` est effacé (une nouvelle tentative
    réussie annule un échec précédent éventuel)."""
    now = time.time()
    with _connect(db_path) as conn:
        conn.execute(
            """UPDATE numeros_introuvable
               SET client_id = ?, status = 'associated', associated_at = ?,
                   updated_at = ?, error_message = NULL
               WHERE id = ?""",
            (client_id, now, now, id),
        )
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
        row = conn.execute(
            "SELECT * FROM numeros_introuvable WHERE id = ?", (id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


# Transitions atomiques compare-and-set (CAS) autour de la confirmation
# dashboard. `_transition` centralise le `WHERE status = ...` et la
# vérification de `rowcount` — c'est ce qui ferme la fenêtre de course entre
# deux confirmations concurrentes du même ticket (un seul appel gagne la
# transition ; l'autre voit rowcount=0 et sait qu'il a perdu la course, sans
# transaction explicite nécessaire côté appelant : chaque UPDATE est atomique
# en SQLite en autocommit).
#
# Pas de nouvelle valeur de `status` en dur dans un CHECK constraint (colonne
# TEXT libre) : aucune migration de schéma requise pour introduire
# 'confirming' et 'queued' en plus de 'pending'/'associated' existants.


def _transition(
    id: int,
    *,
    from_status: str,
    to_status: str,
    set_extra: str = "",
    set_params: tuple = (),
    where_extra: str = "",
    where_params: tuple = (),
    db_path: Optional[str] = None,
) -> bool:
    """CAS générique `from_status` -> `to_status` sur l'enregistrement `id`.

    Retourne True si CET appel a gagné la transition (rowcount == 1), False si
    le statut n'était pas `from_status` (ou si `where_extra` a exclu la ligne)
    au moment de l'UPDATE."""
    now = time.time()
    sql = (
        f"UPDATE numeros_introuvable SET status = ?, updated_at = ?{set_extra} "
        f"WHERE id = ? AND status = ?{where_extra}"
    )
    params = (to_status, now, *set_params, id, from_status, *where_params)
    with _connect(db_path) as conn:
        cur = conn.execute(sql, params)
        return cur.rowcount == 1


def reserve_for_confirmation(id: int, db_path: Optional[str] = None) -> bool:
    """CAS : associated -> confirming. Retourne True si CET appel a gagné la
    réservation. False si le statut n'était pas 'associated' au moment de
    l'UPDATE (déjà en confirmation, déjà en file, ou pas encore associé) —
    l'appelant doit alors refuser la confirmation (409) plutôt que de
    continuer, pour ne jamais construire deux Jobs pour le même reçu."""
    return _transition(
        id, from_status="associated", to_status="confirming", db_path=db_path,
    )


def mark_queued(id: int, job_id: str, db_path: Optional[str] = None) -> bool:
    """CAS : confirming -> queued. Persiste `job_id`, efface `error_message`
    (un succès annule tout échec précédent). Retourne True si la transition a
    eu lieu — False si l'enregistrement n'était plus en 'confirming' (ex :
    appelé deux fois pour le même id)."""
    return _transition(
        id, from_status="confirming", to_status="queued",
        set_extra=", job_id = ?, error_message = NULL", set_params=(job_id,),
        db_path=db_path,
    )


def release_confirmation(id: int, error_message: str, db_path: Optional[str] = None) -> bool:
    """CAS : confirming -> associated (échec récupérable : PostgreSQL/UCRM
    indisponible, validation refusée, doublon introuvable en queue...).
    Enregistre `error_message` pour affichage dashboard. Retourne True si la
    transition a eu lieu.

    Réservé à la requête qui DÉTIENT la réservation courante (celle qui a
    elle-même appelé `reserve_for_confirmation()` puis échoué avant
    `enqueue()`) : elle sait avec certitude qu'aucun Job n'a été empilé, donc
    la libération immédiate est sûre, sans condition d'âge. Pour libérer un
    enregistrement 'confirming' constaté par une AUTRE requête (qui ne sait
    pas si l'original est toujours en cours), utiliser
    `release_stale_confirmation` à la place."""
    return _transition(
        id, from_status="confirming", to_status="associated",
        set_extra=", error_message = ?", set_params=(error_message,),
        db_path=db_path,
    )


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
    cutoff = time.time() - min_age_seconds
    return _transition(
        id, from_status="confirming", to_status="associated",
        set_extra=", error_message = ?", set_params=(error_message,),
        where_extra=" AND updated_at <= ?", where_params=(cutoff,),
        db_path=db_path,
    )
