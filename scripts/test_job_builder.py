"""Tests Phase 4B-1 : extraction de job_builder.py depuis pipeline.py.

Objectif : prouver que la logique déplacée (ucrm_with_retry, fetch_ucrm_context,
compute_unblock_plan, build_job) produit EXACTEMENT le même résultat que
l'ancienne implémentation monolithique de pipeline.process(), sur les cas
métier documentés dans pipeline.py avant refactor :
  - abonnement suspendu unique, paiement complet ;
  - plusieurs abonnements suspendus, paiement partiel (moins chers d'abord) ;
  - tolérance de sous-paiement (abo marginal) ;
  - service UCRM sans MAC exploitable → repli MAC locaux (solde couvert) ;
  - service UCRM sans prix exploitable → repli MAC locaux (solde couvert) ;
  - aucun déblocage (sous-paiement hors tolérance, mode mono-abo) ;
  - repli mono-abo historique (services UCRM indisponibles) ;
  - Job.client.phone == phone_for_worker fourni (indépendant de wnum).

Deux niveaux :
  A. Unit : job_builder.compute_unblock_plan / build_job / ucrm_with_retry /
     fetch_ucrm_context appelés directement (pas de réseau réel : UCRM est
     monkeypatché).
  B. Intégration : pipeline.process() de bout en bout avec PostgreSQL, UCRM,
     ai_ocr et téléchargement d'image tous monkeypatchés — prouve que
     pipeline.py délègue correctement à job_builder sans changer le
     comportement webhook normal.

100% local, aucune écriture PostgreSQL, aucun appel réseau réel (UCRM/MikroTik/
UltraMsg/ai_ocr patchés). À lancer : python scripts/test_job_builder.py
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Isole des données réelles : bases dédiées, fixées AVANT le premier import de
# whatsapp_automation (config lit l'env à l'import).
_TMP = Path(tempfile.mkdtemp(prefix="job_builder_test_"))
os.environ["QUEUE_DB_PATH"] = str(_TMP / "queue.db")
os.environ["UNKNOWN_CLIENTS_DB_PATH"] = str(_TMP / "unknown_clients.db")
os.environ["WHATSAPP_CRM_MAPPINGS_DB_PATH"] = str(_TMP / "whatsapp_crm_mappings.db")
os.environ["EVENTS_DB_PATH"] = str(_TMP / "events.db")
os.environ["SUPPORT_RECIPIENT"] = ""  # notifs support désactivées (pas d'appel UltraMsg)
os.environ["UNDERPAYMENT_TOLERANCE"] = "150"

from whatsapp_automation.db import postgres as pg  # noqa: E402
from whatsapp_automation.jobqueue import store as queue_store  # noqa: E402
from whatsapp_automation.webhook import job_builder, pipeline  # noqa: E402
from whatsapp_automation.worker import mikrotik, ucrm, ultramsg  # noqa: E402

THRESHOLD = 150

passed = failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    marker = " " if cond else ">"
    print(f"{marker}[{'PASS' if cond else 'FAIL'}] {label}" + (f"  -- {detail}" if detail else ""))
    passed += bool(cond)
    failed += not cond


def _forbid(*_a, **_kw):
    raise AssertionError("appel externe interdit dans ce test (UCRM/MikroTik/UltraMsg réels)")


def _patch_forbidden_calls() -> None:
    """Aucun appel réel ne doit jamais sortir : create_payment/unblock/send_*
    sont patchés pour lever si sollicités (défense en profondeur, en plus des
    monkeypatches ciblés de get_client_details/get_client_services)."""
    ucrm.create_payment = _forbid
    ucrm.get_balance = _forbid
    mikrotik.unblock_by_mac = _forbid
    mikrotik.block_by_mac = _forbid
    ultramsg.send_chat = _forbid
    ultramsg.send_document = _forbid
    ultramsg.send_image = _forbid


def client_row(idclient: int, mac: str, statu: int, ip: str = "10.0.0.1", info: str = "") -> dict:
    return {"idclient": idclient, "info": info or f"37600000-Client {idclient}",
            "mac": mac, "statu": statu, "ipaddress": ip}


def svc(mac: str, price, status: int = 3) -> dict:
    return {"status": status, "mac": mac, "price": price}


def details(balance: int, credit: int = 0) -> dict:
    return {"balance": balance, "account_credit": credit}


# ---------------------------------------------------------------------------
# A. Tests unitaires directs sur job_builder
# ---------------------------------------------------------------------------

def test_single_suspended_full_payment() -> None:
    row = client_row(501, "AA:AA:AA:AA:AA:01", statu=2)
    decision = job_builder.compute_unblock_plan(
        client_row=row, client_rows=[row],
        details=details(1500), services=[svc("AA:AA:AA:AA:AA:01", 1500)],
        amount_paid=1500, crm_balance=1500, threshold=THRESHOLD,
    )
    check("1 abo suspendu, payé=solde exact → débloqué",
          decision.unblock_macs == ["AA:AA:AA:AA:AA:01"] and decision.unblock is True,
          str(decision))


def test_multiple_suspended_partial_payment() -> None:
    rows = [
        client_row(502, "BB:01", statu=2),
        client_row(502, "BB:02", statu=2),
        client_row(502, "BB:03", statu=2),
    ]
    services = [svc("BB:01", 2000), svc("BB:02", 1500), svc("BB:03", 1500)]
    decision = job_builder.compute_unblock_plan(
        client_row=rows[0], client_rows=rows,
        details=details(5000), services=services,
        amount_paid=3000, crm_balance=5000, threshold=THRESHOLD,
    )
    check("3 abos suspendus, paiement partiel → 2 moins chers débloqués (BB:02, BB:03)",
          decision.unblock_macs == ["BB:02", "BB:03"], str(decision))


def test_tolerance_marginal_subscription() -> None:
    row = client_row(503, "CC:01", statu=2)
    decision = job_builder.compute_unblock_plan(
        client_row=row, client_rows=[row],
        details=details(1500), services=[svc("CC:01", 1500)],
        amount_paid=1490, crm_balance=1500, threshold=THRESHOLD,
    )
    check("1 abo 1500, payé 1490 (écart 10 ≤ tolérance 150) → débloqué",
          decision.unblock_macs == ["CC:01"], str(decision))


def test_missing_mac_falls_back_to_local() -> None:
    row = client_row(504, "DD:CC:CC:CC:CC:01", statu=2)
    decision = job_builder.compute_unblock_plan(
        client_row=row, client_rows=[row],
        details=details(1500), services=[svc("", 1500)],  # MAC UCRM absente
        amount_paid=1500, crm_balance=1500, threshold=THRESHOLD,
    )
    check("service UCRM sans MAC exploitable, solde couvert → repli MAC locaux",
          decision.unblock_macs == ["DD:CC:CC:CC:CC:01"], str(decision))


def test_missing_price_falls_back_to_local_pg_mac() -> None:
    row = client_row(505, "EE:DD:DD:DD:DD:01", statu=2)
    decision = job_builder.compute_unblock_plan(
        client_row=row, client_rows=[row],
        details=details(1500), services=[svc("EE:DD:DD:DD:DD:01", None)],  # prix UCRM absent
        amount_paid=1500, crm_balance=1500, threshold=THRESHOLD,
    )
    check("service UCRM sans prix exploitable, solde couvert → repli MAC locaux PostgreSQL",
          decision.unblock_macs == ["EE:DD:DD:DD:DD:01"], str(decision))


def test_no_unblock_underpayment_mono_abo() -> None:
    row = client_row(506, "FF:01", statu=2)
    decision = job_builder.compute_unblock_plan(
        client_row=row, client_rows=[row],
        details=details(1500), services=None,  # services UCRM indisponibles
        amount_paid=1000, crm_balance=1500, threshold=THRESHOLD,
    )
    check("services UCRM indispo, sous-paiement hors tolérance (écart 500 > 150) → aucun déblocage",
          decision.unblock_macs == [] and decision.unblock is False, str(decision))


def test_historical_single_subscription_fallback_unblocks() -> None:
    row = client_row(507, "GG:01", statu=2)
    decision = job_builder.compute_unblock_plan(
        client_row=row, client_rows=[row],
        details=details(1500), services=[],  # services UCRM vides (pas None : autre repli)
        amount_paid=1450, crm_balance=1500, threshold=THRESHOLD,
    )
    check("repli mono-abo historique : écart 50 ≤ tolérance → débloqué (MAC principal client_row)",
          decision.unblock_macs == ["GG:01"], str(decision))


def test_build_job_phone_for_worker_independent_of_wnum() -> None:
    row = client_row(508, "HH:01", statu=2)
    job = job_builder.build_job(
        client_row=row, amount_paid=1500, txn_id="TXN-BJ-1", date_heure="2026-07-13 10:00:00",
        template="bankily", crm_balance=1500, unblock_macs=["HH:01"],
        phone_for_worker="46600099", wnum="37600001", sample_id="2026-07-13/aaaa",
    )
    check("Job.client.phone == phone_for_worker fourni",
          job.client.phone == "46600099", job.client.phone)
    check("Job.source.wnum == wnum fourni (indépendant de client.phone)",
          job.source.wnum == "37600001", job.source.wnum)
    check("Job.payment.should_unblock cohérent avec unblock_macs non vide",
          job.payment.should_unblock is True)
    check("Job.client.current_status='suspended' (client_row.statu==2)",
          job.client.current_status == "suspended")


async def _fake_ucrm_ok(client_id: int) -> dict:
    return {"id": client_id, "balance": 1500}


async def _fake_ucrm_fail(client_id: int) -> dict:
    raise ConnectionError("simulated network failure")


def test_ucrm_with_retry_success_no_sleep() -> None:
    result = asyncio.run(job_builder.ucrm_with_retry(lambda: _fake_ucrm_ok(1), "get_client_details", 1))
    check("ucrm_with_retry : succès 1re tentative", result == {"id": 1, "balance": 1500})


def test_ucrm_with_retry_exhausted_returns_none() -> None:
    # Délais raccourcis pour ne pas attendre les 4s réelles (0+1+3) du prod.
    original_delays = job_builder.UCRM_GET_BALANCE_DELAYS
    job_builder.UCRM_GET_BALANCE_DELAYS = (0, 0, 0)
    try:
        result = asyncio.run(job_builder.ucrm_with_retry(lambda: _fake_ucrm_fail(1), "get_client_details", 1))
    finally:
        job_builder.UCRM_GET_BALANCE_DELAYS = original_delays
    check("ucrm_with_retry : échec réseau répété → None (pas d'exception propagée)", result is None)


def test_fetch_ucrm_context_parallel() -> None:
    async def fake_details(cid):
        return {"balance": 1500, "account_credit": 0}

    async def fake_services(cid):
        return [svc("II:01", 1500)]

    original_details, original_services = ucrm.get_client_details, ucrm.get_client_services
    ucrm.get_client_details, ucrm.get_client_services = fake_details, fake_services
    try:
        details_result, services_result = asyncio.run(job_builder.fetch_ucrm_context(999))
    finally:
        ucrm.get_client_details, ucrm.get_client_services = original_details, original_services
    check("fetch_ucrm_context : détails + services récupérés en parallèle",
          details_result == {"balance": 1500, "account_credit": 0} and services_result == [svc("II:01", 1500)])


# ---------------------------------------------------------------------------
# B. Intégration : pipeline.process() de bout en bout (webhook normal)
# ---------------------------------------------------------------------------

FAKE_PG_CLIENTS: dict[str, list[dict]] = {}
FAKE_UCRM_DETAILS: dict[int, dict] = {}
FAKE_UCRM_SERVICES: dict[int, list[dict]] = {}
FAKE_OCR_RESULT: dict = {}


def _install_pipeline_fakes() -> None:
    def fake_get_clients_by_phone(phone: str) -> list[dict]:
        return FAKE_PG_CLIENTS.get(phone, [])

    async def fake_get_client_details(client_id: int) -> dict:
        return FAKE_UCRM_DETAILS.get(client_id) or {}

    async def fake_get_client_services(client_id: int) -> list[dict]:
        return FAKE_UCRM_SERVICES.get(client_id, [])

    async def fake_download(url: str) -> bytes:
        return b"fake-image-bytes"

    async def fake_ai_ocr_extract(image_bytes: bytes, filename: str = "receipt.jpg") -> dict:
        return FAKE_OCR_RESULT

    pg.get_clients_by_phone = fake_get_clients_by_phone
    ucrm.get_client_details = fake_get_client_details
    ucrm.get_client_services = fake_get_client_services
    pipeline.download_image = fake_download
    pipeline.ai_ocr_extract = fake_ai_ocr_extract


def _ultramsg_payload(from_digits: str, sample_txn: str) -> dict:
    return {
        "data": {
            "from": f"222{from_digits}@c.us",
            "author": "",
            "body": "",
            "media": "https://fake-s3/fake.jpg",
            "type": "image",
        }
    }


def test_pipeline_end_to_end_single_subscription() -> None:
    FAKE_PG_CLIENTS.clear()
    FAKE_PG_CLIENTS["37611001"] = [client_row(601, "JJ:01", statu=2)]
    FAKE_UCRM_DETAILS[601] = details(1500)
    FAKE_UCRM_SERVICES[601] = [svc("JJ:01", 1500)]
    FAKE_OCR_RESULT.clear()
    FAKE_OCR_RESULT.update({
        "extracted": {"montant": 1500, "txn_id": "TXN-E2E-1", "date_heure": "2026-07-13 09:00:00"},
        "sample_id": "2026-07-13/e2e1",
        "template": "bankily",
        "raw_text": "PATRINET NKTT reçu 1500 MRU",
    })

    result = asyncio.run(pipeline.process(_ultramsg_payload("37611001", "TXN-E2E-1")))
    check("pipeline E2E (1 abo, paiement complet) : status=enqueued",
          result.get("status") == "enqueued", result)
    check("pipeline E2E : unblock_macs=['JJ:01']", result.get("unblock_macs") == ["JJ:01"], result)
    check("pipeline E2E : client_id=601", result.get("client_id") == 601)

    claimed = queue_store.claim_next("test-worker")
    check("job récupérable en queue avec le bon txn_id",
          claimed is not None and claimed["job"].payment.txn_id == "TXN-E2E-1")
    if claimed:
        check("Job.client.phone == from_phone (pas de body_phone ici)",
              claimed["job"].client.phone == "37611001")
        check("Job.source.wnum == from_phone", claimed["job"].source.wnum == "37611001")
        check("Job.unblock_macs == ['JJ:01']", claimed["job"].unblock_macs == ["JJ:01"])
        check("Job.payment.should_unblock is True", claimed["job"].payment.should_unblock is True)


def test_pipeline_end_to_end_no_unblock_underpayment() -> None:
    FAKE_PG_CLIENTS.clear()
    FAKE_PG_CLIENTS["37611002"] = [client_row(602, "KK:01", statu=2)]
    FAKE_UCRM_DETAILS[602] = details(1500)
    FAKE_UCRM_SERVICES[602] = None  # services indisponibles → mode mono-abo
    FAKE_OCR_RESULT.clear()
    FAKE_OCR_RESULT.update({
        "extracted": {"montant": 1000, "txn_id": "TXN-E2E-2", "date_heure": "2026-07-13 09:05:00"},
        "sample_id": "2026-07-13/e2e2",
        "template": "bankily",
        "raw_text": "PATRINET NKTT reçu 1000 MRU",
    })

    result = asyncio.run(pipeline.process(_ultramsg_payload("37611002", "TXN-E2E-2")))
    check("pipeline E2E (sous-paiement hors tolérance) : status=enqueued (paiement quand même enregistré)",
          result.get("status") == "enqueued", result)
    check("pipeline E2E : should_unblock=False", result.get("should_unblock") is False, result)
    check("pipeline E2E : unblock_macs=[]", result.get("unblock_macs") == [], result)
    # Draine la queue (sinon claim_next() des tests suivants récupère ce job
    # au lieu du leur — FIFO par id ASC dans claim_next).
    claimed = queue_store.claim_next("test-worker")
    check("job (sous-paiement) drainé de la queue", claimed is not None and claimed["job"].payment.txn_id == "TXN-E2E-2")


def test_pipeline_end_to_end_body_phone_fallback_phone_split() -> None:
    """from_phone absent (sender non résolu) mais body_phone présent : le
    client est trouvé via body_phone. Vérifie la règle historique préservée :
    Job.client.phone = body_phone, Job.source.wnum = from_phone (vide ici)."""
    FAKE_PG_CLIENTS.clear()
    FAKE_PG_CLIENTS["46622003"] = [client_row(603, "LL:01", statu=2)]
    FAKE_UCRM_DETAILS[603] = details(1500)
    FAKE_UCRM_SERVICES[603] = [svc("LL:01", 1500)]
    FAKE_OCR_RESULT.clear()
    FAKE_OCR_RESULT.update({
        "extracted": {"montant": 1500, "txn_id": "TXN-E2E-3", "date_heure": "2026-07-13 09:10:00"},
        "sample_id": "2026-07-13/e2e3",
        "template": "bankily",
        "raw_text": "PATRINET NKTT reçu 1500 MRU pour 46622003",
    })

    payload = {
        "data": {
            "from": "@c.us",  # from_phone se résoudra en "" (pas de chiffres)
            "author": "",
            "body": "paiement pour 46622003",
            "media": "https://fake-s3/fake.jpg",
            "type": "image",
        }
    }
    result = asyncio.run(pipeline.process(payload))
    check("pipeline E2E (fallback body_phone) : status=enqueued", result.get("status") == "enqueued", result)
    claimed = queue_store.claim_next("test-worker")
    check("job récupéré (fallback body_phone)", claimed is not None and claimed["job"].payment.txn_id == "TXN-E2E-3")
    if claimed:
        check("Job.client.phone == body_phone ('46622003')",
              claimed["job"].client.phone == "46622003", claimed["job"].client.phone)
        check("Job.source.wnum == from_phone ('' , comportement historique préservé)",
              claimed["job"].source.wnum == "", repr(claimed["job"].source.wnum))


def test_pipeline_end_to_end_client_not_found_unaffected() -> None:
    """Le chemin client_not_found (unrelated à job_builder) doit rester intact :
    aucun Job créé, enregistrement numeros_introuvable créé."""
    from whatsapp_automation.webhook.dashboard import unknown_clients_store

    FAKE_PG_CLIENTS.clear()  # aucun client ne matche
    FAKE_OCR_RESULT.clear()
    FAKE_OCR_RESULT.update({
        "extracted": {"montant": 800, "txn_id": "TXN-E2E-4", "date_heure": "2026-07-13 09:15:00"},
        "sample_id": "2026-07-13/e2e4",
        "template": "bankily",
        "raw_text": "PATRINET NKTT reçu 800 MRU",
    })
    stats_before = queue_store.stats()
    result = asyncio.run(pipeline.process(_ultramsg_payload("37611099", "TXN-E2E-4")))
    check("pipeline E2E (client introuvable) : status=skipped/client_not_found",
          result == {"status": "skipped", "reason": "client_not_found"}, result)
    check("aucun Job créé (stats file d'attente inchangées)", queue_store.stats() == stats_before)
    rec = unknown_clients_store.get_by_sample_id("2026-07-13/e2e4")
    check("enregistrement numeros_introuvable créé", rec is not None and rec.get("txn_id") == "TXN-E2E-4")


def main() -> int:
    queue_store.init_db()
    from whatsapp_automation.webhook.dashboard import unknown_clients_store
    unknown_clients_store.init_db()
    _patch_forbidden_calls()
    _install_pipeline_fakes()

    print("=== A. job_builder (unit) ===\n")
    test_single_suspended_full_payment()
    test_multiple_suspended_partial_payment()
    test_tolerance_marginal_subscription()
    test_missing_mac_falls_back_to_local()
    test_missing_price_falls_back_to_local_pg_mac()
    test_no_unblock_underpayment_mono_abo()
    test_historical_single_subscription_fallback_unblocks()
    test_build_job_phone_for_worker_independent_of_wnum()
    test_ucrm_with_retry_success_no_sleep()
    test_ucrm_with_retry_exhausted_returns_none()
    test_fetch_ucrm_context_parallel()

    print("\n=== B. pipeline.process() (intégration, webhook normal) ===\n")
    test_pipeline_end_to_end_single_subscription()
    test_pipeline_end_to_end_no_unblock_underpayment()
    test_pipeline_end_to_end_body_phone_fallback_phone_split()
    test_pipeline_end_to_end_client_not_found_unaffected()

    print(f"\n=== {passed} PASS / {failed} FAIL ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
