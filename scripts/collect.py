#!/usr/bin/env python3
"""Collecte les vols réellement effectués via l'API OpenSky Network.

À chaque passage, interroge les départs des 2 derniers jours pour un groupe de 125 aéroports
(sur une liste de 250 parcourue en rotation : chaque aéroport est donc couvert sans trou).
Garde les vols avec un indicatif de compagnie (3 lettres + numéro) et des aéroports connus,
et enregistre un résumé par jour, compagnie et route : nombre de vols, durées observées,
heures de départ UTC, types d'avion, indicatifs.

Variables d'environnement : OPENSKY_CLIENT_ID, OPENSKY_CLIENT_SECRET
Usage : python3 scripts/collect.py [--budget 3900] [--aircraft-db aircraft.csv.gz]
"""
import argparse
import datetime as dt
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

API = 'https://opensky-network.org/api'
TOKEN_URL = 'https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token'
ROOT = Path(__file__).resolve().parent.parent
DAILY = ROOT / 'data' / 'daily'
CALLSIGN = re.compile(r'^([A-Z]{3})(\d[A-Z0-9]{0,4})$')

# Aéroports toujours interrogés (le reste de la liste vient des données déjà collectées)
FALLBACK_AIRPORTS = """
LFPG LFPO LFMN LFML LFLL LFBO LFBD LFRS LFSB LFST LFRN LFMP LFKJ LFKB
EGLL EGKK EGSS EGCC EHAM EDDF EDDM EDDB EDDL EDDH LEMD LEBL LEPA LEMG LPPT LPPR
LIRF LIMC LIPZ LSZH LSGG LOWW EBBR EKCH ESSA ENGM EFHK EPWA LKPR LHBP LGAV LTFM
EIDW BIKF LMML LCLK
KJFK KEWR KBOS KIAD KATL KMIA KMCO KORD KDFW KIAH KDEN KLAX KSFO KSEA KLAS KPHX KMSP KDTW
CYUL CYYZ CYVR MMMX MMUN MPTO SKBO SPJC SCEL SAEZ SBGR SBGL TFFF TFFR SOCA
OMDB OMAA OTHH OERK OEJN LLBG HECA GMMN DTTA DAAG
FAOR FACT HKJK DNMM DIAP GOBD FMEE FIMP FMMI
VIDP VABB WSSS VHHH VTBS RPLL ZBAA ZSPD RJTT RJAA RJBB RKSI WIII YSSY YMML NZAA NTAA NWWW
""".split()


def log(*a):
    print(*a, flush=True)


class OpenSky:
    def __init__(self, cid, secret):
        self.cid, self.secret = cid, secret
        self.token, self.exp = None, 0

    def _auth(self):
        if self.token and time.time() < self.exp - 60:
            return self.token
        data = urllib.parse.urlencode({'grant_type': 'client_credentials',
                                       'client_id': self.cid, 'client_secret': self.secret}).encode()
        with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data), timeout=30) as r:
            j = json.load(r)
        self.token = j['access_token']
        self.exp = time.time() + int(j.get('expires_in', 1800))
        return self.token

    def get(self, path, **params):
        url = f'{API}{path}?{urllib.parse.urlencode(params)}'
        for attempt in range(4):
            req = urllib.request.Request(url, headers={'Authorization': f'Bearer {self._auth()}'})
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    left = r.headers.get('X-Rate-Limit-Remaining')
                    body = r.read()
                    return (json.loads(body) if body.strip() else []), left
            except urllib.error.HTTPError as e:
                if e.code == 404:  # OpenSky répond 404 quand il n'y a aucun vol
                    return [], None
                if e.code == 429:
                    wait = int(e.headers.get('X-Rate-Limit-Retry-After-Seconds', '0') or 0)
                    raise RuntimeError(f'Quota OpenSky atteint (réessayer dans {wait // 3600}h{wait % 3600 // 60:02d})')
                if e.code == 401 and attempt == 0:
                    self.token = None
                    continue
                if e.code >= 500 and attempt < 3:
                    time.sleep(10 * (attempt + 1))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError):
                if attempt < 3:
                    time.sleep(10 * (attempt + 1))
                    continue
                raise


def load_aircraft_db(path):
    """icao24 (hex minuscule) -> code type OACI, depuis tar1090-db (aircraft.csv.gz)."""
    db = {}
    if not path or not Path(path).exists():
        log('Base avions absente : types non renseignés')
        return db
    with gzip.open(path, 'rt', encoding='utf-8', errors='replace') as f:
        for line in f:
            p = line.split(';')
            if len(p) > 2 and p[2]:
                db[p[0].lower()] = p[2]
    log(f'Base avions : {len(db)} immatriculations')
    return db


COST = 30          # crédits OpenSky pour une requête sur 1 ou 2 jours passés
GROUP = 125        # aéroports interrogés par passage (125 x 30 = 3 750 crédits, quota 4 000/jour)
N_AIRPORTS = 2 * GROUP
STATE = ROOT / 'data' / 'collect_state.json'


def airport_list():
    """Les aéroports qui ont le plus de départs dans les données déjà collectées, complétés par la liste fixe."""
    count = Counter()
    for p in DAILY.glob('*.json'):
        for key, r in json.loads(p.read_text())['routes'].items():
            count[key.split('|')[1]] += r['n']
    ranked = [a for a, _ in count.most_common()]
    out = list(dict.fromkeys(FALLBACK_AIRPORTS + ranked))
    head = [a for a in out if a in FALLBACK_AIRPORTS]
    rest = [a for a in out if a not in FALLBACK_AIRPORTS]
    return (head + rest)[:N_AIRPORTS]


