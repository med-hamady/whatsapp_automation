"""Initialise les DB locales :
  1. PostgreSQL : crée la base (si elle n'existe pas) et applique schema.sql.
  2. SQLite : crée queue.db avec le schema queue.

Usage :
    python -m whatsapp_automation.scripts.init_db [--reset] [--seed]

--reset : DROP les tables client/paiment/jobs/processed_payments avant de
          les recréer (utile pendant le développement).
--seed  : applique aussi sql/seed.sql après création.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parent.parent
_sys.path.insert(0, str(_ROOT / 'src'))
_sys.path.insert(0, str(_ROOT))


import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

import psycopg

from whatsapp_automation import config
from whatsapp_automation.jobqueue import store as queue_store


_SQL_DIR = Path(__file__).resolve().parent.parent / "src" / "whatsapp_automation" / "db" / "sql"
SCHEMA = _SQL_DIR / "schema.sql"
SEED = _SQL_DIR / "seed.sql"


def _ensure_database_exists():
    """Crée la base PostgreSQL si elle n'existe pas (connexion sur postgres)."""
    parsed = urlparse(config.DATABASE_URL)
    dbname = parsed.path.lstrip("/")
    if not dbname:
        raise RuntimeError("DATABASE_URL doit contenir un nom de base")

    admin_url = config.DATABASE_URL.rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(admin_url, autocommit=True) as conn:
        cur = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
        if cur.fetchone() is None:
            conn.execute(f'CREATE DATABASE "{dbname}"')
            print(f"  → base '{dbname}' créée")
        else:
            print(f"  → base '{dbname}' déjà présente")


def _apply_sql(path: Path):
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        conn.execute(path.read_text(encoding="utf-8"))


def _reset_tables():
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        # On drop aussi les anciens noms (clients/paiements) pour permettre
        # le passage d'une ancienne DB de dev à la nouvelle structure.
        conn.execute("DROP TABLE IF EXISTS paiement CASCADE")
        conn.execute("DROP TABLE IF EXISTS paiment CASCADE")
        conn.execute("DROP TABLE IF EXISTS paiements CASCADE")
        conn.execute("DROP TABLE IF EXISTS client CASCADE")
        conn.execute("DROP TABLE IF EXISTS clients CASCADE")
        print("  → tables client + paiment DROP")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()

    print(f"[Postgres] {config.DATABASE_URL}")
    try:
        _ensure_database_exists()
    except psycopg.OperationalError as exc:
        print(f"  ✗ impossible de se connecter à PostgreSQL : {exc}", file=sys.stderr)
        sys.exit(1)

    if args.reset:
        _reset_tables()

    _apply_sql(SCHEMA)
    print(f"  → schema.sql appliqué")

    if args.seed:
        _apply_sql(SEED)
        print(f"  → seed.sql appliqué")

    print(f"\n[SQLite queue] {config.QUEUE_DB_PATH}")
    queue_store.init_db()
    print(f"  → queue.db initialisée")

    print("\n✅ init_db terminé")


if __name__ == "__main__":
    main()
