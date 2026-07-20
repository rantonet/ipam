# IPAM — IP Address Management

**IPAM** è un'applicazione web open source per la gestione degli spazi di indirizzamento IP (subnet, indirizzi, VLAN) sviluppata in Python 3 / Flask.

- Interfaccia web in italiano con tema scuro
- Autenticazione locale o LDAP/Active Directory
- Scanner IP integrato (ping ICMP + probe TCP + SNMP)
- Scheduler automatico per scansioni pianificate
- Import da SolarWinds Orion
- API REST completa
- Database SQLite — nessun server esterno richiesto

---

## Download

[![Ultima release](https://img.shields.io/badge/ultima%20versione-v1.9.2-blue)](https://github.com/rantonet/ipam/releases/latest)

| Versione | Data | Download |
|---|---|---|
| **v1.9.2** | 2026-07-20 | [ipam_v1.9.2_20260720.zip](https://github.com/rantonet/ipam/releases/download/v1.9.2/ipam_v1.9.2_20260720.zip) |

Tutte le versioni sono disponibili nella pagina [**Releases**](https://github.com/rantonet/ipam/releases).

---

## Indice

- [Screenshot](#screenshot)
- [Requisiti](#requisiti)
- [Installazione](#installazione)
- [Aggiornamento](#aggiornamento)
- [Configurazione Apache](#configurazione-apache)
- [Funzioni principali](#funzioni-principali)
- [API REST](#api-rest)
- [Struttura del progetto](#struttura-del-progetto)

---

## Screenshot

### Dashboard
Statistiche globali in tempo reale: totale reti, IP, utilizzo e le 5 subnet più sature.

![Dashboard](screenshots/01_dashboard.jpg)

---

### Reti & Subnet
Elenco di tutte le reti con albero gerarchico laterale, filtri per location e tipo, e indicatori di utilizzo per ogni subnet.

![Reti e Subnet](screenshots/02_networks.jpg)

---

### Dettaglio Rete / Supernet
Vista dettagliata di una singola rete con metriche di utilizzo, reti figlie e tabella degli indirizzi IP con hostname, MAC, switch e porta.

![Dettaglio Rete](screenshots/02b_network_detail.jpg)

---

### Indirizzi IP — Ricerca Globale
Ricerca e filtraggio su tutti gli indirizzi IP dell'infrastruttura per stato, tipo dispositivo, sistema operativo, switch e rete di appartenenza.

![Indirizzi IP](screenshots/03_ip_addresses.jpg)

---

### VLAN
Gestione delle VLAN configurate con lista delle subnet associate a ciascuna VLAN.

![VLAN](screenshots/04_vlans.jpg)

---

### Modifica Bulk
Aggiornamento in blocco di reti o indirizzi IP: seleziona più record e modifica uno o più campi contemporaneamente.

![Bulk Edit](screenshots/05_bulk_edit.jpg)

---

### Log Scansioni
Storico completo delle scansioni eseguite (subnet, SNMP, globale) con statistiche su host trovati, aggiornati ed errori.

![Log Scansioni](screenshots/06_logs.jpg)

---

### Impostazioni
Configurazione server DNS per reverse lookup, porte TCP per il probe firewall-bypass, e gestione dei Gruppi di Scansione pianificata.

![Impostazioni](screenshots/07_settings.jpg)

---

### Documentazione integrata
Documentazione interna dell'applicazione accessibile direttamente dall'interfaccia web.

![Documentazione](screenshots/08_docs.jpg)

---

### Login
Autenticazione locale (username + password) o tramite LDAP/Active Directory configurato nelle impostazioni.

![Login](screenshots/00_login.jpg)

---

## Requisiti

| Componente | Versione minima |
|---|---|
| Python | 3.9+ (consigliato 3.11) |
| Apache | 2.4 |
| mod_proxy / mod_proxy_http | (inclusi in Apache) |
| Sistema operativo | Linux (Debian/Ubuntu/RHEL consigliato) |

Le dipendenze Python vengono installate automaticamente dallo script di installazione in un virtualenv dedicato.

---

## Installazione

### Installazione automatica

```bash
# 1. Scarica e decomprimi il pacchetto (crea automaticamente la cartella deploy_ipam/)
wget https://github.com/rantonet/ipam/releases/download/v1.9.2/ipam_v1.9.2_20260720.zip
unzip ipam_v1.9.2_20260720.zip
cd deploy_ipam

# 2. Esegui lo script di installazione (richiede sudo)
sudo bash install.sh
```

Lo script esegue automaticamente:
1. Creazione della directory `/var/www/html/ipam`
2. Copia di tutti i file dell'applicazione
3. Creazione del virtualenv Python in `/var/www/html/ipam/venv`
4. Installazione delle dipendenze Python (`pip install -r requirements.txt`)
5. Inizializzazione del database SQLite (`instance/ipam.db`)
6. Installazione e abilitazione del servizio systemd `ipam`
7. Restart del servizio

### Installazione manuale

```bash
# Directory di installazione
INSTALL_DIR=/var/www/html/ipam

# 1. Copia i file
sudo mkdir -p $INSTALL_DIR
sudo cp -r . $INSTALL_DIR/
sudo chown -R www-data:www-data $INSTALL_DIR

# 2. Crea il virtualenv e installa dipendenze
cd $INSTALL_DIR
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt

# 3. Crea la directory instance
mkdir -p instance

# 4. Avvia con il servizio systemd (vedi ipam-scan.service)
sudo cp ipam-scan.service /etc/systemd/system/ipam.service
sudo systemctl daemon-reload
sudo systemctl enable ipam
sudo systemctl start ipam
```

### File servizio systemd

Il file `ipam-scan.service` configura Gunicorn sulla porta `8000`:

```ini
[Unit]
Description=IPAM — IP Address Management
After=network.target

[Service]
User=www-data
WorkingDirectory=/var/www/html/ipam
ExecStart=/var/www/html/ipam/venv/bin/gunicorn \
    --workers 2 \
    --bind 127.0.0.1:8000 \
    --timeout 120 \
    wsgi_gunicorn:application
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

---

## Aggiornamento

```bash
# 1. Scarica il nuovo pacchetto e decomprimi
unzip ipam_v1.9.2_20260720.zip -d /tmp/ipam_update
cd /tmp/ipam_update

# 2. Esegui lo script di aggiornamento
sudo bash update.sh
```

Lo script `update.sh`:
- Verifica la versione Python (≥ 3.9)
- Copia i file aggiornati senza toccare `instance/ipam.db`
- Aggiorna le dipendenze Python nel virtualenv
- Esegue `systemctl restart ipam`

> **Nota:** Il database `instance/ipam.db` non viene mai sovrascritto durante l'aggiornamento.

---

## Configurazione Apache

> **SICUREZZA:** Configurare sempre HTTPS prima di esporre l'applicazione in produzione.
> Senza TLS le credenziali di login e i cookie di sessione viaggiano in chiaro.

Il file `ipam.conf` incluso nel repository configura già Apache con **HTTPS obbligatorio**:
un VirtualHost `:80` che redirige su HTTPS e un VirtualHost `:443` con SSL e proxy verso Gunicorn.
Prima di copiarlo, adattare `ServerName` e i percorsi del certificato TLS.

**Installazione consigliata (automatica con `install.sh`):**

```bash
sudo ./install.sh
# → genera certificato TLS autofirmato, configura Apache HTTPS, avvia Gunicorn
```

**Installazione manuale (Debian/Ubuntu):**

```bash
# 1. Generare certificato TLS autofirmato (se non si ha un certificato firmato)
sudo openssl req -x509 -newkey rsa:2048 \
    -keyout /etc/ssl/private/ipam.key -out /etc/ssl/certs/ipam.crt \
    -days 3650 -nodes -subj "/CN=$(hostname -f)/O=IPAM/C=IT"
sudo chmod 600 /etc/ssl/private/ipam.key

# 2. Adattare ipam.conf (ServerName, percorsi SSL, directory installazione)
#    poi copiarlo e abilitarlo
sudo cp ipam.conf /etc/apache2/sites-available/ipam.conf
sudo a2ensite ipam
sudo a2enmod proxy proxy_http ssl headers
sudo systemctl reload apache2
```

**Installazione manuale (RHEL/Rocky/CentOS):**

```bash
sudo yum install mod_ssl   # o: dnf install mod_ssl
# Adattare ipam.conf (ServerName, percorsi, directory)
sudo cp ipam.conf /etc/httpd/conf.d/ipam.conf
sudo systemctl reload httpd
```

Per ottenere un certificato firmato gratuitamente con Let's Encrypt:

```bash
sudo apt install certbot python3-certbot-apache   # Debian/Ubuntu
sudo certbot --apache -d ipam.example.com
```

L'applicazione sarà accessibile su `https://<server>/ipam`.
Il traffico HTTP (porta 80) viene automaticamente reindirizzato su HTTPS.

---

## Funzioni principali

### Dashboard
- Contatori globali: reti totali, IP totali, IP in uso, VLAN configurate
- Percentuale di utilizzo globale dell'indirizzamento
- Top 5 subnet più utilizzate con barra di utilizzo
- Azioni rapide: aggiungi rete, aggiungi IP, aggiungi VLAN, cerca rete

### Gestione Reti
- **Tipi di rete:** supernet (blocchi aggregati), subnet (reti operative), VLAN
- **Albero gerarchico** navigabile nella sidebar sinistra
- **Campi disponibili per ogni rete:** nome, indirizzo/CIDR, subnet mask, VLAN, location, country, site, usage, zone, gateway, stato, descrizione
- Creazione automatica degli IP record fino a /16 (65.534 host)
- Calcolo automatico delle relazioni parent/child basato sui range IP

### Indirizzi IP
- **Campi per ogni record IP:** IP, hostname, MAC address, switch, porta switch, tipo dispositivo, OS, stato, ultima vista, descrizione
- **Stati disponibili:** `used`, `available`, `reserved`, `dhcp`
- Ricerca globale con filtri combinati (stato + tipo + OS + switch + rete)
- Ordinamento su tutte le colonne

### VLAN
- Anagrafica VLAN con ID, nome, descrizione e stato
- Vista espandibile con tutte le subnet appartenenti a ciascuna VLAN
- Segnalazione delle subnet senza VLAN assegnata

### Modifica Bulk
- Selezione multipla di reti o indirizzi IP
- Aggiornamento simultaneo di uno o più campi su tutti i record selezionati
- Modalità filtro inline per raffinare la selezione

### Scanner IP
Il modulo scanner (`scanner.py`) esegue per ogni subnet:
1. **Ping ICMP** verso ogni indirizzo IP
2. **Probe TCP** (opzionale) su porte configurabili — utile per host con firewall ICMP
3. **Reverse DNS lookup** tramite i server DNS configurati nelle impostazioni
4. Aggiornamento del record IP con hostname, stato e data ultima vista

### SNMP Discovery
Interroga gli switch tramite SNMP per raccogliere:
- Tabella ARP → MAC address degli host
- Bridge/FDB table → porta dello switch per ogni MAC
- Tipo dispositivo e informazioni di sistema

### Scheduler automatico
Gestione dei **Gruppi di Scansione** dalle Impostazioni:
- Ogni gruppo può contenere una o più subnet
- Orario di esecuzione configurabile (HH:MM)
- Esecuzione automatica tramite APScheduler integrato in Flask

### Autenticazione
- **Locale:** username e password hashata con Werkzeug (pbkdf2:sha256)
- **LDAP/Active Directory:** configurabile nelle Impostazioni con server, base DN e bind DN
- Log di autenticazione (login OK, falliti, logout) in `instance/auth.log`

### API REST

Tutti gli endpoint restituiscono JSON. Prefisso base: `https://server/ipam/api/`.

**Autenticazione:** basata su cookie di sessione. Occorre prima effettuare il login e conservare il cookie per le chiamate successive.

> **Nota:** Usare sempre HTTPS per le chiamate API. Con HTTP le credenziali e i cookie di sessione transitano in chiaro.

| Metodo | Endpoint | Descrizione |
|---|---|---|
| POST | `/login` | Autenticazione (restituisce cookie di sessione) |
| GET | `/api/version` | Versione applicazione |
| GET | `/api/stats` | Statistiche globali |
| GET | `/api/networks` | Lista reti (con filtri `?type=`, `?q=`) |
| POST | `/api/networks` | Crea nuova rete |
| PUT | `/api/networks/<id>` | Aggiorna rete |
| DELETE | `/api/networks/<id>` | Elimina rete |
| GET | `/api/networks/tree` | Albero gerarchico reti |
| POST | `/api/networks/<id>/scan` | Avvia scansione subnet |
| GET | `/api/ip-records` | Lista IP (filtri: `?network_id=`, `?q=`, `?status=`, `?limit=`, `?offset=`) |
| POST | `/api/ip-records` | Crea record IP |
| PUT | `/api/ip-records/<id>` | Aggiorna record IP |
| DELETE | `/api/ip-records/<id>` | Elimina record IP |
| PUT | `/api/ip-records/<id>/clear` | Libera un IP (reset hostname/MAC/stato) |
| GET | `/api/vlans` | Lista VLAN |
| POST | `/api/vlans` | Crea VLAN |
| PUT | `/api/vlans/<id>` | Aggiorna VLAN |
| DELETE | `/api/vlans/<id>` | Elimina VLAN |
| GET | `/api/logs` | Log scansioni |
| PUT | `/api/bulk-edit/networks` | Modifica bulk reti |
| PUT | `/api/bulk-edit/ip-records` | Modifica bulk IP |

---

### Esempi di utilizzo API

#### Autenticazione

L'API usa sessioni basate su cookie. Ogni client deve prima autenticarsi e poi includere il cookie in tutte le richieste successive.

**curl:**
```bash
# Login — salva il cookie in un file
curl -s -c cookies.txt -X POST https://server/ipam/login \
  -d "username=admin&password=admin"

# Verifica versione (con cookie)
curl -s -b cookies.txt https://server/ipam/api/version
```

**Python (requests):**
```python
import requests

BASE = "https://server/ipam"
session = requests.Session()

# Login — il cookie viene gestito automaticamente dalla session
session.post(f"{BASE}/login", data={"username": "admin", "password": "admin"})

# Da qui in poi la session include automaticamente il cookie
resp = session.get(f"{BASE}/api/version")
print(resp.json())
# → {"version": "1.9.0", "build": "2026-05-28", ...}
```

---

#### Lettura reti e subnet

**curl:**
```bash
# Tutte le reti
curl -s -b cookies.txt http://server/ipam/api/networks | python3 -m json.tool

# Solo le subnet (filtro per tipo)
curl -s -b cookies.txt "http://server/ipam/api/networks?type=subnet"

# Ricerca per nome o indirizzo
curl -s -b cookies.txt "http://server/ipam/api/networks?q=10.86"
```

**Python:**
```python
# Lista tutte le reti
reti = session.get(f"{BASE}/api/networks").json()
for r in reti:
    print(f"{r['address']}/{r['cidr']}  {r['name']}  ({r['network_type']})")

# Cerca solo le subnet del sito "Milano"
subnet_mi = session.get(f"{BASE}/api/networks", params={"type": "subnet", "q": "Milano"}).json()
```

---

#### Creazione di una rete

**curl:**
```bash
curl -s -b cookies.txt -X POST http://server/ipam/api/networks \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Server Farm DMZ",
    "address": "10.50.10.0",
    "cidr": 24,
    "network_type": "subnet",
    "location": "Data Center Nord",
    "description": "Subnet server DMZ",
    "vlan_id": 3
  }'
```

**Python:**
```python
nuova_rete = session.post(f"{BASE}/api/networks", json={
    "name":         "Server Farm DMZ",
    "address":      "10.50.10.0",
    "cidr":         24,
    "network_type": "subnet",
    "location":     "Data Center Nord",
    "description":  "Subnet server DMZ",
    "vlan_id":      3
})
print(nuova_rete.status_code)   # 201 = creata
print(nuova_rete.json())        # → {"id": 25, "address": "10.50.10.0", ...}
```

> La rete viene normalizzata automaticamente (es. `10.50.10.5/24` → `10.50.10.0/24`).
> Il `parent_id` viene calcolato automaticamente se non specificato.

---

#### Lettura indirizzi IP

**curl:**
```bash
# Tutti gli IP di una subnet (network_id=5)
curl -s -b cookies.txt "http://server/ipam/api/ip-records?network_id=5"

# IP in uso, con paginazione
curl -s -b cookies.txt "http://server/ipam/api/ip-records?status=used&limit=100&offset=0"

# Ricerca per hostname o MAC
curl -s -b cookies.txt "http://server/ipam/api/ip-records?q=srv-web"
```

**Python:**
```python
# Tutti gli IP di una subnet con paginazione automatica
def get_all_ips(network_id):
    tutti = []
    offset = 0
    while True:
        r = session.get(f"{BASE}/api/ip-records", params={
            "network_id": network_id, "limit": 500, "offset": offset
        }).json()
        tutti.extend(r["items"])
        if len(tutti) >= r["total"]:
            break
        offset += 500
    return tutti

ip_list = get_all_ips(network_id=5)
print(f"Trovati {len(ip_list)} indirizzi")
for ip in ip_list:
    print(f"{ip['ip_address']}  {ip['hostname'] or '—'}  {ip['status']}")
```

---

#### Registrazione e aggiornamento di un IP

**curl:**
```bash
# Registra un nuovo IP
curl -s -b cookies.txt -X POST http://server/ipam/api/ip-records \
  -H "Content-Type: application/json" \
  -d '{
    "ip_address": "10.50.10.15",
    "hostname":   "srv-web-01",
    "mac_address":"AA:BB:CC:DD:EE:FF",
    "device_type":"Server",
    "os_type":    "Linux",
    "status":     "used",
    "network_id": 5,
    "description":"Web server principale"
  }'

# Aggiorna hostname di un IP esistente (id=42)
curl -s -b cookies.txt -X PUT http://server/ipam/api/ip-records/42 \
  -H "Content-Type: application/json" \
  -d '{"hostname": "srv-web-01-new", "status": "used"}'

# Libera un IP (lo segna come disponibile e cancella i dati)
curl -s -b cookies.txt -X PUT http://server/ipam/api/ip-records/42/clear
```

**Python:**
```python
# Registra un IP
resp = session.post(f"{BASE}/api/ip-records", json={
    "ip_address":  "10.50.10.15",
    "hostname":    "srv-web-01",
    "mac_address": "AA:BB:CC:DD:EE:FF",
    "device_type": "Server",
    "os_type":     "Linux",
    "status":      "used",
    "network_id":  5
})
ip = resp.json()
print(f"IP creato con id={ip['id']}")

# Aggiorna
session.put(f"{BASE}/api/ip-records/{ip['id']}", json={"hostname": "srv-web-01-new"})

# Libera l'IP
session.put(f"{BASE}/api/ip-records/{ip['id']}/clear")
```

---

#### Statistiche globali

**Python:**
```python
stats = session.get(f"{BASE}/api/stats").json()
print(f"Reti totali : {stats['total_networks']}")
print(f"IP totali   : {stats['total_ips']}")
print(f"IP in uso   : {stats['used_ips']}")
print(f"IP liberi   : {stats['free_ips']}")
print(f"Utilizzo    : {stats['usage_percent']:.1f}%")
```

---

#### Codici di risposta HTTP

| Codice | Significato |
|---|---|
| `200` | OK — operazione riuscita |
| `201` | Created — risorsa creata |
| `400` | Bad Request — parametri non validi |
| `401` | Unauthorized — non autenticato (cookie mancante/scaduto) |
| `404` | Not Found — risorsa non trovata |
| `409` | Conflict — risorsa già esistente (es. IP duplicato) |

---

## Struttura del progetto

```
/var/www/html/ipam/          ← directory di installazione
├── app.py                   # Applicazione Flask — modelli, route, API
├── wsgi_gunicorn.py         # Entry point produzione (Gunicorn + middleware)
├── main.py                  # Entry point sviluppo (porta 5000)
├── scanner.py               # Scanner IP (ping/DNS/SNMP)
├── requirements.txt         # Dipendenze Python
├── install.sh               # Script installazione
├── update.sh                # Script aggiornamento
├── ipam.conf                # Configurazione Apache
├── ipam-scan.service        # Servizio systemd
├── templates/               # Template Jinja2
│   ├── base.html            # Layout base con navbar
│   ├── index.html           # Dashboard
│   ├── networks.html        # Lista reti
│   ├── network_detail.html  # Dettaglio rete/subnet
│   ├── ip_addresses.html    # Ricerca globale IP
│   ├── vlans.html           # Gestione VLAN
│   ├── bulk_edit.html       # Modifica bulk
│   ├── logs.html            # Log scansioni
│   ├── settings.html        # Impostazioni
│   ├── docs.html            # Documentazione
│   └── login.html           # Pagina di login
├── static/
│   └── css/main.css         # Stile dell'applicazione
├── instance/
│   └── ipam.db              # Database SQLite (NON sovrascrivere)
├── venv/                    # Virtualenv Python (creato da install.sh)
├── scripts/
│   └── post-merge.sh        # Script post-merge CI/CD
└── .github/
    └── workflows/
        └── deploy.yml       # GitHub Actions — test + build zip + deploy SSH
```

---

## Avvio in sviluppo

```bash
cd /var/www/html/ipam
python3 main.py
# oppure con il virtualenv:
venv/bin/python main.py
```

L'app è disponibile su `http://localhost:5000/ipam` (solo sviluppo locale; non esporre su rete senza TLS).

Credenziali default: `admin` / `admin` (da cambiare immediatamente in produzione).

---

## Comandi utili

```bash
# Stato del servizio
sudo systemctl status ipam

# Restart del servizio
sudo systemctl restart ipam

# Log in tempo reale
sudo journalctl -u ipam -f

# Verifica versione API
curl http://localhost:8000/ipam/api/version

# Verifica dipendenze Python
curl http://localhost:8000/ipam/api/venv-info
```

---

## Licenza

Questo progetto è distribuito sotto la licenza GNU General Public License v3.0.
Vedi il file [LICENSE](LICENSE) per maggiori dettagli.
