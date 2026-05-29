#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
import_custom.py
================
Importa subnet da un file Excel (.xlsx) o CSV nella fonte custom.
Logica di upsert:
  - Se la subnet (address/cidr) esiste già → aggiorna i campi valorizzati
    (description, country, site, usage, zone, gateway, location, vlan_id)
  - Se non esiste → la inserisce

Formato Excel atteso (intestazione sulla riga 1):
    Description | address* | netmask* | CIDR | Gateway | Country | Zone | Site | Usage | Vlan

Formato CSV alternativo (con intestazione):
    address,cidr,name,description,country,site,usage,zone,gateway,vlan_id,location,network_type,status

Uso:
    python3 import_custom.py <file.xlsx|file.csv> [--dry-run] [--base-url URL]

Esempi:
    python3 import_custom.py Networks_with_CIDR.xlsx
    python3 import_custom.py Networks_with_CIDR.xlsx --dry-run
    python3 import_custom.py subnets.csv --base-url http://localhost/ipam
"""

import sys
import csv
import ipaddress
import argparse
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_BASE_URL = 'http://localhost/ipam'


# ─────────────────────────────────────────────────────────────────────────────
#  HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

def ipam_get(endpoint, base_url):
    r = requests.get(f"{base_url}/api{endpoint}", timeout=15)
    r.raise_for_status()
    return r.json()


def ipam_post(endpoint, payload, base_url):
    return requests.post(f"{base_url}/api{endpoint}", json=payload,
                         headers={'Content-Type': 'application/json'}, timeout=15)


def ipam_put(endpoint, payload, base_url):
    return requests.put(f"{base_url}/api{endpoint}", json=payload,
                        headers={'Content-Type': 'application/json'}, timeout=15)


# ─────────────────────────────────────────────────────────────────────────────
#  Lettori file
# ─────────────────────────────────────────────────────────────────────────────

def leggi_excel(path):
    """Legge il file Excel nel formato Networks_with_CIDR e restituisce lista dict."""
    try:
        import openpyxl
    except ImportError:
        print('[ERRORE] openpyxl non installato. Esegui: pip install openpyxl')
        sys.exit(1)

    wb = openpyxl.load_workbook(path)
    ws = wb.active
    righe = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        description, address, netmask, cidr, gateway, country, zone, site, usage, vlan = (
            row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9]
        )
        if not address or not cidr:
            continue
        # Salta celle con errori Excel (#N/A, #REF!, ecc.)
        def pulisci(v):
            s = str(v).strip() if v is not None else ''
            return '' if s.startswith('#') else s
        addr_str = pulisci(address)
        if not addr_str or '.' not in addr_str:
            continue  # indirizzo non valido (es. numero senza punti)
        nome = pulisci(description)
        if not nome:
            nome = pulisci(usage)
        righe.append({
            'address':     addr_str,
            'cidr':        cidr,
            'name':        nome,
            'description': '',
            'country':     pulisci(country),
            'zone':        pulisci(zone),
            'site':        pulisci(site),
            'usage':       pulisci(usage),
            'gateway':     pulisci(gateway),
            'vlan_id':     int(vlan) if vlan and str(vlan).strip().lstrip('-').isdigit() else None,
            'location':    '',
            'network_type': 'subnet',
            'status':      'active',
        })
    return righe


def leggi_csv(path):
    """
    Legge il CSV esportato dall'Excel (o un CSV generico).
    Gestisce sia le intestazioni originali dell'Excel
    (Description, address*, netmask*, CIDR, Gateway, Country, Zone, Site, Usage, Vlan)
    sia le intestazioni generiche (address, cidr, name, ...).
    """
    # Mappa intestazioni Excel -> chiavi interne
    ALIAS = {
        'description': 'name',
        'address*':    'address',
        'netmask*':    'mask',
        'cidr':        'cidr',
        'gateway':     'gateway',
        'country':     'country',
        'zone':        'zone',
        'site':        'site',
        'usage':       'usage',
        'vlan':        'vlan_id',
    }

    def pulisci(v):
        s = str(v).strip() if v is not None else ''
        return '' if s.startswith('#') else s

    with open(path, newline='', encoding='utf-8-sig') as f:
        campione = f.read(2048)
        f.seek(0)
        sep = ';' if campione.count(';') > campione.count(',') else ','
        reader = csv.DictReader(f, delimiter=sep)
        righe = []
        for row in reader:
            # Normalizza le chiavi (minuscolo, strip)
            row_norm = {k.strip().lower(): v for k, v in row.items() if k is not None}
            # Rinomina le chiavi Excel -> interne
            riga = {}
            for src, dst in ALIAS.items():
                if src in row_norm:
                    riga[dst] = pulisci(row_norm[src])
            # Chiavi già nel formato generico (es. 'name', 'description', 'location'...)
            for k, v in row_norm.items():
                if k not in ALIAS and k not in riga:
                    riga[k] = pulisci(v)
            # Determina il nome: usa 'name' se valorizzato, altrimenti 'usage'
            if not riga.get('name'):
                riga['name'] = riga.get('usage', '')
            if not riga.get('address') or not riga.get('cidr'):
                continue
            # Salta indirizzi senza punti (celle numeriche mal esportate)
            if '.' not in riga['address']:
                continue
            righe.append(riga)
    return righe


# ─────────────────────────────────────────────────────────────────────────────
#  Normalizzazione riga
# ─────────────────────────────────────────────────────────────────────────────

def normalizza(row):
    try:
        net = ipaddress.ip_network(f"{row['address']}/{row['cidr']}", strict=False)
    except Exception as e:
        raise ValueError(f"Indirizzo non valido {row.get('address')}/{row.get('cidr')}: {e}")

    def sv(k):
        v = row.get(k, '')
        return str(v).strip() if v is not None else ''

    vlan = row.get('vlan_id')
    try:
        vlan = int(vlan) if vlan not in (None, '', 'None') else None
    except (ValueError, TypeError):
        vlan = None

    return {
        'address':      str(net.network_address),
        'cidr':         int(row['cidr']),
        'name':         sv('name') or f"{net.network_address}/{int(row['cidr'])}",
        'description':  sv('description'),
        'country':      sv('country'),
        'site':         sv('site'),
        'usage':        sv('usage'),
        'zone':         sv('zone'),
        'gateway':      sv('gateway'),
        'vlan_id':      vlan,
        'location':     sv('location'),
        'network_type': sv('network_type') or 'subnet',
        'status':       sv('status') or 'active',
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Import subnet da Excel/CSV con upsert')
    parser.add_argument('file', help='File da importare (.xlsx o .csv)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Mostra cosa verrebbe fatto senza modificare il DB')
    parser.add_argument('--base-url', default=DEFAULT_BASE_URL,
                        help=f'URL base IPAM (default: {DEFAULT_BASE_URL})')
    args = parser.parse_args()

    base_url = args.base_url.rstrip('/')
    dry      = args.dry_run
    fpath    = args.file

    print("=" * 65)
    print("  Import Custom -> IPAM" + (" [DRY RUN]" if dry else ""))
    print(f"  Destinazione : {base_url}")
    print(f"  File         : {fpath}")
    print("=" * 65)

    # ── Lettura file ──────────────────────────────────────────────────────────
    ext = fpath.rsplit('.', 1)[-1].lower()
    if ext in ('xlsx', 'xls'):
        righe_raw = leggi_excel(fpath)
    elif ext == 'csv':
        righe_raw = leggi_csv(fpath)
    else:
        print(f"[ERRORE] Formato non supportato: .{ext} (usa .xlsx o .csv)")
        sys.exit(1)

    print(f"\nRighe lette: {len(righe_raw)}")

    # ── Upsert ───────────────────────────────────────────────────────────────
    inserite = aggiornate = saltate = errori = 0
    campi_aggiornabili = ['description', 'country', 'site', 'usage', 'zone',
                          'gateway', 'location', 'vlan_id']

    for i, row in enumerate(righe_raw, 1):
        try:
            data = normalizza(row)
        except ValueError as e:
            print(f"  [{i:4}] SKIP - {e}")
            saltate += 1
            continue

        rete_str = f"{data['address']}/{data['cidr']}"

        if dry:
            print(f"  [{i:4}] {rete_str:<22} ({data['name'][:35]})")
            continue

        # Tenta inserimento
        r = ipam_post('/networks', data, base_url)

        if r.status_code == 201:
            inserite += 1
            net_id = r.json().get('id', '?')
            print(f"  [{i:4}] {rete_str:<22} INSERITA (id={net_id})")

        elif r.status_code == 409:
            # Già esistente - recupera id dalla risposta o tramite ricerca
            net_id = r.json().get('id')

            if not net_id:
                try:
                    tutte = ipam_get('/networks', base_url)
                    match = next((n for n in tutte
                                  if n['address'] == data['address']
                                  and int(n['cidr']) == data['cidr']), None)
                    net_id = match['id'] if match else None
                except Exception:
                    net_id = None

            if net_id:
                update = {k: data[k] for k in campi_aggiornabili
                          if data.get(k) not in (None, '')}
                # Aggiorna sempre il nome se valorizzato
                if data.get('name'):
                    update['name'] = data['name']

                if update:
                    ru = ipam_put(f'/networks/{net_id}', update, base_url)
                    if ru.status_code == 200:
                        aggiornate += 1
                        print(f"  [{i:4}] {rete_str:<22} AGGIORNATA (id={net_id})")
                    else:
                        print(f"  [{i:4}] {rete_str:<22} ERR update {ru.status_code}: {ru.text[:60]}")
                        errori += 1
                else:
                    print(f"  [{i:4}] {rete_str:<22} già presente, nessun campo da aggiornare")
                    saltate += 1
            else:
                print(f"  [{i:4}] {rete_str:<22} già presente (id non recuperabile)")
                saltate += 1
        else:
            print(f"  [{i:4}] {rete_str:<22} ERR {r.status_code}: {r.text[:60]}")
            errori += 1

    # ── Riepilogo ─────────────────────────────────────────────────────────────
    print()
    print("=" * 65)
    if dry:
        print("  DRY RUN completato - nessuna modifica effettuata")
    else:
        print(f"  Inserite   : {inserite}")
        print(f"  Aggiornate : {aggiornate}")
        print(f"  Saltate    : {saltate}")
        print(f"  Errori     : {errori}")
        print(f"  TOTALE     : {inserite + aggiornate + saltate + errori} / {len(righe_raw)}")
    print("=" * 65)


if __name__ == '__main__':
    main()
