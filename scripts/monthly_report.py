"""Rapport mensuel : taux de réussite des paiements + taux de détection
des captures dont le destinataire ne matche pas.

Trois sources lues localement (zéro appel externe) :
  - data/queue.db (table `jobs`)            → statut des paiements
  - data/dataset/store/YYYY-MM-DD/*/ ...    → captures OCRisées
  - validators.validate_recipient_name      → re-test du nom destinataire

Usage :
    python scripts/monthly_report.py                       # mois en cours
    python scripts/monthly_report.py --month 2026-06       # un mois donné
    python scripts/monthly_report.py --from 2026-06-01 --to 2026-06-15
"""

from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Console Windows : force UTF-8 pour les caractères arabes/asiatiques OCR.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from whatsapp_automation.webhook.validators import validate_recipient_name  # noqa: E402


QUEUE_DB = ROOT / "data" / "queue.db"
STORE = ROOT / "data" / "dataset" / "store"


# --------------------------------------------------------------------------- #
# Période
# --------------------------------------------------------------------------- #

def _parse_args() -> tuple[date, date]:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--month", help="YYYY-MM (défaut : mois en cours)")
    p.add_argument("--from", dest="d_from", help="YYYY-MM-DD (inclusif)")
    p.add_argument("--to",   dest="d_to",   help="YYYY-MM-DD (inclusif)")
    args = p.parse_args()

    if args.d_from or args.d_to:
        if not (args.d_from and args.d_to):
            p.error("--from et --to vont de pair")
        return (date.fromisoformat(args.d_from), date.fromisoformat(args.d_to))

    if args.month:
        y, m = map(int, args.month.split("-"))
        first = date(y, m, 1)
    else:
        today = date.today()
        first = date(today.year, today.month, 1)
    # Dernier jour du mois = premier du mois suivant - 1
    if first.month == 12:
        last = date(first.year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(first.year, first.month + 1, 1) - timedelta(days=1)
    return (first, last)


def _date_to_unix(d: date, *, end_of_day: bool = False) -> float:
    """date → epoch UTC. end_of_day=True borne en fin de journée (inclusive)."""
    t = datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc)
    if end_of_day:
        t = t + timedelta(days=1) - timedelta(microseconds=1)
    return t.timestamp()


# --------------------------------------------------------------------------- #
# Paiements (queue.db)
# --------------------------------------------------------------------------- #

