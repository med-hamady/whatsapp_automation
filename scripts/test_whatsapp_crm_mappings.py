"""Tests unitaires du store `whatsapp_crm_mappings` (mémoire persistante
numéro WhatsApp → idclient CRM).

Vérifie :
  - init_db idempotent ;
  - get_active_mapping : numéro inconnu -> None, numéro vide -> None ;
  - upsert_mapping crée une correspondance active (created_by conservé) ;
  - upsert même numéro + même idclient -> idempotent (1 seule ligne,
    updated_at rafraîchi, created_at inchangé) ;
  - upsert même numéro + idclient DIFFÉRENT -> ancienne ligne désactivée
    (historique conservé), nouvelle ligne active, une seule active à la fois ;
  - l'index UNIQUE partiel (is_active=1) rejette un doublon actif inséré à la
    main (garantie niveau schéma, en plus de la logique applicative) ;
  - upsert avec numéro ou idclient vide -> None (rien écrit) ;
  - best-effort : chemin SQLite invalide -> None, jamais d'exception (le
    pipeline webhook ne doit jamais planter à cause de cette table).

100% local, aucune dépendance réseau. À lancer :
python scripts/test_whatsapp_crm_mappings.py
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

_TMP = Path(tempfile.mkdtemp(prefix="crm_mappings_test_"))
DB = str(_TMP / "whatsapp_crm_mappings.db")
os.environ["WHATSAPP_CRM_MAPPINGS_DB_PATH"] = DB

from whatsapp_automation.webhook import crm_mappings  # noqa: E402

passed = failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    marker = " " if cond else ">"
    print(f"{marker}[{'PASS' if cond else 'FAIL'}] {label}" + (f"  -- {detail}" if detail else ""))
    passed += bool(cond)
    failed += not cond


def _rows(phone: str) -> list[dict]:
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM whatsapp_crm_mappings WHERE whatsapp_phone = ? ORDER BY id",
            (phone,),
        ).fetchall()
    return [dict(r) for r in rows]


def main() -> int:
    crm_mappings.init_db(DB)
    crm_mappings.init_db(DB)  # idempotent
    check("init_db idempotent (2 appels sans erreur)", True)

    # get_active sur base vide / entrées invalides.
    check("get_active_mapping numéro inconnu -> None",
          crm_mappings.get_active_mapping("37600000", db_path=DB) is None)
    check("get_active_mapping numéro vide -> None",
          crm_mappings.get_active_mapping("", db_path=DB) is None)

    # Création.
    m1 = crm_mappings.upsert_mapping(
        whatsapp_phone="37611111", crm_client_id="555",
        created_by="dashboard", db_path=DB,
    )
    check("upsert crée une correspondance active",
          m1 is not None and m1["crm_client_id"] == "555" and m1["is_active"] is True,
          str(m1))
    check("created_by conservé", m1 is not None and m1["created_by"] == "dashboard")
    active = crm_mappings.get_active_mapping("37611111", db_path=DB)
    check("get_active_mapping retrouve la correspondance",
          active is not None and active["crm_client_id"] == "555")

    # Idempotence : même numéro, même idclient.
    m2 = crm_mappings.upsert_mapping(
        whatsapp_phone="37611111", crm_client_id="555", db_path=DB,
    )
    check("upsert idempotent : même ligne (id identique)",
          m2 is not None and m1 is not None and m2["id"] == m1["id"])
    check("upsert idempotent : updated_at rafraîchi, created_at inchangé",
          m2 is not None and m1 is not None
          and m2["updated_at"] >= m1["updated_at"]
          and m2["created_at"] == m1["created_at"])
    check("upsert idempotent : toujours 1 seule ligne en base",
          len(_rows("37611111")) == 1)

    # Ré-association : même numéro, idclient différent.
    m3 = crm_mappings.upsert_mapping(
        whatsapp_phone="37611111", crm_client_id="777",
        created_by="dashboard", db_path=DB,
    )
    check("ré-association : nouvelle ligne active pour '777'",
          m3 is not None and m3["crm_client_id"] == "777" and m3["is_active"] is True)
    rows = _rows("37611111")
    check("historique conservé : 2 lignes (ancienne inactive, nouvelle active)",
          len(rows) == 2 and rows[0]["is_active"] == 0 and rows[1]["is_active"] == 1,
          str(rows))
    active = crm_mappings.get_active_mapping("37611111", db_path=DB)
    check("get_active_mapping renvoie la NOUVELLE correspondance ('777')",
          active is not None and active["crm_client_id"] == "777")

    # Garantie schéma : impossible d'insérer un 2e actif à la main.
    try:
        with sqlite3.connect(DB) as conn:
            conn.execute(
                """INSERT INTO whatsapp_crm_mappings
                   (whatsapp_phone, crm_client_id, created_at, updated_at, is_active)
                   VALUES ('37611111', '999', 0, 0, 1)""",
            )
            conn.commit()
        duplicate_rejected = False
    except sqlite3.IntegrityError:
        duplicate_rejected = True
    check("index UNIQUE partiel : doublon actif rejeté au niveau schéma",
          duplicate_rejected)

    # Entrées invalides.
    check("upsert numéro vide -> None",
          crm_mappings.upsert_mapping(whatsapp_phone="", crm_client_id="555", db_path=DB) is None)
    check("upsert crm_client_id vide -> None",
          crm_mappings.upsert_mapping(whatsapp_phone="37622222", crm_client_id="", db_path=DB) is None)
    check("aucune ligne écrite pour le numéro à idclient vide",
          len(_rows("37622222")) == 0)

    # Best-effort : chemin invalide (un répertoire) -> None, pas d'exception.
    bad_path = str(_TMP)  # répertoire, pas un fichier SQLite
    check("get_active_mapping chemin invalide -> None (best-effort, pas d'exception)",
          crm_mappings.get_active_mapping("37611111", db_path=bad_path) is None)
    check("upsert_mapping chemin invalide -> None (best-effort, pas d'exception)",
          crm_mappings.upsert_mapping(
              whatsapp_phone="37633333", crm_client_id="555", db_path=bad_path) is None)

    print(f"\n=== {passed} PASS / {failed} FAIL ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
