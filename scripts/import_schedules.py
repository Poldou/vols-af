#!/usr/bin/env python3
"""Convertit des exports d'horaires (exportFlights*.zip ou .csv, séparateur « ; ») en data/schedules.json.

Toutes les compagnies présentes dans la colonne « Operating » sont gardées
(Air France, KLM, Delta, Transavia…). À lancer en local : les exports bruts
ne doivent pas être publiés sur GitHub.

    python3 scripts/import_schedules.py export1.zip export2.zip ...

Chaque lancement remplace complètement data/schedules.json : passe tous les exports
à jour en une seule fois.
"""
import csv
import io
import json
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

# Code IATA (colonne Operating) -> code OACI / indicatif radio
OPERATORS = {
    'AF': 'AFR', 'KL': 'KLM', 'DL': 'DAL', 'TO': 'TVF', 'HV': 'TRA', 'A5': 'HOP', 'WA': 'KLC',
    'VS': 'VIR', 'AZ': 'ITY', 'KQ': 'KQA', 'AM': 'AMX', 'KE': 'KAL', 'MU': 'CES', 'SV': 'SVA',
    'G3': 'GLO', 'LA': 'LAN', 'WS': 'WJA', 'VN': 'HVN', 'CI': 'CAL', 'GA': 'GIA', 'ME': 'MEA',
    'RO': 'ROT', 'UX': 'AEA', 'OK': 'CSA', 'SU': 'AFL', 'TK': 'THY', 'XK': 'CCM', 'SS': 'CRL',
}

# Désignations des exports -> codes OACI (identiques à ceux de Simbrief)
AC_ICAO = {
    'BOEING 777-300ER': 'B77W', 'BOEING 777-200/200 ER': 'B772', 'BOEING 777-200LR': 'B77L',
    'BOEING 787-9': 'B789', 'BOEING 787-10': 'B78X', 'BOEING 787-8': 'B788',
    'BOEING 737-800': 'B738', 'BOEING 737-700': 'B737', 'BOEING 737-900': 'B739',
    'BOEING 737 MAX 8': 'B38M', 'BOEING 737 MAX 9': 'B39M', 'BOEING 757-200': 'B752',
    'BOEING 757-300': 'B753', 'BOEING 767-300': 'B763', 'BOEING 767-400': 'B764',
    'BOEING 717-200': 'B712',
    'AIRBUS A350-900': 'A359', 'AIRBUS A350-1000': 'A35K', 'AIRBUS A330-200': 'A332',
    'AIRBUS A330-300': 'A333', 'AIRBUS A330-900': 'A339', 'AIRBUS A330-900NEO': 'A339',
    'AIRBUS A320': 'A320', 'AIRBUS A320 (SHARKLETS)': 'A320', 'AIRBUS A320NEO': 'A20N',
    'AIRBUS A321': 'A321', 'AIRBUS A321NEO': 'A21N', 'AIRBUS A319': 'A319', 'AIRBUS A318': 'A318',
    'AIRBUS A220-300 PASSENGER': 'BCS3', 'AIRBUS A220-300': 'BCS3', 'AIRBUS A220-100': 'BCS1',
    'EMBRAER 190': 'E190', 'EMBRAER 170': 'E170', 'EMBRAER 175': 'E175', 'EMBRAER 195': 'E195',
    'EMBRAER 195-E2': 'E295', 'EMBRAER 195 E2': 'E295',
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


def ac_code(name):
    name = name.strip().upper()
    if name in AC_ICAO:
        return AC_ICAO[name]
    for k, v in AC_ICAO.items():  # variantes de libellé (« BOEING 737-800 WINGLETS »…)
        if name.startswith(k):
            return v
    return name


def main(paths):
    routes = defaultdict(lambda: {'durs': [], 'deps': [], 'nums': Counter(), 'acs': Counter(), 'co': Counter()})
    unknown_ops = Counter()
    for path in paths:
        for row in read_rows(path):
            if len(row) < 10 or row[0] == 'Num Vol':
                continue
            num, dep, arr, hdep, harr, op, avion = (row[0].strip(), row[2], row[3], row[4], row[5],
                                                    row[8].strip(), row[9].strip())
            co = row[15].strip() if len(row) > 15 else ''
            if not op:
                continue
            al = OPERATORS.get(op)
            if not al:
                unknown_ops[op] += 1
                al = op  # résolu plus tard par build.py via la base OpenFlights
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
            r = routes[(al, dep, arr)]
            r['durs'].append(dur)
            r['deps'].append(d)
            r['nums'][num] += 1
            r['acs'][ac_code(avion)] += 1
            if co:
                r['co'][co] += 1

    out = []
    for (al, dep, arr), r in sorted(routes.items()):
        num = r['nums'].most_common(1)[0][0]
        prefix, digits = num[:2], num[2:].lstrip('0') or '0'
        dep_mode = Counter(r['deps']).most_common(1)[0][0]
        out.append({
            'al': al,
            'cs': (al if len(al) == 3 else prefix) + digits,
            'fn': prefix + digits,
            'dep': dep,
            'arr': arr,
            'hdep': f'{dep_mode // 60:02d}:{dep_mode % 60:02d}',  # UTC
            'dur': round(sum(r['durs']) / len(r['durs'])),
            'ac': r['acs'].most_common(1)[0][0],
            'co': r['co'].most_common(1)[0][0] if r['co'] else '',
            'n': len(r['durs']),
        })

    dest = Path(__file__).resolve().parent.parent / 'data' / 'schedules.json'
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, separators=(',', ':')))
    print(f'{len(out)} routes -> {dest}')
    for al, n in Counter(r['al'] for r in out).most_common():
        print(f'  {al} : {n} routes')
    if unknown_ops:
        print('Codes Operating sans correspondance OACI (gardés tels quels) :', dict(unknown_ops))
    unknown_ac = sorted({r['ac'] for r in out if len(r['ac']) > 4})
    if unknown_ac:
        print('Types avion non convertis :', unknown_ac)


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1:])
