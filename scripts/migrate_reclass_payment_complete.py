"""Reclasse les faux 'underpayment' (écart ≤ tolérance) en 'payment_complete'.

Ces événements venaient du log worker "sous-paiement (...)" émis dès que
should_unblock=False, y compris quand le compte était en réalité à jour
(bug MAC UCRM absent, ou client déjà actif). Un écart ≤ tolérance = compte à
jour → ce n'est PAS un sous-paiement.

On ne SUPPRIME pas les lignes : on corrige leur `type` (audit préservé). Le
`dedup_key` inclut le type (sha1(ts|type|raw)) → on le RECALCULE pour rester
cohérent avec ce que le parser corrigé produira désormais, sinon la prochaine
ré-ingestion des logs ré-insérerait des doublons.

À lancer WEBHOOK ARRÊTÉ (pas d'ingestion concurrente) :
    python scripts/migrate_reclass_payment_complete.py            # DRY-RUN
    python scripts/migrate_reclass_payment_complete.py --apply    # exécute
"""
from __future__ import annotations

import hashlib
import io
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from whatsapp_automation import config  # noqa: E402

APPLY = "--apply" in sys.argv
THRESHOLD = config.UNDERPAYMENT_TOLERANCE
DB = config.EVENTS_DB_PATH
OLD, NEW = "underpayment", "payment_complete"

_ecart_re = re.compile(r"écart=(-?\d+)")
_bal_re = re.compile(r"balance=(\d+)")


def _dedup_key(ts: str, etype: str, raw: str) -> str:
    return hashlib.sha1(f"{ts}|{etype}|{raw}".encode("utf-8")).hexdigest()


def main() -> None:
    print(f"=== Migration reclass '{OLD}' → '{NEW}' (écart ≤ {THRESHOLD}) ===")
    print(f"DB: {DB}   mode: {'APPLY' if APPLY else 'DRY-RUN'}\n")

    if APPLY:
        bak = f"{DB}.bak-{datetime.now():%Y%m%d-%H%M%S}"
        shutil.copy2(DB, bak)
        print(f"Backup: {bak}\n")

    conn = sqlite3.connect(DB, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")

    rows = conn.execute(
        "SELECT id, ts, raw, balance FROM events WHERE type=?", (OLD,)
    ).fetchall()

    to_migrate = []
    genuine = 0
    for r in rows:
        m = _ecart_re.search(r["raw"] or "")
        if not m:
            continue
        ecart = int(m.group(1))
        if ecart > THRESHOLD:
            genuine += 1
            continue
        bm = _bal_re.search(r["raw"] or "")
        bal = int(bm.group(1)) if bm else r["balance"]
        to_migrate.append((r["id"], r["ts"], r["raw"], bal))

    print(f"underpayment en base : {len(rows)}")
    print(f"  → à reclasser (écart ≤ {THRESHOLD}) : {len(to_migrate)}")
    print(f"  → vrais sous-paiements conservés    : {genuine}\n")

    if APPLY:
        for _id, ts, raw, bal in to_migrate:
            conn.execute(
                "UPDATE events SET type=?, balance=?, dedup_key=? WHERE id=?",
                (NEW, bal, _dedup_key(ts, NEW, raw), _id),
            )
        conn.commit()
        # Vérif post-migration
        c_up = conn.execute("SELECT COUNT(*) FROM events WHERE type=?", (OLD,)).fetchone()[0]
        c_pc = conn.execute("SELECT COUNT(*) FROM events WHERE type=?", (NEW,)).fetchone()[0]
        dups = conn.execute(
            "SELECT COUNT(*) FROM (SELECT dedup_key FROM events GROUP BY dedup_key HAVING COUNT(*)>1)"
        ).fetchone()[0]
        print(f"[OK] migré {len(to_migrate)} lignes.")
        print(f"     underpayment restant={c_up}  payment_complete={c_pc}  dedup_dupliqués={dups}")
    else:
        print("(DRY-RUN — relancer avec --apply)")
    conn.close()


if __name__ == "__main__":
    main()
