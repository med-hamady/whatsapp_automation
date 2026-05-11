# Audit complet de webhook.php — Rapport pour l'équipe

**Fichier audité** : [webhook.php](webhook.php) (157 lignes)
**Objectif** : identifier **toutes les sources de problèmes** (lenteur, impossibilité de traiter plusieurs clients en parallèle, bugs, sécurité, robustesse) avant correction.
**Date** : 2026-05-05

---

## Synthèse exécutive

Le fichier `webhook.php` cumule **trois familles de problèmes** qui expliquent les symptômes constatés :

1. **Concurrence cassée** → impossible de traiter plusieurs clients en parallèle (cause racine : fichier `image.jpeg` partagé).
2. **Latence par requête très élevée (15-40s)** → saturation rapide du pool de workers PHP (cause : OCR + appel HTTP synchrone + I/O inutiles).
3. **Bugs de logique et absence de garde-fous** → traitements silencieusement faux ou paiements perdus.

Au total, **27 problèmes distincts** ont été identifiés, classés ci-dessous par sévérité.

---

## 🔴 SÉVÉRITÉ CRITIQUE (cassent le multi-client ou provoquent des pertes/doublons)

### P1 — Race condition sur le fichier `image.jpeg`
- **Localisation** : [webhook.php:34](webhook.php#L34) puis [webhook.php:40](webhook.php#L40)
- **Code** :
  ```php
  file_put_contents("image.jpeg", $image);   // nom FIXE, partagé
  $ocr = new TesseractOCR("image.jpeg");
  ```
- **Cause** : le fichier image est toujours nommé `image.jpeg`. Quand deux webhooks arrivent en même temps, le 2e écrase l'image du 1er **avant** que Tesseract n'ait fini d'OCR la 1re.
- **Conséquence** : le client A se voit attribuer le montant lu sur l'image du client B → paiements faux. **C'est le symptôme exact rapporté par l'entreprise**.
- **Probabilité** : 100 % dès qu'au moins 2 webhooks se chevauchent.

### P2 — Appel HTTP synchrone bloquant vers `remove_suspende_whatsapp.php`
- **Localisation** : [webhook.php:119](webhook.php#L119) et [webhook.php:141](webhook.php#L141)
- **Code** : `$response = file_get_contents($unblockUrl);`
- **Cause** : `webhook.php` attend la fin complète du pipeline aval (UCRM API + MikroTik + envoi PDF UltraMsg ≈ 10–30 s) avant de répondre à UltraMsg.
- **Conséquences** :
  - Le worker PHP reste bloqué tout ce temps → 1 webhook = 1 worker indisponible pendant 10–30 s.
  - Si UltraMsg considère le webhook en timeout, il **rejoue** la livraison → **double paiement** possible (aucune idempotence en place).
  - Saturation rapide : avec 5 workers PHP-FPM et 20 s/requête, débit max ≈ **15 webhooks/min**.

### P3 — Pas de réponse HTTP rapide à UltraMsg
- **Localisation** : début et fin du fichier
- **Cause** : aucun `http_response_code(200)` + `fastcgi_finish_request()` en début de script. UltraMsg attend la fin de tout le traitement avant de recevoir le 200 OK.
- **Conséquence** : timeouts UltraMsg fréquents → rejeux → doublons (aggrave P2).

### P4 — Pas d'idempotence
- **Localisation** : tout le script
- **Cause** : aucun mécanisme pour détecter un message déjà traité (pas de stockage de l'ID UltraMsg du message).
- **Conséquence** : un rejeu UltraMsg ou un double envoi par le client crée un **double paiement** dans UCRM.

### P5 — Lecture inutile et coûteuse de `log.txt` à chaque webhook
- **Localisation** : [webhook.php:36](webhook.php#L36)
- **Code** :
  ```php
  $fileContent = file_exists($file) ? file_get_contents($file) : '';
  ```
- **Cause** : la variable `$fileContent` **n'est jamais utilisée ensuite**. Mais à chaque webhook, on charge l'intégralité de `log.txt` (~3,7 MB aujourd'hui) en mémoire pour rien.
- **Conséquence** : ralentissement linéaire à mesure que `log.txt` grossit. Aujourd'hui ≈ 3,7 MB, demain davantage. Le webhook deviendra de plus en plus lent jusqu'à OOM possible.

### P6 — Verrous `LOCK_EX` qui sérialisent les écritures concurrentes
- **Localisation** : [webhook.php:38](webhook.php#L38), [webhook.php:44](webhook.php#L44), [webhook.php:108](webhook.php#L108), [webhook.php:132](webhook.php#L132)
- **Cause** : 4 fichiers partagés (`log.txt`, `test.txt`, `mount.txt`) sont écrits avec `FILE_APPEND | LOCK_EX`. `LOCK_EX` est bloquant : si plusieurs webhooks veulent écrire en même temps, ils s'attendent les uns les autres.
- **Conséquence** : sérialisation des écritures concurrentes — en plus du problème P1, même les logs deviennent un goulot d'étranglement.

---

## 🟠 SÉVÉRITÉ HAUTE (latence ou robustesse)

### P7 — Tesseract OCR exécuté sur le thread du webhook
- **Localisation** : [webhook.php:40-43](webhook.php#L40-L43)
- **Cause** : Tesseract lance un sous-processus Windows et charge les modèles `eng+fra` (deux langues) à chaque appel. 1–5 s par image, totalement bloquant pour le worker PHP.
- **Conséquence** : OCR fait partie intégrante du temps de réponse au webhook. Aucune file/queue ni worker dédié OCR.

### P8 — Double téléchargement de l'image S3
- **Localisation** : [webhook.php:21](webhook.php#L21) puis [webhook.php:32](webhook.php#L32)
- **Code** :
  ```php
  if(getimagesize($event["data"]["media"])){   // 1er download (potentiellement complet)
      ...
      $image = file_get_contents($url);          // 2e download du même fichier
  ```
- **Cause** : `getimagesize()` sur une URL distante télécharge l'image (entièrement sur certains formats) **uniquement pour la valider**. Puis on retélécharge le fichier ligne 32.
- **Conséquence** : latence + bande passante doublées sans bénéfice. Sur S3 avec une image de 200 KB, c'est ≈ 1 s perdu/requête.

### P9 — `file_get_contents` sans timeout sur l'image S3 ni sur l'appel HTTP
- **Localisation** : [webhook.php:32](webhook.php#L32), [webhook.php:119](webhook.php#L119), [webhook.php:141](webhook.php#L141)
- **Cause** : `file_get_contents` n'a pas de timeout par défaut configurable simplement. Si S3 ou le serveur de paiement est lent/HS, le webhook reste bloqué (jusqu'au `default_socket_timeout` PHP, souvent 60 s).
- **Conséquence** : un seul S3 lent peut bloquer un worker une minute entière.

### P10 — Aucune gestion d'exception autour de Tesseract
- **Localisation** : [webhook.php:43](webhook.php#L43) — `$ocr->run()`
- **Cause** : `TesseractOCR->run()` peut lever une exception (binaire introuvable, image corrompue, langues absentes). Aucun `try/catch`.
- **Conséquence** : un crash silencieux du webhook → UltraMsg ne reçoit jamais de réponse → rejeu → double paiement éventuel (cf. P4).

### P11 — Aucune vérification du retour de `file_get_contents`
- **Localisation** : [webhook.php:32](webhook.php#L32), [webhook.php:119](webhook.php#L119), [webhook.php:141](webhook.php#L141)
- **Cause** : si l'appel échoue, `file_get_contents` retourne `false` et émet juste un warning. Le code continue comme si tout allait bien.
- **Conséquence** : `file_put_contents("image.jpeg", false)` écrit une chaîne vide, Tesseract OCR lit un fichier vide, montant non détecté, paiement perdu.

### P12 — Connexion PostgreSQL ouverte à chaque appel
- **Localisation** : [webhook.php:9](webhook.php#L9) — `include('../connect.php')`
- **Cause** : nouvelle connexion PG à chaque webhook (pas de `pg_pconnect`, pas de pooling). Sous charge, sature `max_connections` du serveur PostgreSQL.
- **Conséquence** : à partir d'un certain seuil, les webhooks échouent sur l'ouverture de la connexion.

### P13 — Pas de validation que le webhook vient bien d'UltraMsg
- **Localisation** : début du fichier
- **Cause** : aucune vérification de signature/token/IP source. L'endpoint est totalement public.
- **Conséquence** : n'importe qui sur internet peut envoyer une requête forgée, déclencher un appel à `remove_suspende_whatsapp.php` et tenter de débloquer un client. **Risque sécurité majeur**.

---

## 🟡 SÉVÉRITÉ MOYENNE (bugs de logique et code fragile)

### P14 — Accès non sécurisés à `$strArr[1]`
- **Localisation** : [webhook.php:75-76](webhook.php#L75-L76)
- **Code** :
  ```php
  if($res == false){
      $pieces1 = explode(' ', $strArr[1]);   // $strArr[1] peut ne pas exister
  ```
- **Cause** : si le mot "MRU" n'est pas trouvé dans le texte OCR, `explode("MRU", $text)` retourne un tableau d'1 seul élément. Accéder à `$strArr[1]` génère un warning PHP et `$pieces1` peut contenir des valeurs vides.
- **Conséquence** : warnings PHP en boucle dans les logs serveur + montants faux possibles.

### P15 — Accès non sécurisés à `$pieces[count-2]`
- **Localisation** : [webhook.php:54](webhook.php#L54)
- **Code** : `$mnt2 = $pieces[count($pieces)-2];`
- **Cause** : si `$pieces` ne contient qu'un seul élément (ex. OCR détecte `1500MRU` collé), `count-2 = -1` → index inexistant → warning.
- **Conséquence** : warnings PHP, mais surtout logique parsing fragile.

### P16 — `pg_fetch_assoc` peut retourner `false` sans vérification
- **Localisation** : [webhook.php:103-105](webhook.php#L103-L105) et [webhook.php:129-131](webhook.php#L129-L131)
- **Code** :
  ```php
  $data = pg_fetch_assoc($admin->GetClientByPhoneNumber($cnx, $num));
  $idClient = $data["idclient"];   // $data peut être false → warning
  ```
- **Cause** : si la requête SQL ne retourne aucun client, `pg_fetch_assoc` retourne `false`, et accéder à `$data["idclient"]` génère un warning.
- **Conséquence** : `$idClient` devient `null`. Dans la 1re branche, le test `$idClient > 0` envoie le code dans le `else`. Mais dans le `else` (ligne 131), **le même bug se reproduit** sans test ensuite.

### P17 — Appel à `remove_suspende_whatsapp.php` même quand `$idClient` est invalide
- **Localisation** : [webhook.php:131-141](webhook.php#L131-L141)
- **Cause** : dans la branche fallback (`body_num`), si `$data` est `false`, `$idClient` est `null` mais on construit quand même l'URL et on appelle `remove_suspende_whatsapp.php?id=&amount=...`. Aucune validation `if ($idClient > 0)` avant l'appel.
- **Conséquence** : appels HTTP inutiles → latence inutile + spam dans `error.log` côté `remove_suspende_whatsapp.php` qui valide les params.

### P18 — `count($strArr) > 0` est toujours vrai
- **Localisation** : [webhook.php:48](webhook.php#L48)
- **Code** : `if(count($strArr) > 0)`
- **Cause** : `explode("MRU", $text)` retourne **toujours** au moins 1 élément (même si la chaîne est vide). Ce test ne sert à rien.
- **Conséquence** : pas de bug fonctionnel, mais code trompeur. Il aurait fallu tester `count > 1` (= "MRU" trouvé) pour entrer en parsing fiable.

### P19 — Logique de parsing du montant fragile
- **Localisation** : [webhook.php:45-97](webhook.php#L45-L97)
- **Cause** : le parsing repose entièrement sur la présence du token "MRU" et sur l'ordre des mots. Une erreur OCR ("MRU"→"MRV", "MRU"→"MR U", "1500"→"1S00") ou une mise en page différente casse le parsing.
- **Conséquence** : montants non détectés → paiement perdu silencieusement.

### P20 — `intval($mnt)` après détection : faux positifs possibles
- **Localisation** : [webhook.php:107](webhook.php#L107)
- **Cause** : `is_numeric` accepte des notations comme `"1500.50"` ou `"1e3"`. `intval` les convertit ensuite, perdant les décimales ou interprétant l'exposant.
- **Conséquence** : montant payé ≠ montant facturé dans des cas exotiques.

### P21 — `getimagesize` requiert `allow_url_fopen=On`
- **Localisation** : [webhook.php:21](webhook.php#L21)
- **Cause** : si le PHP serveur a `allow_url_fopen=Off` (configuration durcie), `getimagesize` sur URL retourne `false` silencieusement → tous les webhooks rejetés.
- **Conséquence** : dépendance forte à une option PHP, fragile en cas de durcissement.

### P22 — Pas de gestion du cas "webhook sans image" (texte simple)
- **Localisation** : [webhook.php:21](webhook.php#L21)
- **Cause** : si l'utilisateur envoie un texte (pas d'image), `$event["data"]["media"]` est vide ou absent. `getimagesize("")` retourne `false`, le `if` est sauté, **le webhook ne fait rien** et ne répond rien d'utile à UltraMsg.
- **Conséquence** : pas grave fonctionnellement, mais aucune trace, aucun retour utilisateur.

### P23 — Variables réassignées (variable shadowing) prêtant à confusion
- **Localisation** : `$data` aux lignes [7](webhook.php#L7), [24](webhook.php#L24), [103](webhook.php#L103), [129](webhook.php#L129) — `$dataTest` aux lignes [25](webhook.php#L25) et [35](webhook.php#L35)
- **Cause** : `$data` désigne tantôt l'input brut, tantôt un JSON encodé jamais utilisé, tantôt le résultat d'une requête SQL. `$dataTest` est un array puis devient une string.
- **Conséquence** : code difficile à relire, propice aux régressions.

### P24 — `$data = json_encode($event)` ligne 24 jamais écrit
- **Localisation** : [webhook.php:24](webhook.php#L24)
- **Code** : `$data = json_encode($event)."\n";`
- **Cause** : la variable `$data` est calculée à la ligne 24 mais l'écriture ligne 38 utilise `$dataTest` (le résumé), pas `$data` (l'event complet).
- **Conséquence** : CPU gaspillé sur un `json_encode` inutile + perte de l'enregistrement complet de l'event qu'on pensait écrire dans `log.txt`.

### P25 — Code dupliqué entre les deux branches `if/else` (lignes 106-148)
- **Localisation** : [webhook.php:106-148](webhook.php#L106-L148)
- **Cause** : la branche `idClient > 0` et la branche fallback sont quasi identiques (construction d'URL, appel HTTP, log).
- **Conséquence** : maintenance lourde, risque de divergence accidentelle entre les branches lors de futures modifications.

---

## 🟢 SÉVÉRITÉ FAIBLE (qualité de code et hygiène)

### P26 — Fichiers logs `.txt` à la racine, sans rotation
- **Localisation** : `log.txt`, `test.txt`, `mount.txt`, `unblock.txt`, `SucessW.txt`
- **Cause** : aucune rotation. Fichiers actuels : `log.txt` (3,7 MB), `unblock.txt` (1,4 MB), `test.txt` (1,3 MB).
- **Conséquence** : disque rempli à terme + lecture P5 de plus en plus lente.

### P27 — Hardcoding et qualité générale
- URL `http://13.49.185.225/...` codée en dur (pas en HTTPS) — [webhook.php:111](webhook.php#L111), [webhook.php:133](webhook.php#L133)
- Chemin Tesseract Windows codé en dur — [webhook.php:41](webhook.php#L41)
- `substr(str_replace(...))` dupliqué inutilement — [webhook.php:18](webhook.php#L18) et [webhook.php:27](webhook.php#L27)
- Mélange `require_once` et `include` ([webhook.php:6](webhook.php#L6) vs [webhook.php:9](webhook.php#L9))
- Aucun namespace, aucune fonction, tout en script linéaire
- Aucun typage, aucune validation centralisée

---

## Tableau de synthèse

| # | Problème | Sévérité | Impact principal |
|---|----------|----------|------------------|
| P1 | `image.jpeg` partagé (race condition) | 🔴 CRITIQUE | Multi-client cassé |
| P2 | Appel HTTP synchrone aval | 🔴 CRITIQUE | Lenteur + saturation workers |
| P3 | Pas de réponse rapide à UltraMsg | 🔴 CRITIQUE | Timeouts → rejeux |
| P4 | Pas d'idempotence | 🔴 CRITIQUE | Doubles paiements |
| P5 | Lecture inutile de `log.txt` (3,7 MB) | 🔴 CRITIQUE | Lenteur croissante |
| P6 | `LOCK_EX` sérialise les écritures | 🔴 CRITIQUE | Goulot d'étranglement |
| P7 | OCR Tesseract bloquant | 🟠 HAUTE | +1 à +5 s/requête |
| P8 | Double download de l'image S3 | 🟠 HAUTE | +1 s/requête |
| P9 | Pas de timeout cURL/file_get_contents | 🟠 HAUTE | Worker bloqué jusqu'à 60 s |
| P10 | Pas de try/catch sur Tesseract | 🟠 HAUTE | Crash silencieux |
| P11 | Pas de check du retour de `file_get_contents` | 🟠 HAUTE | Paiements perdus silencieusement |
| P12 | Pas de pooling PostgreSQL | 🟠 HAUTE | Saturation `max_connections` |
| P13 | Webhook public sans signature | 🟠 HAUTE | Risque sécurité |
| P14 | `$strArr[1]` non vérifié | 🟡 MOYENNE | Warnings + parsing faux |
| P15 | `$pieces[count-2]` non vérifié | 🟡 MOYENNE | Warnings |
| P16 | `pg_fetch_assoc` peut retourner false | 🟡 MOYENNE | Warnings + faux idClient |
| P17 | Appel unblock même sans client valide | 🟡 MOYENNE | Latence inutile |
| P18 | `count($strArr) > 0` toujours vrai | 🟡 MOYENNE | Logique trompeuse |
| P19 | Parsing montant fragile (token MRU) | 🟡 MOYENNE | Paiements perdus |
| P20 | `intval` après `is_numeric` | 🟡 MOYENNE | Montant tronqué |
| P21 | Dépendance `allow_url_fopen` | 🟡 MOYENNE | Fragilité config |
| P22 | Pas de gestion "message texte sans image" | 🟡 MOYENNE | Silencieux |
| P23 | Variable shadowing (`$data`, `$dataTest`) | 🟡 MOYENNE | Lisibilité |
| P24 | `json_encode($event)` jamais écrit | 🟡 MOYENNE | CPU + perte donnée |
| P25 | Code dupliqué if/else | 🟡 MOYENNE | Maintenance |
| P26 | Logs `.txt` sans rotation | 🟢 FAIBLE | Disque |
| P27 | Hardcoding (URL, chemin, etc.) | 🟢 FAIBLE | Maintenance |

---

## Conclusion pour l'équipe

La **cause racine** du symptôme rapporté ("ne traite pas plusieurs clients en même temps") est **P1** : le fichier `image.jpeg` est partagé entre toutes les requêtes simultanées. Tant que ce nom de fichier reste fixe, **aucune autre optimisation ne corrigera le problème de multi-client**.

La **cause racine de la lenteur** est la combinaison **P2 + P3 + P7 + P5** : le webhook fait tout en série (download + OCR + appel HTTP synchrone aval) et lit en plus 3,7 MB de log inutiles à chaque requête. Le temps de réponse réel par webhook est probablement de **15 à 40 secondes**, ce qui sature le pool de workers PHP-FPM dès quelques requêtes simultanées.

Les bugs P14 à P22 expliquent par ailleurs les paiements perdus ou faussés signalés ponctuellement, ainsi que les warnings PHP probables dans les logs serveur.

**Recommandation** : ne pas corriger ponctuellement, mais **refondre le pipeline** :
1. Réponse immédiate à UltraMsg (`fastcgi_finish_request`) dès réception.
2. Nom d'image unique par requête (ex. `tmp/img_<uniqid>.jpeg`).
3. Découplage : webhook ne fait que valider + enfiler dans une queue (DB ou Redis). Un worker dédié consomme la queue, fait l'OCR et appelle `remove_suspende_whatsapp.php`.
4. Idempotence : stocker l'ID UltraMsg du message pour rejeter les rejeux.
5. Suppression de la lecture de `log.txt` (P5) et rotation des logs.
6. Gestion d'erreur stricte sur Tesseract et les appels HTTP.
7. Validation du token UltraMsg en entrée.

Une fois ces points adressés, la latence par webhook devrait passer de 15-40 s à <500 ms, et le multi-client fonctionnera sans collision.
