"""Tests Phase 4B-2 : POST /dashboard/api/unknown-clients/{id}/confirm.

Vérifie :
  1. sans session -> 401 ;
  2. id inconnu -> 404 ;
  3. statut 'pending' -> 409 ;
  4. txn_id manquant -> 409 ;
  5. amount/original_phone/client_id manquants -> 409 chacun ; entered_phone/
     subscription_phone absents (association par identifiant CRM) -> accepté ;
  6. réservation atomique (reserve_for_confirmation) empêche 2 confirmations
     concurrentes du même enregistrement ;
  7. les lignes PostgreSQL relues sont FRAÎCHES (pas le MAC figé en Phase 3) ;
  8. toutes les lignes d'abonnement sont prises en compte (multi-abonnements) ;
  9. les lectures UCRM (détails/services) sont mockées (aucun réseau réel) ;
  10. Job.client.phone == original_phone ;
  11. Job.source.wnum == original_phone ;
  12. queue_store.enqueue() appelé exactement une fois en succès ;
  13. succès -> status='queued' + job_id persistés en SQLite ;
  14. une 2e confirmation ne ré-empile pas de Job (idempotent) ;
  15. doublon détecté par enqueue() (dédup atomique par txn_id) -> réconcilié
      avec le job actif existant, jamais un 2e Job, jamais de perte du reçu
      (pas de 'restore associated' silencieux si un job existe déjà) ;
  16. fenêtre de crash (Job enqueued mais mark_queued jamais exécuté) ->
      récupérable sans 2e Job, via une nouvelle confirmation qui réconcilie ;
  17. create_payment / unblock_by_mac / send_document / écritures PostgreSQL
      ne sont JAMAIS appelés ;
  18. le worker n'est jamais lancé (aucun import/démarrage de worker.main ici) ;
  19. confirming FRAIS sans job -> 409, enregistrement INCHANGÉ (jamais relâché
      prématurément — la requête d'origine peut être encore en cours) ;
  20. confirming STALE (> timeout) sans job -> restauré vers 'associated' avec
      error_message, prêt pour un nouvel essai ;
  21. confirming STALE AVEC un job déjà en queue -> réconcilié vers 'queued',
      JAMAIS restauré vers 'associated' (la réconciliation prime toujours sur
      la logique de staleness) ;
  22. récupération stale concurrente (release_stale_confirmation) reste
      atomique : N tentatives simultanées sur le même enregistrement stale,
      une seule réussit ;
  23. correspondance whatsapp_crm_mappings (décision Ali) : écrite UNIQUEMENT
      quand l'enregistrement atteint 'queued' — sur les 4 chemins (succès
      nominal, réponse idempotente déjà-en-file qui répare une mémoire
      manquante, réconciliation doublon txn_id, récupération après crash) —
      et JAMAIS sur un refus (gates 409), un confirming frais, ou une
      restauration stale.

100% local : bases SQLite temporaires, PostgreSQL et UCRM entièrement mockés,
aucun réseau réel, aucune écriture PostgreSQL, worker jamais démarré.
À lancer : python scripts/test_unknown_clients_confirm.py
"""

from __future__ import annotations

import io
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

_TMP = Path(tempfile.mkdtemp(prefix="uc_confirm_test_"))
os.environ["UNKNOWN_CLIENTS_DB_PATH"] = str(_TMP / "unknown_clients.db")
os.environ["WHATSAPP_CRM_MAPPINGS_DB_PATH"] = str(_TMP / "whatsapp_crm_mappings.db")
os.environ["EVENTS_DB_PATH"] = str(_TMP / "events.db")
os.environ["QUEUE_DB_PATH"] = str(_TMP / "queue.db")
os.environ["DASHBOARD_PASSWORD"] = "test-password-phase4b2"
os.environ["UNDERPAYMENT_TOLERANCE"] = "150"
# Timeout raccourci (5s au lieu des 300s par défaut prod) : les tests de
# staleness manipulent directement `updated_at` en base (pas de sleep réel).
os.environ["UNKNOWN_CLIENT_CONFIRM_TIMEOUT_SECONDS"] = "5"

from whatsapp_automation import config  # noqa: E402
from whatsapp_automation.db import postgres as pg  # noqa: E402
from whatsapp_automation.jobqueue import store as queue_store  # noqa: E402
from whatsapp_automation.webhook import crm_mappings  # noqa: E402
from whatsapp_automation.webhook.dashboard import unknown_clients_store as store  # noqa: E402
from whatsapp_automation.worker import mikrotik, ucrm, ultramsg  # noqa: E402

CONFIRM_TIMEOUT = config.UNKNOWN_CLIENT_CONFIRM_TIMEOUT_SECONDS

DB = os.environ["UNKNOWN_CLIENTS_DB_PATH"]
MAP_DB = os.environ["WHATSAPP_CRM_MAPPINGS_DB_PATH"]


