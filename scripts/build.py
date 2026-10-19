#!/usr/bin/env python3
"""Construit routes.json, airports.json et meta.json pour l'app.

Sources :
  - data/af_gpnet.json  : programme Air France (GPNet), prioritaire pour AF
  - data/daily/*.json   : vols observés par OpenSky (toutes compagnies)
  - airports.csv        : OurAirports (OACI -> IATA, coordonnées, pays)
  - airlines.dat        : OpenFlights (OACI compagnie -> nom, code IATA)

Les durées OpenSky (premier/dernier contact ADS-B) sont converties en temps de bloc
estimé grâce à un décalage calibré sur les routes Air France présentes dans les deux sources.
"""
import argparse
import csv
import datetime as dt
import json
import math
import statistics
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOM = {'GP', 'MQ', 'GF', 'RE', 'YT', 'PF', 'NC', 'PM', 'BL', 'MF', 'WF'}
MIDDLE_EAST = {'AE', 'SA', 'QA', 'IL', 'JO', 'LB', 'KW', 'BH', 'OM', 'IR', 'IQ', 'SY', 'YE', 'PS'}
CONTINENT = {'EU': 'europe', 'NA': 'ameriques', 'SA': 'ameriques', 'AF': 'afrique',
             'AS': 'asie', 'OC': 'oceanie', 'AN': 'oceanie'}
COLS = ['al', 'cs', 'fn', 'dep', 'arr', 'hdep', 'dur', 'ac', 'co', 'reg', 'n', 'src']


def load_airports(path):
    by_icao, by_iata = {}, {}
    with open(path, encoding='utf-8', newline='') as f:
        for r in csv.DictReader(f):
            if r['type'] in ('heliport', 'closed', 'balloonport', 'seaplane_base'):
                continue
            a = {'icao': r['icao_code'] or r['ident'], 'iata': r['iata_code'], 'name': r['name'],
                 'city': r['municipality'], 'country': r['iso_country'], 'cont': r['continent'],
                 'lat': float(r['latitude_deg']), 'lon': float(r['longitude_deg']),
                 'big': r['type'] == 'large_airport', 'sched': r['scheduled_service'] == 'yes'}
            for code in {r['ident'], r['icao_code'], r['gps_code']} - {''}:
                if code not in by_icao or (a['sched'] and not by_icao[code]['sched']):
                    by_icao[code] = a
            if r['iata_code'] and (r['iata_code'] not in by_iata or a['sched']):
                by_iata[r['iata_code']] = a
    return by_icao, by_iata


def load_airlines(path):
    out = {}
    with open(path, encoding='utf-8', newline='') as f:
        for r in csv.reader(f):
            if len(r) < 8 or len(r[4]) != 3 or not r[4].isalpha():
                continue
            icao, iata, name, active = r[4], r[3] if r[3] not in ('\\N', '-') else '', r[1], r[7] == 'Y'
            if icao not in out or (active and not out[icao][2]):
                out[icao] = (iata, name, active)
    return out


def region(a):
    if not a:
        return 'europe'
    if a['country'] == 'FR':
        return 'france'
    if a['country'] in DOM:
        return 'dom'
    if a['country'] in MIDDLE_EAST:
        return 'moyen-orient'
    return CONTINENT.get(a['cont'], 'europe')


def route_region(d, a):
    rd, ra = region(d), region(a)
    if rd == 'france' and ra != 'france':
        return ra
    if ra == 'france' and rd != 'france':
        return rd
    return ra


def km(a, b):
    p1, p2 = math.radians(a['lat']), math.radians(b['lat'])
    dl = math.radians(b['lon'] - a['lon'])
    return 6371 * math.acos(max(-1, min(1, math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl))))


def robust_mean(values):
    med = statistics.median(values)
    keep = [v for v in values if abs(v - med) <= max(10, 0.2 * med)]
    return sum(keep) / len(keep) if keep else med


