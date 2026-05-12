-- Schéma PostgreSQL aligné sur la structure de la base de PRODUCTION.
-- Tables : `client` (5 colonnes) et `paiment` (7 colonnes + txn_id ajouté
-- pour l'idempotence côté pipeline Python).
--
-- Utilisation :
--   psql -U postgres -d whatsapp_test -f schema.sql

CREATE TABLE IF NOT EXISTS client (
    idclient    SERIAL PRIMARY KEY,
    info        TEXT,                          -- texte libre contenant notamment le téléphone (lookup via LIKE)
    mac         VARCHAR(17),
    statu       SMALLINT NOT NULL DEFAULT 0,   -- 0 = actif, 2 = suspendu (codes PROD)
    ipaddress   VARCHAR(45)                    -- IPv4/IPv6 — utilisé pour retrouver la rule firewall côté MikroTik
);

CREATE INDEX IF NOT EXISTS idx_client_info ON client(info);
CREATE INDEX IF NOT EXISTS idx_client_mac ON client(mac);
CREATE INDEX IF NOT EXISTS idx_client_ipaddress ON client(ipaddress);

CREATE TABLE IF NOT EXISTS paiment (
    id_payment  VARCHAR(64) PRIMARY KEY,       -- paymentId UCRM (fourni par l'API billing, non auto-incrément)
    idclient    INTEGER NOT NULL REFERENCES client(idclient),
    phone       VARCHAR(20),
    amount      INTEGER NOT NULL,
    day         SMALLINT,
    month       SMALLINT,
    year        SMALLINT,
    txn_id      VARCHAR(50)                    -- colonne AJOUTÉE en prod pour l'idempotence (clé du reçu)
);

CREATE INDEX IF NOT EXISTS idx_paiment_idclient ON paiment(idclient);
CREATE INDEX IF NOT EXISTS idx_paiment_txn ON paiment(txn_id);