def _active_mapping(phone: str):
    return crm_mappings.get_active_mapping(phone, db_path=MAP_DB)


def _delete_mappings(phone: str) -> None:
    """Supprime les correspondances d'un numéro (simule une mémoire perdue,
    ex : crash juste après mark_queued mais avant l'écriture du mapping)."""
    with sqlite3.connect(MAP_DB) as conn:
        conn.execute("DELETE FROM whatsapp_crm_mappings WHERE whatsapp_phone = ?", (phone,))
        conn.commit()

passed = failed = 0
_sample_seq = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    marker = " " if cond else ">"
    print(f"{marker}[{'PASS' if cond else 'FAIL'}] {label}" + (f"  -- {detail}" if detail else ""))
    passed += bool(cond)
    failed += not cond


def _forbid(name):
    def _raise(*_a, **_kw):
        raise AssertionError(f"appel externe interdit en Phase 4B-2 : {name}")
    return _raise


def _patch_forbidden_calls() -> None:
    """Aucune de ces fonctions ne doit jamais être invoquée par la route
    confirm — elles sont réservées au worker."""
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


def new_sample_id() -> str:
    global _sample_seq
    _sample_seq += 1
    return f"2026-07-13/{'a' * 31}{_sample_seq}"


def seed_associated(
    *,
    idclient: int,
    original_phone: str = "37600001",
    amount: int | None = 1500,
    txn_id: str | None = "TXNCF1",
    entered_phone: str | None = "37600001",
    subscription_phone: str | None = "37600001",
    client_id: str | None = None,
    mac_address: str | None = "OLD:MAC:STORED:PHASE3",
) -> int:
    """Crée un enregistrement `numeros_introuvable` et l'amène en 'associated'
    via les fonctions du store directement (pas de dépendance à la route
    HTTP /associate — on isole les tests confirm de la logique associate)."""
    sample_id = new_sample_id()
    rec_id = store.insert_unknown_client(
        sample_id=sample_id, txn_id=txn_id, amount=amount,
        date_heure="2026-07-13 09:00:00", operator="bankily",
        original_phone=original_phone, body_phone=None, group_id=None,
        raw_text="reçu bankily", db_path=DB,
    )
    store.associate_unknown_client(
        rec_id, entered_phone=entered_phone, subscription_phone=subscription_phone,
        client_id=client_id if client_id is not None else str(idclient),
        mac_address=mac_address, db_path=DB,
    )
    return rec_id


def _force_field(rec_id: int, **fields) -> None:
    """Ecrase directement des colonnes en SQLite (hors API du store) pour
    fabriquer des états impossibles à atteindre via le flux normal (ex :
    'associated' avec amount NULL) — sert uniquement à tester la défense en
    profondeur des préconditions de /confirm."""
    with sqlite3.connect(DB) as conn:
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE numeros_introuvable SET {set_clause} WHERE id = ?",
                      (*fields.values(), rec_id))
        conn.commit()


FAKE_PG_BY_ID: dict[int, list[dict]] = {}
FAKE_UCRM_DETAILS: dict[int, dict] = {}
FAKE_UCRM_SERVICES: dict[int, list[dict]] = {}


def client_row(idclient: int, mac: str, statu: int, ip: str = "10.0.1.1", info: str = "") -> dict:
    return {"idclient": idclient, "info": info or f"37600000-Client {idclient}",
            "mac": mac, "statu": statu, "ipaddress": ip}


def svc(mac: str, price, status: int = 3) -> dict:
    return {"status": status, "mac": mac, "price": price}


def _install_fakes() -> None:
    def fake_get_client_by_id(idclient) -> list[dict]:
        return FAKE_PG_BY_ID.get(int(idclient), [])

    async def fake_get_client_details(client_id: int) -> dict:
        return FAKE_UCRM_DETAILS.get(int(client_id)) or {}

    async def fake_get_client_services(client_id: int) -> list[dict]:
        return FAKE_UCRM_SERVICES.get(int(client_id), [])

    pg.get_client_by_id = fake_get_client_by_id
    ucrm.get_client_details = fake_get_client_details
    ucrm.get_client_services = fake_get_client_services


def _make_app():
    from fastapi import FastAPI
    from whatsapp_automation.webhook.dashboard import router as dashboard_router
    app = FastAPI()
    app.include_router(dashboard_router)
    return app


def _login(client):
    r = client.post("/dashboard/login", json={"password": "test-password-phase4b2"})
    assert r.status_code == 200 and r.json().get("ok") is True, "login setup failed"