def mode_time(minutes):
    c = Counter(5 * round(m / 5) % 1440 for m in minutes)
    m = c.most_common(1)[0][0]
    return f'{m // 60:02d}:{m % 60:02d}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--airports', default='airports.csv')
    ap.add_argument('--airlines', default='airlines.dat')
    ap.add_argument('--window', type=int, default=21)
    ap.add_argument('--min-n', type=int, default=2, help='vols observés minimum pour garder une route OpenSky')
    args = ap.parse_args()

    by_icao, by_iata = load_airports(args.airports)
    airlines = load_airlines(args.airlines)
    used = {}

    def code_of(a, fallback):
        if not a:
            return fallback
        c = a['iata'] or a['icao']
        used[c] = a
        return c

    # 1. Agrégation des journées OpenSky
    agg = {}
    days = sorted((ROOT / 'data' / 'daily').glob('*.json'))[-args.window:]
    for p in days:
        for key, r in json.loads(p.read_text())['routes'].items():
            g = agg.setdefault(key, {'n': 0, 'dur': [], 'dep': [], 'ac': Counter(), 'cs': Counter()})
            g['n'] += r['n']
            g['dur'] += r['dur']
            g['dep'] += r['dep']
            g['ac'].update(r['ac'])
            g['cs'].update(r['cs'])

    # 2. Programme AF (GPNet)
    gp_path = ROOT / 'data' / 'af_gpnet.json'
    gpnet = json.loads(gp_path.read_text()) if gp_path.exists() else []
    gp_index = {(r['dep'], r['arr']): r for r in gpnet}

    # 3. Calibrage temps observé -> temps de bloc sur les routes AF communes
    diffs = []
    for key, g in agg.items():
        al, d, a = key.split('|')
        if al != 'AFR' or g['n'] < 2:
            continue
        da, aa = by_icao.get(d), by_icao.get(a)
        gp = gp_index.get((da and da['iata'], aa and aa['iata']))
        if gp:
            diffs.append(gp['dur'] - robust_mean(g['dur']))
    offset = round(min(40, max(0, statistics.median(diffs)))) if len(diffs) >= 15 else 15

    rows = []
    for r in gpnet:
        da, aa = by_iata.get(r['dep']), by_iata.get(r['arr'])
        code_of(da, r['dep'])
        code_of(aa, r['arr'])
        rows.append(['AFR', r['cs'], r['fn'], r['dep'], r['arr'], r['hdep'], r['dur'], r['ac'],
                     r['co'], route_region(da, aa), r['n'], 'G'])

    for key, g in agg.items():
        al, d, a = key.split('|')
        if g['n'] < args.min_n or (al == 'AFR' and gpnet):
            continue
        da, aa = by_icao.get(d), by_icao.get(a)
        if not (da and aa):
            continue
        dist = km(da, aa)
        if dist < 80:
            continue
        cs = g['cs'].most_common(1)[0][0]
        iata = airlines.get(al, ('', '', False))[0]
        rows.append([al, cs, (iata + cs[3:]) if iata else '', code_of(da, d), code_of(aa, a),
                     mode_time(g['dep']), round(robust_mean(g['dur'])) + offset,
                     g['ac'].most_common(1)[0][0] if g['ac'] else '',
                     'SH' if dist < 1000 else 'MH' if dist < 4500 else 'LH',
                     route_region(da, aa), g['n'], 'A'])

    rows.sort(key=lambda r: (r[0], r[3], r[4]))
    present = Counter(r[0] for r in rows)
    al_out = {k: [airlines.get(k, ('', k, False))[1], airlines.get(k, ('', '', False))[0], present[k]]
              for k in present}

    (ROOT / 'routes.json').write_text(json.dumps({'cols': COLS, 'rows': rows, 'airlines': al_out},
                                                 ensure_ascii=False, separators=(',', ':')))
    (ROOT / 'airports.json').write_text(json.dumps(
        {c: [round(a['lat'], 3), round(a['lon'], 3), a['name'], a['city'], a['country']] for c, a in used.items()},
        ensure_ascii=False, separators=(',', ':')))
    meta = {'updated': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
            'days': [p.stem for p in days], 'offset_min': offset, 'calib_routes': len(diffs),
            'routes': len(rows), 'airlines': len(al_out), 'airports': len(used)}
    (ROOT / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == '__main__':
    main()
