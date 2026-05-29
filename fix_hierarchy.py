#!/usr/bin/env python3
"""
fix_hierarchy.py — Ricostruisce i parent_id in base ai range IP.
Compatibile con Python 3.5+.

Opzioni:
    --dry-run           Mostra le modifiche senza applicarle
    --create-supernets  Crea le supernet mancanti per le subnet orfane
                        (implica prima un dry-run, poi chiede conferma
                         se usato senza --dry-run)

Uso:
    python3 fix_hierarchy.py [percorso_db] [--dry-run] [--create-supernets]

Esempi:
    python3 fix_hierarchy.py /var/www/html/ipam/instance/ipam.db
    python3 fix_hierarchy.py /var/www/html/ipam/instance/ipam.db --dry-run
    python3 fix_hierarchy.py /var/www/html/ipam/instance/ipam.db --create-supernets
    python3 fix_hierarchy.py /var/www/html/ipam/instance/ipam.db --dry-run --create-supernets
"""
import sys
import sqlite3
import ipaddress
from datetime import datetime

dry_run         = '--dry-run'         in sys.argv
create_super    = '--create-supernets' in sys.argv
args            = [a for a in sys.argv[1:] if not a.startswith('--')]
DB_PATH         = args[0] if args else '/var/www/html/ipam/instance/ipam.db'

print(f'Python  : {sys.version.split()[0]}')
print(f'Database: {DB_PATH}')
flags = []
if dry_run:      flags.append('DRY RUN')
if create_super: flags.append('crea supernet mancanti')
print(f'Modalita: {" + ".join(flags) if flags else "SCRITTURA"}')
print()

conn = sqlite3.connect(DB_PATH)
cur  = conn.cursor()

rows = cur.execute(
    'SELECT id, address, cidr, name, parent_id FROM networks ORDER BY cidr ASC'
).fetchall()

print(f'Reti lette dal DB: {len(rows)}')


def contains(big, small):
    """Ritorna True se big contiene small (senza usare subnet_of)."""
    return (
        big != small
        and int(big.network_address)     <= int(small.network_address)
        and int(small.broadcast_address) <= int(big.broadcast_address)
    )


def supernet_minima(reti):
    """
    Dato un insieme di reti IPv4, restituisce la supernet piu' piccola
    che le contiene tutte (sale di 1 bit alla volta finche' non trovata).
    Torna None se la lista e' vuota.
    """
    if not reti:
        return None
    candidate = reti[0]
    while candidate.prefixlen > 0:
        if all(contains(candidate, n) or candidate == n for n in reti):
            return candidate
        candidate = candidate.supernet()
    return candidate


# Pre-calcola gli oggetti ipaddress
parsed  = []
skipped = 0
for net_id, addr, cidr, name, parent_id in rows:
    try:
        obj = ipaddress.ip_network('{}/{}'.format(addr, cidr), strict=False)
        parsed.append({'id': net_id, 'obj': obj, 'name': name or '',
                       'addr': addr, 'cidr': cidr, 'parent_id': parent_id})
    except Exception as e:
        print('  SKIP id={} addr={}/{}: {}'.format(net_id, addr, cidr, e))
        skipped += 1

print(f'Reti valide: {len(parsed)}  |  Saltate: {skipped}')
print()

id_map  = {n['id']: n for n in parsed}
changes = []   # (net, old_parent_id, new_parent_id)

for net in parsed:
    best_parent_id = None
    best_prefix    = -1

    for cand in parsed:
        if cand['id'] == net['id']:
            continue
        try:
            if contains(cand['obj'], net['obj']):
                if cand['obj'].prefixlen > best_prefix:
                    best_prefix    = cand['obj'].prefixlen
                    best_parent_id = cand['id']
        except Exception:
            continue

    if best_parent_id != net['parent_id']:
        changes.append((net, net['parent_id'], best_parent_id))
        if not dry_run:
            cur.execute(
                'UPDATE networks SET parent_id=? WHERE id=?',
                (best_parent_id, net['id'])
            )

updated   = len(changes)
unchanged = len(parsed) - updated

# ── Riepilogo gerarchia ───────────────────────────────────────────────────────
print(f'parent_id da aggiornare : {updated}')
print(f'parent_id invariati     : {unchanged}')