def test_auth_and_basic_status_gates() -> None:
    from fastapi.testclient import TestClient
    app = _make_app()

    with TestClient(app) as client:
        rec_id = seed_associated(idclient=701)

        # 1. Sans session -> 401.
        r = client.post(f"/dashboard/api/unknown-clients/{rec_id}/confirm")
        check("1. confirm sans session -> 401", r.status_code == 401)

        _login(client)

        # 2. id inconnu -> 404.
        r = client.post("/dashboard/api/unknown-clients/999999/confirm")
        check("2. id inconnu -> 404", r.status_code == 404)

        # 3. statut 'pending' -> 409.
        pending_id = store.insert_unknown_client(
            sample_id=new_sample_id(), txn_id="TXNPEND", amount=500,
            date_heure="2026-07-13 09:00:00", operator="bankily",
            original_phone="37699999", body_phone=None, group_id=None,
            raw_text="reçu", db_path=DB,
        )
        r = client.post(f"/dashboard/api/unknown-clients/{pending_id}/confirm")
        check("3. statut 'pending' -> 409", r.status_code == 409)
        check("3bis. statut inchangé après refus", store.get_by_id(pending_id, db_path=DB)["status"] == "pending")

        # 4. txn_id manquant -> 409.
        no_txn_id = seed_associated(idclient=702, txn_id="TXNTMP")
        _force_field(no_txn_id, txn_id=None)
        r = client.post(f"/dashboard/api/unknown-clients/{no_txn_id}/confirm")
        check("4. txn_id manquant -> 409", r.status_code == 409)
        check("4bis. statut reste 'associated' (pas de réservation)",
              store.get_by_id(no_txn_id, db_path=DB)["status"] == "associated")

        # 5a. amount manquant -> 409.
        no_amount = seed_associated(idclient=703, txn_id="TXNAMT")
        _force_field(no_amount, amount=None)
        r = client.post(f"/dashboard/api/unknown-clients/{no_amount}/confirm")
        check("5a. amount manquant -> 409", r.status_code == 409)

        # 5b. amount <= 0 -> 409.
        zero_amount = seed_associated(idclient=704, txn_id="TXNAMT0")
        _force_field(zero_amount, amount=0)
        r = client.post(f"/dashboard/api/unknown-clients/{zero_amount}/confirm")
        check("5b. amount=0 -> 409", r.status_code == 409)

        # 5c. original_phone manquant -> 409.
        no_phone = seed_associated(idclient=705, txn_id="TXNPHONE")
        _force_field(no_phone, original_phone=None)
        r = client.post(f"/dashboard/api/unknown-clients/{no_phone}/confirm")
        check("5c. original_phone manquant -> 409", r.status_code == 409)

        # 5d. client_id manquant -> 409.
        no_client_id = seed_associated(idclient=706, txn_id="TXNCID")
        _force_field(no_client_id, client_id=None)
        r = client.post(f"/dashboard/api/unknown-clients/{no_client_id}/confirm")
        check("5d. client_id manquant -> 409", r.status_code == 409)

        # Toutes les confirmations refusées ci-dessus (gates 3→5d, même
        # original_phone par défaut) ne doivent avoir écrit AUCUNE
        # correspondance WhatsApp -> CRM : la mémoire n'est écrite qu'au
        # statut 'queued'.
        check("gates refusées : aucune correspondance créée avant 'queued'",
              _active_mapping("37600001") is None)

        # 5e. entered_phone/subscription_phone absents (association par
        # identifiant CRM) : la confirmation est ACCEPTÉE — le Job n'en dépend
        # pas (client_id pilote PostgreSQL/UCRM, original_phone la réponse
        # WhatsApp). C'était un 409 du temps de l'association par téléphone.
        no_sub_phone = seed_associated(idclient=707, txn_id="TXNSUBP")
        _force_field(no_sub_phone, entered_phone=None, subscription_phone=None)
        FAKE_PG_BY_ID[707] = [client_row(707, "SUBP:MAC:01", statu=2)]
        FAKE_UCRM_DETAILS[707] = {"balance": 1500, "account_credit": 0}
        FAKE_UCRM_SERVICES[707] = [svc("SUBP:MAC:01", 1500)]
        r = client.post(f"/dashboard/api/unknown-clients/{no_sub_phone}/confirm")
        check("5e. sans entered/subscription_phone (association CRM) -> confirm 200",
              r.status_code == 200, r.text)
        # Draine le job enqueued par 5e (sinon le claim_next des tests
        # suivants — FIFO — récupérerait ce job au lieu du leur).
        drained = queue_store.claim_next("phase-gates-drain")
        check("5e-bis. job 5e drainé de la queue (txn TXNSUBP)",
              drained is not None and drained["job"].payment.txn_id == "TXNSUBP")
        # Chemin 'queued' nominal : la correspondance est créée MAINTENANT.
        m = _active_mapping("37600001")
        check("5e-ter. correspondance créée au passage en 'queued' (37600001 -> 707)",
              m is not None and m["crm_client_id"] == "707", str(m))


