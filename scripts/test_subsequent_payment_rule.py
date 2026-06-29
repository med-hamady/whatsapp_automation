"""Test unitaire de validate_payment_balance.

Vérifie la règle anti-sur-paiement unifiée (pas de notion de 1er paiement
ni de mois) :
- balance ≥ montant payé → OK (exact ou sous-paiement, comportement normal)
- balance < montant payé (sur-paiement) :
  - balance ≤ threshold (150) → REFUSÉ
  - balance > threshold → OK (sur-paiement toléré, génère un avoir)

Aucune dépendance DB / réseau. À lancer avant déploiement :
    python scripts/test_subsequent_payment_rule.py
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from whatsapp_automation.webhook.validators import (  # noqa: E402
    validate_payment_balance,
)


THRESHOLD = 150


# (label, amount_paid, crm_balance, expected_ok, expected_reason)
CASES: list[tuple[str, int, int, bool, str | None]] = [
    # --- balance ≥ payé : toujours OK ---
    ("sous-paiement : balance=1500 payé=1400",
     1400, 1500, True, None),
    ("exact : balance=1500 payé=1500",
     1500, 1500, True, None),
    ("sous-paiement : balance=100 payé=50",
     50, 100, True, None),
    ("exact : balance=100 payé=100",
     100, 100, True, None),

    # --- sur-paiement avec balance > 150 : OK ---
    ("sur-paiement, balance=1500 payé=2000 (balance > 150)",
     2000, 1500, True, None),
    ("sur-paiement, balance=200 payé=1000 (balance > 150)",
     1000, 200, True, None),
    ("sur-paiement limite, balance=151 payé=500 (balance > 150)",
     500, 151, True, None),

    # --- sur-paiement avec balance ≤ 150 : REFUS ---
    ("sur-paiement, balance=150 payé=500 (balance == 150) — REFUS",
     500, 150, False, "overpayment_balance_too_low"),
    ("sur-paiement, balance=100 payé=700 (CAS DU TICKET) — REFUS",
     700, 100, False, "overpayment_balance_too_low"),
    ("sur-paiement, balance=0 payé=500 — REFUS",
     500, 0, False, "overpayment_balance_too_low"),
    ("sur-paiement, balance=0 payé=100 — REFUS",
     100, 0, False, "overpayment_balance_too_low"),
    ("sur-paiement, balance=50 payé=1000 — REFUS",
     1000, 50, False, "overpayment_balance_too_low"),
]


def main() -> int:
    passed = 0
    failed = 0
    print(f"=== Test validate_payment_balance (threshold={THRESHOLD}) ===\n")

    for label, paid, balance, expected_ok, expected_reason in CASES:
        result = validate_payment_balance(
            amount_paid=paid,
            crm_balance=balance,
            threshold=THRESHOLD,
        )
        ok = result.ok == expected_ok and result.reason == expected_reason
        status = "PASS" if ok else "FAIL"
        marker = " " if ok else ">"
        print(f"{marker}[{status}] {label}")
        if not ok:
            print(f"        attendu: ok={expected_ok} reason={expected_reason!r}")
            print(f"        obtenu : ok={result.ok} reason={result.reason!r}")
            failed += 1
        else:
            passed += 1

    print(f"\n=== {passed} PASS / {failed} FAIL sur {len(CASES)} cas ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
