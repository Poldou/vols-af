# Planificateur Vol

App web (PWA) pour trouver un vol réel qui colle à un créneau : « je pars à 19h, j'arrive entre 22h et 23h ».

- Toutes les autres compagnies : vols réellement effectués, collectés chaque mois via l'API OpenSky Network.
  La durée affichée est un temps de bloc estimé : temps observé en ADS-B + un décalage calibré automatiquement
  sur les routes présentes dans les horaires importés et dans OpenSky (voir `meta.json`).

## Mise à jour automatique

Le workflow `.github/workflows/update.yml` tourne les 1er, 2 et 3 de chaque mois. Il interroge les départs de 250 aéroports
(125 par passage, limite du quota gratuit OpenSky), garde 4 mois d'historique dans `data/daily/`
et régénère `routes.json`, `airports.json` et `meta.json`. Une fois les 250 aéroports faits, les passages suivants du mois s'arrêtent seuls.
Pour forcer une collecte : modifier `.github/run-now`.

Secrets à créer dans *Settings → Secrets and variables → Actions* :

| Nom | Valeur |
|---|---|
| `OPENSKY_CLIENT_ID` | `clientId` du fichier credentials.json OpenSky |
| `OPENSKY_CLIENT_SECRET` | `clientSecret` du même fichier |

## Sources

OpenSky Network (vols), OurAirports (aéroports), OpenFlights (compagnies), tar1090-db (types d'avion).
