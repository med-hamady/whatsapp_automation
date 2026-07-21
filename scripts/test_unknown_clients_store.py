"""Test de la table numeros_introuvable (webhook/dashboard/unknown_clients_store.py).

Vérifie : initialisation du schéma, insertion, idempotence par sample_id,
préservation des champs, les helpers de lecture get_by_sample_id / list_recent,
la migration d'une base créée par une version antérieure du schéma, et
find_client_id_for_phone (auto-résolution des paiements futurs — ne considère
que les tickets 'queued').

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
        whatsapp_phone="37697850",
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
        whatsapp_phone="37697850",
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
    check(f"whatsapp_phone préservé = {rec['whatsapp_phone']!r}",
          rec["whatsapp_phone"] == "37697850")
    check(f"status par défaut = {rec['status']!r} (attendu 'pending')",
          rec["status"] == "pending")
    check("job_id / client_id / error_message vides à l'insertion",
          rec["job_id"] is None and rec["client_id"] is None
          and rec["error_message"] is None)

    # 5. txn_id vide autorisé, mais l'unicité par sample_id protège toujours.
    id2 = store.insert_unknown_client(
        sample_id=SAMPLE_2,
        txn_id="",
        amount=800,
        date_heure=None,
        operator="generic",
        whatsapp_phone="",
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

    # 7. Migration d'une base créée par une version antérieure du schéma :
    # colonnes mortes (entered_phone/subscription_phone/mac_address/
    # ucrm_payment_id) supprimées, original_phone renommée en whatsapp_phone,
    # DONNÉES EXISTANTES préservées (aucune perte pour une base déjà en prod).
    import sqlite3
    legacy_db = str(tmp / "legacy_unknown_clients.db")
    conn = sqlite3.connect(legacy_db)
    conn.executescript("""
        CREATE TABLE numeros_introuvable (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            sample_id        TEXT NOT NULL,
            sample_date      TEXT,
            original_phone   TEXT,
            body_phone       TEXT,
            group_id         TEXT,
            txn_id           TEXT,
            amount           INTEGER,
            date_heure       TEXT,
            operator         TEXT,
            raw_text         TEXT,
            status           TEXT NOT NULL DEFAULT 'pending',
            job_id           TEXT,
            ucrm_payment_id  TEXT,
            error_message    TEXT,
            created_at       REAL NOT NULL,
            updated_at       REAL NOT NULL,
            entered_phone    TEXT,
            subscription_phone TEXT,
            client_id        TEXT,
            mac_address      TEXT,
            associated_at    REAL
        );
    """)
    conn.execute(
        """INSERT INTO numeros_introuvable
           (sample_id, original_phone, txn_id, amount, client_id,
            entered_phone, subscription_phone, mac_address, created_at, updated_at)
           VALUES ('2026-07-01/legacy0000000000000000000000000001', '37600123', 'TXNLEGACY',
                   1000, '999', NULL, '37600999', 'AA:BB:CC:00:11:22', 1.0, 1.0)"""
    )
    conn.commit()
    conn.close()

    store.init_db(legacy_db)  # doit migrer sans exception
    legacy_cols = {
        r[1] for r in sqlite3.connect(legacy_db).execute("PRAGMA table_info(numeros_introuvable)")
    }
    check("migration : colonnes mortes supprimées",
          not ({"entered_phone", "subscription_phone", "mac_address", "ucrm_payment_id"} & legacy_cols))
    check("migration : original_phone renommée en whatsapp_phone",
          "whatsapp_phone" in legacy_cols and "original_phone" not in legacy_cols)

    legacy_rec = store.get_by_sample_id(
        "2026-07-01/legacy0000000000000000000000000001", db_path=legacy_db,
    )
    check("migration : ligne existante toujours présente", legacy_rec is not None)
    check(f"migration : whatsapp_phone préserve l'ancienne valeur original_phone ({legacy_rec['whatsapp_phone']!r})",
          legacy_rec["whatsapp_phone"] == "37600123")
    check(f"migration : client_id préservé ({legacy_rec['client_id']!r})",
          legacy_rec["client_id"] == "999")

    store.init_db(legacy_db)  # ré-appel : idempotent, ne doit rien casser
    check("migration idempotente (deuxième init_db sans erreur)", True)

    # 8. find_client_id_for_phone : auto-résolution des paiements futurs.
    # Seuls les tickets 'queued' comptent — jamais 'pending'/'associated'.
    check("find_client_id_for_phone : numéro inconnu -> None",
          store.find_client_id_for_phone("00000000", db_path=db) is None)
    check("find_client_id_for_phone : numéro vide -> None",
          store.find_client_id_for_phone("", db_path=db) is None)

    PHONE_RESOLVED = "37699000"
    id3 = store.insert_unknown_client(sample_id="2026-07-11/resolved0000000000000001",
                                       whatsapp_phone=PHONE_RESOLVED, txn_id="TXNRES1", db_path=db)
    check("find_client_id_for_phone : ticket 'pending' -> None (pas encore associé)",
          store.find_client_id_for_phone(PHONE_RESOLVED, db_path=db) is None)

    store.associate_unknown_client(id3, client_id="321", db_path=db)
    check("find_client_id_for_phone : ticket 'associated' -> None (pas encore confirmé)",
          store.find_client_id_for_phone(PHONE_RESOLVED, db_path=db) is None)

    store.reserve_for_confirmation(id3, db_path=db)
    store.mark_queued(id3, "job-res-1", db_path=db)
    check("find_client_id_for_phone : ticket 'queued' -> client_id résolu",
          store.find_client_id_for_phone(PHONE_RESOLVED, db_path=db) == "321")

    # Un 2e ticket 'queued' plus récent pour le MÊME numéro doit l'emporter
    # (cas rare : le client redevient introuvable puis est ré-associé).
    id4 = store.insert_unknown_client(sample_id="2026-07-11/resolved0000000000000002",
                                       whatsapp_phone=PHONE_RESOLVED, txn_id="TXNRES2", db_path=db)
    store.associate_unknown_client(id4, client_id="654", db_path=db)
    store.reserve_for_confirmation(id4, db_path=db)
    store.mark_queued(id4, "job-res-2", db_path=db)
    check("find_client_id_for_phone : le ticket 'queued' le plus récent l'emporte",
          store.find_client_id_for_phone(PHONE_RESOLVED, db_path=db) == "654")

    print(f"\n=== {passed} PASS / {failed} FAIL ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
