# -*- coding: utf-8 -*-
"""
import_from_solarwinds.py
=========================
Legge le subnet IPAM da SolarWinds tramite SWIS REST API (porta 17778)
e le importa nel nostro IPAM tramite REST API.

Documentazione SWIS: https://solarwinds.github.io/OrionSDK/docs/rest/
Autenticazione: Basic Auth (utente locale SolarWinds, non AD)
Metodo query: GET con query string

Utilizzo:
    pip3 install requests urllib3
    python3 import_from_solarwinds.py

Configurazione: modifica la sezione CONFIG qui sotto.
"""

import sys
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ================================================================
#  CONFIG
# ================================================================
SOLARWINDS = {
    'host':       'solarwinds.example.com',   # <-- sostituire con hostname reale
    'port':       17774,
    'username':   'admin',                    # <-- sostituire con utente reale
    'password':   'CAMBIA_PASSWORD',          # <-- sostituire con password reale
    'verify_ssl': False,
}

IPAM = {
    'base_url': 'http://localhost/ipam',
}
# ================================================================


def swis_query(cfg, query):
    """Esegue una query SWQL via GET su SWIS REST API porta 17778."""
    url = (f"https://{cfg['host']}:{cfg['port']}"
           f"/SolarWinds/InformationService/v3/Json/Query")
    resp = requests.get(
        url,
        params={'query': query.strip()},
        auth=(cfg['username'], cfg['password']),
        verify=cfg['verify_ssl'],
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json().get('results', [])


def ipam_post(endpoint, payload, base_url):
    url = f"{base_url}/api{endpoint}"
    return requests.post(
        url,
        json=payload,
        headers={'Content-Type': 'application/json'},
        timeout=15,
    )


def main():
    sw  = SOLARWINDS
    ipam_url = IPAM['base_url']

    print("=" * 60)
    print("  Import SolarWinds IPAM -> IPAM")
    print(f"  Sorgente : https://{sw['host']}:{sw['port']}")
    print(f"  Dest.    : {ipam_url}")
    print("=" * 60)

    # ── 1. Test connessione ─────────────────────────────────────
    print("\n[0/4] Test connessione SWIS...")
    try:
        test = swis_query(sw, "SELECT TOP 1 SubnetId FROM IPAM.Subnet")
        print(f"  Connessione OK (trovata {len(test)} subnet di test)")
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            print(f"  [ERRORE] Autenticazione fallita (401) - verifica username/password")
        elif e.response.status_code == 404:
            print(f"  [ERRORE] Endpoint non trovato (404) - verifica host e porta")
        else:
            print(f"  [ERRORE] HTTP {e.response.status_code}: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"  [ERRORE] {e}")
        sys.exit(1)

    # ── 2. Leggi Supernet ───────────────────────────────────────
    print("\n[1/4] Lettura Supernet...")
    supernets = swis_query(sw, """
        SELECT GroupId, FriendlyName, Address, CIDR, Location, Comments
        FROM IPAM.GroupReport
        WHERE GroupTypeText = 'Supernet'
        ORDER BY Address
    """)
    print(f"  Trovati {len(supernets)} supernet")

    # ── 3. Leggi Subnet ─────────────────────────────────────────
    print("\n[2/4] Lettura Subnet...")
    subnets = swis_query(sw, """
        SELECT SubnetId, FriendlyName, Address, CIDR,
               Location, Comments
        FROM IPAM.Subnet
        ORDER BY Address
    """)
    print(f"  Trovate {len(subnets)} subnet")

    # ── 4. Leggi IP in uso ──────────────────────────────────────
    print("\n[3/4] Lettura IP address in uso...")
    # Prima prova con tutti i campi, se fallisce usa i campi minimi
    try:
        ip_records = swis_query(sw, """
            SELECT IPAddress, DnsBackward, MAC,
                   Status, SubnetId
            FROM IPAM.IPNode
            WHERE Status != 'Available'
            ORDER BY IPAddress
        """)
    except Exception:
        ip_records = swis_query(sw, """
            SELECT IPAddress, Status, SubnetId
            FROM IPAM.IPNode
            WHERE Status != 'Available'
            ORDER BY IPAddress
        """)
    print(f"  Trovati {len(ip_records)} indirizzi IP non disponibili")

    # ── 5. Importa ──────────────────────────────────────────────
    print("\n[4/4] Importazione nel nostro IPAM...")

    subnet_id_map = {}
    ok = err = skip = 0

    # Supernet
    print(f"\n  Supernet ({len(supernets)})...")
    for sn in supernets:
        r = ipam_post('/networks', {
            'name':         sn.get('FriendlyName') or f"Supernet {sn['Address']}/{sn['CIDR']}",
            'address':      sn['Address'],
            'cidr':         int(sn['CIDR']),
            'location':     sn.get('Location') or '',
            'description':  sn.get('Comments') or '',
            'network_type': 'supernet',
            'status':       'active',
        }, ipam_url)
        if r.status_code == 201:
            ok += 1
        elif r.status_code == 409:
            skip += 1
        else:
            print(f"    [WARN] {sn['Address']}/{sn['CIDR']}: {r.text[:60]}")
            err += 1
    print(f"    Inserite: {ok} | Gia' presenti: {skip} | Errori: {err}")

    # Subnet
    ok = err = skip = 0
    print(f"\n  Subnet ({len(subnets)})...")
    for sn in subnets:
        try:
            vlan = int(sn['VLANID']) if sn.get('VLANID') else None
        except (ValueError, TypeError):
            vlan = None

        r = ipam_post('/networks', {
            'name':         sn.get('FriendlyName') or f"{sn['Address']}/{sn['CIDR']}",
            'address':      sn['Address'],
            'cidr':         int(sn['CIDR']),
            'vlan_id':      vlan,
            'location':     sn.get('Location') or '',
            'description':  sn.get('Comments') or '',
            'network_type': 'subnet',
            'status':       'active',
        }, ipam_url)
        if r.status_code == 201:
            subnet_id_map[sn['SubnetId']] = r.json()['id']
            ok += 1
        elif r.status_code == 409:
            skip += 1
        else:
            print(f"    [WARN] {sn['Address']}/{sn['CIDR']}: {r.text[:60]}")
            err += 1
    print(f"    Inserite: {ok} | Gia' presenti: {skip} | Errori: {err}")

    # IP
    ok = err = skip = 0
    print(f"\n  Indirizzi IP ({len(ip_records)})...")
    STATUS_MAP = {
        'Used':      'used',
        'Reserved':  'reserved',
        'Transient': 'used',
        'DHCP':      'dhcp',
        'Blocked':   'reserved',
    }
    for i, ip in enumerate(ip_records):
        r = ipam_post('/ip-records', {
            'ip_address':  ip['IPAddress'],
            'hostname':    ip.get('DnsBackward') or ip.get('DnsForward') or '',
            'mac_address': ip.get('MAC') or ip.get('MacAddress') or '',
            'status':      STATUS_MAP.get(ip.get('Status', 'Used'), 'used'),
            'network_id':  subnet_id_map.get(ip.get('SubnetId')),
            'description': ip.get('Comments') or '',
        }, ipam_url)
        if r.status_code == 201:
            ok += 1
        elif r.status_code == 409:
            skip += 1
        else:
            err += 1
        if (i + 1) % 200 == 0:
            print(f"    ...{i+1}/{len(ip_records)} ({ok} inseriti, {skip} skip, {err} err)")

    print(f"    Inseriti: {ok} | Gia' presenti: {skip} | Errori: {err}")

    print("\n" + "=" * 60)
    print("  Import completato!")
    print(f"  Apri: {ipam_url}/")
    print("=" * 60)


if __name__ == '__main__':
    main()
