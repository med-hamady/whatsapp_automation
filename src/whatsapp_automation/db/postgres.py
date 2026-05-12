"""Accès PostgreSQL local (tables `client` et `paiment`, alignées sur la prod).

On utilise psycopg 3 en mode synchrone. Les appels DB sont rapides (DB locale,
index sur info/mac/ipaddress/txn_id) et tournent dans le threadpool de
FastAPI/du worker. Pas besoin d'async pour ces requêtes.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

import psycopg
from psycopg.rows import dict_row

from .. import config


logger = logging.getLogger("whatsapp_automation.db")


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(config.DATABASE_URL, row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_client_by_phone(phone: str) -> Optional[dict]:
    """Retourne {idclient, info, mac, statu, ipaddress} ou None.

    Le téléphone est cherché par sous-chaîne dans le champ texte `info`
    (le schéma prod n'a pas de colonne phone dédiée sur la table client).
    """
    if not phone:
        return None
    with connection() as conn:
        cur = conn.execute(
            """SELECT idclient, info, mac, statu, ipaddress
               FROM client
               WHERE info LIKE %s
               LIMIT 1""",
            (f"%{phone}%",),
        )
        return cur.fetchone()


def insert_paiement(
    idclient: int,
    amount: int,
    phone: str,
    id_payment: str,
    txn_id: str | None,
    paid_at: datetime | None = None,
) -> str:
    """Insère un paiement dans `paiment`.

    `id_payment` est le paymentId retourné par UCRM (PRIMARY KEY de la table
    en prod — non auto-incrément). La date est éclatée en day/month/year
    (schéma prod). `paid_at` par défaut = maintenant (UTC).
    """
    dt = paid_at or datetime.now(timezone.utc)
    with connection() as conn:
        conn.execute(
            """INSERT INTO paiment
               (id_payment, idclient, phone, amount, day, month, year, txn_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (id_payment, idclient, phone, amount, dt.day, dt.month, dt.year, txn_id),
        )
    return id_payment


def update_client_status(idclient: int, statu: int) -> None:
    """Met à jour statu (codes PROD : 0 = actif, 2 = suspendu)."""
    with connection() as conn:
        conn.execute(
            "UPDATE client SET statu = %s WHERE idclient = %s",
            (statu, idclient),
        )


def payment_exists_by_txn(txn_id: str) -> bool:
    """Idempotence côté DB métier (en plus de processed_payments en queue)."""
    if not txn_id:
        return False
    with connection() as conn:
        cur = conn.execute(
            "SELECT 1 FROM paiment WHERE txn_id = %s LIMIT 1",
            (txn_id,),
        )
        return cur.fetchone() is not None
