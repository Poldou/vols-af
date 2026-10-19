# Vols réels

App web (PWA) pour trouver un vol réel qui colle à un créneau : « je pars à 19h, j'arrive entre 22h et 23h ».

- **Air France, HOP! et compagnies importées** : programmes officiels issus de GPNet (`data/gpnet.json`), temps de bloc exacts.
- **Toutes les autres compagnies** : vols réellement effectués, collectés chaque nuit via l'API OpenSky Network.
  La durée affichée est un temps de bloc estimé : temps observé en ADS-B + un décalage calibré automatiquement
  sur les routes Air France présentes dans les deux sources (voir `meta.json`).

## Mise à jour automatique

Le workflow `.github/workflows/update.yml` tourne chaque jour. Il collecte les vols de la veille,
garde une fenêtre glissante de 21 jours dans `data/daily/` et régénère `routes.json`, `airports.json` et `meta.json`.

Secrets à créer dans *Settings → Secrets and variables → Actions* :

| Nom | Valeur |
|---|---|
| `OPENSKY_CLIENT_ID` | `clientId` du fichier credentials.json OpenSky |
| `OPENSKY_CLIENT_SECRET` | `clientSecret` du même fichier |

Premier remplissage : onglet *Actions* → *Mise à jour des vols* → *Run workflow* (rattrape 7 jours par défaut).

## Mettre à jour les programmes GPNet (AF, HOP!, KLM, Delta, Transavia…)

Sur ton ordinateur, avec les exports GPNet (ne pas les publier sur GitHub) :

```
python3 scripts/gpnet_to_json.py export_AF_ete.zip export_AF_hiver.zip export_KL.zip export_DL.zip ...
```

puis publier `data/gpnet.json`. Le prochain passage du workflow reconstruit l'app.

## Sources

OpenSky Network (vols), OurAirports (aéroports), OpenFlights (compagnies), tar1090-db (types d'avion).