def _load_jobs(d_from: date, d_to: date) -> list[dict]:
    if not QUEUE_DB.exists():
        return []
    ts_from = _date_to_unix(d_from)
    ts_to = _date_to_unix(d_to, end_of_day=True)
    with sqlite3.connect(QUEUE_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT id, job_id, status, created_at, finished_at, payload_json,
                      last_error, attempts
               FROM jobs
               WHERE created_at BETWEEN ? AND ?""",
            (ts_from, ts_to),
        ).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"])
        except json.JSONDecodeError:
            payload = {}
        out.append({
            "id": r["id"],
            "job_id": r["job_id"],
            "status": r["status"],
            "created_at": r["created_at"],
            "finished_at": r["finished_at"],
            "sample_id": (payload.get("source") or {}).get("sample_id", ""),
            "operator": (payload.get("payment") or {}).get("operator", ""),
            "amount_mru": (payload.get("payment") or {}).get("amount_mru", 0),
            "last_error": r["last_error"] or "",
        })
    return out


def _payments_report(jobs: list[dict]) -> str:
    if not jobs:
        return "(aucun job sur la période)"
    by_status: Counter = Counter(j["status"] for j in jobs)
    total = len(jobs)
    done = by_status.get("done", 0)
    failed = by_status.get("failed", 0)
    in_flight = total - done - failed
    lines = [
        f"  Total jobs       : {total}",
        f"  ✅ done           : {done:>4}  ({done/total*100:5.1f} %)",
        f"  ❌ failed         : {failed:>4}  ({failed/total*100:5.1f} %)",
        f"  ⏳ en cours       : {in_flight:>4}  ({in_flight/total*100:5.1f} %)",
        f"",
        f"  Détail par statut : " + ", ".join(f"{s}={n}" for s, n in by_status.most_common()),
    ]
    # Breakdown par opérateur (sur les jobs done)
    op_counts: Counter = Counter(j["operator"] for j in jobs if j["status"] == "done")
    if op_counts:
        lines.append("")
        lines.append("  Paiements aboutis par opérateur :")
        for op, n in op_counts.most_common():
            lines.append(f"    {op or '(inconnu)':10s} : {n}")
    # Top erreurs des failed
    err_counts: Counter = Counter(
        (j["last_error"] or "").split("\n", 1)[0][:80]
        for j in jobs if j["status"] == "failed"
    )
    if err_counts:
        lines.append("")
        lines.append("  Top erreurs (jobs failed) :")
        for err, n in err_counts.most_common(5):
            lines.append(f"    [{n:>2}] {err or '(vide)'}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Captures (dataset/store) + re-jeu vérif destinataire
# --------------------------------------------------------------------------- #

def _iter_captures(d_from: date, d_to: date):
    """Yield (sample_id, template, raw_text, extracted) pour chaque capture
    de la période. Le sample_id local est 'YYYY-MM-DD/<uuid32>'.
    """
    if not STORE.exists():
        return
    cur = d_from
    while cur <= d_to:
        day_dir = STORE / cur.isoformat()
        if day_dir.is_dir():
            for sample_dir in day_dir.iterdir():
                if not sample_dir.is_dir():
                    continue
                pred = sample_dir / "prediction.json"
                if not pred.exists():
                    continue
                try:
                    data = json.loads(pred.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                yield (
                    f"{cur.isoformat()}/{sample_dir.name}",
                    data.get("template", ""),
                    data.get("raw_text", "") or "",
                    data.get("extracted") or {},
                )
        cur += timedelta(days=1)


def _is_extraction_ok(extracted: dict) -> bool:
    """Reproduit validators.validate_extraction sans le décorrélateur."""
    m = extracted.get("montant")
    return isinstance(m, int) and m > 0


def _extraction_and_funnel_report(captures: list[dict], jobs: list[dict]) -> str:
    """Deux taux clés :
    1. Extraction OCR : captures où on a su lire un montant valide.
    2. Entonnoir image → job → done : combien arrivent jusqu'au bout.
    """
    n_cap = len(captures)
    if n_cap == 0:
        return "(aucune capture sur la période)"

    n_extracted = sum(1 for c in captures if _is_extraction_ok(c["extracted"]))
    # Pour l'entonnoir, on croise par sample_id : combien des captures
    # extraites se retrouvent en job, et combien finissent en done.
    enqueued_samples = {j["sample_id"] for j in jobs if j["sample_id"]}
    done_samples = {j["sample_id"] for j in jobs if j["sample_id"] and j["status"] == "done"}
    ok_captures = [c for c in captures if _is_extraction_ok(c["extracted"])]
    n_enqueued = sum(1 for c in ok_captures if c["sample_id"] in enqueued_samples)
    n_done = sum(1 for c in ok_captures if c["sample_id"] in done_samples)
    not_enqueued = sum(1 for c in ok_captures if c["sample_id"] not in enqueued_samples)

    pct = lambda n, d: f"{n/d*100:5.1f} %" if d else "  n/a"

    # Breakdown extraction par template (l'OCR ne reconnaît bien que les vrais
    # reçus → les "generic" sont presque toujours non-extraits, et c'est OK).
    by_tpl_total: Counter = Counter(c["template"] or "(empty)" for c in captures)
    by_tpl_ok: Counter = Counter(
        c["template"] or "(empty)" for c in captures if _is_extraction_ok(c["extracted"])
    )

    lines = [
        "  ── Extraction OCR (montant valide lu) ──",
        f"  Captures totales        : {n_cap}",
        f"  Extraction réussie      : {n_extracted}  ({pct(n_extracted, n_cap)})",
        f"  Extraction échouée      : {n_cap - n_extracted}  ({pct(n_cap - n_extracted, n_cap)})",
        f"    (les échecs = images non-reçus : CIN, écrans Wi-Fi, formulaires...)",
        "",
        "  Par template :",
    ]
    for tpl in sorted(by_tpl_total):
        tot = by_tpl_total[tpl]
        ok = by_tpl_ok[tpl]
        lines.append(f"    {tpl:10s} : {ok:>4} / {tot:<4}  ({pct(ok, tot)})")
    lines.append("")
    lines.append("  ── Entonnoir image → paiement ──")
    lines.append(f"  Captures avec extraction OK     : {n_extracted}")
    lines.append(f"    → enqueuées (devenues un job) : {n_enqueued}  ({pct(n_enqueued, n_extracted)})")
    lines.append(f"    → abouties (job done)         : {n_done}  ({pct(n_done, n_extracted)})")
    lines.append(f"    → non-enqueuées (rejetées avant queue) : {not_enqueued}")
    lines.append(f"      (idempotence, client introuvable, CRM injoignable, etc. — cf logs)")
    return "\n".join(lines)


def _mismatch_report(captures: list[dict], jobs: list[dict]) -> str:
    job_status_by_sample: dict[str, str] = {
        j["sample_id"]: j["status"] for j in jobs if j["sample_id"]
    }
    by_template: dict[str, list[bool]] = defaultdict(list)  # True = mismatch
    reason_counts: Counter = Counter()
    mismatch_then_done = 0
    mismatch_then_failed = 0
    mismatch_no_job = 0
    mismatch_samples_examples: list[tuple[str, str, str]] = []  # (sample_id, template, raw_excerpt)

    for c in captures:
        sample_id = c["sample_id"]
        template = c["template"]
        raw_text = c["raw_text"]
        res = validate_recipient_name(template, raw_text)
        is_mismatch = not res.ok
        by_template[template or "(empty)"].append(is_mismatch)
        if is_mismatch:
            reason_counts[res.reason or "unknown"] += 1
            status = job_status_by_sample.get(sample_id)
            if status == "done":
                mismatch_then_done += 1
            elif status == "failed":
                mismatch_then_failed += 1
            else:
                mismatch_no_job += 1
            if len(mismatch_samples_examples) < 5:
                mismatch_samples_examples.append(
                    (sample_id, template, raw_text[:100].replace("\n", " "))
                )

    total = sum(len(v) for v in by_template.values())
    if total == 0:
        return "(aucune capture sur la période)"
    total_mm = sum(sum(v) for v in by_template.values())
    lines = [
        f"  Total captures   : {total}",
        f"  ⚠ Mismatch       : {total_mm:>4}  ({total_mm/total*100:5.1f} %)",
        f"  ✅ Conformes      : {total - total_mm:>4}  ({(total-total_mm)/total*100:5.1f} %)",
        f"",
        f"  Mismatch par template :",
    ]
    for tpl in sorted(by_template):
        items = by_template[tpl]
        mm = sum(items)
        rate = (mm / len(items) * 100) if items else 0
        lines.append(f"    {tpl:10s} : {mm:>3} / {len(items):>3}  ({rate:5.1f} %)")
    if reason_counts:
        lines.append("")
        lines.append("  Motif de mismatch :")
        for reason, n in reason_counts.most_common():
            lines.append(f"    {reason:30s} {n}")
    lines.append("")
    lines.append("  Devenir des captures en mismatch (pass-through mode) :")
    lines.append(f"    → paiement abouti (done)  : {mismatch_then_done}")
    lines.append(f"    → paiement échoué (failed): {mismatch_then_failed}")
    lines.append(f"    → non-rattachée à un job  : {mismatch_no_job}")
    lines.append("    (non-rattaché = capture rejetée AVANT enqueue : ai_ocr KO, "
                 "client introuvable, doublon idempotence, etc.)")
    if mismatch_samples_examples:
        lines.append("")
        lines.append("  Exemples (5 max) :")
        for sid, tpl, raw in mismatch_samples_examples:
            lines.append(f"    [{tpl}] {sid}")
            lines.append(f"      raw: {raw}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    d_from, d_to = _parse_args()
    print(f"╔══════════════════════════════════════════════════════════════════╗")
    print(f"║  Rapport WhatsApp Automation — {d_from} → {d_to}     ║")
    print(f"╚══════════════════════════════════════════════════════════════════╝")
    print()
    jobs = _load_jobs(d_from, d_to)
    captures = [
        {"sample_id": sid, "template": tpl, "raw_text": raw, "extracted": ext}
        for sid, tpl, raw, ext in _iter_captures(d_from, d_to)
    ]

    print("PAIEMENTS (queue.db)")
    print("─" * 68)
    print(_payments_report(jobs))
    print()
    print("EXTRACTION OCR & ENTONNOIR (dataset/store ↔ queue.db)")
    print("─" * 68)
    print(_extraction_and_funnel_report(captures, jobs))
    print()
    print("VÉRIFICATION DESTINATAIRE (dataset/store, rejeu)")
    print("─" * 68)
    print(_mismatch_report(captures, jobs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
