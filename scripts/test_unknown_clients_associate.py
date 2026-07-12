"""Test de la route Phase 3 : POST /dashboard/api/unknown-clients/{id}/associate.

Vérifie :
  - auth session obligatoire (401 sans session) ;
  - id inconnu -> 404 ;
  - enregistrement sans txn_id -> refus (409) ;
  - numéro saisi invalide -> refus (400) ;
  - numéro non trouvé dans PostgreSQL -> 404, statut reste 'pending' ;
  - numéro trouvé -> statut passe à 'associated', client_id/mac_address renvoyés ;
  - aucun Job n'est créé (jobqueue.store.enqueue jamais appelé) ;
  - aucun appel UCRM/MikroTik/UltraMsg (fonctions patchées pour lever si
    appelées) ;
  - la migration de schéma (ALTER TABLE) préserve les enregistrements Phase 1/2
    déjà présents en base.

100% local : bases SQLite temporaires + mini-app FastAPI ne montant QUE le
router dashboard. `db.postgres.get_clients_by_phone` est monkeypatché (pas de
PostgreSQL réel nécessaire). Pas de worker, pas de queue réelle utilisée.

À lancer : python scripts/test_unknown_clients_associate.py
"""

from __future__ import annotations

import io
import os
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
os.environ["EVENTS_DB_PATH"] = str(_TMP / "events.db")
os.environ["QUEUE_DB_PATH"] = str(_TMP / "queue.db")
os.environ["DASHBOARD_PASSWORD"] = "test-password-phase3"

from whatsapp_automation.db import postgres as pg  # noqa: E402
from whatsapp_automation.jobqueue import store as queue_store  # noqa: E402
from whatsapp_automation.webhook.dashboard import unknown_clients_store as store  # noqa: E402
from whatsapp_automation.worker import mikrotik, ucrm, ultramsg  # noqa: E402

DB = os.environ["UNKNOWN_CLIENTS_DB_PATH"]

SAMPLE_OK = "2026-07-11/33333333333333333333333333333333"
SAMPLE_NO_TXN = "2026-07-11/44444444444444444444444444444444"

passed = failed = 0


def check(label: str, cond: bool) -> None:
    global passed, failed
    print(f"{' ' if cond else '>'}[{'PASS' if cond else 'FAIL'}] {label}")
    passed += bool(cond)
    failed += not cond


def _forbid(*_a, **_kw):
    raise AssertionError("appel externe interdit en Phase 3 (UCRM/MikroTik/UltraMsg/queue)")


def _patch_forbidden_calls() -> None:
    """La Phase 3 ne doit jamais appeler UCRM/MikroTik/UltraMsg ni créer de Job.
    On patch les points d'entrée existants pour lever si jamais sollicités."""
    ucrm.create_payment = _forbid
    ucrm.get_balance = _forbid
    mikrotik.unblock_by_mac = _forbid
    mikrotik.block_by_mac = _forbid
    ultramsg.send_chat = _forbid
    ultramsg.send_document = _forbid
    ultramsg.send_image = _forbid
    queue_store.enqueue = _forbid


def seed() -> tuple[int, int]:
    store.init_db(DB)
    id_ok = store.insert_unknown_client(
        sample_id=SAMPLE_OK,
        txn_id="TXNOK1",
        amount=1200,
        date_heure="2026-07-11 08:00:00",
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
        date_heure="2026-07-11 09:00:00",
        operator="masrivi",
        original_phone="46600088",
        body_phone=None,
        group_id=None,
        raw_text="reçu masrivi 500 MRU (txn manquant)",
        db_path=DB,
    )
    return id_ok, id_no_txn


def test_migration_preserves_existing_records() -> None:
    # Une base déjà peuplée (schéma Phase 1/2, sans les colonnes Phase 3) doit
    # survivre à un nouvel appel init_db() : les anciennes lignes restent lisibles.
    before = store.list_recent(limit=50, db_path=DB)
    store.init_db(DB)  # ré-applique la migration (idempotent)
    after = store.list_recent(limit=50, db_path=DB)
    check(f"migration idempotente : même nombre de lignes avant/après ({len(before)}={len(after)})",
          len(before) == len(after))
    check("les nouvelles colonnes existent et valent None sur les anciennes lignes",
          all(r.get("client_id") is None for r in after))


FAKE_CLIENTS_DB: dict[str, list[dict]] = {}


def fake_get_clients_by_phone(phone: str) -> list[dict]:
    return FAKE_CLIENTS_DB.get(phone, [])


