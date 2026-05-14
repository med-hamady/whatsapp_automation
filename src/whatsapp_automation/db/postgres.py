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
    id_payment: str | int,
    txn_id: str | None,
    paid_at: datetime | None = None,
) -> int:
    """Insère un paiement dans `paiment`.

    `id_payment` est le paymentId retourné par UCRM (PRIMARY KEY de la table
    en prod — non auto-incrément, colonne `integer`). On accepte str ou int
    en entrée et on caste : UCRM le renvoie en str (paymentCovers[0].paymentId),
    mais la colonne prod attend un integer.

    `txn_id` est nullable (autres systèmes écrivant dans `paiment` peuvent ne
    pas le fournir).
    """
    dt = paid_at or datetime.now(timezone.utc)
    id_payment_int = int(id_payment)
    with connection() as conn:
        conn.execute(
            """INSERT INTO paiment
               (id_payment, idclient, phone, amount, day, month, year, txn_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (id_payment_int, idclient, phone, amount, dt.day, dt.month, dt.year, txn_id),
        )
    return id_payment_int


def update_client_status(idclient: int | str, statu: int) -> None:
    """Met à jour statu (codes PROD : 0 = actif, 2 = suspendu).

    ⚠ Schéma prod incohérent : `client.idclient` est `VARCHAR(250)` alors
    que `paiment.idclient` est `INTEGER`. On caste en str ici pour matcher
    le type réel de la colonne (sinon : `operator does not exist:
    character varying = smallint`).
    """
    with connection() as conn:
        conn.execute(
            "UPDATE client SET statu = %s WHERE idclient = %s",
            (statu, str(idclient)),
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
