"""Backfill local paiment table from UCRM payments API.

Used to recover payments that exist in UCRM but were never written to the local
`paiment` table — typically the period when the webhook was dropping receipts
with `client_not_suspended`.

Idempotence : on `id_payment` (UCRM paymentId = local PRIMARY KEY). Running
twice never duplicates.

Usage :
    # Dry-run : just print what would be inserted
    python scripts/backfill_payments.py --since 2026-05-13 --until 2026-06-01

    # Apply
    python scripts/backfill_payments.py --since 2026-05-13 --until 2026-06-01 --apply
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx

from whatsapp_automation import config
from whatsapp_automation.db import postgres as pg


logger = logging.getLogger("backfill")

# Le champ client.info est du texte libre où le numéro 8 chiffres peut être
# collé à un nom ("36888822AHM DHA"), espacé ("46 41 25 97") ou en suffixe
# ("FATMATOU32715092"). On strip les espaces puis on cherche un groupe de
# 8 chiffres NON inclus dans un groupe plus long (lookaround anti-digit).
PHONE_RE = re.compile(r"(?<!\d)\d{8}(?!\d)")


def _billing_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "X-Auth-App-Key": config.UCRM_APP_KEY,
    }


async def fetch_ucrm_payments(since: str, until: str) -> list[dict]:
    """Récupère tous les paiements UCRM dans la plage, paginé."""
    url = f"{config.UCRM_BASE_URL}/api/v1.0/payments"
    all_payments: list[dict] = []
    offset = 0
    limit = 1000
    async with httpx.AsyncClient(timeout=120.0, verify=False) as c:
        while True:
            r = await c.get(url, headers=_billing_headers(), params={
                "createdDateFrom": since,
                "createdDateTo": until,
                "limit": limit,
                "offset": offset,
            })
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            all_payments.extend(batch)
            if len(batch) < limit:
                break
            offset += limit
    return all_payments


def payment_id_exists(id_payment: int) -> bool:
    with pg.connection() as conn:
        cur = conn.execute(
            "SELECT 1 FROM paiment WHERE id_payment=%s LIMIT 1",
            (id_payment,),
        )
        return cur.fetchone() is not None


def get_client_info(idclient: int) -> dict | None:
    """idclient est VARCHAR(250) en prod — on caste en str pour la query."""
    with pg.connection() as conn:
        cur = conn.execute(
            "SELECT idclient, info FROM client WHERE idclient=%s LIMIT 1",
            (str(idclient),),
        )
        return cur.fetchone()


def extract_phone(info: str | None) -> str:
    if not info:
        return ""
    cleaned = re.sub(r"\s+", "", info)
    # 1ère passe : groupe de 8 chiffres isolé (cas standard).
    m = PHONE_RE.search(cleaned)
    if m:
        return m.group(0)
    # 2ème passe : numéro avec indicatif Mauritanie +222 ou 222 collé.
    m = re.search(r"\+?222(\d{8})(?!\d)", cleaned)
    if m:
        return m.group(1)
    return ""


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True, help="Date début YYYY-MM-DD (inclus)")
    parser.add_argument("--until", required=True, help="Date fin YYYY-MM-DD (inclus)")
    parser.add_argument("--apply", action="store_true",
                        help="Effectuer les inserts (sinon dry-run)")
    parser.add_argument("--verbose", action="store_true",
                        help="Affiche chaque ligne à insérer")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    payments = await fetch_ucrm_payments(args.since, args.until)
    logger.info("UCRM : %d paiements entre %s et %s",
                len(payments), args.since, args.until)

    stats = {"already_in_db": 0, "no_client": 0, "no_phone": 0,
             "to_insert": 0, "inserted": 0, "errors": 0}
    to_insert: list[dict] = []

    for p in payments:
        pid = int(p["id"])
        if payment_id_exists(pid):
            stats["already_in_db"] += 1
            continue
        client_id = int(p["clientId"])
        client_row = get_client_info(client_id)
        if client_row is None:
            logger.warning("paiment %d : client %d introuvable en DB, skip",
                           pid, client_id)
            stats["no_client"] += 1
            continue
        phone = extract_phone(client_row.get("info"))
        if not phone:
            # phone est NOT NULL : empty string accepté mais on signale
            stats["no_phone"] += 1
            logger.debug("paiment %d : pas de phone trouvé dans info=%r",
                         pid, client_row.get("info"))
        amount = int(round(float(p["amount"])))
        # createdDate ex : "2026-05-31T13:28:48+0000"
        dt = datetime.strptime(p["createdDate"], "%Y-%m-%dT%H:%M:%S%z")
        to_insert.append({
            "id_payment": pid,
            "idclient": client_id,
            "phone": phone,
            "amount": amount,
            "paid_at": dt,
            "note": (p.get("note") or "").strip(),
        })

    stats["to_insert"] = len(to_insert)
    logger.info("Bilan : déjà_en_db=%d, client_introuvable=%d, phone_vide=%d, à_insérer=%d",
                stats["already_in_db"], stats["no_client"],
                stats["no_phone"], stats["to_insert"])

    if not args.apply:
        logger.info("DRY-RUN : aucun insert. Relance avec --apply pour appliquer.")
        sample = to_insert if args.verbose else to_insert[:10]
        for row in sample:
            logger.info("  id_payment=%d idclient=%d amount=%d phone=%r note=%r date=%s",
                        row["id_payment"], row["idclient"], row["amount"],
                        row["phone"], row["note"], row["paid_at"].date())
        if not args.verbose and len(to_insert) > 10:
            logger.info("  ... (+%d autres ; --verbose pour tout afficher)",
                        len(to_insert) - 10)
        return 0

    for row in to_insert:
        try:
            pg.insert_paiement(
                idclient=row["idclient"],
                amount=row["amount"],
                phone=row["phone"],
                id_payment=row["id_payment"],
                txn_id=None,
                paid_at=row["paid_at"],
            )
            stats["inserted"] += 1
        except Exception as exc:
            logger.error("insert id_payment=%d échec : %s: %r",
                         row["id_payment"], type(exc).__name__, exc)
            stats["errors"] += 1

    logger.info("FAIT : inséré=%d, erreurs=%d",
                stats["inserted"], stats["errors"])
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
