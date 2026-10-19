#!/usr/bin/env python3
"""Collecte les vols réellement effectués la veille via l'API OpenSky Network.

Pour chaque jour UTC manquant dans data/daily/, récupère tous les vols observés,
garde ceux qui ont un indicatif de compagnie (3 lettres + numéro) et des aéroports
de départ/arrivée connus, puis enregistre un résumé par compagnie et par route :
nombre de vols, durées observées, heures de départ UTC, types d'avion, indicatifs.

Variables d'environnement : OPENSKY_CLIENT_ID, OPENSKY_CLIENT_SECRET
Usage : python3 scripts/collect.py [--days 3] [--aircraft-db aircraft.csv.gz]
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
MIN_FLIGHTS_PER_DAY = 2000  # en dessous, on considère que la journée n'est pas encore traitée par OpenSky

# Aéroports interrogés si /flights/all n'est pas accessible avec le compte (repli)
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


def fetch_day(api, day):
    begin = int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp())
    flights, left = [], None
    try:
        for i in range(12):  # /flights/all accepte au plus 2 h par requête
            chunk, left = api.get('/flights/all', begin=begin + i * 7200, end=begin + (i + 1) * 7200)
            flights.extend(chunk)
            log(f'  {day} tranche {i + 1}/12 : {len(chunk)} vols (crédits restants : {left})')
        return flights
    except urllib.error.HTTPError as e:
        if e.code not in (400, 403):
            raise
        log(f'  /flights/all refusé ({e.code}), repli sur {len(FALLBACK_AIRPORTS)} aéroports')
    flights = []
    for apt in FALLBACK_AIRPORTS:
        chunk, left = api.get('/flights/departure', airport=apt, begin=begin, end=begin + 86400)
        flights.extend(chunk)
        log(f'  {day} {apt} : {len(chunk)} départs (crédits restants : {left})')
    return flights


def summarize(flights, acdb):
    seen, routes = set(), defaultdict(lambda: {'n': 0, 'dur': [], 'dep': [], 'ac': Counter(), 'cs': Counter()})
    for f in flights:
        cs = (f.get('callsign') or '').strip().upper()
        m = CALLSIGN.match(cs)
        dep, arr = f.get('estDepartureAirport'), f.get('estArrivalAirport')
        t0, t1 = f.get('firstSeen'), f.get('lastSeen')
        if not (m and dep and arr and t0 and t1) or dep == arr:
            continue
        key_f = (f.get('icao24'), t0)
        if key_f in seen:
            continue
        seen.add(key_f)
        dur = round((t1 - t0) / 60)
        if not 15 <= dur <= 20 * 60:
            continue
        r = routes[f'{m.group(1)}|{dep}|{arr}']
        r['n'] += 1
        r['dur'].append(dur)
        r['dep'].append(dt.datetime.fromtimestamp(t0, dt.timezone.utc).hour * 60
                        + dt.datetime.fromtimestamp(t0, dt.timezone.utc).minute)
        r['cs'][cs] += 1
        t = acdb.get((f.get('icao24') or '').lower())
        if t:
            r['ac'][t] += 1
    return {k: {'n': v['n'], 'dur': v['dur'], 'dep': v['dep'],
                'ac': dict(v['ac']), 'cs': dict(v['cs'])} for k, v in routes.items()}, len(seen)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=3, help='nombre de jours passés à vérifier')
    ap.add_argument('--aircraft-db', default='aircraft.csv.gz')
    ap.add_argument('--keep', type=int, default=21, help='jours conservés dans data/daily')
    args = ap.parse_args()

    cid, secret = os.environ.get('OPENSKY_CLIENT_ID'), os.environ.get('OPENSKY_CLIENT_SECRET')
    if not (cid and secret):
        sys.exit('OPENSKY_CLIENT_ID / OPENSKY_CLIENT_SECRET manquants')
    api = OpenSky(cid, secret)
    acdb = load_aircraft_db(args.aircraft_db)
    DAILY.mkdir(parents=True, exist_ok=True)

    today = dt.datetime.now(dt.timezone.utc).date()
    todo = [today - dt.timedelta(days=i) for i in range(1, args.days + 1)]
    todo = [d for d in todo if not (DAILY / f'{d}.json').exists()]
    log(f'Jours à collecter : {[str(d) for d in todo] or "aucun"}')
    for day in todo:
        try:
            flights = fetch_day(api, day)
        except RuntimeError as e:
            log(f'Arrêt : {e}')
            break
        routes, n = summarize(flights, acdb)
        if n < MIN_FLIGHTS_PER_DAY and len(flights) < MIN_FLIGHTS_PER_DAY:
            log(f'  {day} : seulement {n} vols exploitables, journée ignorée (pas encore traitée ?)')
            continue
        (DAILY / f'{day}.json').write_text(json.dumps({'day': str(day), 'flights': n, 'routes': routes},
                                                      separators=(',', ':')))
        log(f'  {day} : {n} vols, {len(routes)} routes enregistrées')

    cutoff = today - dt.timedelta(days=args.keep)
    for p in DAILY.glob('*.json'):
        try:
            if dt.date.fromisoformat(p.stem) < cutoff:
                p.unlink()
        except ValueError:
            pass


if __name__ == '__main__':
    main()
