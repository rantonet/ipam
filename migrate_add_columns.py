#!/usr/bin/env python3
"""
migrate_add_columns.py
======================
Aggiunge le colonne country, site, usage, zone, gateway alla tabella networks.
Sicuro da eseguire più volte: salta le colonne già presenti.

Uso:
    python3 migrate_add_columns.py [percorso_db]

Esempio:
    python3 migrate_add_columns.py /var/www/html/ipam/instance/ipam.db
"""
import sys
import sqlite3

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else '/var/www/html/ipam/instance/ipam.db'
print(f'Database: {DB_PATH}')

NUOVE_COLONNE = [
    ('country', 'VARCHAR(100)'),
    ('site',    'VARCHAR(200)'),
    ('usage',   'VARCHAR(200)'),
    ('zone',    'VARCHAR(100)'),
    ('gateway', 'VARCHAR(50)'),
]

conn = sqlite3.connect(DB_PATH)
cur  = conn.cursor()

colonne_esistenti = {r[1] for r in cur.execute('PRAGMA table_info(networks)').fetchall()}

aggiunte = 0
for nome, tipo in NUOVE_COLONNE:
    if nome in colonne_esistenti:
        print(f'  {nome}: già presente, saltata')
    else:
        cur.execute(f'ALTER TABLE networks ADD COLUMN {nome} {tipo}')
        print(f'  {nome}: aggiunta')
        aggiunte += 1

conn.commit()
conn.close()

print()
print(f'Colonne aggiunte: {aggiunte}  |  Già presenti: {len(NUOVE_COLONNE) - aggiunte}')
if aggiunte > 0:
    print('Riavvia Gunicorn per applicare le modifiche:')
    print('  sudo systemctl restart ipam')
