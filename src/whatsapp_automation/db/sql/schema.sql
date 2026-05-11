-- Schéma PostgreSQL local pour tests du pipeline WhatsApp Automation.
-- Crée les tables clients et paiements telles qu'utilisées par le code PHP
-- (admin.php :: GetClientByPhoneNumber, GetClientById, insert_paiement, etc.).
--
-- Utilisation :
--   psql -U postgres -d whatsapp_test -f schema.sql

CREATE TABLE IF NOT EXISTS clients (
    idclient         SERIAL PRIMARY KEY,
    num              VARCHAR(20) NOT NULL,
    mac              VARCHAR(17) NOT NULL,
    nom              VARCHAR(100),
    statu            SMALLINT NOT NULL DEFAULT 0,   -- 0 = actif, 1 = suspendu
    firewall_rule_id VARCHAR(20),                   -- .id côté MikroTik (rempli quand suspendu)
    created_at       TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_clients_num ON clients(num);
CREATE INDEX IF NOT EXISTS idx_clients_mac ON clients(mac);

CREATE TABLE IF NOT EXISTS paiements (
    id              SERIAL PRIMARY KEY,
    idclient        INTEGER NOT NULL REFERENCES clients(idclient),
    montant         INTEGER NOT NULL,
    num             VARCHAR(20),
    ucrm_payment_id VARCHAR(50),
    txn_id          VARCHAR(50),
    operator        VARCHAR(20),
    paid_at         TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_paiements_txn ON paiements(txn_id);
CREATE INDEX IF NOT EXISTS idx_paiements_idclient ON paiements(idclient);
