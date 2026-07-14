"""Test de la route : POST /dashboard/api/unknown-clients/{id}/associate
(association par IDENTIFIANT CRM — remplace l'association par téléphone).

Vérifie :
  - auth session obligatoire (401 sans session) ;
  - id inconnu -> 404 ;
  - enregistrement sans txn_id -> refus (409) ;
  - identifiant CRM invalide ("abc", "0", "-5") -> refus (400) ;
  - identifiant CRM non trouvé dans PostgreSQL -> 404, statut reste 'pending',
    error_message renseigné ;
  - identifiant trouvé -> statut 'associated', client_id stocké,
    entered_phone reste NULL (plus de saisie téléphone), subscription_phone
    dérivé du champ `info` (informatif), associated_at renseigné ;
  - aperçu complet : nom, nombre d'abonnements, liste des abonnements
    (MAC/IP/statut/info) — cas multi-abonnements couvert ;
  - AUCUNE correspondance whatsapp_crm_mappings créée à l'association — la
    mémoire n'est écrite qu'une fois le paiement en file (statut 'queued',
    décision Ali ; couvert par test_unknown_clients_confirm.py) ;
  - ré-association vers un AUTRE identifiant CRM (statut 'associated')
    autorisée, toujours sans correspondance créée ;
  - statut 'queued' -> ré-association refusée (409) ;
  - enregistrement sans original_phone -> association OK ;
  - le lookup passe par pg.get_client_by_id — JAMAIS get_clients_by_phone
    (patché pour lever si appelé) ;
  - aucun Job créé (jobqueue.store.enqueue jamais appelé) ;
  - aucun appel UCRM/MikroTik/UltraMsg (fonctions patchées pour lever si
    appelées) ;
  - la migration de schéma (ALTER TABLE) préserve les enregistrements
    déjà présents en base.

100% local : bases SQLite temporaires + mini-app FastAPI ne montant QUE le
router dashboard. `db.postgres.get_client_by_id` est monkeypatché (pas de
PostgreSQL réel nécessaire). Pas de worker, pas de queue réelle utilisée.

À lancer : python scripts/test_unknown_clients_associate.py
"""

from __future__ import annotations

import io
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Isole totalement des données réelles : bases dédiées + mot de passe de test,
# fixés AVANT le premier import de whatsapp_automation (config lit l'env à l'import).
_TMP = Path(tempfile.mkdtemp(prefix="uc_associate_test_"))
os.environ["UNKNOWN_CLIENTS_DB_PATH"] = str(_TMP / "unknown_clients.db")
os.environ["WHATSAPP_CRM_MAPPINGS_DB_PATH"] = str(_TMP / "whatsapp_crm_mappings.db")
os.environ["EVENTS_DB_PATH"] = str(_TMP / "events.db")
os.environ["QUEUE_DB_PATH"] = str(_TMP / "queue.db")
os.environ["DASHBOARD_PASSWORD"] = "test-password-crm-assoc"

from whatsapp_automation.db import postgres as pg  # noqa: E402
from whatsapp_automation.jobqueue import store as queue_store  # noqa: E402
from whatsapp_automation.webhook import crm_mappings  # noqa: E402
from whatsapp_automation.webhook.dashboard import unknown_clients_store as store  # noqa: E402
from whatsapp_automation.worker import mikrotik, ucrm, ultramsg  # noqa: E402

DB = os.environ["UNKNOWN_CLIENTS_DB_PATH"]
MAP_DB = os.environ["WHATSAPP_CRM_MAPPINGS_DB_PATH"]

SAMPLE_OK = "2026-07-13/33333333333333333333333333333333"
SAMPLE_NO_TXN = "2026-07-13/44444444444444444444444444444444"
SAMPLE_NO_PHONE = "2026-07-13/55555555555555555555555555555555"

passed = failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    marker = " " if cond else ">"
    print(f"{marker}[{'PASS' if cond else 'FAIL'}] {label}" + (f"  -- {detail}" if detail else ""))
    passed += bool(cond)
    failed += not cond


def _forbid(name):
    def _raise(*_a, **_kw):
        raise AssertionError(f"appel interdit pendant l'association : {name}")
    return _raise


