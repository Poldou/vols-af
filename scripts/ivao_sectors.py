#!/usr/bin/env python3
"""Télécharge les limites des secteurs IVAO (centres en route et approches) et écrit data/ivao_sectors.json.

Les positions ouvertes changent sans cesse, mais leurs limites très peu : l'app combine ce fichier
avec le flux public des contrôleurs en ligne pour savoir quelles zones un vol traverse.

Variable d'environnement : IVAO_API_KEY (secret GitHub). La clé n'est jamais écrite dans un fichier.
Usage : python3 scripts/ivao_sectors.py
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = 'https://api.ivao.aero'
OUT = Path(__file__).resolve().parent.parent / 'data' / 'ivao_sectors.json'
KEY = os.environ.get('IVAO_API_KEY', '').strip()


def log(*a):
    print(*a, flush=True)


def shape(o, depth=0):
    """Résumé de la structure d'une réponse, pour le journal (sans données sensibles)."""
    if depth > 3:
        return '…'
    if isinstance(o, dict):
        return {k: shape(v, depth + 1) for k, v in list(o.items())[:25]}
    if isinstance(o, list):
        return [shape(o[0], depth + 1), f'({len(o)} éléments)'] if o else []
    return type(o).__name__ if not isinstance(o, (int, float, str)) or depth < 2 else o


AUTH_STYLES = [
    lambda url: (url, {'apiKey': KEY}),
    lambda url: (url + ('&' if '?' in url else '?') + 'apiKey=' + urllib.parse.quote(KEY), {}),
    lambda url: (url, {'X-API-Key': KEY}),
    lambda url: (url, {'Authorization': f'Bearer {KEY}'}),
]
auth_ok = None


def get(path, **params):
    global auth_ok
    url = f'{API}{path}' + ('?' + urllib.parse.urlencode(params) if params else '')
    styles = [auth_ok] if auth_ok is not None else range(len(AUTH_STYLES))
    last = None
    for i in styles:
        u, h = AUTH_STYLES[i](url)
        req = urllib.request.Request(u, headers={**h, 'Accept': 'application/json', 'User-Agent': 'planificateur-vol'})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                auth_ok = i
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = f'HTTP {e.code}'
            log(f'  {path} (auth {i}) : {last}')
            if e.code not in (401, 403):
                break
    raise RuntimeError(f'{path} : {last}')


def items_of(j):
    if isinstance(j, list):
        return j
    if isinstance(j, dict):
        for k in ('items', 'data', 'results'):
            if isinstance(j.get(k), list):
                return j[k]
    return []


def fetch_all(path, **params):
    j = get(path, **params)
    out = items_of(j)
    pages = j.get('pages') if isinstance(j, dict) else None
    if pages and pages > 1:
        for p in range(2, pages + 1):
            out += items_of(get(path, page=p, **params))
    log(f'{path} : {len(out)} éléments')
    if out:
        log('  exemple :', json.dumps(shape(out[0]), ensure_ascii=False)[:1500])
    return out


def to_poly(v):
    """Accepte [{lat,lng}], [[lng,lat]], GeoJSON Polygon/MultiPolygon ; renvoie une liste de polygones [[lat,lon],...]."""
    if not v:
        return []
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    if isinstance(v, dict):
        if v.get('type') == 'Polygon':
            return [[[round(p[1], 3), round(p[0], 3)] for p in ring] for ring in v['coordinates'][:1]]
        if v.get('type') == 'MultiPolygon':
            return [[[round(p[1], 3), round(p[0], 3)] for p in poly[0]] for poly in v['coordinates']]
        for k in ('coordinates', 'points', 'regionMap'):
            if k in v:
                return to_poly(v[k])
        return []
    if isinstance(v, list) and v:
        first = v[0]
        if isinstance(first, dict):
            pts = []
            for p in v:
                la = p.get('lat', p.get('latitude'))
                lo = p.get('lng', p.get('lon', p.get('longitude')))
                if la is not None and lo is not None:
                    pts.append([round(float(la), 3), round(float(lo), 3)])
            return [pts] if len(pts) > 2 else []
        if isinstance(first, (list, tuple)) and first and isinstance(first[0], (int, float)):
            return [[[round(p[1], 3), round(p[0], 3)] for p in v]] if len(v) > 2 else []   # GeoJSON : [lng, lat]
        if isinstance(first, (list, tuple)):
            out = []
            for sub in v:
                out += to_poly(sub)
            return out
    return []


def poly_of(o):
    for k in ('regionMapPolygon', 'regionMap', 'polygon', 'geometry'):
        if o.get(k):
            p = to_poly(o[k])
            if p:
                return p
    return []


def main():
    if not KEY:
        sys.exit('IVAO_API_KEY manquant')
    by_cs, n_sub, n_pos = {}, 0, 0

    for o in fetch_all('/v2/subcenters/all', mapType='regionMapPolygon'):
        polys = poly_of(o)
        cs = (o.get('composePosition') or o.get('callsign') or '').upper()
        if polys and cs:
            by_cs[cs] = {'p': polys, 'c': (o.get('centerId') or cs.split('_')[0]).upper(), 't': 'CTR'}
            n_sub += 1

    try:
        positions = fetch_all('/v2/ATCPositions/all', mapType='regionMapPolygon', loadAirport='false')
    except RuntimeError as e:
        log('Positions d\'aéroport indisponibles :', e)
        positions = []
    for o in positions:
        pos = (o.get('position') or '').upper()
        if pos not in ('APP', 'DEP', 'CTR', 'FSS'):
            continue
        polys = poly_of(o)
        cs = (o.get('composePosition') or o.get('callsign') or '').upper()
        if polys and cs and cs not in by_cs:
            by_cs[cs] = {'p': polys, 'c': (o.get('airportId') or cs.split('_')[0]).upper(), 't': pos}
            n_pos += 1

    if not by_cs:
        sys.exit('Aucun secteur avec limites trouvé : voir la structure des réponses ci-dessus')
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(by_cs, separators=(',', ':')))
    log(f'{n_sub} secteurs en route, {n_pos} approches -> {OUT.name} ({OUT.stat().st_size // 1024} Ko)')


if __name__ == '__main__':
    main()