def test_concurrent_reservation_is_atomic() -> None:
    """Test 6 : réservation CAS atomique — N threads tentent
    reserve_for_confirmation() sur le MÊME enregistrement 'associated' ;
    un seul doit réussir (reproduit la course de deux confirmations
    dashboard simultanées, même pattern que test_enqueue_dedup.py)."""
    rec_id = seed_associated(idclient=708, txn_id="TXNRACE")

    N = 8
    results: list = [None] * N
    barrier = threading.Barrier(N)

    def worker(idx: int):
        barrier.wait()
        results[idx] = store.reserve_for_confirmation(rec_id, db_path=DB)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    wins = [r for r in results if r is True]
    check(f"6. réservation atomique : 1 seul gagnant sur {N} threads concurrents (gagnants={len(wins)})",
          len(wins) == 1)
    check("6bis. statut final = 'confirming'", store.get_by_id(rec_id, db_path=DB)["status"] == "confirming")

    # Nettoyage pour ne pas polluer les tests suivants.
    store.release_confirmation(rec_id, "reset après test concurrence", db_path=DB)


def test_fresh_postgres_and_multi_subscription_and_phone_rules() -> None:
    """Tests 7, 8, 9, 10, 11, 12, 13 : succès nominal multi-abonnements avec
    relecture PostgreSQL/UCRM fraîche (pas le MAC Phase 3), Job.client.phone
    et Job.source.wnum == original_phone, enqueue() appelé une seule fois,
    status='queued'+job_id persistés."""
    from fastapi.testclient import TestClient
    app = _make_app()

    IDCLIENT = 709
    ORIGINAL_PHONE = "37655001"

    # 3 lignes PostgreSQL FRAÎCHES — le MAC stocké en Phase 3 (mac_address=
    # "OLD:MAC:STORED:PHASE3") ne doit apparaître NULLE PART dans le Job final.
    FAKE_PG_BY_ID[IDCLIENT] = [
        client_row(IDCLIENT, "FRESH:MAC:01", statu=2),
        client_row(IDCLIENT, "FRESH:MAC:02", statu=2),
        client_row(IDCLIENT, "FRESH:MAC:03", statu=2),
    ]
    FAKE_UCRM_DETAILS[IDCLIENT] = {"balance": 4500, "account_credit": 0}
    FAKE_UCRM_SERVICES[IDCLIENT] = [
        svc("FRESH:MAC:01", 1500), svc("FRESH:MAC:02", 1500), svc("FRESH:MAC:03", 1500),
    ]

    rec_id = seed_associated(
        idclient=IDCLIENT, original_phone=ORIGINAL_PHONE, amount=4500, txn_id="TXNFRESH1",
        mac_address="OLD:MAC:STORED:PHASE3",  # jamais réutilisé par confirm
    )

    original_enqueue = queue_store.enqueue
    call_count = {"n": 0}

    def counting_enqueue(job):
        call_count["n"] += 1
        return original_enqueue(job)

    queue_store.enqueue = counting_enqueue
    try:
        with TestClient(app) as client:
            _login(client)
            r = client.post(f"/dashboard/api/unknown-clients/{rec_id}/confirm")
            check("succès nominal : 200", r.status_code == 200, r.text)
            body = r.json()
            check("13. réponse status='queued'", body.get("status") == "queued")
            check("13bis. réponse job_id présent", bool(body.get("job_id")))
            check("8. les 3 abonnements suspendus sont débloqués (multi-sub)",
                  sorted(body.get("unblock_macs", [])) == ["FRESH:MAC:01", "FRESH:MAC:02", "FRESH:MAC:03"],
                  body.get("unblock_macs"))
            check("7. MAC Phase 3 stocké absent du résultat (relecture fraîche)",
                  "OLD:MAC:STORED:PHASE3" not in body.get("unblock_macs", []))
            check("12. enqueue() appelé exactement 1 fois", call_count["n"] == 1)

            rec = store.get_by_id(rec_id, db_path=DB)
            check("13ter. SQLite : status='queued'", rec["status"] == "queued")
            check("13quater. SQLite : job_id persisté == réponse", rec["job_id"] == body.get("job_id"))
            check("13quinquies. error_message effacé", rec.get("error_message") is None)

            # Chemin (a) nominal : correspondance créée à la mise en file.
            m = _active_mapping(ORIGINAL_PHONE)
            check("mapping (a) : créé au succès nominal (original_phone -> 709)",
                  m is not None and m["crm_client_id"] == str(IDCLIENT), str(m))

            claimed = queue_store.claim_next("phase4b2-test-worker")
            check("job récupérable en queue", claimed is not None and claimed["job"].payment.txn_id == "TXNFRESH1")
            if claimed:
                job = claimed["job"]
                check("10. Job.client.phone == original_phone", job.client.phone == ORIGINAL_PHONE, job.client.phone)
                check("11. Job.source.wnum == original_phone", job.source.wnum == ORIGINAL_PHONE, job.source.wnum)
                check("Job.client.id == idclient associé", job.client.id == IDCLIENT)
                check("9. Job construit à partir des données UCRM mockées (balance cohérente)",
                      job.payment.crm_balance_before == 4500)

            # 14. Une 2e confirmation ne ré-empile pas de Job (idempotent).
            # Chemin (d) : on simule une mémoire perdue (crash juste après
            # mark_queued, avant l'écriture du mapping) — la réponse
            # idempotente doit la RÉPARER, best-effort.
            _delete_mappings(ORIGINAL_PHONE)
            check("setup (d) : correspondance supprimée (mémoire perdue simulée)",
                  _active_mapping(ORIGINAL_PHONE) is None)
            call_count["n"] = 0
            r2 = client.post(f"/dashboard/api/unknown-clients/{rec_id}/confirm")
            check("14. 2e confirmation -> 200 (idempotent)", r2.status_code == 200)
            check("14bis. 2e confirmation renvoie le MÊME job_id", r2.json().get("job_id") == body.get("job_id"))
            check("14ter. enqueue() PAS rappelé à la 2e confirmation", call_count["n"] == 0)
            m = _active_mapping(ORIGINAL_PHONE)
            check("mapping (d) : réparé par la réponse idempotente 'déjà en file'",
                  m is not None and m["crm_client_id"] == str(IDCLIENT), str(m))
    finally:
        queue_store.enqueue = original_enqueue


