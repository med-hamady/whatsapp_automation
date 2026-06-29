"""Test unitaire de plan_unblocks (répartition d'un paiement multi-abonnements).

Un client peut payer plusieurs abonnements (services UCRM) en un seul
versement. plan_unblocks répartit `disponible` (montant payé + crédit) sur les
abonnements suspendus triés par prix croissant, débloque chaque abo couvert
(+ 1 abo marginal dans la tolérance), et renvoie le reliquat.

Aucune dépendance DB / réseau. À lancer avant déploiement :
    python scripts/test_unblock_allocation.py
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from whatsapp_automation.webhook.validators import plan_unblocks  # noqa: E402


THRESHOLD = 150


def svc(mac: str, price) -> dict:
    return {"mac": mac, "price": price}


# (label, suspended_services, available, expected_macs, expected_remainder)
CASES: list[tuple[str, list[dict], int, list[str], int]] = [
    # --- 3 abos, paiement complet ---
    ("3 abos 1500+1500+2000, payé 5000 → tous débloqués",
     [svc("AA", 1500), svc("BB", 1500), svc("CC", 2000)], 5000,
     ["AA", "BB", "CC"], 0),

    # --- 3 abos, paiement partiel (couvre 2 sur 3, les moins chers d'abord) ---
    ("3 abos, payé 3000 → 2 moins chers débloqués",
     [svc("CC", 2000), svc("AA", 1500), svc("BB", 1500)], 3000,
     ["AA", "BB"], 0),

    # --- 1 abo, tolérance (sous-paiement de 10 ≤ 150) ---
    ("1 abo 1500, payé 1490 → débloqué (tolérance)",
     [svc("AA", 1500)], 1490, ["AA"], 0),

    # --- 1 abo, sous-paiement hors tolérance (151 > 150) ---
    ("1 abo 1500, payé 1349 → NON débloqué",
     [svc("AA", 1500)], 1349, [], 1349),

    # --- tolérance utilisée une seule fois ---
    ("2 abos 1500+1500, payé 1490 → 1 seul (tolérance ponctuelle)",
     [svc("AA", 1500), svc("BB", 1500)], 1490, ["AA"], 0),
    ("2 abos 1500+1500, payé 2990 → les 2 (tolérance sur le 2e)",
     [svc("AA", 1500), svc("BB", 1500)], 2990, ["AA", "BB"], 0),

    # --- rien payé ---
    ("1 abo 1500, payé 0 → rien",
     [svc("AA", 1500)], 0, [], 0),

    # --- MAC placeholder / prix invalide ignorés ---
    ("pending- ignoré",
     [svc("pending-42", 1500), svc("BB", 1500)], 1500, ["BB"], 0),
    ("prix None ignoré",
     [svc("AA", None), svc("BB", 1000)], 1000, ["BB"], 0),
    ("MAC vide ignoré",
     [svc("", 1500), svc("BB", 1000)], 1000, ["BB"], 0),

    # --- surplus → reliquat (crédit) ---
    ("1 abo 1000, payé 1500 → débloqué, reliquat 500",
     [svc("AA", 1000)], 1500, ["AA"], 500),

    # --- casse du MAC préservée ---
    ("casse préservée",
     [svc("6c:63:f8:b8:cd:0c", 1000)], 1000, ["6c:63:f8:b8:cd:0c"], 0),

    # --- aucun abo suspendu ---
    ("aucun abo → rien",
     [], 5000, [], 5000),
]


def main() -> int:
    passed = 0
    failed = 0
    print(f"=== Test plan_unblocks (threshold={THRESHOLD}) ===\n")

    for label, suspended, available, exp_macs, exp_remainder in CASES:
        plan = plan_unblocks(suspended, available, THRESHOLD)
        ok = plan.macs == exp_macs and plan.remainder == exp_remainder
        status = "PASS" if ok else "FAIL"
        marker = " " if ok else ">"
        print(f"{marker}[{status}] {label}")
        if not ok:
            print(f"        attendu: macs={exp_macs} remainder={exp_remainder}")
            print(f"        obtenu : macs={plan.macs} remainder={plan.remainder} "
                  f"(covered={plan.covered_count} total_due={plan.total_due})")
            failed += 1
        else:
            passed += 1

    print(f"\n=== {passed} PASS / {failed} FAIL sur {len(CASES)} cas ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
