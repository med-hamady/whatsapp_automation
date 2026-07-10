"""Test de la table numeros_introuvable (webhook/dashboard/unknown_clients_store.py).

Vérifie : initialisation du schéma, insertion, idempotence par sample_id,
préservation des champs, et les helpers de lecture get_by_sample_id / list_recent.

100% local (SQLite temporaire) : pas de PostgreSQL, pas d'UCRM, pas de MikroTik,
pas d'UltraMsg.

À lancer : python scripts/test_unknown_clients_store.py
"""

from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from whatsapp_automation.webhook.dashboard import unknown_clients_store as store  # noqa: E402

SAMPLE_1 = "2026-07-10/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SAMPLE_2 = "2026-07-10/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="unknown_clients_test_"))
    db = str(tmp / "unknown_clients.db")

    passed = failed = 0

    def check(label, cond):
        nonlocal passed, failed
        print(f"{' ' if cond else '>'}[{'PASS' if cond else 'FAIL'}] {label}")
        passed += bool(cond); failed += not cond

    # 1. Le schéma s'initialise sans erreur, y compris rappelé deux fois.
    store.init_db(db)
    store.init_db(db)
    check("init_db() idempotent (fichier créé)", Path(db).exists())

    # 2. Insertion d'un client introuvable.
    id1 = store.insert_unknown_client(
        sample_id=SAMPLE_1,
        txn_id="TXN123",
        amount=1500,
        date_heure="2026-07-10 10:00:00",
        operator="bankily",
        original_phone="37697850",
        body_phone=None,
        group_id=None,
        raw_text="BANKILY paiement 1500 MRU",
        db_path=db,
    )
    check(f"insert_unknown_client retourne un id ({id1})", isinstance(id1, int) and id1 > 0)

    # 3. Insertion du même sample_id → pas de doublon, même id retourné.
    id1_bis = store.insert_unknown_client(
        sample_id=SAMPLE_1,
        txn_id="TXN123",
        amount=1500,
        date_heure="2026-07-10 10:00:00",
        operator="bankily",
        original_phone="37697850",
        body_phone=None,
        group_id=None,
        raw_text="BANKILY paiement 1500 MRU",
        db_path=db,
    )
    check(f"ré-insertion même sample_id → même id ({id1_bis})", id1_bis == id1)
    rows = store.list_recent(limit=50, db_path=db)
    check(f"pas de doublon en base (count={len(rows)})", len(rows) == 1)

    # 4. Les champs sont bien préservés (y compris sample_date dérivé du sample_id).
    rec = store.get_by_sample_id(SAMPLE_1, db_path=db)
    check("get_by_sample_id trouve la ligne", rec is not None)
    check(f"sample_date dérivé = {rec['sample_date']!r} (attendu '2026-07-10')",
          rec["sample_date"] == "2026-07-10")
    check(f"txn_id préservé = {rec['txn_id']!r}", rec["txn_id"] == "TXN123")
    check(f"amount préservé = {rec['amount']!r}", rec["amount"] == 1500)
    check(f"operator préservé = {rec['operator']!r}", rec["operator"] == "bankily")
    check(f"original_phone préservé = {rec['original_phone']!r}",
          rec["original_phone"] == "37697850")
    check(f"status par défaut = {rec['status']!r} (attendu 'pending')",
          rec["status"] == "pending")
    check("job_id / ucrm_payment_id / error_message vides (Phase 1, non traités)",
          rec["job_id"] is None and rec["ucrm_payment_id"] is None
          and rec["error_message"] is None)

    # 5. txn_id vide autorisé, mais l'unicité par sample_id protège toujours.
    id2 = store.insert_unknown_client(
        sample_id=SAMPLE_2,
        txn_id="",
        amount=800,
        date_heure=None,
        operator="generic",
        original_phone="",
        body_phone="46123456",
        group_id="120363999@g.us",
        raw_text="",
        db_path=db,
    )
    check(f"insertion avec txn_id vide acceptée (id={id2})", isinstance(id2, int) and id2 != id1)
    rec2 = store.get_by_sample_id(SAMPLE_2, db_path=db)
    check("txn_id vide stocké comme NULL (pas de collision d'unicité future)",
          rec2["txn_id"] is None)
    check(f"body_phone/group_id préservés pour SAMPLE_2 "
          f"({rec2['body_phone']!r}, {rec2['group_id']!r})",
          rec2["body_phone"] == "46123456" and rec2["group_id"] == "120363999@g.us")

    # 6. list_recent renvoie les 2 lignes, la plus récente en premier.
    rows = store.list_recent(limit=50, db_path=db)
    check(f"list_recent renvoie 2 lignes ({len(rows)})", len(rows) == 2)
    check("list_recent trié par created_at DESC (SAMPLE_2 en premier)",
          rows[0]["sample_id"] == SAMPLE_2)

    # Robustesse : sample_id vide refusé proprement (pas d'exception).
    id_empty = store.insert_unknown_client(sample_id="", db_path=db)
    check("sample_id vide → None (pas d'exception)", id_empty is None)

    print(f"\n=== {passed} PASS / {failed} FAIL ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
