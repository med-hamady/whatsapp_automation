# Formats standards des captures de paiement

Synthèse des formats observés sur les 188 captures de `D:\facture` (échantillon de 20). Ce document est la **spécification d'entrée** du module [ai_ocr/](../ai_ocr/) : tout extracteur doit savoir gérer chacune des variantes ci-dessous.

---

## 1. Vue d'ensemble

| Source | Variantes UI | Langues observées |
|---|---|---|
| **Bankily** | Modal popup (FR/AR), Notification push (FR/AR), Ligne d'historique (FR), Transfert P2P (FR/AR) | Français, Arabe |
| **Masrvi** | Modal popup "Succès" | Français |
| **Sedad** | Page pleine résultat | Français (UI) + logo arabe |

> Note : aucune capture **Bankily anglais** dans `D:\facture`, mais la variante existe (cf. `ai_ocr/tests/fixtures/samples.json`, sample `test_txt_l2`). À conserver dans la couverture.

---

## 2. Champs cibles (sortie attendue)

```json
{
  "montant": 990,
  "devise": "MRU",
  "txn_id": "0526050214011846872",
  "date_heure": "2026-05-02T14:01:20",
  "template": "bankily" | "masrvi" | "sedad",
  "merchant_id": "016456",
  "merchant_name": "PATRINET NKTT"
}
```

`montant` est toujours un entier MRU (les `.00` ou `,00` sont décoratifs — pas de décimales métier).

---

## 3. Bankily

Marqueurs visuels : couleur cyan/teal, icône "PN" verte, logo "Bankily" jaune-vert.
Constantes : `PATRINET NKTT` + `Code/معرف التاجر: 016456`.

### 3.1 Modal "Paiement réussi" — Français
**Exemple** : `WhatsApp Image 2026-05-06 at 13.30.00.jpeg`

```
Paiement réussi!
[✓]
Nom du commerçant: PATRINET NKTT
Identifiant du marchand: 016456
Montant payé: MRU 990
Trs ID : 0526050214011846872
Date et heure: 02-05-26 14:01:20
[Effectué]
```

- **Montant** : motif `MRU\s+(\d[\d\s]*)`.
- **Trs ID** : 19 chiffres après `Trs ID :`.
- **Date** : `DD-MM-YY HH:MM:SS`.

### 3.2 Modal "الدفع الناجح" — Arabe
**Exemple** : `WhatsApp Image 2026-05-06 at 13.29.40.jpeg`

```
الدفع الناجح!
[✓]
اسم التاجر : PATRINET NKTT
معرف التاجر: 016456
المبلغ المدفوع: MRU 990
معرف المعاملة : 1326050317382130803
التاريخ والوقت : 17:38:23 26-05-03
```

- Même mise en page, libellés en arabe. Les **valeurs restent en latin** (chiffres + "MRU" + "PATRINET").
- **Date inversée RTL** : l'OCR peut sortir `17:38:23 26-05-03` (heure puis date) ou `26-05-03 17:38:23` selon le moteur. Format année possiblement inversé : `YY-MM-DD` (ex: `26-05-03` = 3 mai 2026).
- Bouton final : `تم` ou `فعله!`.

### 3.3 Notification push (iOS) — superposée en haut d'écran
**Exemples** : `13.29.36 (1).jpeg`, `13.29.42 (2).jpeg`, `13.30.08 (4).jpeg`, `13.29.57 (3).jpeg`

```
[Bankily logo]  تسديد مشتريات       maintenant / now
                1490.0 MRU
                التاجر : ... PATRINET...
```
ou en français :
```
[Bankily logo]  Paiement commerçant   maintenant
                Montant : 1190.0 MRU
                Commerçant : PATRINET...
```

- **Information partielle** : seuls montant + commerçant (PATRINET tronqué). Pas de Trs ID ni de date détaillée — `maintenant` / `now` / `02:45` (= il y a X minutes).
- **À fusionner** avec le modal en arrière-plan si présent ; sinon retourner `txn_id: null` et `date_heure: null` (et abaisser `confidence`).
- **Piège** : la **barre de statut iOS** (ex `09:43`) n'est PAS l'heure de transaction.

