#!/usr/bin/env python3
"""
show_10_86.py — Mostra le reti che iniziano con 10.86. e la loro gerarchia.

Uso:
    python3 show_10_86.py [percorso_db]

Esempio:
    python3 show_10_86.py /var/www/html/ipam/instance/ipam.db
"""
import sys
import sqlite3

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else '/var/www/html/ipam/instance/ipam.db'

conn = sqlite3.connect(DB_PATH)
cur  = conn.cursor()

rows = cur.execute("""
    SELECT
        n.id,
        n.address,
        n.cidr,
        n.name,
        n.network_type,
        n.parent_id,
        p.address  AS parent_addr,
        p.cidr     AS parent_cidr,
        p.name     AS parent_name
    FROM networks n
    LEFT JOIN networks p ON n.parent_id = p.id
    WHERE n.address LIKE '10.86.%'
    ORDER BY n.address, n.cidr
""").fetchall()

conn.close()

print(f"{'ID':>6}  {'Rete':<22}  {'Tipo':<10}  {'Nome':<35}  {'Parent':<25}  {'Parent nome'}")
print("-" * 120)

for r in rows:
    net_id, addr, cidr, name, ntype, parent_id, paddr, pcidr, pname = r
    rete      = f"{addr}/{cidr}"
    parent    = f"{paddr}/{pcidr}" if paddr else "(nessuno)"
    pname_str = pname or ""
    name_str  = name or ""
    print(f"{net_id:>6}  {rete:<22}  {ntype:<10}  {name_str:<35}  {parent:<25}  {pname_str}")

print()
print(f"Totale reti 10.86.x trovate: {len(rows)}")
senza_parent = sum(1 for r in rows if r[5] is None)
print(f"Senza parent_id (nodi radice): {senza_parent}")