if dry_run and changes:
    print()
    show = min(40, len(changes))
    print(f'Anteprima modifiche (prime {show} su {len(changes)}):')
    print('  {:<22} {:<22} {:<22} {}'.format(
          'SUBNET', 'VECCHIO PADRE', 'NUOVO PADRE', 'NOME'))
    print('  ' + '-' * 88)
    for net, old_pid, new_pid in sorted(
            changes, key=lambda x: int(x[0]['obj'].network_address))[:show]:
        subnet_str = '{}/{}'.format(net['addr'], net['cidr'])
        old_str = ('{}/{}'.format(id_map[old_pid]['addr'], id_map[old_pid]['cidr'])
                   if old_pid and old_pid in id_map else '(nessuno)')
        new_str = ('{}/{}'.format(id_map[new_pid]['addr'], id_map[new_pid]['cidr'])
                   if new_pid and new_pid in id_map else '(nessuno)')
        print('  {:<22} {:<22} {:<22} {}'.format(
              subnet_str, old_str, new_str, net['name'][:25]))
    if len(changes) > show:
        print('  ... altri {} non mostrati'.format(len(changes) - show))

# ── Subnet orfane (senza padre dopo fix) ─────────────────────────────────────
print()

# Calcola il parent_id finale per ogni rete (dopo le modifiche)
final_parent = {}
for net in parsed:
    final_parent[net['id']] = net['parent_id']
for net, old_pid, new_pid in changes:
    final_parent[net['id']] = new_pid

orfane = [n for n in parsed if final_parent[n['id']] is None]
print('=' * 55)
print(f'Subnet senza padre (orfane): {len(orfane)} su {len(parsed)}')
print('=' * 55)

if orfane:
    # Raggruppa per /16 di appartenenza
    gruppi = {}
    for n in orfane:
        sup16 = ipaddress.ip_network(
            '{}/16'.format(n['obj'].network_address), strict=False)
        key = str(sup16)
        gruppi.setdefault(key, []).append(n)

    print()
    print('Orfane per blocco /16:')
    print('  {:<20} {:>6}  {}'.format('BLOCCO /16', 'ORFANE', 'ESEMPI'))
    print('  ' + '-' * 60)
    for key in sorted(gruppi, key=lambda k: int(
            ipaddress.ip_network(k).network_address)):
        nets = gruppi[key]
        esempi = ', '.join(
            '{}/{}'.format(n['addr'], n['cidr'])
            for n in sorted(nets, key=lambda x: int(x['obj'].network_address))[:3]
        )
        if len(nets) > 3:
            esempi += ' ...'
        print('  {:<20} {:>6}  {}'.format(key, len(nets), esempi))

    # ── Crea supernet mancanti ──────────────────────────────────────────────
    if create_super:
        print()
        print('Supernet proposte (supernet minima che contiene tutte le orfane del blocco):')
        print('  {:<22} {:<6}  {}'.format('SUPERNET', 'CIDR', 'COPRIRA'))
        print('  ' + '-' * 65)

        proposte = []
        for key in sorted(gruppi, key=lambda k: int(
                ipaddress.ip_network(k).network_address)):
            nets   = gruppi[key]
            objs   = [n['obj'] for n in nets]
            super_net = supernet_minima(objs)
            if super_net is None:
                continue
            # Salta se esiste gia' nel DB
            gia_presente = any(
                p['obj'] == super_net for p in parsed
            )
            if gia_presente:
                continue
            proposte.append((super_net, nets))
            coprira = '{} subnet'.format(len(nets))
            print('  {:<22} /{:<5}  {}'.format(
                  str(super_net), super_net.prefixlen, coprira))

        if proposte:
            print()
            if dry_run:
                print('Dry-run: le supernet NON verranno create.')
                print('Rilancia senza --dry-run --create-supernets per crearle.')
            else:
                now = datetime.utcnow().isoformat()
                create_count = 0
                for super_net, nets in proposte:
                    addr = str(super_net.network_address)
                    cidr = super_net.prefixlen
                    name = 'Supernet {}'.format(super_net)
                    mask = str(super_net.netmask)
                    cur.execute(
                        '''INSERT OR IGNORE INTO networks
                           (name, address, cidr, mask, network_type, status,
                            created_at, updated_at)
                           VALUES (?,?,?,?,'supernet','active',?,?)''',
                        (name, addr, cidr, mask, now, now)
                    )
                    if cur.rowcount > 0:
                        create_count += 1
                        print('  CREATA: {} ({})'.format(super_net, name))
                    else:
                        print('  GIA\' PRESENTE: {}'.format(super_net))
                print()
                print('Supernet create: {}'.format(create_count))
                print('Riesegui fix_hierarchy.py per agganciare le orfane alle nuove supernet.')
        else:
            print()
            print('Nessuna supernet da creare (tutte gia\' presenti o non rilevabili).')

# ── Commit e chiusura ─────────────────────────────────────────────────────────
if not dry_run:
    conn.commit()
conn.close()

print()
if dry_run:
    print('Dry-run completato - nessuna modifica effettuata.')
    print('Rilancia senza --dry-run per applicare le modifiche.')
else:
    print('Completato. Riavvia Gunicorn per azzerare la cache:')
    print('  sudo systemctl restart ipam')
