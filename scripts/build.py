#!/usr/bin/env python3
"""Construit routes.json, airports.json et meta.json pour l'app.

Sources :
  - data/schedules.json : vols datés importés (horaires exacts), prioritaires
  - data/weekly.json    : programmes hebdomadaires importés (durée exacte ou estimée)
  - data/daily/*.json   : vols observés par OpenSky (toutes compagnies)
  - airports.csv        : OurAirports (OACI -> IATA, coordonnées, pays)
  - airlines.dat        : OpenFlights (OACI compagnie -> nom, code IATA)

Les durées OpenSky (premier/dernier contact ADS-B) sont converties en temps de bloc
estimé grâce à un décalage calibré sur les routes présentes à la fois dans les horaires importés et dans OpenSky.
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

    # 2. Horaires officiels importés — prioritaires sur OpenSky
    #    schedules.json : vols datés (horaires et durées exacts)
    #    weekly.json    : programmes hebdomadaires (durée exacte si heures locales fournies, sinon estimée)
    ops = {'AF': 'AFR', 'KL': 'KLM', 'DL': 'DAL', 'TO': 'TVF', 'HV': 'TRA', 'A5': 'HOP', 'WA': 'KLC'}
    iata_to_icao = {v[0]: k for k, v in airlines.items() if v[0] and v[2]}

    def to_icao(code):
        return code if len(code) == 3 else ops.get(code) or iata_to_icao.get(code, code)

    def load(name):
        p = ROOT / 'data' / name
        return json.loads(p.read_text()) if p.exists() else []

    sched = load('schedules.json')
    for r in sched:
        r['al'] = to_icao(r['al'])
        r['cs'] = r['al'] + r['fn'][2:] if len(r['al']) == 3 else r['cs']
    sc_index = {(r['al'], r['dep'], r['arr']): r for r in sched}

    weekly = []
    for r in load('weekly.json'):
        al = to_icao(r['op'])
        if (al, r['dep'], r['arr']) in sc_index:
            continue  # déjà couvert par les vols datés
        weekly.append({**r, 'al': al, 'cs': (al if len(al) == 3 else r['op']) + r['fn'][2:]})

    official = {(r['al'], r['dep'], r['arr']) for r in sched} | {(r['al'], r['dep'], r['arr']) for r in weekly}
    timed = {(r['al'], r['dep'], r['arr']): r['dur'] for r in sched}
    timed.update({(r['al'], r['dep'], r['arr']): r['dur'] for r in weekly if r['dur']})

    # 3. Calibrage temps observé -> temps de bloc sur les routes présentes dans les deux sources
    diffs, observed = [], {}
    for key, g in agg.items():
        al, d, a = key.split('|')
        da, aa = by_icao.get(d), by_icao.get(a)
        k = (al, da and da['iata'], aa and aa['iata'])
        observed[k] = g
        if g['n'] >= 2 and k in timed:
            diffs.append(timed[k] - robust_mean(g['dur']))
    offset = round(min(40, max(0, statistics.median(diffs)))) if len(diffs) >= 15 else 15

    # 4. Modèle distance -> temps de bloc, appris sur les durées officielles
    pts = []
    for (al, d, a), dur in timed.items():
        da, aa = by_iata.get(d), by_iata.get(a)
        if da and aa:
            pts.append((km(da, aa), dur))
    if len(pts) >= 30:
        mx, my = sum(x for x, _ in pts) / len(pts), sum(y for _, y in pts) / len(pts)
        slope = sum((x - mx) * (y - my) for x, y in pts) / sum((x - mx) ** 2 for x, _ in pts)
        inter = my - slope * mx
    else:
        slope, inter = 60 / 800, 30

    def courrier(da, aa):
        if not (da and aa):
            return 'MH'
        dist = km(da, aa)
        return 'SH' if dist < 1000 else 'MH' if dist < 4500 else 'LH'

    rows = []
    for r in sched:
        da, aa = by_iata.get(r['dep']), by_iata.get(r['arr'])
        code_of(da, r['dep'])
        code_of(aa, r['arr'])
        rows.append([r['al'], r['cs'], r['fn'], r['dep'], r['arr'], r['hdep'], r['dur'], r['ac'],
                     r['co'] or courrier(da, aa), route_region(da, aa), r['n'], 'G'])

    n_est = {'opensky': 0, 'modele': 0}
    for r in weekly:
        da, aa = by_iata.get(r['dep']), by_iata.get(r['arr'])
        if not (da and aa):
            continue
        code_of(da, r['dep'])
        code_of(aa, r['arr'])
        dur, hdep, ac, src = r['dur'], r['hdep'], r['ac'], 'G'
        if not dur:
            g = observed.get((r['al'], r['dep'], r['arr']))
            if g and g['n'] >= 2:
                dur, src = round(robust_mean(g['dur'])) + offset, 'E'
                hdep = hdep or mode_time(g['dep'])
                ac = ac or (g['ac'].most_common(1)[0][0] if g['ac'] else '')
                n_est['opensky'] += 1
            else:
                dur, src = round(inter + slope * km(da, aa)), 'E'
                n_est['modele'] += 1
        rows.append([r['al'], r['cs'], r['fn'], r['dep'], r['arr'], hdep, dur, ac,
                     courrier(da, aa), route_region(da, aa), r['n'], src])

    for key, g in agg.items():
        al, d, a = key.split('|')
        if g['n'] < args.min_n:
            continue
        da, aa = by_icao.get(d), by_icao.get(a)
        if not (da and aa) or (al, da['iata'], aa['iata']) in official:
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
            'model': {'min_fixe': round(inter), 'min_par_1000km': round(slope * 1000)}, 'estimees': n_est,
            'routes': len(rows), 'airlines': len(al_out), 'airports': len(used)}
    (ROOT / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == '__main__':
    main()
