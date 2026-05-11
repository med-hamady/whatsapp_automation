-- Données de test : 3 clients suspendus avec différents montants attendus.

INSERT INTO clients (num, mac, nom, statu, firewall_rule_id) VALUES
    ('37697850', 'AA:BB:CC:00:00:01', 'Client Test Bankily',  1, '*1A'),
    ('33848414', 'AA:BB:CC:00:00:02', 'Client Test MASRIVI',  1, '*2B'),
    ('49593871', 'AA:BB:CC:00:00:03', 'Client Test Sedad',    1, '*3C'),
    ('11111111', 'AA:BB:CC:00:00:04', 'Client Actif (non suspendu)', 0, NULL);