def test_enqueue_duplicate_is_reconciled_not_lost() -> None:
    """Test 15 : si queue_store.enqueue() renvoie None (dédup atomique sur
    txn_id) alors qu'un Job actif existe déjà pour ce txn_id (ex : un webhook
    UltraMsg a traité le même reçu entre-temps), la confirmation doit se
    RATTACHER à ce Job existant — jamais recréer, jamais perdre le reçu en le
    laissant bloqué sans explication."""
    from fastapi.testclient import TestClient
    from whatsapp_automation.models import Client, Job, Payment, Source
    app = _make_app()

    IDCLIENT = 710
    TXN = "TXNDUP1"
    FAKE_PG_BY_ID[IDCLIENT] = [client_row(IDCLIENT, "DUP:MAC:01", statu=2)]
    FAKE_UCRM_DETAILS[IDCLIENT] = {"balance": 1500, "account_credit": 0}
    FAKE_UCRM_SERVICES[IDCLIENT] = [svc("DUP:MAC:01", 1500)]

    # Un Job "concurrent" (simule un autre chemin — ex : webhook) existe déjà
    # pour ce txn_id, AVANT même que la confirmation dashboard ne démarre.
    pre_existing_job = Job(
        job_id=queue_store.new_job_id(),
        client=Client(id=IDCLIENT, phone="37655002", mac_address="DUP:MAC:01",
                      ip_address="10.0.1.1", current_status="suspended"),
        payment=Payment(amount_mru=1500, txn_id=TXN, date_heure=None, operator="bankily",
                        crm_balance_before=1500, should_unblock=True),
        source=Source(wnum="37655002", sample_id="2026-07-13/preexisting",
                      received_at="2026-07-13T09:00:00+00:00"),
        unblock_macs=["DUP:MAC:01"],
    )
    pre_existing_internal_id = queue_store.enqueue(pre_existing_job)
    check("setup : job concurrent pré-existant bien inséré", pre_existing_internal_id is not None)

    rec_id = seed_associated(idclient=IDCLIENT, original_phone="37655002", amount=1500, txn_id=TXN)

    with TestClient(app) as client:
        _login(client)
        r = client.post(f"/dashboard/api/unknown-clients/{rec_id}/confirm")
        check("15. doublon enqueue -> 200 (réconcilié, pas d'erreur perdue)", r.status_code == 200, r.text)
        body = r.json()
        check("15bis. job_id renvoyé == job pré-existant (jamais recréé)",
              body.get("job_id") == pre_existing_job.job_id, body.get("job_id"))
        check("15ter. réponse signale la réconciliation", body.get("reconciled") is True)

        rec = store.get_by_id(rec_id, db_path=DB)
        check("15quater. SQLite : status='queued', job_id == job pré-existant",
              rec["status"] == "queued" and rec["job_id"] == pre_existing_job.job_id)

        # Chemin (b) : la réconciliation doublon écrit aussi la correspondance.
        m = _active_mapping("37655002")
        check("mapping (b) : créé par la réconciliation doublon (37655002 -> 710)",
              m is not None and m["crm_client_id"] == str(IDCLIENT), str(m))

        # Un seul Job en base pour ce txn_id (jamais un 2e).
        with sqlite3.connect(os.environ["QUEUE_DB_PATH"]) as conn:
            n = conn.execute("SELECT COUNT(*) FROM jobs WHERE txn_id = ?", (TXN,)).fetchone()[0]
        check("15quinquies. un seul job en queue pour ce txn_id (jamais dupliqué)", n == 1, n)