### 3.4 Ligne d'historique condensée
**Exemple** : `WhatsApp Image 2026-05-06 at 13.29.38 (3).jpeg`

```
BKL-Paiement Commerçant            Dr 1 490 MRU
PATRINET NKTT                      03-05-26 15:54:46
Trs id: 0526050315544574651
```

- Préfixe `BKL-` et `Dr` (= Débit) à reconnaître.
- Montant à droite en rouge ; séparateur de milliers = espace.
- Format compact, 3 lignes seulement.

### 3.5 Transfert P2P (pas paiement marchand) — Français
**Exemple** : `WhatsApp Image 2026-05-06 at 13.29.41 (3).jpeg`

```
Transfert réussi!
[✓]
Receveur: 34610101
Montant envoyé: 500 MRU
Trs ID : 0126041512440737430
Date et heure: 15-04-26 12:44:09
[Effectué]
```

### 3.6 Transfert P2P — Arabe
**Exemple** : `WhatsApp Image 2026-05-06 at 13.29.57 (3).jpeg`

```
النقل ناجح!
[✓]
المستفيد: 34610101  المبلغ المرسل: 990 MRU
معرف المعاملة : 0526050212181390821
التاريخ والوقت : 26-05-02 12:18:15
```

> ✅ **Décision métier** : les transferts P2P sont **valides** (un client peut payer en envoyant directement de l'argent au numéro de l'agent au lieu d'utiliser le code marchand). Le receveur est un numéro de téléphone (`34610101`) et non `PATRINET`. L'extracteur doit récupérer `montant`, `txn_id`, `date_heure` comme pour un paiement marchand.

---

## 4. Masrvi

Marqueurs : popup blanc compact, titre `Succès` en arrière-plan, bouton `OK` cyan.
Pas de logo Masrvi visible — uniquement le texte du popup.

**Exemple** : `WhatsApp Image 2026-05-06 at 13.29.48 (2).jpeg`

```
Succès
Paiement facture depuis compte
1000.00 MRU (Montant: 1000.00 MRU, frais : 0.00 MRU, taxes : 0.00 MRU)
payé chez A2 CONNECT 019370 (REF218461476).
[OK]
```

- **Montant** : prendre le **premier** `XXXX.XX MRU` avant la parenthèse (les autres sont frais/taxes à 0).
- **Commerçant variable** : `PATRINET NETWORKING` ou `A2 CONNECT` (différents agents commerçants).
- **Code marchand** : `019370` (différent du Bankily `016456`).
- **Trs ID** : `REF` + chiffres entre crochets ou parenthèses.
- **Pas de date** dans le modal — uniquement l'heure de la barre de statut (à ignorer).

---

## 5. Sedad

Marqueurs : page pleine fond blanc/bleu clair, **logo "بنك السداد / SEDAD BANK BY BMI"** en haut.

**Exemple** : `WhatsApp Image 2026-05-06 at 13.29.45.jpeg`

```
[Logo بنك السداد / SEDAD BANK BY BMI]
[✓]
Vous avez payé 990,00 MRU à
PATRIE NET

Code commerçant         01471
Date de paiement        27-04-2026 12:14:37
ID de la transaction    TR06186990242

[Retour à l'accueil]
```

- **Format année 4 chiffres** : `27-04-2026` (vs Bankily/Masrvi en YY).
- **Séparateur décimal** : virgule (`990,00`) au lieu de point.
- **Trs ID** préfixé `TR` + chiffres.
- Commerçant = `PATRIE NET` (≠ Bankily `PATRINET NKTT` ≠ Masrvi `PATRINET NETWORKING`).
- Code commerçant = `01471` (5 chiffres, pas 6).

---

## 6. Pièges OCR récurrents

| Piège | Exemple | Mitigation |
|---|---|---|
| Heure barre de statut prise pour heure transaction | `09:43` en haut + `13:59:29` dans modal | Toujours préférer l'heure proche du label `Date et heure` / `Date de paiement` |
| Notification push superposée au modal | `09:43 \| Paiement commerçant 1190 MRU \| ... \| Paiement réussi! ... 1190 MRU` | Si les deux contiennent le même montant → garder le modal (plus complet). Sinon prioriser le modal. |
| Format date YY-MM-DD vs DD-MM-YY | `26-05-02` vs `02-05-26` | Détecter par contexte : si le 1er groupe = année courante (`26`) → YY-MM-DD ; sinon DD-MM-YY |
| Séparateur de milliers | `MRU 1 500`, `MRU 1500`, `1500.00`, `1.500` | Strip espaces et points internes avant `int()` |
| Décimales décoratives | `1490.00`, `990,00`, `1490.0` | Tronquer à l'entier |
| Numéro client en surimpression | `36669112` ajouté en bas de la capture 1 | Ignorer tout texte hors zone du modal central |
| `MRU` en arabe : `أوقية` | `1500.0 أوقية` (notification) | Accepter les deux comme devise |
| Caractères mal reconnus | `é→e`, `ç→g`, `i→l/I/!`, `0→O`, `B→8`, `Identifiant→Idemiﬂant` | Tolérance dans les regex (classes de caractères larges) |
| Transferts P2P (paiement valide) | `Transfert réussi!` / `النقل ناجح!` + receveur = numéro téléphone | Extraire normalement (montant, txn_id, date) — déblocage à déclencher |
| Bankily anglais (existe mais hors `D:\facture`) | `Payment Successful! ... Amount Paid : MRU 1500 ... Txn ID: ... Date & Time:` | À traiter comme variante 3.x supplémentaire |

---

## 7. Règles de décision (template detection)

Ordre de priorité — premier match gagne :

1. **Sedad** : présence de `SEDAD` ou `السداد` ou `BMI` → `template = sedad`.
2. **Masrvi** : présence de `Paiement facture depuis compte` ou `payé chez ... (REF` → `template = masrvi`.
3. **Bankily transfert P2P** : `Transfert réussi` ou `النقل ناجح` → `template = bankily_p2p` (paiement valide, extraction standard).
4. **Bankily** : présence de `Paiement réussi` / `Payment Successful` / `الدفع الناجح` ou `BKL-` ou `016456` → `template = bankily`.
5. **Sinon** : `template = unknown`, `confidence.overall = 0`, garder `raw_text` pour annotation manuelle.

---

## 8. Couverture actuelle vs cible

| Variante | Présent dans `samples.json` ? | Présent dans `D:\facture` ? | Action |
|---|---|---|---|
| Bankily modal FR | ✅ (7 ex.) | ✅ | OK |
| Bankily modal AR | ❌ | ✅ (majoritaire) | **Ajouter** échantillons |
| Bankily modal EN | ✅ (1 ex.) | ❌ | OK (déjà couvert) |
| Bankily notification push | ❌ | ✅ | **Ajouter** + décider stratégie (fusion modal ?) |
| Bankily ligne historique | ❌ | ✅ | **Ajouter** |
| Bankily transfert P2P | ❌ | ✅ | **Ajouter** échantillons + extracteur dédié |
| Masrvi FR | ✅ (2 ex.) | ✅ | OK |
| Sedad FR/mixte | ✅ (1 ex.) | ✅ | OK (en élargir) |

---

## 9. Plan d'attaque proposé

1. **Étendre `samples.json`** avec les variantes manquantes ci-dessus (AR, push, historique, P2P).
2. **Ajouter `bankily_p2p` template** + règle de rejet métier dans l'extracteur.
3. **Coder la fusion notification + modal** (ou simple priorisation).
4. **Adapter les regex de date** pour gérer YY-MM-DD ↔ DD-MM-YY (Bankily) et DD-MM-YYYY (Sedad).
5. **Pré-traitement OpenCV** : binarisation Otsu + recadrage sur la zone modal centrale (réduit le bruit barre de statut + notif).