def _patch_forbidden_calls() -> None:
    """L'association ne doit jamais appeler UCRM/MikroTik/UltraMsg, ni créer
    de Job, ni utiliser le lookup téléphone (get_clients_by_phone). On patch
    les points d'entrée existants pour lever si jamais sollicités."""
    ucrm.create_payment = _forbid("ucrm.create_payment")
    ucrm.get_balance = _forbid("ucrm.get_balance")
    mikrotik.unblock_by_mac = _forbid("mikrotik.unblock_by_mac")
    mikrotik.block_by_mac = _forbid("mikrotik.block_by_mac")
    ultramsg.send_chat = _forbid("ultramsg.send_chat")
    ultramsg.send_document = _forbid("ultramsg.send_document")
    ultramsg.send_image = _forbid("ultramsg.send_image")
    queue_store.enqueue = _forbid("queue_store.enqueue")
    pg.get_clients_by_phone = _forbid("pg.get_clients_by_phone")


FAKE_PG_BY_ID: dict[int, list[dict]] = {}


def fake_get_client_by_id(idclient) -> list[dict]:
    return FAKE_PG_BY_ID.get(int(idclient), [])


def seed() -> tuple[int, int, int]:
    store.init_db(DB)
    id_ok = store.insert_unknown_client(
        sample_id=SAMPLE_OK,
        txn_id="TXNOK1",
        amount=1200,
        date_heure="2026-07-13 08:00:00",
        operator="bankily",
        original_phone="37600099",
        body_phone=None,
        group_id=None,
        raw_text="reçu bankily 1200 MRU",
        db_path=DB,
    )
    id_no_txn = store.insert_unknown_client(
        sample_id=SAMPLE_NO_TXN,
        txn_id="",
        amount=500,
        date_heure="2026-07-13 09:00:00",
        operator="masrivi",
        original_phone="46600088",
        body_phone=None,
        group_id=None,
        raw_text="reçu masrivi 500 MRU (txn manquant)",
        db_path=DB,
    )
    id_no_phone = store.insert_unknown_client(
        sample_id=SAMPLE_NO_PHONE,
        txn_id="TXNNOPHONE",
        amount=900,
        date_heure="2026-07-13 10:00:00",
        operator="sedad",
        original_phone="",
        body_phone=None,
        group_id="120363000000000000",
        raw_text="reçu sedad 900 MRU (expéditeur groupe non résolu)",
        db_path=DB,
    )
    return id_ok, id_no_txn, id_no_phone


def test_migration_preserves_existing_records() -> None:
    # Une base déjà peuplée doit survivre à un nouvel appel init_db() : les
    # anciennes lignes restent lisibles (migration ALTER TABLE idempotente).
    before = store.list_recent(limit=50, db_path=DB)
    store.init_db(DB)  # ré-applique la migration (idempotent)
    after = store.list_recent(limit=50, db_path=DB)
    check(f"migration idempotente : même nombre de lignes avant/après ({len(before)}={len(after)})",
          len(before) == len(after))
    check("les colonnes d'association existent et valent None sur les lignes fraîches",
          all(r.get("client_id") is None for r in after))


def _mapping_rows(phone: str) -> list[dict]:
    with sqlite3.connect(MAP_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM whatsapp_crm_mappings WHERE whatsapp_phone = ? ORDER BY id",
            (phone,),
        ).fetchall()
    return [dict(r) for r in rows]


