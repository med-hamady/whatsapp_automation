"""Boucle principale du worker : pull → process → ack/retry.

Lancement :
    python -m whatsapp_automation.worker.main

N workers en parallèle = lancer N processus (la queue SQLite gère le
locking atomique via BEGIN IMMEDIATE).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket

from .. import config
from ..jobqueue import store as queue_store
from . import handlers


logger = logging.getLogger("whatsapp_automation.worker")


_RUNNING = True


def _stop(*_):
    global _RUNNING
    _RUNNING = False
    logger.info("arrêt demandé, fin de la boucle après le job courant")


async def _run_once(worker_id: str) -> bool:
    """Tente de prendre 1 job et le traite. Retourne True si un job a été traité."""
    claimed = queue_store.claim_next(worker_id)
    if claimed is None:
        return False

    job = claimed["job"]
    internal_id = claimed["id"]
    attempt = claimed["attempts"]
    last_step = claimed.get("step_done")
    known_payment_id = claimed.get("ucrm_payment_id")

    logger.info(
        "claim job_id=%s attempt=%d last_step=%s known_payment_id=%s",
        job.job_id, attempt, last_step, known_payment_id,
    )

    def on_step_done(step: str):
        queue_store.mark_step_done(internal_id, step)

    def on_payment_created(payment_id: str):
        # Persiste IMMÉDIATEMENT le paymentId UCRM. Si le worker crashe juste
        # après, le prochain claim retrouvera ce paymentId via known_payment_id
        # et n'essaiera pas de re-créer un paiement chez UCRM (= double paiement).
        queue_store.set_payment_id(internal_id, payment_id)

    try:
        result = await handlers.process_job(
            job, last_step, on_step_done,
            known_payment_id=known_payment_id,
            on_payment_created=on_payment_created,
        )
        queue_store.mark_done(internal_id, job.payment.txn_id, result.ucrm_payment_id)
        logger.info("job %s ✅ done steps=%s", job.job_id, result.completed_steps)
    except Exception as exc:
        attempts = attempt
        max_attempts = 5
        if attempts < max_attempts:
            backoff = min(60.0 * (2 ** (attempts - 1)), 600.0)
            queue_store.mark_retry(internal_id, repr(exc), backoff_seconds=backoff)
            logger.warning("job %s ⚠ retry dans %.0fs (attempt %d/%d): %s",
                           job.job_id, backoff, attempts, max_attempts, exc)
        else:
            queue_store.mark_failed(internal_id, repr(exc))
            logger.error("job %s ❌ failed définitivement: %s", job.job_id, exc)

    return True


async def run_forever():
    queue_store.init_db()
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    logger.info("worker %s démarré, poll=%.1fs", worker_id, config.WORKER_POLL_INTERVAL)

    while _RUNNING:
        did_work = await _run_once(worker_id)
        if not did_work:
            await asyncio.sleep(config.WORKER_POLL_INTERVAL)

    logger.info("worker %s arrêté proprement", worker_id)


def main():
    from logging.handlers import RotatingFileHandler
    log_dir = os.path.join(os.getcwd(), "data", "logs")
    os.makedirs(log_dir, exist_ok=True)
    # Un fichier par PID pour éviter que deux workers concurrents se
    # marchent dessus en écriture sur le même fichier.
    log_file = os.path.join(log_dir, f"worker-{os.getpid()}.log")
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")
    fh = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh], force=True)
    signal.signal(signal.SIGINT, _stop)
    try:
        signal.signal(signal.SIGTERM, _stop)
    except (AttributeError, ValueError):
        pass  # Windows
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