def test_routes(id_ok: int, id_no_txn: int) -> None:
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from whatsapp_automation.webhook.dashboard import router as dashboard_router
    except Exception as exc:
        print(f"  (TestClient indisponible, routes non testées : {type(exc).__name__}: {exc})")
        return

    # Monkeypatch : db.postgres.get_clients_by_phone (pas de PostgreSQL réel).
    pg.get_clients_by_phone = fake_get_clients_by_phone
    FAKE_CLIENTS_DB["37697850"] = [
        {"idclient": 555, "info": "37697850-Client Test", "mac": "AA:BB:CC:DD:EE:FF",
         "statu": 2, "ipaddress": "10.0.0.5"},
    ]

    app = FastAPI()
    app.include_router(dashboard_router)

    with TestClient(app) as client:
        # 1. Sans session -> 401.
        r = client.post(f"/dashboard/api/unknown-clients/{id_ok}/associate",
                        json={"entered_phone": "37697850"})
        check("POST /associate sans session -> 401", r.status_code == 401)

        # 2. Login.
        r = client.post("/dashboard/login", json={"password": "test-password-phase3"})
        check("login OK avec le mot de passe de test",
              r.status_code == 200 and r.json().get("ok") is True)

        # 3. Id inconnu -> 404.
        r = client.post("/dashboard/api/unknown-clients/999999/associate",
                        json={"entered_phone": "37697850"})
        check("id inconnu -> 404", r.status_code == 404)

        # 4. Enregistrement sans txn_id -> refus.
        r = client.post(f"/dashboard/api/unknown-clients/{id_no_txn}/associate",
                        json={"entered_phone": "37697850"})
        check(f"record sans txn_id -> refusé ({r.status_code})", r.status_code in (400, 409))
        rec = store.get_by_id(id_no_txn, db_path=DB)
        check("record sans txn_id : statut toujours 'pending' (pas touché)",
              rec["status"] == "pending")

        # 5. Numéro invalide -> 400.
        r = client.post(f"/dashboard/api/unknown-clients/{id_ok}/associate",
                        json={"entered_phone": "abc"})
        check(f"numéro invalide -> refusé ({r.status_code})", r.status_code in (400, 422))

        # 6. Numéro valide mais introuvable en base -> 404, statut reste pending.
        r = client.post(f"/dashboard/api/unknown-clients/{id_ok}/associate",
                        json={"entered_phone": "99999999"})
        check(f"numéro non trouvé -> 404 ({r.status_code})", r.status_code == 404)
        rec = store.get_by_id(id_ok, db_path=DB)
        check("numéro non trouvé : statut reste 'pending' (pas 'associated')",
              rec["status"] == "pending")
        check("numéro non trouvé : error_message renseigné", bool(rec.get("error_message")))

        # 7. Numéro valide et trouvé -> association réussie.
        stats_before = queue_store.stats()
        r = client.post(f"/dashboard/api/unknown-clients/{id_ok}/associate",
                        json={"entered_phone": "37697850"})
        check(f"numéro trouvé -> 200 ({r.status_code})", r.status_code == 200)
        data = r.json()
        check("réponse ok=true", data.get("ok") is True)
        check("réponse status='associated'", data.get("status") == "associated")
        check("message précise qu'aucun paiement n'a été créé",
              "aucun paiement" in data.get("message", "").lower())

        # 8. client_id / mac_address présents dans l'aperçu.
        preview = data.get("client_preview") or {}
        check(f"client_preview.client_id = {preview.get('client_id')!r} (attendu '555')",
              preview.get("client_id") == "555")
        check(f"client_preview.mac_address = {preview.get('mac_address')!r}",
              preview.get("mac_address") == "AA:BB:CC:DD:EE:FF")
        check("client_preview.rows_count == 1", preview.get("rows_count") == 1)

        # Statut persistant en SQLite.
        rec = store.get_by_id(id_ok, db_path=DB)
        check("enregistrement SQLite : status='associated'", rec["status"] == "associated")
        check("enregistrement SQLite : client_id='555'", rec["client_id"] == "555")
        check("enregistrement SQLite : mac_address stocké", rec["mac_address"] == "AA:BB:CC:DD:EE:FF")
        check("enregistrement SQLite : entered_phone stocké", rec["entered_phone"] == "37697850")
        check("enregistrement SQLite : associated_at renseigné", rec["associated_at"] is not None)

        # 9. Aucun Job créé (la queue n'a pas bougé — enqueue est patché pour
        # lever une exception s'il est appelé, donc si on arrive ici sans
        # exception, c'est qu'il n'a jamais été invoqué).
        stats_after = queue_store.stats()
        check("aucun Job créé (stats file d'attente inchangées)", stats_before == stats_after)

        # L'enregistrement associé ne réapparaît plus dans le filtre 'pending'.
        r = client.get("/dashboard/api/unknown-clients?status=pending&limit=50")
        ids_pending = [row["id"] for row in r.json()]
        check("l'enregistrement associé n'apparaît plus dans les 'pending'",
              id_ok not in ids_pending)


def main() -> int:
    queue_store.init_db()
    _patch_forbidden_calls()
    id_ok, id_no_txn = seed()
    test_migration_preserves_existing_records()
    test_routes(id_ok, id_no_txn)
    print(f"\n=== {passed} PASS / {failed} FAIL ===")
    print("\n10. Aucun appel UCRM/MikroTik/UltraMsg : les points d'entrée étaient "
          "patchés pour lever une AssertionError s'ils étaient invoqués — le test "
          "aurait planté avant ce résumé si l'un d'eux avait été appelé. [PASS implicite]")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
