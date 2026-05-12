"""Test fake sur les 9 clients seedés (mix unblock / keep suspended).

  | idclient | phone     | balance | payé | écart | décision         |
  |     1    | 48783201  |   900   |  900 |   0   | UNBLOCK (exact)  |
  |     2    | 48249066  |  1000   | 1000 |   0   | UNBLOCK (exact)  |
  |     3    | 46603985  |  1190   | 1100 |  -90  | UNBLOCK (≤150)   |
  |     4    | 31752614  |  1500   | 1350 | -150  | UNBLOCK (=seuil) |
  |     5    | 37888210  |   990   |  800 | -190  | KEEP suspended   |
  |     6    | 44160960  |   850   |  500 | -350  | KEEP suspended   |
  |     7    | 777565497 |  1200   | 1500 | +300  | UNBLOCK (sur)    |
  |     8    | 33848414  |  1100   | 1100 |   0   | UNBLOCK (exact)  |
  |     9    | 41769945  |   950   |  700 | -250  | KEEP suspended   |

Le script :
 1. POST 9 rules firewall dans le fake MikroTik (idempotent).
 2. Construit et empile 9 Jobs avec balance/paid définis ci-dessus.
 3. Attend que le worker traite tout.
 4. Vérifie statu DB, rule MT, paiement UCRM, paiement Postgres pour chacun.

Prérequis : init_db --reset --seed déjà passé, fakes (9001/9002/9003) et
worker en cours.
"""
from __future__ import annotations

import sys as _sys
import time
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parent.parent
_sys.path.insert(0, str(_ROOT / "src"))
_sys.path.insert(0, str(_ROOT))

import httpx
import psycopg

from whatsapp_automation import config
from whatsapp_automation.jobqueue import store as queue_store
from whatsapp_automation.models import Client, Job, Payment, Source


SCENARIOS = [
    {"idclient": 1, "phone": "48783201",  "balance": 900,  "paid": 900,  "label": "exact"},
    {"idclient": 2, "phone": "48249066",  "balance": 1000, "paid": 1000, "label": "exact"},
    {"idclient": 3, "phone": "46603985",  "balance": 1190, "paid": 1100, "label": "sous-paiement -90 (toléré)"},
    {"idclient": 4, "phone": "31752614",  "balance": 1500, "paid": 1350, "label": "sous-paiement -150 (= seuil)"},
    {"idclient": 5, "phone": "37888210",  "balance": 990,  "paid": 800,  "label": "sous-paiement -190 (excessif)"},
    {"idclient": 6, "phone": "44160960",  "balance": 850,  "paid": 500,  "label": "sous-paiement -350 (excessif)"},
    {"idclient": 7, "phone": "777565497", "balance": 1200, "paid": 1500, "label": "sur-paiement +300"},
    {"idclient": 8, "phone": "33848414",  "balance": 1100, "paid": 1100, "label": "exact"},
    {"idclient": 9, "phone": "41769945",  "balance": 950,  "paid": 700,  "label": "sous-paiement -250 (excessif)"},
]


def _meta(idclient: int) -> dict:
    return {
        "rule_id": f"*0{idclient}",
        "mac": f"AA:BB:CC:00:00:0{idclient}",
        "ip": f"10.0.0.{idclient}",
    }


def create_mikrotik_rule(s: dict) -> None:
    m = _meta(s["idclient"])
    payload = {
        "id": m["rule_id"],
        "mac_address": m["mac"],
        "src_address": m["ip"],
        "comment": f"Suspended {s['phone']}",
    }
    r = httpx.post(f"{config.MIKROTIK_BASE_URL}/firewall/rules", json=payload, timeout=5)
    state = "créée" if r.status_code in (200, 201) else f"HTTP {r.status_code}"
    print(f"  rule {m['rule_id']} → {m['ip']} : {state}")


def build_job(s: dict) -> Job:
    m = _meta(s["idclient"])
    diff = s["balance"] - s["paid"]
    unblock = diff <= config.UNDERPAYMENT_TOLERANCE
    return Job(
        job_id=queue_store.new_job_id(),
        client=Client(
            id=s["idclient"],
            phone=s["phone"],
            mac_address=m["mac"],
            ip_address=m["ip"],
            current_status="suspended",
        ),
        payment=Payment(
            amount_mru=s["paid"],
            txn_id=f"TEST9-{s['phone']}-{int(time.time())}",
            operator="bankily",
            crm_balance_before=s["balance"],
            should_unblock=unblock,
        ),
        source=Source(
            wnum=s["phone"],
            sample_id=f"2026-05-12/{s['phone']}",
            received_at="2026-05-12T11:00:00Z",
        ),
    )


