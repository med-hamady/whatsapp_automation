"""Smoke test : index UNIQUE partiel créé + dédup atomique fonctionne.

Lance N enqueue() en parallèle (threads) avec le même txn_id et vérifie
qu'un seul réussit. Reproduit la race condition TOCTOU du webhook.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from whatsapp_automation.jobqueue import store as q
from whatsapp_automation.models import Job, Client, Payment, Source


def main():
    q.init_db()

    with sqlite3.connect(q.config.QUEUE_DB_PATH) as c:
        rows = c.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND name='idx_jobs_txn_active'"
        ).fetchall()
        print("Index UNIQUE partiel existe:", bool(rows))
        if rows:
            print("  SQL:", rows[0][1])

    # Race test : N enqueues parallèles, même txn_id
    txn = f"TEST-RACE-{uuid.uuid4().hex[:8]}"

    def make_job(idx: int) -> Job:
        return Job(
            job_id=q.new_job_id(),
            client=Client(id=999999, phone="+22200000000", mac_address=None,
                          ip_address=None, current_status="active"),
            payment=Payment(amount_mru=100, txn_id=txn, date_heure=None,
                            operator="test", crm_balance_before=0, should_unblock=False),
            source=Source(wnum="+22200000000", sample_id=f"smoke-{idx}",
                          received_at="2026-06-04T00:00:00+00:00"),
        )

    N = 5
    results: list = [None] * N
    barrier = threading.Barrier(N)

    def worker(idx: int):
        job = make_job(idx)
        barrier.wait()
        results[idx] = q.enqueue(job)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads: t.start()
    for t in threads: t.join()

    inserted = [r for r in results if r is not None]
    skipped = [r for r in results if r is None]
    print(f"Race N={N} txn={txn} -> inserted={len(inserted)} skipped={len(skipped)}")
    assert len(inserted) == 1, f"BUG: {len(inserted)} jobs insérés (devrait être 1)"
    print("  OK : un seul job inséré, dédup atomique fonctionne")

    # Cleanup
    with sqlite3.connect(q.config.QUEUE_DB_PATH) as c:
        c.execute("DELETE FROM jobs WHERE txn_id = ?", (txn,))


if __name__ == "__main__":
    main()
