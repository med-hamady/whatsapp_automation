"""Queue SQLite : persistance + claim atomique pour N workers concurrents.

SQLite gère la concurrence via BEGIN IMMEDIATE. Avec quelques workers
(2-4) le throughput est largement suffisant.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Optional

from ..models import Job
from .. import config


_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        config.QUEUE_DB_PATH,
        isolation_level=None,        # autocommit ; on gère BEGIN nous-mêmes
        timeout=10.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Crée le fichier queue.db et les tables si absentes.

    Inclut une mini-migration en place pour les déploiements existants : si la
    table jobs a été créée avant l'ajout de `ucrm_payment_id`, on l'ajoute via
    ALTER. SQLite est tolérant : ADD COLUMN sans valeur par défaut est une
    opération en O(1)."""
    Path(config.QUEUE_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        # Migration en place pour les queue.db existants
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        if "ucrm_payment_id" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN ucrm_payment_id TEXT")


def is_txn_processed(txn_id: str) -> bool:
    """Vérifie si un txn_id a déjà été traité avec succès."""
    if not txn_id:
        return False
    with _connect() as conn:
        cur = conn.execute(
            "SELECT 1 FROM processed_payments WHERE txn_id = ?",
            (txn_id,),
        )
        return cur.fetchone() is not None


def is_txn_in_flight(txn_id: str) -> bool:
    """Vérifie si un job avec ce txn_id est déjà en queue (pending/processing)."""
    if not txn_id:
        return False
    with _connect() as conn:
        cur = conn.execute(
            "SELECT 1 FROM jobs WHERE txn_id = ? AND status IN ('pending', 'processing', 'retry')",
            (txn_id,),
        )
        return cur.fetchone() is not None


def enqueue(job: Job) -> Optional[int]:
    """Insère un job en queue de façon atomique avec dédup par txn_id.

    Retourne l'id interne sur succès, ou None si un job avec le même txn_id
    est déjà traité (processed_payments) ou déjà en queue (pending/processing/
    retry). La déduplication est faite dans une BEGIN IMMEDIATE pour fermer la
    fenêtre TOCTOU entre les check `is_txn_*` du pipeline et l'INSERT — sinon
    plusieurs webhooks UltraMsg du même reçu reçus en parallèle peuvent tous
    passer le check avant qu'aucun n'ait inséré, et on crée N paiements UCRM
    pour le même txn_id."""
    now = time.time()
    payload = job.model_dump_json()
    txn_id = job.payment.txn_id or ""
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if txn_id:
                cur = conn.execute(
                    "SELECT 1 FROM processed_payments WHERE txn_id = ?",
                    (txn_id,),
                )
                if cur.fetchone() is not None:
                    conn.execute("COMMIT")
                    return None
                cur = conn.execute(
                    """SELECT 1 FROM jobs
                       WHERE txn_id = ? AND status IN ('pending', 'processing', 'retry')""",
                    (txn_id,),
                )
                if cur.fetchone() is not None:
                    conn.execute("COMMIT")
                    return None
            try:
                cur = conn.execute(
                    """INSERT INTO jobs
                       (job_id, txn_id, payload_json, status, next_attempt_at, created_at)
                       VALUES (?, ?, ?, 'pending', ?, ?)""",
                    (job.job_id, txn_id, payload, now, now),
                )
            except sqlite3.IntegrityError:
                # Filet de sécurité : si l'index UNIQUE partiel sur txn_id a
                # détecté un doublon (théoriquement impossible vu les checks
                # ci-dessus dans la même transaction, mais on garde la garantie
                # niveau schéma).
                conn.execute("ROLLBACK")
                return None
            conn.execute("COMMIT")
            return cur.lastrowid
        except Exception:
            conn.execute("ROLLBACK")
            raise


def claim_next(worker_id: str) -> Optional[dict]:
    """Atomiquement : prend le prochain job 'pending' dont next_attempt_at est échu.
    Le marque 'processing' et retourne le dict {id, job_id, job, attempts}.
    None s'il n'y a rien à faire."""
    now = time.time()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                """SELECT id, job_id, payload_json, attempts, step_done, ucrm_payment_id
                   FROM jobs
                   WHERE status IN ('pending', 'retry') AND next_attempt_at <= ?
                   ORDER BY id ASC
                   LIMIT 1""",
                (now,),
            )
            row = cur.fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            conn.execute(
                """UPDATE jobs
                   SET status = 'processing', worker_id = ?, started_at = ?, attempts = attempts + 1
                   WHERE id = ?""",
                (worker_id, now, row["id"]),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        return {
            "id": row["id"],
            "job_id": row["job_id"],
            "job": Job.model_validate_json(row["payload_json"]),
            "attempts": row["attempts"] + 1,
            "step_done": row["step_done"],
            "ucrm_payment_id": row["ucrm_payment_id"],
        }


def mark_step_done(job_internal_id: int, step: str) -> None:
    """Persiste l'avancement (reprise sur incident sans répéter une étape déjà faite)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE jobs SET step_done = ? WHERE id = ?",
            (step, job_internal_id),
        )


def set_payment_id(job_internal_id: int, ucrm_payment_id: str) -> None:
    """Persiste le paymentId UCRM dès que le paiement a été créé côté UCRM.

    Indispensable pour que les retries du job ne perdent pas l'identifiant
    après un crash entre l'étape PAID_UCRM et les étapes suivantes (DB,
    MikroTik, PDF) — sinon on ne saurait plus rattacher le paiement déjà
    enregistré dans UCRM."""
    with _connect() as conn:
        conn.execute(
            "UPDATE jobs SET ucrm_payment_id = ? WHERE id = ?",
            (ucrm_payment_id, job_internal_id),
        )


def mark_done(job_internal_id: int, txn_id: str, ucrm_payment_id: Optional[str]) -> None:
    """Marque le job comme terminé et insère dans processed_payments (idempotence)."""
    now = time.time()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE jobs SET status = 'done', finished_at = ? WHERE id = ?",
                (now, job_internal_id),
            )
            cur = conn.execute(
                "SELECT job_id FROM jobs WHERE id = ?",
                (job_internal_id,),
            )
            job_id = cur.fetchone()["job_id"]
            conn.execute(
                """INSERT OR REPLACE INTO processed_payments
                   (txn_id, ucrm_payment_id, job_id, processed_at)
                   VALUES (?, ?, ?, ?)""",
                (txn_id, ucrm_payment_id, job_id, now),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def mark_retry(job_internal_id: int, error: str, backoff_seconds: float = 30.0) -> None:
    """Repousse le job pour une nouvelle tentative."""
    next_at = time.time() + backoff_seconds
    with _connect() as conn:
        conn.execute(
            """UPDATE jobs
               SET status = 'retry', last_error = ?, next_attempt_at = ?
               WHERE id = ?""",
            (error, next_at, job_internal_id),
        )


def mark_failed(job_internal_id: int, error: str) -> None:
    """Abandon définitif (alerter humain)."""
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """UPDATE jobs
               SET status = 'failed', last_error = ?, finished_at = ?
               WHERE id = ?""",
            (error, now, job_internal_id),
        )


def get_job_by_payment_id(payment_id: str) -> Optional[Job]:
    """Retrouve un job par son paymentId UCRM.

    Utilisé par le dashboard pour reconstituer EXACTEMENT le reçu envoyé au
    client (le payload contient montants, solde et statut de déblocage, qui
    déterminent le texte du message). Retourne None si introuvable/illisible.
    """
    if not payment_id:
        return None
    with _connect() as conn:
        cur = conn.execute(
            "SELECT payload_json FROM jobs WHERE ucrm_payment_id = ? ORDER BY id DESC LIMIT 1",
            (str(payment_id),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    try:
        return Job.model_validate_json(row["payload_json"])
    except Exception:
        return None


def find_job_by_txn(txn_id: str) -> Optional[dict]:
    """Retrouve le job le plus récent pour un txn_id, actif ou terminé avec
    succès (pending/processing/retry/done) — exclut 'failed' (un job abandonné
    n'empêche pas d'en recréer un, cf. `enqueue`).

    Utilisé par la confirmation dashboard (Phase 4B-2) pour décider, quand
    `enqueue()` renvoie None (dédup) ou quand une confirmation reste bloquée
    en 'confirming' (crash entre l'enqueue et le mark_queued côté
    numeros_introuvable), si un Job existe déjà pour ce reçu et peut être
    rattaché sans jamais en créer un second. Retourne {"id", "job_id",
    "status"} ou None."""
    if not txn_id:
        return None
    with _connect() as conn:
        cur = conn.execute(
            """SELECT id, job_id, status FROM jobs
               WHERE txn_id = ? AND status IN ('pending', 'processing', 'retry', 'done')
               ORDER BY id DESC LIMIT 1""",
            (txn_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"id": row["id"], "job_id": row["job_id"], "status": row["status"]}


def stats() -> dict:
    """Retourne {pending, processing, done, failed, retry} pour monitoring."""
    with _connect() as conn:
        cur = conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
        result = {"pending": 0, "processing": 0, "done": 0, "failed": 0, "retry": 0}
        for row in cur:
            result[row["status"]] = row["n"]
        return result


def new_job_id() -> str:
    return uuid.uuid4().hex
