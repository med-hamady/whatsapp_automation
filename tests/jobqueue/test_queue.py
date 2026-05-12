"""Tests de la queue SQLite : enqueue, claim atomique, idempotence."""

import os
import tempfile

import pytest

from whatsapp_automation.models import Client, Job, Payment, Source


@pytest.fixture
def temp_queue(monkeypatch):
    """Crée une queue SQLite isolée par test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    monkeypatch.setenv("QUEUE_DB_PATH", path)
    # Force le rechargement du module config + store
    import importlib
    from whatsapp_automation import config
    importlib.reload(config)
    from whatsapp_automation.jobqueue import store
    importlib.reload(store)
    store.init_db()
    yield store
    try:
        os.unlink(path)
    except OSError:
        pass


def _make_job(txn_id: str = "TXN001", client_id: int = 1) -> Job:
    return Job(
        job_id=f"job-{txn_id}",
        client=Client(
            id=client_id, phone="37697850",
            mac_address="AA:BB:CC:00:00:01",
            ip_address="10.0.0.1",
            current_status="suspended",
        ),
        payment=Payment(
            amount_mru=1500, txn_id=txn_id, operator="bankily",
            crm_balance_before=1500, should_unblock=True,
        ),
        source=Source(wnum="37697850", sample_id="2026-05-11/abc", received_at="2026-05-11T10:00:00"),
    )


def test_enqueue_and_claim(temp_queue):
    store = temp_queue
    store.enqueue(_make_job("TXN001"))

    claimed = store.claim_next(worker_id="w1")
    assert claimed is not None
    assert claimed["job"].payment.txn_id == "TXN001"
    assert claimed["attempts"] == 1


def test_claim_returns_none_when_empty(temp_queue):
    assert temp_queue.claim_next("w1") is None


def test_claim_marks_processing_so_other_workers_skip(temp_queue):
    store = temp_queue
    store.enqueue(_make_job("TXN001"))
    first = store.claim_next("w1")
    second = store.claim_next("w2")
    assert first is not None
    assert second is None  # job déjà pris


def test_mark_done_and_idempotence(temp_queue):
    store = temp_queue
    store.enqueue(_make_job("TXN001"))
    claimed = store.claim_next("w1")
    store.mark_done(claimed["id"], "TXN001", ucrm_payment_id="1000")

    assert store.is_txn_processed("TXN001")
    assert not store.is_txn_in_flight("TXN001")


def test_in_flight_prevents_duplicate(temp_queue):
    store = temp_queue
    store.enqueue(_make_job("TXN001"))
    assert store.is_txn_in_flight("TXN001")
    # Pas encore traité
    assert not store.is_txn_processed("TXN001")


def test_retry_repushes_job(temp_queue):
    store = temp_queue
    store.enqueue(_make_job("TXN001"))
    claimed = store.claim_next("w1")
    store.mark_retry(claimed["id"], "Boom", backoff_seconds=0.0)

    # Récupérable à nouveau (next_attempt_at = maintenant)
    re_claimed = store.claim_next("w1")
    assert re_claimed is not None
    assert re_claimed["attempts"] == 2
