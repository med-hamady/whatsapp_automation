-- Données de test : 9 clients suspendus.
-- `info` contient le téléphone brut (lookup via LIKE '%phone%').
-- Le solde dû correspondant pour chaque client est dans fake_ucrm.BALANCES.

INSERT INTO client (idclient, info, mac, statu, ipaddress) VALUES
    (1, 'Client 48783201',  'AA:BB:CC:00:00:01', 2, '10.0.0.1'),
    (2, 'Client 48249066',  'AA:BB:CC:00:00:02', 2, '10.0.0.2'),
    (3, 'Client 46603985',  'AA:BB:CC:00:00:03', 2, '10.0.0.3'),
    (4, 'Client 31752614',  'AA:BB:CC:00:00:04', 2, '10.0.0.4'),
    (5, 'Client 37888210',  'AA:BB:CC:00:00:05', 2, '10.0.0.5'),
    (6, 'Client 44160960',  'AA:BB:CC:00:00:06', 2, '10.0.0.6'),
    (7, 'Client 777565497', 'AA:BB:CC:00:00:07', 2, '10.0.0.7'),
    (8, 'Client 33848414',  'AA:BB:CC:00:00:08', 2, '10.0.0.8'),
    (9, 'Client 41769945',  'AA:BB:CC:00:00:09', 2, '10.0.0.9');

-- Resync SERIAL pour que les futurs INSERT (sans idclient explicite) repartent à 10.
SELECT setval(pg_get_serial_sequence('client', 'idclient'), (SELECT MAX(idclient) FROM client));