def summarize(flights, acdb):
    """Regroupe les vols par jour UTC de départ, puis par compagnie|départ|arrivée."""
    seen = set()
    days = defaultdict(lambda: defaultdict(lambda: {'n': 0, 'dur': [], 'dep': [], 'ac': Counter(), 'cs': Counter()}))
    for f in flights:
        cs = (f.get('callsign') or '').strip().upper()
        m = CALLSIGN.match(cs)
        dep, arr = f.get('estDepartureAirport'), f.get('estArrivalAirport')
        t0, t1 = f.get('firstSeen'), f.get('lastSeen')
        if not (m and dep and arr and t0 and t1) or dep == arr:
            continue
        # dernier contact encore en croisière : l'aéroport d'arrivée estimé n'est pas fiable
        if (f.get('estArrivalAirportVertDistance') or 0) > 6000:
            continue
        key_f = (f.get('icao24'), t0)
        if key_f in seen:
            continue
        seen.add(key_f)
        dur = round((t1 - t0) / 60)
        if not 15 <= dur <= 20 * 60:
            continue
        t = dt.datetime.fromtimestamp(t0, dt.timezone.utc)
        r = days[str(t.date())][f'{m.group(1)}|{dep}|{arr}']
        r['n'] += 1
        r['dur'].append(dur)
        r['dep'].append(t.hour * 60 + t.minute)
        r['cs'][cs] += 1
        ty = acdb.get((f.get('icao24') or '').lower())
        if ty:
            r['ac'][ty] += 1
    return {d: {k: {'n': v['n'], 'dur': v['dur'], 'dep': v['dep'], 'ac': dict(v['ac']), 'cs': dict(v['cs'])}
                for k, v in routes.items()} for d, routes in days.items()}


def merge_airport(day, airport, routes):
    """Remplace, dans data/daily/<jour>.json, les départs de cet aéroport par ceux qu'on vient de récupérer."""
    p = DAILY / f'{day}.json'
    data = json.loads(p.read_text()) if p.exists() else {'day': day, 'routes': {}}
    data['routes'] = {k: v for k, v in data['routes'].items() if k.split('|')[1] != airport}
    data['routes'].update(routes)
    data['flights'] = sum(r['n'] for r in data['routes'].values())
    data.setdefault('airports', [])
    if airport not in data['airports']:
        data['airports'].append(airport)
    p.write_text(json.dumps(data, separators=(',', ':')))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=2, help='(ignoré, conservé pour compatibilité)')
    ap.add_argument('--aircraft-db', default='aircraft.csv.gz')
    ap.add_argument('--keep', type=int, default=21, help='jours conservés dans data/daily')
    ap.add_argument('--budget', type=int, default=3900, help='crédits OpenSky utilisables par passage')
    args = ap.parse_args()

    cid, secret = os.environ.get('OPENSKY_CLIENT_ID'), os.environ.get('OPENSKY_CLIENT_SECRET')
    if not (cid and secret):
        sys.exit('OPENSKY_CLIENT_ID / OPENSKY_CLIENT_SECRET manquants')
    api = OpenSky(cid, secret)
    acdb = load_aircraft_db(args.aircraft_db)
    DAILY.mkdir(parents=True, exist_ok=True)

    today = dt.datetime.now(dt.timezone.utc).date()
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    if not state.get('airports') or state.get('built', '') < str(today - dt.timedelta(days=14)):
        state = {'airports': airport_list(), 'cursor': 0, 'built': str(today)}
        log(f'Nouvelle liste de {len(state["airports"])} aéroports')
    apts = state['airports']

    # Fenêtre : avant-hier 00:00 -> hier 23:59:59 UTC (2 partitions = 30 crédits par aéroport)
    end = int(dt.datetime(today.year, today.month, today.day, tzinfo=dt.timezone.utc).timestamp()) - 1
    begin = end + 1 - 2 * 86400
    spent, done = 0, 0
    while done < GROUP and spent + COST <= args.budget:
        apt = apts[state['cursor'] % len(apts)]
        try:
            flights, left = api.get('/flights/departure', airport=apt, begin=begin, end=end)
        except RuntimeError as e:
            log(f'Arrêt : {e}')
            break
        except urllib.error.HTTPError as e:
            log(f'  {apt} : erreur HTTP {e.code}, ignoré')
            flights, left = [], None
        spent += COST
        by_day = summarize(flights, acdb)
        for day in (str(today - dt.timedelta(days=2)), str(today - dt.timedelta(days=1))):
            merge_airport(day, apt, by_day.get(day, {}))
        n = sum(r['n'] for d in by_day.values() for r in d.values())
        log(f'  {apt} : {len(flights)} départs, {n} vols de ligne retenus (crédits restants : {left})')
        state['cursor'] = (state['cursor'] + 1) % len(apts)
        done += 1
        if left is not None and int(left) < COST * 2:
            log('Quota du jour presque épuisé, arrêt')
            break
    STATE.write_text(json.dumps(state, indent=1))
    log(f'{done} aéroports interrogés, ~{spent} crédits utilisés, prochain départ au n°{state["cursor"]}')

    cutoff = today - dt.timedelta(days=args.keep)
    for p in DAILY.glob('*.json'):
        try:
            if dt.date.fromisoformat(p.stem) < cutoff:
                p.unlink()
        except ValueError:
            pass


if __name__ == '__main__':
    main()