def test_routes(id_ok: int, id_no_txn: int, id_no_phone: int) -> None:
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from whatsapp_automation.webhook.dashboard import router as dashboard_router
    except Exception as exc:
        print(f"  (TestClient indisponible, routes non testées : {type(exc).__name__}: {exc})")
        return

    # Monkeypatch : db.postgres.get_client_by_id (pas de PostgreSQL réel).
    pg.get_client_by_id = fake_get_client_by_id
    # Client 555 : DEUX abonnements (multi-MAC), même idclient.
    FAKE_PG_BY_ID[555] = [
        {"idclient": 555, "info": "37697850-Client Test", "mac": "AA:BB:CC:DD:EE:FF",
         "statu": 2, "ipaddress": "10.0.0.5"},
        {"idclient": 555, "info": "37697850-Client Test", "mac": "AA:BB:CC:DD:EE:00",
         "statu": 0, "ipaddress": "10.0.0.6"},
    ]
    # Client 777 : mono-abonnement (cible de la ré-association corrective).
    FAKE_PG_BY_ID[777] = [
        {"idclient": 777, "info": "31000001-Autre Client", "mac": "77:77:77:77:77:77",
         "statu": 2, "ipaddress": "10.0.7.7"},
    ]

    app = FastAPI()
    app.include_router(dashboard_router)

    with TestClient(app) as client:
        # 1. Sans session -> 401.
        r = client.post(f"/dashboard/api/unknown-clients/{id_ok}/associate",
                        json={"crm_client_id": "555"})
        check("POST /associate sans session -> 401", r.status_code == 401)

        # 2. Login.
        r = client.post("/dashboard/login", json={"password": "test-password-crm-assoc"})
        check("login OK avec le mot de passe de test",
              r.status_code == 200 and r.json().get("ok") is True)

        # 3. Id inconnu -> 404.
        r = client.post("/dashboard/api/unknown-clients/999999/associate",
                        json={"crm_client_id": "555"})
        check("id inconnu -> 404", r.status_code == 404)

        # 4. Enregistrement sans txn_id -> refus.
        r = client.post(f"/dashboard/api/unknown-clients/{id_no_txn}/associate",
                        json={"crm_client_id": "555"})
        check(f"record sans txn_id -> refusé ({r.status_code})", r.status_code in (400, 409))
        rec = store.get_by_id(id_no_txn, db_path=DB)
        check("record sans txn_id : statut toujours 'pending' (pas touché)",
              rec["status"] == "pending")

        # 5. Identifiant CRM invalide -> 400 (non numérique, zéro, négatif).
        for bad in ("abc", "0", "-5", "", "12 34"):
            r = client.post(f"/dashboard/api/unknown-clients/{id_ok}/associate",
                            json={"crm_client_id": bad})
            check(f"identifiant CRM invalide {bad!r} -> 400 ({r.status_code})",
                  r.status_code == 400)

        # 6. Identifiant CRM valide mais introuvable -> 404, statut reste pending.
        r = client.post(f"/dashboard/api/unknown-clients/{id_ok}/associate",
                        json={"crm_client_id": "99999"})
        check(f"identifiant CRM non trouvé -> 404 ({r.status_code})", r.status_code == 404)
        rec = store.get_by_id(id_ok, db_path=DB)
        check("identifiant non trouvé : statut reste 'pending' (pas 'associated')",
              rec["status"] == "pending")
        check("identifiant non trouvé : error_message renseigné", bool(rec.get("error_message")))
        check("identifiant non trouvé : aucune correspondance créée",
              crm_mappings.get_active_mapping("37600099", db_path=MAP_DB) is None)

        # 7. Identifiant CRM trouvé -> association réussie.
        stats_before = queue_store.stats()
        r = client.post(f"/dashboard/api/unknown-clients/{id_ok}/associate",
                        json={"crm_client_id": "555"})
        check(f"identifiant trouvé -> 200 ({r.status_code})", r.status_code == 200, r.text)
        data = r.json()
        check("réponse ok=true", data.get("ok") is True)
        check("réponse status='associated'", data.get("status") == "associated")
        check("message précise qu'aucun paiement n'a été créé",
              "aucun paiement" in data.get("message", "").lower())

        # 8. Aperçu complet : identifiant, nom, abonnements (multi-MAC).
        preview = data.get("client_preview") or {}
        check(f"client_preview.client_id = {preview.get('client_id')!r} (attendu '555')",
              preview.get("client_id") == "555")
        check("client_preview.name parsé depuis info ('Client Test')",
              preview.get("name") == "Client Test", preview.get("name"))
        check("client_preview.subscriptions_count == 2 (multi-abonnements)",
              preview.get("subscriptions_count") == 2)
        subs = preview.get("subscriptions") or []
        check("client_preview.subscriptions : 2 lignes avec MAC/IP/statut",
              len(subs) == 2
              and subs[0].get("mac") == "AA:BB:CC:DD:EE:FF"
              and subs[0].get("ip") == "10.0.0.5"
              and subs[0].get("status") == "suspended"
              and subs[1].get("status") == "active",
              str(subs))
        check("client_preview.subscription_phone dérivé de info ('37697850')",
              preview.get("subscription_phone") == "37697850")

        # Statut persistant en SQLite.
        rec = store.get_by_id(id_ok, db_path=DB)
        check("enregistrement SQLite : status='associated'", rec["status"] == "associated")
        check("enregistrement SQLite : client_id='555'", rec["client_id"] == "555")
        check("enregistrement SQLite : entered_phone reste NULL (plus de saisie téléphone)",
              rec["entered_phone"] is None)
        check("enregistrement SQLite : subscription_phone dérivé de info",
              rec["subscription_phone"] == "37697850")
        check("enregistrement SQLite : mac_address (aperçu) stocké",
              rec["mac_address"] == "AA:BB:CC:DD:EE:FF")
        check("enregistrement SQLite : associated_at renseigné", rec["associated_at"] is not None)

        # 9. AUCUNE correspondance WhatsApp -> CRM créée à l'association
        # (décision Ali : la mémoire n'est écrite qu'au statut 'queued' —
        # une association abandonnée ne doit jamais router les reçus futurs).
        check("aucune correspondance créée à l'association",
              crm_mappings.get_active_mapping("37600099", db_path=MAP_DB) is None)
        check("réponse : pas de champ mapping", data.get("mapping") is None)

        # 10. Ré-association corrective (statut 'associated') vers un autre
        # identifiant : autorisée, toujours sans correspondance créée.
        r = client.post(f"/dashboard/api/unknown-clients/{id_ok}/associate",
                        json={"crm_client_id": 777})  # int JSON accepté aussi
        check(f"ré-association 'associated' -> 200 ({r.status_code})", r.status_code == 200, r.text)
        rec = store.get_by_id(id_ok, db_path=DB)
        check("ré-association : client_id mis à jour ('777')", rec["client_id"] == "777")
        check("ré-association : toujours aucune correspondance",
              crm_mappings.get_active_mapping("37600099", db_path=MAP_DB) is None)
        check("table des correspondances vide pour ce numéro (aucun historique parasite)",
              len(_mapping_rows("37600099")) == 0)

        # 11. Statut 'queued' -> ré-association refusée (jamais changer le
        # client d'un paiement déjà engagé).
        store.reserve_for_confirmation(id_ok, db_path=DB)
        store.mark_queued(id_ok, "job-test-lock", db_path=DB)
        r = client.post(f"/dashboard/api/unknown-clients/{id_ok}/associate",
                        json={"crm_client_id": "555"})
        check("statut 'queued' -> associate 409", r.status_code == 409)
        rec = store.get_by_id(id_ok, db_path=DB)
        check("statut 'queued' : client_id inchangé ('777')", rec["client_id"] == "777")

        # 12. Enregistrement sans original_phone : association OK (la mémoire
        # n'est de toute façon écrite qu'au statut 'queued').
        r = client.post(f"/dashboard/api/unknown-clients/{id_no_phone}/associate",
                        json={"crm_client_id": "555"})
        check(f"record sans original_phone -> association 200 ({r.status_code})",
              r.status_code == 200, r.text)

        # 13. Aucun Job créé (la queue n'a pas bougé — enqueue est patché pour
        # lever une exception s'il est appelé, donc si on arrive ici sans
        # exception, c'est qu'il n'a jamais été invoqué).
        stats_after = queue_store.stats()
        check("aucun Job créé (stats file d'attente inchangées)", stats_before == stats_after)

        # L'enregistrement associé/queued ne réapparaît plus dans 'pending'.
        r = client.get("/dashboard/api/unknown-clients?status=pending&limit=50")
        ids_pending = [row["id"] for row in r.json()]
        check("l'enregistrement traité n'apparaît plus dans les 'pending'",
              id_ok not in ids_pending)


def main() -> int:
    queue_store.init_db()
    crm_mappings.init_db(MAP_DB)
    _patch_forbidden_calls()
    id_ok, id_no_txn, id_no_phone = seed()
    test_migration_preserves_existing_records()
    test_routes(id_ok, id_no_txn, id_no_phone)
    print(f"\n=== {passed} PASS / {failed} FAIL ===")
    print("\nAucun appel UCRM/MikroTik/UltraMsg/queue/get_clients_by_phone : les points "
          "d'entrée étaient patchés pour lever une AssertionError s'ils étaient invoqués "
          "— le test aurait planté avant ce résumé si l'un d'eux avait été appelé. "
          "[PASS implicite]")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
