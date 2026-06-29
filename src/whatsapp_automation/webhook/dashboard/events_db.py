"""Table SQLite `events` : cache interrogeable des événements du système.

Les logs restent la source brute (intouchée). Un ingesteur les parse (via
log_parser) et insère ici en `INSERT OR IGNORE` (idempotent, ré-exécutable
sans doublon grâce à `dedup_key`). Le dashboard lit ENSUITE cette table au lieu
de re-parser des dizaines de Mo de logs à chaque requête.

La déduplication par contenu (hash de ts+type+message) gère naturellement la
rotation des logs : une même ligne déplacée de webhook.log vers webhook.log.1
garde la même clé et n'est pas réinsérée.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Optional

from ... import config
from . import log_parser

logger = logging.getLogger("whatsapp_automation.webhook.dashboard")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,            -- 'YYYY-MM-DD HH:MM:SS' (heure locale du log)
    type        TEXT NOT NULL,
    reason      TEXT,
    client_id   INTEGER,
    phone       TEXT,
    txn_id      TEXT,
    amount      INTEGER,
    balance     INTEGER,
    mac         TEXT,
    operator    TEXT,
    payment_id  TEXT,
    raw         TEXT,
    dedup_key   TEXT UNIQUE NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(type, ts);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_txn ON events(txn_id);
CREATE INDEX IF NOT EXISTS idx_events_payment ON events(payment_id);
"""

_INSERT = """
INSERT OR IGNORE INTO events
    (ts, type, reason, client_id, phone, txn_id, amount, balance, mac, operator,
     payment_id, raw, dedup_key)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_ingest_lock = threading.Lock()


def _path(db_path: Optional[str]) -> str:
    return db_path or config.EVENTS_DB_PATH


def _connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(_path(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(db_path: Optional[str] = None) -> None:
    from pathlib import Path
    Path(_path(db_path)).parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)


def _dedup_key(ts: str, etype: str, raw: str) -> str:
    return hashlib.sha1(f"{ts}|{etype}|{raw}".encode("utf-8")).hexdigest()


def ingest(events: list, db_path: Optional[str] = None) -> int:
    """Insère une liste d'Event (log_parser.Event) ; retourne le nb réellement ajouté."""
    rows = []
    for e in events:
        ts = e.ts.strftime("%Y-%m-%d %H:%M:%S")
        rows.append((
            ts, e.type, e.reason, e.client_id, e.phone, e.txn_id, e.amount,
            e.balance, e.mac, e.operator, e.payment_id, e.raw,
            _dedup_key(ts, e.type, e.raw),
        ))
    if not rows:
        return 0
    with _connect(db_path) as conn:
        before = conn.total_changes
        conn.executemany(_INSERT, rows)
        conn.commit()
        return conn.total_changes - before


def ingest_from_logs(db_path: Optional[str] = None) -> int:
    """Parse les logs et alimente la table. Sérialisé (un seul ingest à la fois)."""
    with _ingest_lock:
        events = log_parser.get_events(force=True)
        return ingest(events, db_path)


# --------------------------------------------------------------------------- #
# Requêtes (mêmes signatures/retours que log_parser, mais lues depuis la table).
# Les exclusions de "refus" sont appliquées en SQL (mêmes règles que le parser).
# --------------------------------------------------------------------------- #
_EXCL = sorted(log_parser.EXCLUDED_REFUSAL_REASONS)
_EXCL_PH = ",".join("?" * len(_EXCL))


def _since(days: Optional[int]) -> str:
    if not days:
        return "0000-00-00 00:00:00"
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


# Prédicat SQL : "vrai refus comptabilisé" (type refused, hors causes exclues).
_COUNTED_REFUSAL = f"type='refused' AND COALESCE(reason,'') NOT IN ({_EXCL_PH})"
# Prédicat SQL : event NON masqué (on cache les refus exclus partout).
_NOT_HIDDEN = f"NOT (type='refused' AND COALESCE(reason,'') IN ({_EXCL_PH}))"


def summary(days: Optional[int] = 30, db_path: Optional[str] = None) -> dict:
    since = _since(days)
    with _connect(db_path) as conn:
        by_type = {r["type"]: r["n"] for r in conn.execute(
            "SELECT type, COUNT(*) n FROM events WHERE ts>=? GROUP BY type", (since,))}
        refused = conn.execute(
            f"SELECT COUNT(*) n FROM events WHERE ts>=? AND {_COUNTED_REFUSAL}",
            (since, *_EXCL)).fetchone()["n"]
        unblocked_distinct = conn.execute(
            "SELECT COUNT(DISTINCT client_id) n FROM events "
            "WHERE ts>=? AND type='client_unblocked' AND client_id IS NOT NULL",
            (since,)).fetchone()["n"]
    return {
        "period_days": days,
        "payments_enqueued": by_type.get("payment_enqueued", 0),
        "ucrm_created": by_type.get("ucrm_created", 0),
        "messages_sent": by_type.get("message_sent", 0),
        "clients_unblocked": by_type.get("client_unblocked", 0),
        "clients_unblocked_distinct": unblocked_distinct,
        "subscriptions_activated": by_type.get("subscription_activated", 0),
        "underpayments": by_type.get("underpayment", 0),
        "refused_total": refused,
        "clients_not_found": by_type.get("client_not_found", 0),
        "support_notified": by_type.get("support_notified", 0),
        "recipient_suspect": by_type.get("recipient_suspect", 0),
    }


