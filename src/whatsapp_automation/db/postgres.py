"""Accès PostgreSQL local (DB clients/paiements).

On utilise psycopg 3 en mode synchrone. Les appels DB sont rapides (DB locale,
index sur num/mac) et tournent dans le threadpool de FastAPI/du worker. Pas
besoin d'async pour ces requêtes.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
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
    """Retourne {idclient, num, mac, nom, statu, firewall_rule_id} ou None."""
    if not phone:
        return None
    with connection() as conn:
        cur = conn.execute(
            """SELECT idclient, num, mac, nom, statu, firewall_rule_id
               FROM clients
               WHERE num = %s
               LIMIT 1""",
            (phone,),
        )
        return cur.fetchone()


def insert_paiement(
    idclient: int,
    montant: int,
    num: str,
    ucrm_payment_id: str | None,
    txn_id: str | None,
    operator: str | None,
) -> int:
    """Insère un paiement dans la table paiements. Retourne l'id généré."""
    with connection() as conn:
        cur = conn.execute(
            """INSERT INTO paiements
               (idclient, montant, num, ucrm_payment_id, txn_id, operator)
               VALUES (%s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (idclient, montant, num, ucrm_payment_id, txn_id, operator),
        )
        row = cur.fetchone()
        return row["id"]


def update_client_status(idclient: int, statu: int) -> None:
    """Met à jour statu (0 = actif, 1 = suspendu)."""
    with connection() as conn:
        conn.execute(
            "UPDATE clients SET statu = %s WHERE idclient = %s",
            (statu, idclient),
        )


def payment_exists_by_txn(txn_id: str) -> bool:
    """Idempotence côté DB métier (en plus de processed_payments en queue)."""
    if not txn_id:
        return False
    with connection() as conn:
        cur = conn.execute(
            "SELECT 1 FROM paiements WHERE txn_id = %s LIMIT 1",
            (txn_id,),
        )
        return cur.fetchone() is not None