def test_crash_window_between_enqueue_and_mark_queued() -> None:
    """Test 16 : simule un crash process EXACTEMENT entre queue_store.enqueue()
    (qui a réussi — le Job existe réellement en queue) et le mark_queued()
    correspondant côté numeros_introuvable (jamais exécuté, ex: process tué).
    L'enregistrement reste bloqué en 'confirming' avec job_id=NULL. Une
    NOUVELLE confirmation doit détecter le Job existant via son txn_id et
    compléter la transition confirming->queued SANS créer de second Job."""
    from fastapi.testclient import TestClient
    from whatsapp_automation.models import Client, Job, Payment, Source
    app = _make_app()

    IDCLIENT = 711
    TXN = "TXNCRASH1"
    ORIGINAL_PHONE = "37655003"
    FAKE_PG_BY_ID[IDCLIENT] = [client_row(IDCLIENT, "CRASH:MAC:01", statu=2)]
    FAKE_UCRM_DETAILS[IDCLIENT] = {"balance": 1500, "account_credit": 0}
    FAKE_UCRM_SERVICES[IDCLIENT] = [svc("CRASH:MAC:01", 1500)]

    rec_id = seed_associated(idclient=IDCLIENT, original_phone=ORIGINAL_PHONE, amount=1500, txn_id=TXN)

    # Simule manuellement ce qu'aurait fait la route jusqu'au point de crash :
    # reserve_for_confirmation() OK, Job construit et enqueue() a réussi, mais
    # mark_queued() n'a JAMAIS été appelé (process tué juste après l'enqueue).
    reserved = store.reserve_for_confirmation(rec_id, db_path=DB)
    check("setup crash : réservation OK avant le 'crash' simulé", reserved)

    crashed_job = Job(
        job_id=queue_store.new_job_id(),
        client=Client(id=IDCLIENT, phone=ORIGINAL_PHONE, mac_address="CRASH:MAC:01",
                      ip_address="10.0.1.1", current_status="suspended"),
        payment=Payment(amount_mru=1500, txn_id=TXN, date_heure=None, operator="bankily",
                        crm_balance_before=1500, should_unblock=True),
        source=Source(wnum=ORIGINAL_PHONE, sample_id="2026-07-13/crash",
                      received_at="2026-07-13T09:00:00+00:00"),
        unblock_macs=["CRASH:MAC:01"],
    )
    internal_id = queue_store.enqueue(crashed_job)
    check("setup crash : le Job a bien été enqueued (avant le 'crash')", internal_id is not None)
    # ⚠ mark_queued() volontairement PAS appelé ici : reproduit le crash.
    rec_after_crash = store.get_by_id(rec_id, db_path=DB)
    check("setup crash : enregistrement bloqué en 'confirming', job_id=NULL",
          rec_after_crash["status"] == "confirming" and rec_after_crash.get("job_id") is None)

    with TestClient(app) as client:
        _login(client)
        r = client.post(f"/dashboard/api/unknown-clients/{rec_id}/confirm")
        check("16. nouvelle confirmation après crash -> 200 (récupéré)", r.status_code == 200, r.text)
        body = r.json()
        check("16bis. job_id récupéré == job créé avant le crash (jamais un 2e Job)",
              body.get("job_id") == crashed_job.job_id, body.get("job_id"))
        check("16ter. réponse signale la réconciliation", body.get("reconciled") is True)

        rec = store.get_by_id(rec_id, db_path=DB)
        check("16quater. SQLite : status='queued', job_id correct",
              rec["status"] == "queued" and rec["job_id"] == crashed_job.job_id)

        # Chemin (c) : la récupération après crash écrit aussi la correspondance.
        m = _active_mapping(ORIGINAL_PHONE)
        check("mapping (c) : créé par la récupération après crash (37655003 -> 711)",
              m is not None and m["crm_client_id"] == str(IDCLIENT), str(m))

        with sqlite3.connect(os.environ["QUEUE_DB_PATH"]) as conn:
            n = conn.execute("SELECT COUNT(*) FROM jobs WHERE txn_id = ?", (TXN,)).fetchone()[0]
        check("16quinquies. un seul job en queue pour ce txn_id (récupération sans doublon)", n == 1, n)


