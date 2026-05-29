#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fill_missing_ips.py
-------------------
Completa tutte le subnet esistenti aggiungendo gli IP mancanti con
stato 'available', esattamente come avviene alla creazione di una rete.

Comportamento:
  - Carica tutti gli IP gia' presenti in memoria (set), poi per ogni rete
    calcola solo gli IP davvero assenti. Nessun confronto stringa su indirizzi IP.
  - Usa INSERT OR IGNORE: gli IP gia' presenti (qualunque stato, qualunque
    network_id) NON vengono toccati.
  - Salta le reti con CIDR < cidr_min (default 17, cioe' CIDR <= 16 saltati).
  - /31 e /32: considera tutti gli indirizzi (senza escludere network/broadcast).
  - Idempotente: puo' essere eseguito piu' volte senza danni.

Utilizzo:
  python3 fill_missing_ips.py [--dry-run] [--cidr-min N] [INSTALL_DIR]

  --dry-run      mostra quanti IP verrebbero aggiunti senza scrivere nulla
  --cidr-min N   salta reti con CIDR < N (default: 17, salta /8-/16)
  INSTALL_DIR    percorso installazione (default: /var/www/html/ipam)

Esempi:
  python3 fill_missing_ips.py --dry-run
  python3 fill_missing_ips.py
  python3 fill_missing_ips.py --cidr-min 20 /var/www/html/ipam
"""

import os
import sys
import sqlite3
import ipaddress
from datetime import datetime

# ── Parametri da riga di comando ──────────────────────────────────────────────
dry_run  = '--dry-run' in sys.argv
cidr_min = 17

install_dir = '/var/www/html/ipam'
args = sys.argv[1:]
i = 0
while i < len(args):
    if args[i] == '--dry-run':
        i += 1
    elif args[i] == '--cidr-min' and i + 1 < len(args):
        cidr_min = int(args[i + 1])
        i += 2
    elif args[i].startswith('--cidr-min='):
        cidr_min = int(args[i].split('=')[1])
        i += 1
    elif not args[i].startswith('--'):
        install_dir = args[i]
        i += 1
    else:
        i += 1

DB_PATH = os.path.join(install_dir, 'instance', 'ipam.db')
if not os.path.isfile(DB_PATH):
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance', 'ipam.db')
    if os.path.isfile(local):
        DB_PATH = local
    else:
        print('[ERRORE] DB non trovato: {}'.format(DB_PATH))
        sys.exit(1)

print('=' * 62)
print('  fill_missing_ips.py')
print('  DB      : {}'.format(DB_PATH))
print('  CIDR >= : {}'.format(cidr_min))
print('  Modalita: {}'.format('DRY-RUN (nessuna scrittura)' if dry_run else 'SCRITTURA'))
print('=' * 62)

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

# ── Carica TUTTI gli IP esistenti in un set Python ────────────────────────────
# (unica query su tutto il DB; evita confronti IP-come-stringa in SQLite)
print('\nCaricamento IP esistenti dal DB...')
existing_ips = set(
    r[0] for r in conn.execute('SELECT ip_address FROM ip_records').fetchall()
)
print('  {} IP gia\' presenti nel DB'.format(len(existing_ips)))

# ── Carica tutte le reti ──────────────────────────────────────────────────────
networks = conn.execute(
    'SELECT id, name, address, cidr, network_type FROM networks ORDER BY address, cidr'
).fetchall()
print('  {} reti trovate'.format(len(networks)))

# ── Costruisci set degli ID che sono parent di qualcuno (supernet) ────────────
supernet_ids = set(
    r[0] for r in conn.execute(
        'SELECT DISTINCT parent_id FROM networks WHERE parent_id IS NOT NULL'
    ).fetchall()
)
print('  {} supernet (con subnet figlie) -- verranno saltate\n'.format(len(supernet_ids)))

total_added   = 0
total_already = 0
total_skipped = 0
total_errors  = 0

now_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S.%f')

for net_row in networks:
    net_id      = net_row['id']
    net_name    = net_row['name']
    net_address = net_row['address']
    net_cidr    = net_row['cidr']
    net_type    = net_row['network_type'] or 'subnet'
    label       = '{}/{} ({})'.format(net_address, net_cidr, net_name)

    # ── Salta supernet per network_type (gestite manualmente) ─────────────────
    if net_type != 'subnet':
        print('  SKIP {} (tipo): {}'.format(net_type, label))
        total_skipped += 1
        continue

    # ── Salta anche reti con figli ma network_type='subnet' (edge case) ───────
    if net_id in supernet_ids:
        print('  SKIP supernet (ha figli): {}'.format(label))
        total_skipped += 1
        continue

    # ── Salta reti troppo grandi ──────────────────────────────────────────────
    if net_cidr < cidr_min:
        print('  SKIP /{} < {}: {}'.format(net_cidr, cidr_min, label))
        total_skipped += 1
        continue

    # ── Calcola IP attesi ─────────────────────────────────────────────────────
    try:
        net_obj = ipaddress.ip_network('{}/{}'.format(net_address, net_cidr), strict=False)
    except Exception as e:
        print('  ERRORE parsing {}: {}'.format(label, e))
        total_errors += 1
        continue

    if net_cidr >= 31:
        host_ips = [str(ip) for ip in net_obj]
    else:
        host_ips = [str(ip) for ip in net_obj.hosts()]

    if not host_ips:
        total_skipped += 1
        continue

    # ── Trova IP mancanti confrontando con il set in memoria ─────────────────
    # (corretto: usa Python, non confronto stringa SQL)
    missing = [ip for ip in host_ips if ip not in existing_ips]

    if not missing:
        total_already += 1
        print('  OK  ({}/{} IP): {}'.format(len(host_ips), len(host_ips), label))
        continue

    n_existing = len(host_ips) - len(missing)
    print('  ADD {} mancanti / {} totali: {}'.format(
        len(missing), len(host_ips), label))

    if dry_run:
        total_added += len(missing)
        # Aggiorna il set anche in dry-run per avere statistiche coerenti
        # tra reti padre-figlio (non influisce sul DB)
        existing_ips.update(missing)
        continue

    # ── Inserimento a batch di 500 ────────────────────────────────────────────
    import socket as _sock, struct as _st
    sql = ('INSERT OR IGNORE INTO ip_records '
           '(ip_address, ip_int, status, network_id, created_at, updated_at) '
           "VALUES (?, ?, 'available', ?, ?, ?)")

    batch = []
    for ip_str in missing:
        try:
            _iv = _st.unpack('!I', _sock.inet_aton(ip_str))[0]
        except Exception:
            _iv = 0
        batch.append((ip_str, _iv, net_id, now_str, now_str))
        if len(batch) >= 500:
            conn.executemany(sql, batch)
            batch = []
    if batch:
        conn.executemany(sql, batch)
    conn.commit()

    # Aggiorna il set in memoria cosi' le reti padri non ri-segnalano
    # gli stessi IP come mancanti
    existing_ips.update(missing)
    total_added += len(missing)

# ── Riepilogo finale ──────────────────────────────────────────────────────────
print()
print('=' * 62)
if dry_run:
    print('  [DRY-RUN] IP che verrebbero aggiunti : {:>8}'.format(total_added))
    print('  Riesegui senza --dry-run per applicare.')
else:
    print('  IP aggiunti                          : {:>8}'.format(total_added))
print('  Reti gia\' complete                   : {:>8}'.format(total_already))
print('  Reti saltate (CIDR troppo basso)      : {:>8}'.format(total_skipped))
if total_errors:
    print('  Errori                               : {:>8}'.format(total_errors))
print('=' * 62)

conn.close()
