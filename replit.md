# IPAM — IP Address Management

Applicazione Flask per la gestione degli indirizzi IP, in italiano.
Database SQLite reale (`instance/ipam.db`, ~21MB, 827+ reti, 159K+ IP).

## Avvio
```
python3 main.py
```
Porta: 5000 — tutte le route sotto il prefisso `/ipam`.

## Struttura
- `app.py` — Flask app, tutti gli endpoint API e le pagine
- `scanner.py` — scanner IP (ping/SNMP)
- `main.py` — entry point sviluppo
- `wsgi_gunicorn.py` — entry point produzione (Apache + Gunicorn)
- `templates/` — Jinja2 templates
- `static/` — CSS, JS, immagini
- `instance/ipam.db` — database SQLite (NON sovrascrivere mai con seed)
- `install.sh` — script di installazione (default: `/var/www/html/ipam`)
- `fix_hierarchy.py` — ricostruisce i parent_id in base ai range IP
- `show_10_86.py` — mostra le reti del range 10.86.x con la loro gerarchia
- `import_from_solarwinds.py` — import completo da SolarWinds
- `import_solarwinds_10_86.py` — import solo reti 10.86.x da SolarWinds

## User preferences
- Lingua interfaccia e comunicazione: **italiano**
- Porta SolarWinds SWIS API: **17774** (non 17778) — da usare in tutti gli script di import
- Server SolarWinds: `orion.comifar.it`
- Directory installazione produzione: `/var/www/html/ipam`
- Dopo ogni modifica rigenerare **sempre** `ipam_deploy.zip` con tutti i file del progetto e comunicarlo esplicitamente all'utente senza aspettare che lo chieda
- Non sovrascrivere mai `instance/ipam.db` con dati di seed
- Invalidare sempre `_tree_cache` dopo create/update/delete di reti
