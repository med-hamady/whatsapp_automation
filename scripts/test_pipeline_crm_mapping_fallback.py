"""Tests d'intégration : repli mapping WhatsApp → CRM dans pipeline.process().

Vérifie :
  1. lookup téléphone OK -> le mapping n'est JAMAIS consulté (le repli ne
     remplace pas le lookup normal, même si une correspondance existe vers un
     autre client) ;
  2. lookup téléphone KO + correspondance active -> client rechargé par
     pg.get_client_by_id, flux normal continue (Job enqueued, client.id =
     idclient mappé, client.phone/wnum = from_phone), AUCUN enregistrement
     numeros_introuvable créé ;
  3. lookup téléphone KO + pas de correspondance -> comportement
     client_not_found historique intact (skip + enregistrement
     numeros_introuvable) ;
  4. correspondance périmée (idclient disparu de PostgreSQL) -> retombe sur
     client_not_found (jamais de crash) ;
  5. parcours complet : 1er reçu -> client_not_found -> association par
     identifiant CRM via la route dashboard (AUCUNE correspondance créée à ce
     stade — décision Ali) -> confirmation -> statut 'queued' -> correspondance
     créée -> 2e reçu du MÊME numéro reconnu automatiquement via la
     correspondance, enqueued, aucun nouveau numeros_introuvable ;
  6. aucun appel réel UCRM create_payment / MikroTik / UltraMsg (patchés pour
     lever si appelés) ; worker jamais démarré.

100% local : bases SQLite temporaires, PostgreSQL/UCRM/ai_ocr/téléchargement
tous monkeypatchés. À lancer : python scripts/test_pipeline_crm_mapping_fallback.py
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

_TMP = Path(tempfile.mkdtemp(prefix="crm_fallback_test_"))
os.environ["QUEUE_DB_PATH"] = str(_TMP / "queue.db")
os.environ["UNKNOWN_CLIENTS_DB_PATH"] = str(_TMP / "unknown_clients.db")
os.environ["WHATSAPP_CRM_MAPPINGS_DB_PATH"] = str(_TMP / "whatsapp_crm_mappings.db")
os.environ["EVENTS_DB_PATH"] = str(_TMP / "events.db")
os.environ["SUPPORT_RECIPIENT"] = ""  # notifs support désactivées (pas d'appel UltraMsg)
os.environ["UNDERPAYMENT_TOLERANCE"] = "150"
os.environ["DASHBOARD_PASSWORD"] = "test-password-fallback"

from whatsapp_automation.db import postgres as pg  # noqa: E402
from whatsapp_automation.jobqueue import store as queue_store  # noqa: E402
from whatsapp_automation.webhook import crm_mappings, pipeline  # noqa: E402
from whatsapp_automation.webhook.dashboard import unknown_clients_store  # noqa: E402
from whatsapp_automation.worker import mikrotik, ucrm, ultramsg  # noqa: E402

MAP_DB = os.environ["WHATSAPP_CRM_MAPPINGS_DB_PATH"]

passed = failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    marker = " " if cond else ">"
    print(f"{marker}[{'PASS' if cond else 'FAIL'}] {label}" + (f"  -- {detail}" if detail else ""))
    passed += bool(cond)
    failed += not cond


def _forbid(name):
    def _raise(*_a, **_kw):
        raise AssertionError(f"appel externe interdit dans ce test : {name}")
    return _raise


def _patch_forbidden_calls() -> None:
    ucrm.create_payment = _forbid("ucrm.create_payment")
    ucrm.get_balance = _forbid("ucrm.get_balance")
    mikrotik.unblock_by_mac = _forbid("mikrotik.unblock_by_mac")
    mikrotik.block_by_mac = _forbid("mikrotik.block_by_mac")
    ultramsg.send_chat = _forbid("ultramsg.send_chat")
    ultramsg.send_document = _forbid("ultramsg.send_document")
    ultramsg.send_image = _forbid("ultramsg.send_image")
    pg.insert_paiement = _forbid("pg.insert_paiement")
    pg.update_client_status = _forbid("pg.update_client_status")
    pg.update_client_status_by_mac = _forbid("pg.update_client_status_by_mac")


def client_row(idclient: int, mac: str, statu: int, ip: str = "10.0.0.1", info: str = "") -> dict:
    return {"idclient": idclient, "info": info or f"37600000-Client {idclient}",
            "mac": mac, "statu": statu, "ipaddress": ip}


def svc(mac: str, price, status: int = 3) -> dict:
    return {"status": status, "mac": mac, "price": price}


FAKE_PG_CLIENTS: dict[str, list[dict]] = {}     # téléphone -> lignes
FAKE_PG_BY_ID: dict[int, list[dict]] = {}       # idclient  -> lignes
FAKE_UCRM_DETAILS: dict[int, dict] = {}
FAKE_UCRM_SERVICES: dict[int, list[dict]] = {}
FAKE_OCR_RESULT: dict = {}
PG_BY_ID_CALLS: list = []                       # trace des lookups par idclient


def _install_pipeline_fakes() -> None:
    def fake_get_clients_by_phone(phone: str) -> list[dict]:
        return FAKE_PG_CLIENTS.get(phone, [])

    def fake_get_client_by_id(idclient) -> list[dict]:
        PG_BY_ID_CALLS.append(int(idclient))
        return FAKE_PG_BY_ID.get(int(idclient), [])

    async def fake_get_client_details(client_id: int) -> dict:
        return FAKE_UCRM_DETAILS.get(client_id) or {}

    async def fake_get_client_services(client_id: int) -> list[dict]:
        return FAKE_UCRM_SERVICES.get(client_id, [])

    async def fake_download(url: str) -> bytes:
        return b"fake-image-bytes"

    async def fake_ai_ocr_extract(image_bytes: bytes, filename: str = "receipt.jpg") -> dict:
        return FAKE_OCR_RESULT

    pg.get_clients_by_phone = fake_get_clients_by_phone
    pg.get_client_by_id = fake_get_client_by_id
    ucrm.get_client_details = fake_get_client_details
    ucrm.get_client_services = fake_get_client_services
    pipeline.download_image = fake_download
    pipeline.ai_ocr_extract = fake_ai_ocr_extract


def _payload(from_digits: str) -> dict:
    return {
        "data": {
            "from": f"222{from_digits}@c.us",
            "author": "",
            "body": "",
            "media": "https://fake-s3/fake.jpg",
            "type": "image",
        }
    }


def _set_ocr(txn: str, sample: str, montant: int = 1500) -> None:
    FAKE_OCR_RESULT.clear()
    FAKE_OCR_RESULT.update({
        "extracted": {"montant": montant, "txn_id": txn, "date_heure": "2026-07-13 09:00:00"},
        "sample_id": sample,
        "template": "bankily",
        "raw_text": f"PATRINET NKTT reçu {montant} MRU",
    })


def test_phone_lookup_wins_over_mapping() -> None:
    """1. Le lookup téléphone normal prime TOUJOURS : mapping présent vers un
    autre client, mais le téléphone matche -> client du lookup téléphone."""
    PHONE = "37641001"
    FAKE_PG_CLIENTS[PHONE] = [client_row(801, "AA:01", statu=2)]
    FAKE_PG_BY_ID[802] = [client_row(802, "BB:01", statu=2)]
    FAKE_UCRM_DETAILS[801] = {"balance": 1500, "account_credit": 0}
    FAKE_UCRM_SERVICES[801] = [svc("AA:01", 1500)]
    crm_mappings.upsert_mapping(whatsapp_phone=PHONE, crm_client_id="802",
                                created_by="test", db_path=MAP_DB)
    PG_BY_ID_CALLS.clear()
    _set_ocr("TXN-FB-1", "2026-07-13/fb1")

    result = asyncio.run(pipeline.process(_payload(PHONE)))
    check("1. lookup téléphone OK -> enqueued", result.get("status") == "enqueued", result)
    check("1bis. client du lookup téléphone (801), pas du mapping (802)",
          result.get("client_id") == 801, result)
    check("1ter. get_client_by_id jamais appelé (mapping non consulté)",
          PG_BY_ID_CALLS == [], str(PG_BY_ID_CALLS))
    claimed = queue_store.claim_next("test-worker")
    check("1quater. job drainé (client 801)",
          claimed is not None and claimed["job"].client.id == 801)


def test_mapping_fallback_loads_client() -> None:
    """2. Téléphone inconnu mais correspondance active -> client rechargé par
    idclient, flux normal (Job complet), AUCUN numeros_introuvable créé."""
    PHONE = "37641002"
    IDCLIENT = 803
    # PAS d'entrée FAKE_PG_CLIENTS pour ce téléphone (lookup téléphone KO).
    FAKE_PG_BY_ID[IDCLIENT] = [client_row(IDCLIENT, "CC:01", statu=2)]
    FAKE_UCRM_DETAILS[IDCLIENT] = {"balance": 1500, "account_credit": 0}
    FAKE_UCRM_SERVICES[IDCLIENT] = [svc("CC:01", 1500)]
    crm_mappings.upsert_mapping(whatsapp_phone=PHONE, crm_client_id=str(IDCLIENT),
                                created_by="test", db_path=MAP_DB)
    _set_ocr("TXN-FB-2", "2026-07-13/fb2")

    result = asyncio.run(pipeline.process(_payload(PHONE)))
    check("2. repli mapping -> enqueued", result.get("status") == "enqueued", result)
    check("2bis. client_id = idclient mappé", result.get("client_id") == IDCLIENT, result)
    check("2ter. AUCUN numeros_introuvable créé",
          unknown_clients_store.get_by_sample_id("2026-07-13/fb2") is None)

    claimed = queue_store.claim_next("test-worker")
    check("2quater. job récupérable (txn TXN-FB-2)",
          claimed is not None and claimed["job"].payment.txn_id == "TXN-FB-2")
    if claimed:
        job = claimed["job"]
        check("2quinquies. Job.client.id == idclient mappé", job.client.id == IDCLIENT)
        check("2sexies. Job.client.phone == numéro WhatsApp expéditeur",
              job.client.phone == PHONE, job.client.phone)
        check("2septies. Job.source.wnum == numéro WhatsApp expéditeur",
              job.source.wnum == PHONE, job.source.wnum)
        check("2octies. déblocage calculé normalement (unblock_macs=['CC:01'])",
              job.unblock_macs == ["CC:01"])


def test_no_mapping_keeps_client_not_found() -> None:
    """3. Ni téléphone ni correspondance -> client_not_found historique intact."""
    PHONE = "37641003"
    _set_ocr("TXN-FB-3", "2026-07-13/fb3")
    stats_before = queue_store.stats()

    result = asyncio.run(pipeline.process(_payload(PHONE)))
    check("3. sans mapping -> skipped/client_not_found",
          result == {"status": "skipped", "reason": "client_not_found"}, result)
    check("3bis. aucun Job créé", queue_store.stats() == stats_before)
    rec = unknown_clients_store.get_by_sample_id("2026-07-13/fb3")
    check("3ter. enregistrement numeros_introuvable créé (comportement historique)",
          rec is not None and rec.get("txn_id") == "TXN-FB-3")


def test_stale_mapping_falls_back_to_not_found() -> None:
    """4. Correspondance active mais idclient disparu de PostgreSQL ->
    client_not_found (jamais de crash, enregistrement préservé)."""
    PHONE = "37641004"
    crm_mappings.upsert_mapping(whatsapp_phone=PHONE, crm_client_id="99404",
                                created_by="test", db_path=MAP_DB)
    # FAKE_PG_BY_ID[99404] absent : le client n'existe plus.
    _set_ocr("TXN-FB-4", "2026-07-13/fb4")

    result = asyncio.run(pipeline.process(_payload(PHONE)))
    check("4. mapping périmé -> skipped/client_not_found",
          result == {"status": "skipped", "reason": "client_not_found"}, result)
    rec = unknown_clients_store.get_by_sample_id("2026-07-13/fb4")
    check("4bis. enregistrement numeros_introuvable créé", rec is not None)


def test_full_journey_unknown_then_associate_then_recognized() -> None:
    """5. Parcours complet : 1er reçu inconnu -> association par identifiant
    CRM (AUCUNE correspondance à ce stade) -> confirmation -> 'queued' ->
    correspondance créée -> 2e reçu du même numéro reconnu via la
    correspondance (plus jamais de numeros_introuvable pour ce numéro)."""
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from whatsapp_automation.webhook.dashboard import router as dashboard_router
    except Exception as exc:
        print(f"  (TestClient indisponible, parcours complet non testé : {type(exc).__name__})")
        return

    PHONE = "37641005"
    IDCLIENT = 805
    FAKE_PG_BY_ID[IDCLIENT] = [client_row(IDCLIENT, "EE:01", statu=2)]
    FAKE_UCRM_DETAILS[IDCLIENT] = {"balance": 3000, "account_credit": 0}
    FAKE_UCRM_SERVICES[IDCLIENT] = [svc("EE:01", 1500)]

    # 1er reçu : client introuvable.
    _set_ocr("TXN-FB-5A", "2026-07-13/fb5a")
    result = asyncio.run(pipeline.process(_payload(PHONE)))
    check("5a. 1er reçu -> client_not_found",
          result == {"status": "skipped", "reason": "client_not_found"}, result)
    rec = unknown_clients_store.get_by_sample_id("2026-07-13/fb5a")
    check("5b. enregistrement numeros_introuvable créé", rec is not None)

    # Association par identifiant CRM via la route dashboard, puis
    # confirmation — la correspondance n'apparaît qu'au 'queued'.
    app = FastAPI()
    app.include_router(dashboard_router)
    with TestClient(app) as client:
        r = client.post("/dashboard/login", json={"password": "test-password-fallback"})
        check("5c. login dashboard OK", r.status_code == 200)
        r = client.post(f"/dashboard/api/unknown-clients/{rec['id']}/associate",
                        json={"crm_client_id": str(IDCLIENT)})
        check("5d. association par identifiant CRM -> 200", r.status_code == 200, r.text)
        check("5e. AUCUNE correspondance créée à l'association (décision Ali)",
              crm_mappings.get_active_mapping(PHONE, db_path=MAP_DB) is None)

        r = client.post(f"/dashboard/api/unknown-clients/{rec['id']}/confirm")
        check("5f. confirmation -> 200 (statut 'queued')", r.status_code == 200, r.text)

    mapping = crm_mappings.get_active_mapping(PHONE, db_path=MAP_DB)
    check("5g. correspondance créée au passage en 'queued'",
          mapping is not None and mapping["crm_client_id"] == str(IDCLIENT), str(mapping))

    # Draine le job de la confirmation (TXN-FB-5A) pour que le claim suivant
    # récupère bien celui du 2e reçu (queue FIFO).
    drained = queue_store.claim_next("test-worker")
    check("5h. job de la confirmation drainé (TXN-FB-5A)",
          drained is not None and drained["job"].payment.txn_id == "TXN-FB-5A")

    # 2e reçu du MÊME numéro : reconnu automatiquement, plus d'introuvable.
    _set_ocr("TXN-FB-5B", "2026-07-13/fb5b", montant=1500)
    result = asyncio.run(pipeline.process(_payload(PHONE)))
    check("5i. 2e reçu -> enqueued (reconnu via la correspondance)",
          result.get("status") == "enqueued", result)
    check("5j. client_id = idclient associé", result.get("client_id") == IDCLIENT)
    check("5k. AUCUN nouveau numeros_introuvable",
          unknown_clients_store.get_by_sample_id("2026-07-13/fb5b") is None)

    claimed = queue_store.claim_next("test-worker")
    check("5l. job du 2e reçu récupérable",
          claimed is not None and claimed["job"].payment.txn_id == "TXN-FB-5B")
    if claimed:
        check("5m. Job.client.phone == numéro WhatsApp d'origine",
              claimed["job"].client.phone == PHONE)
        check("5n. Job.source.wnum == numéro WhatsApp d'origine",
              claimed["job"].source.wnum == PHONE)


def main() -> int:
    queue_store.init_db()
    unknown_clients_store.init_db()
    crm_mappings.init_db(MAP_DB)
    _patch_forbidden_calls()
    _install_pipeline_fakes()

    test_phone_lookup_wins_over_mapping()
    test_mapping_fallback_loads_client()
    test_no_mapping_keeps_client_not_found()
    test_stale_mapping_falls_back_to_not_found()
    test_full_journey_unknown_then_associate_then_recognized()

    print(f"\n=== {passed} PASS / {failed} FAIL ===")
    print("\n6. create_payment/unblock_by_mac/send_*/écritures PostgreSQL : patchés pour "
          "lever une AssertionError s'ils étaient appelés — le test aurait planté avant "
          "ce résumé si l'un d'eux avait été invoqué. Worker jamais importé/démarré. "
          "[PASS implicite]")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
