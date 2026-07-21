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


def _search_clients(column: str, phone: str) -> list[dict]:
    """Lignes `client` dont `column` contient le téléphone (sous-chaîne).

    `column` est un littéral choisi par l'appelant (jamais une entrée utilisateur) ;
    seule la valeur du téléphone est passée en paramètre lié.
    """
    with connection() as conn:
        cur = conn.execute(
            f"""SELECT idclient, info, mac, statu, ipaddress
                FROM client
                WHERE {column} LIKE %s""",
            (f"%{phone}%",),
        )
        rows = list(cur.fetchall())
    for row in rows:
        row["idclient"] = int(row["idclient"])
    return rows


def find_clients_by_phone(phone: str) -> list[dict]:
    """Résout téléphone → lignes `client`, via `info` puis en repli `numero_crm`.

    Le schéma prod n'a pas de colonne téléphone normalisée : le numéro est
    historiquement préfixé dans le champ texte `info` ("37697850-Nom Client"),
    et depuis peu recopié dans `numero_crm` (parfois PLUSIEURS numéros dans la
    même cellule : "37954960/48588190" — d'où la recherche en sous-chaîne).

    `info` reste prioritaire, et `numero_crm` n'est interrogé QUE s'il ne donne
    rien : `numero_crm` porte parfois le numéro d'un revendeur partagé par
    plusieurs clients distincts (ex : 38819364 sur 3 comptes "Bankily"). En
    repli seulement, on ne retient donc que les lignes du PREMIER idclient
    trouvé, pour ne jamais mélanger deux clients (le pipeline crédite le
    paiement et débloque les MAC de toutes les lignes rendues).
    """
    if not phone:
        return []
    rows = _search_clients("info", phone)
    if rows:
        return rows

    rows = _search_clients("numero_crm", phone)
    if not rows:
        return []
    idclient = rows[0]["idclient"]
    kept = [r for r in rows if r["idclient"] == idclient]
    if len(kept) != len(rows):
        logger.warning(
            "lookup %s : numero_crm partagé par %d clients — on retient idclient=%d",
            phone, len({r["idclient"] for r in rows}), idclient,
        )
    return kept


def get_client_by_phone(phone: str) -> Optional[dict]:
    """Retourne {idclient, info, mac, statu, ipaddress} ou None (1re ligne).

    Voir ``find_clients_by_phone`` pour la stratégie de résolution du numéro.
    `idclient` est VARCHAR(250) en prod mais contient toujours un entier ;
    on caste pour que tout le code en aval (modèle pydantic Client.id,
    signatures UCRM, formats %d dans les logs) puisse le traiter comme int.
    """
    rows = find_clients_by_phone(phone)
    return rows[0] if rows else None


def get_client_by_id(idclient: int | str) -> list[dict]:
    """Retourne toutes les lignes `client` d'un idclient (1 par abonnement/MAC).

    `client.idclient` est VARCHAR(250) en prod (contenu entier) ; on filtre en
    str et on recaste en int en sortie, comme get_client_by_phone.
    """
    with connection() as conn:
        cur = conn.execute(
            """SELECT idclient, info, mac, statu, ipaddress
               FROM client
               WHERE idclient = %s""",
            (str(idclient),),
        )
        rows = cur.fetchall()
    for row in rows:
        row["idclient"] = int(row["idclient"])
    return list(rows)


def get_clients_by_phone(phone: str) -> list[dict]:
    """Retourne TOUTES les lignes `client` matchant le téléphone.

    Contrairement à ``get_client_by_phone`` (qui prend la 1re ligne, pour le
    pipeline de paiement), un même client peut avoir plusieurs abonnements /
    équipements en prod : autant de lignes que de MAC distincts, partageant en
    général le même ``idclient``. Cet endpoint de consultation a besoin de
    toutes ces lignes pour exposer le MAC de chaque abonnement.

    Même résolution du numéro que ``get_client_by_phone`` (`info` puis repli
    `numero_crm`) : cf. ``find_clients_by_phone``.
    """
    return find_clients_by_phone(phone)


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


def update_client_status_by_mac(mac: str, statu: int) -> int:
    """Met à jour `statu` pour la ligne client portant ce MAC précis.

    Contrairement à ``update_client_status`` (qui agit sur toutes les lignes
    d'un idclient), on cible un seul abonnement par sa MAC — cohérent avec le
    blocage/déblocage d'un équipement unique (cf. PHP ``EditStatuClient`` qui
    filtre aussi par MAC). Retourne le nombre de lignes modifiées.
    """
    if not mac:
        return 0
    with connection() as conn:
        cur = conn.execute(
            "UPDATE client SET statu = %s WHERE mac = %s",
            (statu, mac),
        )
        return cur.rowcount


def count_paiements() -> int:
    """Nombre total de paiements enregistrés (table `paiment`).

    Utilisé par le dashboard de supervision comme repère cumulatif. La table
    n'a pas de timestamp complet (jour/mois/année séparés) : on renvoie le total
    brut, les compteurs par période venant des logs.
    """
    with connection() as conn:
        cur = conn.execute("SELECT COUNT(*) AS n FROM paiment")
        row = cur.fetchone()
        return int(row["n"]) if row else 0


def get_paiements_by_client(idclient: int, limit: int = 20) -> list[dict]:
    """Historique des paiements ENREGISTRÉS d'un client (table `paiment`).

    Utilisé par le détail d'un événement du dashboard (ex : montrer les
    paiements précédents d'un client dont un nouveau reçu est refusé pour
    sur-paiement). Trié du plus récent au plus ancien. La table n'a pas de
    timestamp complet : on ordonne sur year/month/day puis id_payment.
    """
    with connection() as conn:
        cur = conn.execute(
            """SELECT id_payment, amount, day, month, year, txn_id, phone
               FROM paiment
               WHERE idclient = %s
               ORDER BY year DESC, month DESC, day DESC, id_payment DESC
               LIMIT %s""",
            (idclient, limit),
        )
        return list(cur.fetchall())


def get_paiements_by_phone(phone: str, limit: int = 20) -> list[dict]:
    """Historique des paiements d'un client par TÉLÉPHONE (repli quand on ne
    connaît pas l'idclient, ex : reçu envoyé dont le log ne porte que le numéro).
    """
    if not phone:
        return []
    with connection() as conn:
        cur = conn.execute(
            """SELECT id_payment, idclient, amount, day, month, year, txn_id, phone
               FROM paiment
               WHERE phone = %s
               ORDER BY year DESC, month DESC, day DESC, id_payment DESC
               LIMIT %s""",
            (phone, limit),
        )
        return list(cur.fetchall())


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
