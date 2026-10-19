#!/usr/bin/env python3
"""Convertit un ou plusieurs exports GPNet (exportFlights*.zip ou .csv) en data/af_gpnet.json.

Usage (en local, pas dans GitHub — les exports bruts ne doivent pas être publiés) :
    python3 scripts/gpnet_to_json.py export_ete.zip export_hiver.zip

Le fichier produit ne contient que des données agrégées par route :
numéro de vol le plus fréquent, heure de départ UTC habituelle, durée bloc moyenne,
type avion le plus fréquent, type de courrier.
"""
import csv
import io
import json
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

# Désignations GPNet -> codes OACI (identiques à ceux de Simbrief)
AC_ICAO = {
    'BOEING 777-300ER': 'B77W',
    'BOEING 777-200/200 ER': 'B772',
    'BOEING 787-9': 'B789',
    'AIRBUS A350-900': 'A359',
    'AIRBUS A320': 'A320',
    'AIRBUS A320 (SHARKLETS)': 'A320',
    'AIRBUS A321': 'A321',
    'AIRBUS A319': 'A319',
    'AIRBUS A318': 'A318',
    'AIRBUS A220-300 PASSENGER': 'BCS3',
    'AIRBUS A330-200': 'A332',
    'EMBRAER 190': 'E190',
    'EMBRAER 170': 'E170',
}


def read_rows(path):
    p = Path(path)
    if p.suffix.lower() == '.zip':
        with zipfile.ZipFile(p) as z:
            for name in z.namelist():
                if name.lower().endswith('.csv'):
                    text = z.read(name).decode('utf-8-sig')
                    yield from csv.reader(io.StringIO(text), delimiter=';')
    else:
        with open(p, encoding='utf-8-sig', newline='') as f:
            yield from csv.reader(f, delimiter=';')


def hhmm(s):
    h, m = s.strip().split(':')
    return int(h) * 60 + int(m)


def main(paths):
    routes = defaultdict(lambda: {'durs': [], 'deps': [], 'nums': Counter(), 'acs': Counter(), 'co': Counter()})
    for path in paths:
        for row in read_rows(path):
            if len(row) < 16 or row[0] == 'Num Vol':
                continue
            num, dep, arr, hdep, harr, op, avion, co = (row[0].strip(), row[2], row[3], row[4], row[5],
                                                        row[8].strip(), row[9].strip(), row[15].strip())
            if op != 'AF' or not co:
                continue
            dep = dep.strip().split()[0] if dep.strip() else ''
            arr = arr.strip().split()[0] if arr.strip() else ''
            if not (len(dep) == 3 and dep.isalpha() and len(arr) == 3 and arr.isalpha()):
                continue
            try:
                d, a = hhmm(hdep), hhmm(harr)
            except ValueError:
                continue
            dur = (a - d) % (24 * 60)
            if dur == 0:
                continue
            r = routes[(dep, arr)]
            r['durs'].append(dur)
            r['deps'].append(d)
            r['nums'][num] += 1
            r['acs'][AC_ICAO.get(avion, avion)] += 1
            r['co'][co] += 1

    out = []
    for (dep, arr), r in sorted(routes.items()):
        num = r['nums'].most_common(1)[0][0]
        digits = num[2:].lstrip('0') or '0'
        dep_mode = Counter(r['deps']).most_common(1)[0][0]
        out.append({
            'al': 'AFR',
            'cs': 'AFR' + digits,
            'fn': 'AF' + digits,
            'dep': dep,
            'arr': arr,
            'hdep': f'{dep_mode // 60:02d}:{dep_mode % 60:02d}',  # UTC
            'dur': round(sum(r['durs']) / len(r['durs'])),
            'ac': r['acs'].most_common(1)[0][0],
            'co': r['co'].most_common(1)[0][0],
            'n': len(r['durs']),
        })

    dest = Path(__file__).resolve().parent.parent / 'data' / 'af_gpnet.json'
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, separators=(',', ':')))
    print(f'{len(out)} routes AF -> {dest}')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1:])