def test_fresh_confirming_without_job_returns_409_unchanged() -> None:
    """Test 19 : un enregistrement 'confirming' FRAIS (réservé il y a
    quelques instants, aucun Job en queue) doit rester INTACT — la requête
    d'origine qui l'a réservé est peut-être toujours en train de lire
    PostgreSQL/UCRM. Ne jamais le relâcher prématurément."""
    from fastapi.testclient import TestClient
    app = _make_app()

    rec_id = seed_associated(idclient=716, original_phone="37655007", amount=1500, txn_id="TXNFRESHCONF")
    reserved = store.reserve_for_confirmation(rec_id, db_path=DB)
    check("setup fresh-confirming : réservation OK", reserved)
    before = store.get_by_id(rec_id, db_path=DB)
    check("setup fresh-confirming : réservé maintenant (âge < timeout)",
          (time.time() - before["updated_at"]) < CONFIRM_TIMEOUT)

    with TestClient(app) as client:
        _login(client)
        r = client.post(f"/dashboard/api/unknown-clients/{rec_id}/confirm")
        check("19. confirming frais sans job -> 409", r.status_code == 409, r.text)

    after = store.get_by_id(rec_id, db_path=DB)
    check("19bis. statut INCHANGÉ ('confirming')", after["status"] == "confirming")
    check("19ter. updated_at INCHANGÉ (aucune écriture)", after["updated_at"] == before["updated_at"])
    check("19quater. job_id toujours NULL", after.get("job_id") is None)
    check("19quinquies. error_message toujours vide (pas de release)", after.get("error_message") is None)
    check("19sexies. aucune correspondance créée (jamais 'queued')",
          _active_mapping("37655007") is None)


def test_stale_confirming_without_job_is_restored() -> None:
    """Test 20 : un enregistrement 'confirming' STALE (âge > timeout) sans
    Job trouvé en queue doit être restauré vers 'associated' avec un
    error_message clair, prêt pour un nouvel essai de l'admin."""
    from fastapi.testclient import TestClient
    app = _make_app()

    rec_id = seed_associated(idclient=717, original_phone="37655008", amount=1500, txn_id="TXNSTALE1")
    reserved = store.reserve_for_confirmation(rec_id, db_path=DB)
    check("setup stale-confirming : réservation OK", reserved)
    # Simule le temps écoulé : recule updated_at au-delà du timeout de test.
    _force_field(rec_id, updated_at=time.time() - (CONFIRM_TIMEOUT + 5))

    with TestClient(app) as client:
        _login(client)
        r = client.post(f"/dashboard/api/unknown-clients/{rec_id}/confirm")
        check("20. stale confirming sans job -> 409 (restauration effectuée)", r.status_code == 409, r.text)

    after = store.get_by_id(rec_id, db_path=DB)
    check("20bis. restauré vers 'associated'", after["status"] == "associated")
    check("20ter. error_message renseigné (raison de la restauration)", bool(after.get("error_message")))
    check("20quater. job_id toujours NULL (aucun Job créé par la restauration)", after.get("job_id") is None)
    check("20quater-bis. aucune correspondance créée par la restauration",
          _active_mapping("37655008") is None)

    # Un nouvel essai doit désormais fonctionner normalement (précondition
    # 'associated' de nouveau satisfaite grâce à la restauration).
    FAKE_PG_BY_ID[717] = [client_row(717, "RESTORED:MAC:01", statu=2)]
    FAKE_UCRM_DETAILS[717] = {"balance": 1500, "account_credit": 0}
    FAKE_UCRM_SERVICES[717] = [svc("RESTORED:MAC:01", 1500)]
    with TestClient(app) as client:
        _login(client)
        r2 = client.post(f"/dashboard/api/unknown-clients/{rec_id}/confirm")
        check("20quinquies. re-confirmation après restauration -> 200", r2.status_code == 200, r2.text)
    m = _active_mapping("37655008")
    check("20sexies. correspondance créée par la re-confirmation réussie (-> 717)",
          m is not None and m["crm_client_id"] == "717", str(m))


