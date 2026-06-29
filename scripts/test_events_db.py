"""Test de la table events (webhook/dashboard/events_db.py).

Parse des lignes de log d'exemple, les ingère dans une events.db temporaire,
puis vérifie : idempotence (ré-ingestion = 0 ajout), et que les requêtes
summary / refusals_by_cause / recent_events lisent les bons chiffres avec les
mêmes exclusions que le parser.

À lancer : python scripts/test_events_db.py
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

from whatsapp_automation.webhook.dashboard import log_parser as lp  # noqa: E402
from whatsapp_automation.webhook.dashboard import events_db  # noqa: E402

TS = "2026-06-20 10:00:00,001"
P = "[whatsapp_automation.webhook.pipeline]"
H = "[whatsapp_automation.worker.handlers]"

LINES = [
    f"{TS} {P} INFO type=video non supporté, drop (from=37697850)",          # refused exclu
    f"{TS} {P} INFO extraction invalide: no_or_invalid_amount",               # refused exclu
    f"{TS} {P} INFO document non-paiement écarté: subscription_form (from=x)",# refused exclu
    f"{TS} {P} INFO UCRM injoignable (client=1234) — skip",                   # refused crm_unreachable
    f"{TS} {P} INFO paiement refusé : overpayment_balance_too_low (client=1234 balance=100 payé=700 txn=ABC)",
    f"{TS} {P} INFO idempotence: txn_id ABC déjà traité avec succès",         # duplicate_processed
    f"{TS} {P} INFO validation client KO: client_not_found (phone=37697850 group=-)",  # type dédié
    f"{TS} {P} INFO job enqueued id=42 job_id=abc client=1234 amount=1500 txn=TXN9",
    f"{TS} {H} INFO UCRM payment created: client=1234 amount=1500 paymentId=98765 operator=bankily txn=TXN9",
    f"{TS} {H} INFO MikroTik unblock: client=1234 mac=AA:BB rules_removed=2",
    f"{TS} {H} INFO PDF envoyé via UltraMsg → +22237697850 (unblocked=True) url=http://x/paymentrecue.php?id=98765",
]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="events_test_"))
    logdir = tmp / "logs"; logdir.mkdir()
    (logdir / "webhook.log").write_text("\n".join(LINES) + "\n", encoding="utf-8")
    db = str(tmp / "events.db")
    events_db.init_db(db)

    events = lp.get_events(log_dir=str(logdir))
    n1 = events_db.ingest(events, db_path=db)
    n2 = events_db.ingest(events, db_path=db)  # ré-ingestion : doit ajouter 0

    passed = failed = 0

    def check(label, cond):
        nonlocal passed, failed
        print(f"{' ' if cond else '>'}[{'PASS' if cond else 'FAIL'}] {label}")
        passed += bool(cond); failed += not cond

    check(f"1re ingestion = {n1} (= {len(events)})", n1 == len(events))
    check(f"ré-ingestion = {n2} (idempotent)", n2 == 0)

    s = events_db.summary(days=None, db_path=db)
    # refus comptés : crm_unreachable + overpayment + duplicate_processed = 3
    check(f"refused_total = {s['refused_total']} (attendu 3)", s["refused_total"] == 3)
    check(f"clients_not_found = {s['clients_not_found']} (attendu 1)", s["clients_not_found"] == 1)
    check(f"ucrm_created = {s['ucrm_created']} (attendu 1)", s["ucrm_created"] == 1)
    check(f"messages_sent = {s['messages_sent']} (attendu 1)", s["messages_sent"] == 1)
    check(f"clients_unblocked = {s['clients_unblocked']} (attendu 1)", s["clients_unblocked"] == 1)

    causes = events_db.refusals_by_cause(days=None, db_path=db)
    check(f"causes refus = {sorted(causes)}",
          set(causes) == {"crm_unreachable", "overpayment_balance_too_low", "duplicate_processed"})

    evs = events_db.recent_events(days=None, db_path=db)
    check("recent_events sans causes exclues",
          not any(e["type"] == "refused" and e["reason"] in lp.EXCLUDED_REFUSAL_REASONS for e in evs))
    msg = next((e for e in evs if e["type"] == "message_sent"), None)
    check("message_sent payment_id=98765 en base", msg and msg["payment_id"] == "98765")

    evs_cnf = events_db.recent_events(type_filter="client_not_found", days=None, db_path=db)
    check("filtre type=client_not_found", len(evs_cnf) == 1 and evs_cnf[0]["phone"] == "37697850")

    print(f"\n=== {passed} PASS / {failed} FAIL ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
