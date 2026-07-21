"""Test des endpoints dashboard de lecture des numéros introuvables (Phase 2 UI).

Vérifie :
  - unknown_clients_store.get_by_id() et list_recent(status=...) (helpers Phase 2) ;
  - GET /dashboard/api/unknown-clients (liste, filtrée par statut, auth session) ;
  - GET /dashboard/api/unknown-clients/{id} (détail + sample_sid dérivé du sample_id) ;
  - 401 sans session, 404 sur id inconnu.

100% local : bases SQLite temporaires + mini-app FastAPI ne montant QUE le
router dashboard (pas de PostgreSQL, pas d'UCRM, pas de MikroTik, pas
d'UltraMsg, pas de worker, pas de création de Job/paiement).

À lancer : python scripts/test_unknown_clients_dashboard_api.py
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
_TMP = Path(tempfile.mkdtemp(prefix="uc_dashboard_test_"))
os.environ["UNKNOWN_CLIENTS_DB_PATH"] = str(_TMP / "unknown_clients.db")
os.environ["EVENTS_DB_PATH"] = str(_TMP / "events.db")
os.environ["QUEUE_DB_PATH"] = str(_TMP / "queue.db")
os.environ["DASHBOARD_PASSWORD"] = "test-password-phase2"

from whatsapp_automation.webhook.dashboard import unknown_clients_store as store  # noqa: E402

DB = os.environ["UNKNOWN_CLIENTS_DB_PATH"]

SAMPLE_1 = "2026-07-10/11111111111111111111111111111111"
SAMPLE_2 = "2026-07-10/22222222222222222222222222222222"

passed = failed = 0


def check(label: str, cond: bool) -> None:
    global passed, failed
    print(f"{' ' if cond else '>'}[{'PASS' if cond else 'FAIL'}] {label}")
    passed += bool(cond)
    failed += not cond


def seed() -> tuple[int, int]:
    store.init_db(DB)
    id1 = store.insert_unknown_client(
        sample_id=SAMPLE_1,
        txn_id="TXNAAA",
        amount=1000,
        date_heure="2026-07-10 09:00:00",
        operator="bankily",
        whatsapp_phone="37600001",
        body_phone=None,
        group_id=None,
        raw_text="reçu bankily 1000 MRU",
        db_path=DB,
    )
    id2 = store.insert_unknown_client(
        sample_id=SAMPLE_2,
        txn_id="",  # txn manquant → doit déclencher l'avertissement Phase 2 côté UI
        amount=500,
        date_heure="2026-07-10 10:00:00",
        operator="masrivi",
        whatsapp_phone="46600002",
        body_phone=None,
        group_id=None,
        raw_text="reçu masrivi 500 MRU (txn manquant)",
        db_path=DB,
    )
    return id1, id2


def test_store_helpers() -> tuple[int, int]:
    id1, id2 = seed()

    rec1 = store.get_by_id(id1, db_path=DB)
    check("get_by_id retrouve l'enregistrement 1", rec1 is not None and rec1["id"] == id1)
    check("get_by_id id inconnu -> None", store.get_by_id(999999, db_path=DB) is None)

    rows = store.list_recent(limit=50, db_path=DB)
    check(f"list_recent (sans filtre) renvoie 2 lignes ({len(rows)})", len(rows) == 2)

    pending = store.list_recent(limit=50, status="pending", db_path=DB)
    check(f"list_recent(status='pending') renvoie 2 lignes ({len(pending)})", len(pending) == 2)

    processed = store.list_recent(limit=50, status="processed", db_path=DB)
    check("list_recent(status='processed') renvoie 0 ligne (rien traité en Phase 2)",
          len(processed) == 0)

    return id1, id2


def test_routes(id1: int, id2: int) -> None:
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from whatsapp_automation.webhook.dashboard import router as dashboard_router
    except Exception as exc:
        print(f"  (TestClient indisponible, routes non testées : {type(exc).__name__}: {exc})")
        return

    app = FastAPI()
    app.include_router(dashboard_router)

    with TestClient(app) as client:
        r = client.get("/dashboard/api/unknown-clients")
        check("GET /unknown-clients sans session -> 401", r.status_code == 401)

        r = client.post("/dashboard/login", json={"password": "test-password-phase2"})
        check("login OK avec le mot de passe de test", r.status_code == 200 and r.json().get("ok") is True)

        r = client.get("/dashboard/api/unknown-clients?status=pending&limit=50")
        check("GET /unknown-clients (session valide) -> 200", r.status_code == 200)
        data = r.json()
        check(f"2 enregistrements pending renvoyés ({len(data)})", len(data) == 2)

        r = client.get(f"/dashboard/api/unknown-clients/{id1}")
        check("GET /unknown-clients/{id} -> 200", r.status_code == 200)
        detail = r.json()
        check(f"détail contient txn_id={detail.get('txn_id')!r}", detail.get("txn_id") == "TXNAAA")
        check(f"sample_sid dérivé du sample_id ({detail.get('sample_sid')!r})",
              detail.get("sample_sid") == "11111111111111111111111111111111")

        r = client.get(f"/dashboard/api/unknown-clients/{id2}")
        check("enregistrement 2 : txn_id manquant -> vide/None (déclenche l'avertissement UI)",
              not r.json().get("txn_id"))

        r = client.get("/dashboard/api/unknown-clients/999999")
        check("id inconnu -> 404", r.status_code == 404)

        # Rappel Phase 2 : aucune route POST de traitement n'existe encore.
        r = client.post(f"/dashboard/api/unknown-clients/{id1}/pay")
        check("aucune route POST de paiement n'existe (404 attendu)", r.status_code == 404)


def main() -> int:
    id1, id2 = test_store_helpers()
    test_routes(id1, id2)
    print(f"\n=== {passed} PASS / {failed} FAIL ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