def test_stale_confirming_with_existing_job_is_reconciled_not_restored() -> None:
    """Test 21 : même lorsque 'confirming' est STALE, si un Job existe déjà
    en queue pour ce txn_id, la réconciliation doit TOUJOURS primer sur la
    logique de staleness — jamais de restauration vers 'associated' quand un
    Job est déjà réellement en file (cela romprait le lien vers ce Job)."""
    from fastapi.testclient import TestClient
    from whatsapp_automation.models import Client, Job, Payment, Source
    app = _make_app()

    IDCLIENT = 718
    TXN = "TXNSTALEJOB1"
    ORIGINAL_PHONE = "37655009"
    FAKE_PG_BY_ID[IDCLIENT] = [client_row(IDCLIENT, "STALE:MAC:01", statu=2)]
    FAKE_UCRM_DETAILS[IDCLIENT] = {"balance": 1500, "account_credit": 0}
    FAKE_UCRM_SERVICES[IDCLIENT] = [svc("STALE:MAC:01", 1500)]

    rec_id = seed_associated(idclient=IDCLIENT, original_phone=ORIGINAL_PHONE, amount=1500, txn_id=TXN)
    reserved = store.reserve_for_confirmation(rec_id, db_path=DB)
    check("setup stale+job : réservation OK", reserved)

    stale_job = Job(
        job_id=queue_store.new_job_id(),
        client=Client(id=IDCLIENT, phone=ORIGINAL_PHONE, mac_address="STALE:MAC:01",
                      ip_address="10.0.1.1", current_status="suspended"),
        payment=Payment(amount_mru=1500, txn_id=TXN, date_heure=None, operator="bankily",
                        crm_balance_before=1500, should_unblock=True),
        source=Source(wnum=ORIGINAL_PHONE, sample_id="2026-07-13/stalejob",
                      received_at="2026-07-13T09:00:00+00:00"),
        unblock_macs=["STALE:MAC:01"],
    )
    internal_id = queue_store.enqueue(stale_job)
    check("setup stale+job : job bien enqueued (avant le 'crash')", internal_id is not None)

    # Ancien (au-delà du timeout) : un Job EXISTE déjà, la réconciliation doit
    # primer sur toute logique de staleness (jamais de restauration ici).
    _force_field(rec_id, updated_at=time.time() - (CONFIRM_TIMEOUT + 5))

    with TestClient(app) as client:
        _login(client)
        r = client.post(f"/dashboard/api/unknown-clients/{rec_id}/confirm")
        check("21. stale confirming AVEC job -> 200 (réconcilié, jamais restauré)", r.status_code == 200, r.text)
        body = r.json()
        check("21bis. job_id renvoyé == job pré-existant", body.get("job_id") == stale_job.job_id)
        check("21ter. réponse signale la réconciliation", body.get("reconciled") is True)

    after = store.get_by_id(rec_id, db_path=DB)
    check("21quater. statut = 'queued' (JAMAIS 'associated')", after["status"] == "queued")
    check("21quinquies. job_id persisté == job pré-existant", after["job_id"] == stale_job.job_id)
    m = _active_mapping(ORIGINAL_PHONE)
    check("21quinquies-bis. mapping créé par la réconciliation stale (37655009 -> 718)",
          m is not None and m["crm_client_id"] == str(IDCLIENT), str(m))

    with sqlite3.connect(os.environ["QUEUE_DB_PATH"]) as conn:
        n = conn.execute("SELECT COUNT(*) FROM jobs WHERE txn_id = ?", (TXN,)).fetchone()[0]
    check("21sexies. un seul job en queue pour ce txn_id", n == 1, n)


def test_concurrent_stale_recovery_is_atomic() -> None:
    """Test 22 : N appels concurrents à release_stale_confirmation() sur le
    MÊME enregistrement stale — un seul doit réussir la transition (CAS avec
    condition d'âge dans le même UPDATE atomique)."""
    rec_id = seed_associated(idclient=719, txn_id="TXNSTALERACE")
    reserved = store.reserve_for_confirmation(rec_id, db_path=DB)
    check("setup concurrence stale : réservation OK", reserved)
    _force_field(rec_id, updated_at=time.time() - (CONFIRM_TIMEOUT + 5))

    N = 8
    results: list = [None] * N
    barrier = threading.Barrier(N)

    def worker(idx: int):
        barrier.wait()
        results[idx] = store.release_stale_confirmation(
            rec_id, f"reset concurrent #{idx}", min_age_seconds=CONFIRM_TIMEOUT, db_path=DB,
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    wins = [r for r in results if r is True]
    check(f"22. récupération stale atomique : 1 seul gagnant sur {N} threads concurrents (gagnants={len(wins)})",
          len(wins) == 1)
    check("22bis. statut final = 'associated'", store.get_by_id(rec_id, db_path=DB)["status"] == "associated")


def main() -> int:
    queue_store.init_db()
    store.init_db(DB)
    crm_mappings.init_db(MAP_DB)
    _patch_forbidden_calls()
    _install_fakes()

    test_auth_and_basic_status_gates()
    test_concurrent_reservation_is_atomic()
    test_fresh_postgres_and_multi_subscription_and_phone_rules()
    test_enqueue_duplicate_is_reconciled_not_lost()
    test_crash_window_between_enqueue_and_mark_queued()
    test_fresh_confirming_without_job_returns_409_unchanged()
    test_stale_confirming_without_job_is_restored()
    test_stale_confirming_with_existing_job_is_reconciled_not_restored()
    test_concurrent_stale_recovery_is_atomic()

    print(f"\n=== {passed} PASS / {failed} FAIL ===")
    print("\n17. create_payment/unblock_by_mac/send_document/écritures PostgreSQL : "
          "tous patchés pour lever une AssertionError s'ils étaient appelés — "
          "le test aurait planté avant ce résumé si l'un d'eux avait été invoqué. [PASS implicite]")
    print("18. Aucun import ni démarrage de whatsapp_automation.worker.main dans ce "
          "script : le worker n'a jamais tourné. [PASS implicite]")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