def main() -> None:
    queue_store.init_db()

    print("=" * 78)
    print("TEST 9 CLIENTS — règle métier sous/sur-paiement (seuil tolérance "
          f"{config.UNDERPAYMENT_TOLERANCE} MRU)")
    print("=" * 78)

    print("\n[1] Création des 9 rules MikroTik")
    for s in SCENARIOS:
        create_mikrotik_rule(s)

    print("\n[2] Enqueue des 9 jobs")
    jobs = []
    for s in SCENARIOS:
        job = build_job(s)
        queue_store.enqueue(job)
        jobs.append((s, job))
        diff = s["balance"] - s["paid"]
        sign = "+" if diff < 0 else ""  # sign for the *paid - balance* direction
        decision = "UNBLOCK" if job.payment.should_unblock else "KEEP   "
        print(f"  {s['idclient']} {s['phone']:10s} bal={s['balance']:>5} paid={s['paid']:>5} "
              f"écart={-diff:>+5} → {decision}  [{s['label']}]")

    print("\n[3] Attente du worker (timeout 60 s)")
    txn_ids = [j.payment.txn_id for _, j in jobs]
    for i in range(120):
        time.sleep(0.5)
        done = sum(queue_store.is_txn_processed(t) for t in txn_ids)
        if done == len(txn_ids):
            print(f"  ✅ {done}/{len(txn_ids)} jobs traités après {(i + 1) * 0.5:.1f}s")
            break
        if i % 10 == 9:
            print(f"  ... {done}/{len(txn_ids)} (à {(i + 1) * 0.5:.1f}s)")
    else:
        print(f"  ✗ TIMEOUT — {done}/{len(txn_ids)} traités, worker bloqué ?")
        return

    print("\n[4] Vérification état final")
    auth = {"X-Auth-App-Key": config.UCRM_APP_KEY}
    ucrm_payments = httpx.get(f"{config.UCRM_BASE_URL}/payments", headers=auth).json()
    mt_rules = httpx.get(f"{config.MIKROTIK_BASE_URL}/firewall/rules").json()
    rule_ids_present = {r["id"] for r in mt_rules}

    print(f"\n  {'phone':10s} | {'attendu':9s} | {'statu':6s} | "
          f"{'rule MT':12s} | {'UCRM':6s} | {'Postgres':9s} | verdict")
    print(f"  {'-'*10} | {'-'*9} | {'-'*6} | {'-'*12} | {'-'*6} | {'-'*9} | -------")

    all_ok = True
    with psycopg.connect(config.DATABASE_URL) as conn:
        for s, job in jobs:
            m = _meta(s["idclient"])
            cur = conn.execute("SELECT statu FROM client WHERE idclient=%s", (s["idclient"],))
            statu = cur.fetchone()[0]
            cur = conn.execute(
                "SELECT 1 FROM paiment WHERE idclient=%s AND txn_id=%s",
                (s["idclient"], job.payment.txn_id),
            )
            pg_ok = cur.fetchone() is not None

            ucrm_for_client = [
                p for p in ucrm_payments
                if p["clientId"] == s["idclient"]
                and p.get("note", "").endswith(job.payment.txn_id)
            ]
            ucrm_ok = bool(ucrm_for_client)

            expected_statu = 0 if job.payment.should_unblock else 2
            expected_rule_removed = job.payment.should_unblock
            rule_removed = m["rule_id"] not in rule_ids_present

            statu_ok = statu == expected_statu
            rule_ok = rule_removed == expected_rule_removed

            verdict = "OK" if (statu_ok and rule_ok and ucrm_ok and pg_ok) else "KO"
            if verdict == "KO":
                all_ok = False

            expected = "UNBLOCK" if job.payment.should_unblock else "KEEP"
            statu_str = f"{statu}{'✅' if statu_ok else '❌'}"
            rule_str = ("removed" if rule_removed else "present") + ("✅" if rule_ok else "❌")
            print(f"  {s['phone']:10s} | {expected:9s} | {statu_str:6s} | "
                  f"{rule_str:12s} | {'✅' if ucrm_ok else '❌':6s} | "
                  f"{'✅' if pg_ok else '❌':9s} | {verdict}")

    print("\n" + ("=" * 78))
    print(f"RESULTAT GLOBAL : {'TOUS OK ✅' if all_ok else 'CERTAINS KO ❌'}")
    print("=" * 78)


if __name__ == "__main__":
    main()