def refusals_by_cause(days: Optional[int] = 30, db_path: Optional[str] = None) -> dict:
    since = _since(days)
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT COALESCE(reason,'inconnu') reason, COUNT(*) n FROM events "
            f"WHERE ts>=? AND {_COUNTED_REFUSAL} GROUP BY reason ORDER BY n DESC",
            (since, *_EXCL)).fetchall()
    return {r["reason"]: r["n"] for r in rows}


def timeseries(days: int = 30, db_path: Optional[str] = None) -> dict:
    since = _since(days)
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT substr(ts,1,10) d, type, COUNT(*) n FROM events "
            f"WHERE ts>=? AND {_NOT_HIDDEN} GROUP BY d, type",
            (since, *_EXCL)).fetchall()
    buckets: dict[str, dict[str, int]] = {}
    for r in rows:
        buckets.setdefault(r["d"], {})[r["type"]] = r["n"]
    labels = sorted(buckets.keys())
    pick = lambda t: [buckets[d].get(t, 0) for d in labels]
    return {
        "labels": labels,
        "ucrm_created": pick("ucrm_created"),
        "refused": pick("refused"),
        "messages_sent": pick("message_sent"),
        "clients_unblocked": pick("client_unblocked"),
    }


def recent_events(
    limit: int = 150,
    type_filter: Optional[str] = None,
    days: Optional[int] = 30,
    q: Optional[str] = None,
    db_path: Optional[str] = None,
) -> list[dict]:
    since = _since(days)
    sql = f"SELECT * FROM events WHERE ts>=? AND {_NOT_HIDDEN}"
    params: list = [since, *_EXCL]
    if type_filter:
        sql += " AND type=?"
        params.append(type_filter)
    if q and q.strip():
        # Recherche libre : client, téléphone, transaction, paiement, ou texte brut
        # (le `raw` permet de retrouver par montant, mac, opérateur, etc.).
        like = f"%{q.strip()}%"
        sql += (" AND (CAST(client_id AS TEXT) LIKE ? OR COALESCE(phone,'') LIKE ? "
                "OR COALESCE(txn_id,'') LIKE ? OR COALESCE(payment_id,'') LIKE ? "
                "OR COALESCE(raw,'') LIKE ?)")
        params += [like, like, like, like, like]
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    with _connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [{
        "ts": r["ts"],
        "date": r["ts"][:10],
        "type": r["type"],
        "reason": r["reason"],
        "client_id": r["client_id"],
        "phone": r["phone"],
        "txn_id": r["txn_id"],
        "amount": r["amount"],
        "balance": r["balance"],
        "mac": r["mac"],
        "operator": r["operator"],
        "payment_id": r["payment_id"],
    } for r in rows]


def _row_to_dict(r) -> dict:
    return {
        "ts": r["ts"], "date": r["ts"][:10], "type": r["type"], "reason": r["reason"],
        "client_id": r["client_id"], "phone": r["phone"], "txn_id": r["txn_id"],
        "amount": r["amount"], "balance": r["balance"], "mac": r["mac"],
        "operator": r["operator"], "payment_id": r["payment_id"],
    }


def events_for_client(
    client_id: Optional[int],
    phones: Optional[list] = None,
    limit: int = 300,
    db_path: Optional[str] = None,
) -> list[dict]:
    """Tous les événements d'un client : par client_id OU par un de ses téléphones
    (certains types — reçu envoyé, client introuvable — n'ont que le téléphone)."""
    phones = [str(p) for p in (phones or []) if p]
    sub, sub_params = [], []
    if client_id:
        sub.append("client_id = ?")
        sub_params.append(client_id)
    if phones:
        sub.append("phone IN (%s)" % ",".join("?" * len(phones)))
        sub_params.extend(phones)
    if not sub:
        return []
    sql = (f"SELECT * FROM events WHERE {_NOT_HIDDEN} AND (" + " OR ".join(sub) + ")"
           " ORDER BY ts DESC LIMIT ?")
    params = [*_EXCL, *sub_params, limit]
    with _connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def count(db_path: Optional[str] = None) -> int:
    with _connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) n FROM events").fetchone()["n"]
