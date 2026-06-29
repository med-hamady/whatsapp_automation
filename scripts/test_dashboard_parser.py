"""Test du parser de logs du dashboard (webhook/dashboard/log_parser.py).

Écrit des fichiers de log d'exemple (webhook.log + worker-123.log) reproduisant
EXACTEMENT les messages émis par pipeline.py / handlers.py / app.py, puis vérifie
que le parser reconnaît chaque motif avec le bon type / cause / champs extraits.

Aucune dépendance DB / réseau. À lancer avant déploiement :
    python scripts/test_dashboard_parser.py
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

TS = "2026-06-20 10:00:00,001"
P = "[whatsapp_automation.webhook.pipeline]"
H = "[whatsapp_automation.worker.handlers]"
W = "[whatsapp_automation.webhook]"

# Lignes calquées sur les logger.info/.warning réels.
WEBHOOK_LINES = [
    f"{TS} {P} INFO type=video non supporté, drop (from=37697850)",
    f"{TS} {P} INFO no media, drop (from=37697850)",
    f"{TS} {P} INFO document non-paiement écarté: subscription_form (from=37697850)",
    f"{TS} {P} INFO extraction invalide: no_or_invalid_amount",
    f"{TS} {P} INFO validation client KO: client_not_found (phone=37697850 group=-)",
    f"{TS} {P} INFO UCRM injoignable (client=1234) — skip",
    f"{TS} {P} INFO paiement refusé : overpayment_balance_too_low (client=1234 balance=100 payé=700 txn=ABC123)",
    f"{TS} {P} INFO idempotence: txn_id ABC déjà traité avec succès",
    f"{TS} {P} INFO idempotence: txn_id ABC déjà en queue",
    f"{TS} {P} INFO idempotence atomique: txn_id ABC déjà traité ou en queue (course gagnée ailleurs)",
    f"{TS} {P} WARNING destinataire suspect (PASS-THROUGH) : recipient_name_mismatch (template=bankily, from=37697850)",
    f"{TS} {P} INFO job enqueued id=42 job_id=abc123 client=1234 amount=1500 txn=TXN9",
    f"{TS} {P} INFO support notifié reason=client_not_found from=37697850 group=- media=yes",
    # Blocage/déblocage MANUEL : volontairement NON parsés (hors périmètre).
    f"{TS} {W} INFO block_client OK phone=37697850 mac=AA:BB:CC:DD:EE:FF action=block rules_changed=1 statu=2",
    f"{TS} {W} INFO block_client OK phone=37697850 mac=AA:BB:CC:DD:EE:FF action=unblock rules_changed=1 statu=0",
    # Lignes à ignorer (continuation / non métier) :
    "Traceback (most recent call last):",
    f"{TS} {P} INFO pipeline result: {{'status': 'enqueued'}}",
]

WORKER_LINES = [
    f"{TS} {H} INFO UCRM payment created: client=1234 amount=1500 paymentId=98765 operator=bankily txn=TXN9",
    f"{TS} {H} INFO DB insert: client=1234 amount=1500 id_payment=98765",
    f"{TS} {H} INFO MikroTik unblock: client=1234 mac=AA:BB:CC:DD:EE:FF rules_removed=2",
    f"{TS} {H} INFO Statut abo mac=AA:BB:CC:DD:EE:FF → actif (lignes=1, client=1234)",
    f"{TS} {H} INFO sous-paiement (balance=2000 payé=500 écart=1500) — paiement enregistré mais client NON débloqué (client=1234)",
    f"{TS} {H} INFO PDF envoyé via UltraMsg → +22237697850 (unblocked=True) url=http://13.49.185.225/uisp/paymentrecue.php?id=98765",
]

# type attendu → nombre d'occurrences attendu
EXPECTED_COUNTS = {
    lp.REFUSED: 9,            # client_not_found a désormais son propre type
    lp.CLIENT_NOT_FOUND: 1,
    lp.RECIPIENT_SUSPECT: 1,
    lp.PAYMENT_ENQUEUED: 1,
    lp.SUPPORT_NOTIFIED: 1,
    lp.UCRM_CREATED: 1,
    lp.CLIENT_UNBLOCKED: 1,
    lp.SUBSCRIPTION_ACTIVATED: 1,
    lp.UNDERPAYMENT: 1,
    lp.MESSAGE_SENT: 1,
}

# unsupported_type / subscription_form / client_not_suspended / no_or_invalid_amount
# sont PARSÉS (type=refused) mais EXCLUS des agrégations de refus.
# client_not_found a son propre type → absent des causes de refus.
EXPECTED_CAUSES = {
    "no_media",
    "crm_unreachable", "overpayment_balance_too_low",
    "duplicate_processed", "duplicate_in_flight", "duplicate_race",
}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dash_logs_"))
    (tmp / "webhook.log").write_text("\n".join(WEBHOOK_LINES) + "\n", encoding="utf-8")
    (tmp / "worker-123.log").write_text("\n".join(WORKER_LINES) + "\n", encoding="utf-8")
    # Doit être ignoré (redirection NSSM) — sinon double comptage :
    (tmp / "webhook-stdout.log").write_text("\n".join(WEBHOOK_LINES) + "\n", encoding="utf-8")

    events = lp.get_events(log_dir=str(tmp))
    by_type: dict[str, int] = {}
    for e in events:
        by_type[e.type] = by_type.get(e.type, 0) + 1

    passed = failed = 0
    print("=== Test log_parser du dashboard ===\n")

    for etype, expected in EXPECTED_COUNTS.items():
        got = by_type.get(etype, 0)
        ok = got == expected
        print(f"{' ' if ok else '>'}[{'PASS' if ok else 'FAIL'}] {etype}: {got} (attendu {expected})")
        passed += ok
        failed += not ok

    causes = lp.refusals_by_cause(days=None, log_dir=str(tmp))
    ok = set(causes) == EXPECTED_CAUSES and all(v == 1 for v in causes.values())
    print(f"{' ' if ok else '>'}[{'PASS' if ok else 'FAIL'}] refusals_by_cause = {len(causes)} causes (exclusions appliquées)")
    if not ok:
        print(f"        attendu: {sorted(EXPECTED_CAUSES)}")
        print(f"        obtenu : {sorted(causes)}")
    passed += ok
    failed += not ok

    # 9 refus bruts ; exclus : unsupported_type, subscription_form,
    # no_or_invalid_amount → 6 comptés. client_not_found est un type à part.
    s = lp.summary(days=None, log_dir=str(tmp))
    ok = s["refused_total"] == 6
    print(f"{' ' if ok else '>'}[{'PASS' if ok else 'FAIL'}] summary.refused_total = {s['refused_total']} (attendu 6)")
    passed += ok
    failed += not ok

    ok = s["clients_not_found"] == 1
    print(f"{' ' if ok else '>'}[{'PASS' if ok else 'FAIL'}] summary.clients_not_found = {s['clients_not_found']} (attendu 1)")
    passed += ok
    failed += not ok

    # client_not_found absent des causes de refus.
    ok = "client_not_found" not in causes
    print(f"{' ' if ok else '>'}[{'PASS' if ok else 'FAIL'}] client_not_found hors des causes de refus")
    passed += ok
    failed += not ok

    # recent_events ne renvoie aucun unsupported_type.
    evs_all = lp.recent_events(limit=1000, days=None, log_dir=str(tmp))
    ok = not any(e["reason"] == "unsupported_type" for e in evs_all)
    print(f"{' ' if ok else '>'}[{'PASS' if ok else 'FAIL'}] recent_events sans unsupported_type")
    passed += ok
    failed += not ok

    # Vérifie quelques champs extraits.
    over = next((e for e in events if e.reason == "overpayment_balance_too_low"), None)
    checks = [
        ("overpayment client_id=1234", over and over.client_id == 1234),
        ("overpayment amount=700", over and over.amount == 700),
        ("overpayment txn=ABC123", over and over.txn_id == "ABC123"),
    ]
    ucrm = next((e for e in events if e.type == lp.UCRM_CREATED), None)
    checks += [
        ("ucrm payment_id=98765", ucrm and ucrm.payment_id == "98765"),
        ("ucrm operator=bankily", ucrm and ucrm.operator == "bankily"),
    ]
    msg = next((e for e in events if e.type == lp.MESSAGE_SENT), None)
    checks.append(("message_sent phone=37697850", msg and msg.phone == "37697850"))
    checks.append(("message_sent payment_id=98765", msg and msg.payment_id == "98765"))
    unb = next((e for e in events if e.type == lp.CLIENT_UNBLOCKED), None)
    checks.append(("unblock mac=AA:BB:CC:DD:EE:FF", unb and unb.mac == "AA:BB:CC:DD:EE:FF"))

    for label, ok in checks:
        print(f"{' ' if ok else '>'}[{'PASS' if ok else 'FAIL'}] {label}")
        passed += bool(ok)
        failed += not ok

    # Blocages/déblocages manuels NON parsés (hors périmètre de l'interface).
    ok = by_type.get("manual_block", 0) == 0 and by_type.get("manual_unblock", 0) == 0
    print(f"{' ' if ok else '>'}[{'PASS' if ok else 'FAIL'}] blocages manuels ignorés (non parsés)")
    passed += ok
    failed += not ok

    # webhook-stdout.log ignoré → pas de double comptage (payment_enqueued reste à 1).
    ok = by_type.get(lp.PAYMENT_ENQUEUED, 0) == 1
    print(f"{' ' if ok else '>'}[{'PASS' if ok else 'FAIL'}] *-stdout.log ignoré (pas de doublon)")
    passed += ok
    failed += not ok

    print(f"\n=== {passed} PASS / {failed} FAIL ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
