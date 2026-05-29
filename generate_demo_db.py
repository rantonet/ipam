"""
Genera un database demo pulito per la documentazione GitHub.
Tutti i dati sono inventati — nessun riferimento a sistemi reali.
"""
import os, sys, sqlite3, hashlib, random, struct, socket
from datetime import datetime, timedelta

DB_PATH = "instance/ipam_demo.db"
os.makedirs("instance", exist_ok=True)
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# ---------- SCHEMA ----------
cur.executescript("""
CREATE TABLE networks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(120) NOT NULL,
    address VARCHAR(50) NOT NULL,
    cidr INTEGER NOT NULL,
    mask VARCHAR(20),
    vlan_id INTEGER,
    location VARCHAR(200),
    description VARCHAR(500),
    country VARCHAR(100),
    site VARCHAR(200),
    usage VARCHAR(200),
    zone VARCHAR(100),
    gateway VARCHAR(50),
    parent_id INTEGER REFERENCES networks(id),
    network_type VARCHAR(20) DEFAULT 'subnet',
    status VARCHAR(20) DEFAULT 'active',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_scan DATETIME
);
CREATE TABLE ip_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip_address VARCHAR(50) NOT NULL UNIQUE,
    ip_int INTEGER,
    hostname VARCHAR(200),
    mac_address VARCHAR(20),
    switch_name VARCHAR(100),
    switch_port VARCHAR(50),
    snmp_updated_at DATETIME,
    device_type VARCHAR(50),
    os_type VARCHAR(100),
    status VARCHAR(20) DEFAULT 'used',
    network_id INTEGER REFERENCES networks(id),
    description VARCHAR(500),
    last_seen DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE vlans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vlan_id INTEGER NOT NULL UNIQUE,
    name VARCHAR(100) NOT NULL,
    description VARCHAR(500),
    status VARCHAR(20) DEFAULT 'active',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE app_config (
    key VARCHAR(100) PRIMARY KEY,
    value TEXT DEFAULT ''
);
CREATE TABLE scan_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_type VARCHAR(20),
    target VARCHAR(200),
    started_at DATETIME,
    completed_at DATETIME,
    status VARCHAR(20) DEFAULT 'running',
    hosts_total INTEGER,
    hosts_found INTEGER,
    hosts_updated INTEGER,
    hosts_error INTEGER,
    duration_s INTEGER,
    error_msg VARCHAR(500),
    notes TEXT
);
CREATE TABLE scan_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(200) NOT NULL,
    group_type VARCHAR(20) DEFAULT 'subnet',
    schedule_time VARCHAR(5),
    enabled BOOLEAN DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE scan_group_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER REFERENCES scan_groups(id),
    network_id INTEGER REFERENCES networks(id),
    ip_range VARCHAR(100)
);
CREATE TABLE local_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username VARCHAR(100) NOT NULL UNIQUE,
    password_hash VARCHAR(256) NOT NULL,
    display_name VARCHAR(200),
    is_admin BOOLEAN DEFAULT 0,
    enabled BOOLEAN DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_networks_address_cidr ON networks(address, cidr);
CREATE INDEX idx_networks_parent_id ON networks(parent_id);
CREATE INDEX idx_ip_records_network_id ON ip_records(network_id);
CREATE INDEX idx_ip_records_status ON ip_records(status);
CREATE INDEX idx_ip_records_ip_int ON ip_records(ip_int);
""")

# ---------- HELPERS ----------
def ip2int(ip):
    return struct.unpack("!I", socket.inet_aton(ip))[0]

def int2ip(n):
    return socket.inet_ntoa(struct.pack("!I", n))

def make_mac():
    return ":".join(f"{random.randint(0,255):02x}" for _ in range(6))

def pw_hash(pw):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), b"ipam_salt", 260000).hex()

NOW  = datetime(2026, 5, 28, 10, 0, 0)
def ago(days=0, hours=0, minutes=0):
    return (NOW - timedelta(days=days, hours=hours, minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")

# ---------- USERS ----------
cur.execute("INSERT INTO local_users(username,password_hash,display_name,is_admin,enabled) VALUES(?,?,?,1,1)",
            ("admin", pw_hash("admin"), "Amministratore"))
cur.execute("INSERT INTO local_users(username,password_hash,display_name,is_admin,enabled) VALUES(?,?,?,0,1)",
            ("operatore", pw_hash("operatore"), "Operatore Rete"))

# ---------- APP CONFIG ----------
configs = [
    ("dns_primary",   "8.8.8.8"),
    ("dns_secondary", "8.8.4.4"),
    ("tcp_ports",     "22,80,443,3389"),
    ("auth_mode",     "local"),
    ("ldap_server",   ""),
    ("ldap_base_dn",  ""),
    ("ldap_bind_dn",  ""),
    ("snmp_version",  "2c"),
]
cur.executemany("INSERT INTO app_config(key,value) VALUES(?,?)", configs)

# ---------- VLAN ----------
vlans = [
    (10,  "Uffici",         "Postazioni di lavoro uffici"),
    (20,  "Server",         "Server aziendali e virtualizzazione"),
    (30,  "DMZ",            "Zona demilitarizzata — servizi pubblici"),
    (40,  "VoIP",           "Telefonia IP"),
    (50,  "Stampanti",      "Periferiche di stampa"),
    (100, "Gestione",       "Out-of-band management switch/router"),
    (200, "Ospiti WiFi",    "Rete ospiti isolata"),
    (300, "Videosorveglianza", "Telecamere IP CCTV"),
]
for vid, name, desc in vlans:
    cur.execute("INSERT INTO vlans(vlan_id,name,description) VALUES(?,?,?)", (vid, name, desc))

# ---------- NETWORKS ----------
def net(name, addr, cidr, mask, parent=None, vlan=None, loc=None, desc=None,
        site=None, usage=None, zone=None, gw=None, ntype="subnet",
        status="active", last_scan=None):
    cur.execute("""INSERT INTO networks(name,address,cidr,mask,vlan_id,location,description,
                   site,usage,zone,gateway,parent_id,network_type,status,created_at,updated_at,last_scan)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (name, addr, cidr, mask, vlan, loc, desc, site, usage, zone, gw,
                 parent, ntype, status, ago(60), ago(0), last_scan))
    return cur.lastrowid

# Supernet radice
r1 = net("Rete Aziendale 10.x",     "10.0.0.0",   8,  "255.0.0.0",
         ntype="supernet", desc="Spazio IP privato principale", loc="Tutti i siti")
r2 = net("Rete Aziendale 172.16.x", "172.16.0.0", 12, "255.240.0.0",
         ntype="supernet", desc="Spazio IP secondario", loc="Tutti i siti")
r3 = net("Rete Locale 192.168.x",   "192.168.0.0",16, "255.255.0.0",
         ntype="supernet", desc="Reti locali laboratori", loc="Sede Centrale")

# Supernet di sede
hq = net("Sede Centrale — Roma",  "10.10.0.0", 16, "255.255.0.0",
         parent=r1, ntype="supernet", loc="Roma, Via Nazionale 1", site="HQ")
br1 = net("Filiale Milano",        "10.20.0.0", 16, "255.255.0.0",
          parent=r1, ntype="supernet", loc="Milano, Corso Sempione 5", site="MIL")
br2 = net("Filiale Napoli",        "10.30.0.0", 16, "255.255.0.0",
          parent=r1, ntype="supernet", loc="Napoli, Via Toledo 22", site="NAP")
br3 = net("Data Center Nord",      "172.16.0.0",16, "255.255.0.0",
          parent=r2, ntype="supernet", loc="Milano, DC1", site="DC-NORD")

# Subnet Sede Centrale
hq_srv = net("Server Farm HQ",    "10.10.1.0", 24, "255.255.255.0",
             parent=hq, vlan=20, loc="Roma HQ", site="HQ",
             usage="Server virtualizzazione e storage", zone="Core",
             gw="10.10.1.1", last_scan=ago(0,2))
hq_ufc = net("Uffici Piano 1",    "10.10.2.0", 24, "255.255.255.0",
             parent=hq, vlan=10, loc="Roma HQ", site="HQ",
             usage="Postazioni utenti Piano 1", zone="User",
             gw="10.10.2.1", last_scan=ago(0,1))
hq_ufc2 = net("Uffici Piano 2",   "10.10.3.0", 24, "255.255.255.0",
              parent=hq, vlan=10, loc="Roma HQ", site="HQ",
              usage="Postazioni utenti Piano 2", zone="User",
              gw="10.10.3.1", last_scan=ago(0,1))
hq_dmz = net("DMZ Pubblica",      "10.10.10.0",24, "255.255.255.0",
             parent=hq, vlan=30, loc="Roma HQ", site="HQ",
             usage="Web server, reverse proxy, WAF", zone="DMZ",
             gw="10.10.10.1", last_scan=ago(1))
hq_voip = net("VoIP HQ",          "10.10.20.0",24, "255.255.255.0",
              parent=hq, vlan=40, loc="Roma HQ", site="HQ",
              usage="Telefonia IP centralino", zone="Voice",
              gw="10.10.20.1", last_scan=ago(2))
hq_mgmt = net("Management HQ",    "10.10.100.0",24,"255.255.255.0",
              parent=hq, vlan=100, loc="Roma HQ", site="HQ",
              usage="OOB management dispositivi", zone="MGMT",
              gw="10.10.100.1", last_scan=ago(1))
hq_guest = net("WiFi Ospiti HQ",  "10.10.200.0",24,"255.255.255.0",
               parent=hq, vlan=200, loc="Roma HQ", site="HQ",
               usage="Rete ospiti isolata", zone="Guest",
               gw="10.10.200.1", last_scan=ago(3))

# Subnet Filiale Milano
mil_ufc = net("Uffici Milano",     "10.20.1.0", 24, "255.255.255.0",
              parent=br1, vlan=10, loc="Milano", site="MIL",
              usage="Postazioni utenti", zone="User",
              gw="10.20.1.1", last_scan=ago(0,3))
mil_srv = net("Server Milano",     "10.20.10.0",24, "255.255.255.0",
              parent=br1, vlan=20, loc="Milano", site="MIL",
              usage="Server locali filiale", zone="Core",
              gw="10.20.10.1", last_scan=ago(1))
mil_voip = net("VoIP Milano",      "10.20.20.0",24, "255.255.255.0",
               parent=br1, vlan=40, loc="Milano", site="MIL",
               usage="Telefonia IP", zone="Voice",
               gw="10.20.20.1", last_scan=ago(2))

# Subnet Filiale Napoli
nap_ufc = net("Uffici Napoli",     "10.30.1.0", 24, "255.255.255.0",
              parent=br2, vlan=10, loc="Napoli", site="NAP",
              usage="Postazioni utenti", zone="User",
              gw="10.30.1.1", last_scan=ago(0,4))
nap_srv = net("Server Napoli",     "10.30.10.0",24, "255.255.255.0",
              parent=br2, vlan=20, loc="Napoli", site="NAP",
              usage="Server locali", zone="Core",
              gw="10.30.10.1", last_scan=ago(2))

# Subnet Data Center
dc_prod = net("Produzione DC",     "172.16.1.0", 24, "255.255.255.0",
              parent=br3, vlan=20, loc="DC Nord", site="DC-NORD",
              usage="Server produzione", zone="PROD",
              gw="172.16.1.1", last_scan=ago(0,1))
dc_test = net("Test/Staging DC",   "172.16.2.0", 24, "255.255.255.0",
              parent=br3, vlan=20, loc="DC Nord", site="DC-NORD",
              usage="Ambiente di test e staging", zone="TEST",
              gw="172.16.2.1", last_scan=ago(1))
dc_backup = net("Backup DC",       "172.16.3.0", 24, "255.255.255.0",
                parent=br3, vlan=20, loc="DC Nord", site="DC-NORD",
                usage="Infrastruttura di backup", zone="BACKUP",
                gw="172.16.3.1", last_scan=ago(3))

# Subnet 192.168
lab = net("Laboratorio R&D",       "192.168.1.0",24, "255.255.255.0",
          parent=r3, loc="Roma HQ", site="HQ",
          usage="Laboratorio sviluppo e test", zone="LAB",
          gw="192.168.1.1", last_scan=ago(5))
cctv = net("Videosorveglianza",    "192.168.10.0",24,"255.255.255.0",
           parent=r3, vlan=300, loc="Tutti i siti", site="HQ",
           usage="Telecamere IP CCTV", zone="IoT",
           gw="192.168.10.1", last_scan=ago(7))

# ---------- IP RECORDS ----------
random.seed(42)

device_types = ["server", "workstation", "printer", "switch", "router",
                "phone", "camera", "ap", "firewall", "storage"]
os_types = ["Windows Server 2022", "Windows Server 2019", "Windows 11 Pro",
            "Windows 10 Pro", "Ubuntu 22.04 LTS", "RHEL 9", "VMware ESXi 8",
            "Cisco IOS 15.x", "Debian 12", "FreeBSD 14"]
sw_names = ["SW-HQ-CORE-01", "SW-HQ-ACCESS-01", "SW-HQ-ACCESS-02",
            "SW-MIL-CORE-01", "SW-NAP-CORE-01", "SW-DC-TOR-01"]
sw_ports = [f"Gi{random.randint(0,1)}/{random.randint(0,3)}/{random.randint(1,48)}" for _ in range(30)]

def hostname_for(prefix, n):
    return f"{prefix}-{n:03d}"

def make_ips(net_id, base_ip, count, used_ratio=0.65,
             prefix="HOST", dev_override=None, os_override=None,
             sw_name=None):
    base = ip2int(base_ip)
    rows = []
    total = min(count, 254)
    used_count = int(total * used_ratio)
    indices = list(range(1, total + 1))

    for i, idx in enumerate(indices):
        ip = int2ip(base + idx)
        ip_i = base + idx
        status = "used" if i < used_count else "available"
        if i == 0:
            status = "used"  # gateway

        hn  = None; mac = None; dev = None; os_ = None
        sw  = None; swp = None; ls  = None; desc = None

        if status == "used":
            hn  = hostname_for(prefix, idx)
            mac = make_mac()
            dev = dev_override or random.choice(device_types)
            os_ = os_override or random.choice(os_types)
            sw  = sw_name or random.choice(sw_names)
            swp = random.choice(sw_ports)
            ls  = ago(random.randint(0, 5), random.randint(0, 23))

        rows.append((ip, ip_i, hn, mac, sw, swp,
                     ago(1) if sw else None,
                     dev, os_, status, net_id, desc, ls,
                     ago(random.randint(30,60)), ago(0)))
    cur.executemany("""INSERT OR IGNORE INTO ip_records
        (ip_address,ip_int,hostname,mac_address,switch_name,switch_port,
         snmp_updated_at,device_type,os_type,status,network_id,description,
         last_seen,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)

# Server Farm HQ — server + VMware
make_ips(hq_srv,   "10.10.1.0",   60, used_ratio=0.80, prefix="SRV-HQ",
         dev_override="server", sw_name="SW-HQ-CORE-01")
# Uffici Piano 1 — workstation Windows
make_ips(hq_ufc,   "10.10.2.0",   80, used_ratio=0.75, prefix="WS-P1",
         dev_override="workstation", os_override="Windows 11 Pro", sw_name="SW-HQ-ACCESS-01")
# Uffici Piano 2
make_ips(hq_ufc2,  "10.10.3.0",   60, used_ratio=0.60, prefix="WS-P2",
         dev_override="workstation", os_override="Windows 11 Pro", sw_name="SW-HQ-ACCESS-02")
# DMZ
make_ips(hq_dmz,   "10.10.10.0",  20, used_ratio=0.70, prefix="DMZ-HQ",
         dev_override="server", sw_name="SW-HQ-CORE-01")
# VoIP
make_ips(hq_voip,  "10.10.20.0",  40, used_ratio=0.60, prefix="PHONE-HQ",
         dev_override="phone", os_override="Cisco IOS 15.x", sw_name="SW-HQ-ACCESS-01")
# MGMT
make_ips(hq_mgmt,  "10.10.100.0", 30, used_ratio=0.50, prefix="MGMT-HQ",
         dev_override="switch", sw_name="SW-HQ-CORE-01")
# WiFi Ospiti
make_ips(hq_guest, "10.10.200.0", 50, used_ratio=0.30, prefix="GUEST-HQ")
# Milano Uffici
make_ips(mil_ufc,  "10.20.1.0",   50, used_ratio=0.70, prefix="WS-MIL",
         dev_override="workstation", os_override="Windows 11 Pro", sw_name="SW-MIL-CORE-01")
# Milano Server
make_ips(mil_srv,  "10.20.10.0",  20, used_ratio=0.65, prefix="SRV-MIL",
         dev_override="server", sw_name="SW-MIL-CORE-01")
# Milano VoIP
make_ips(mil_voip, "10.20.20.0",  25, used_ratio=0.55, prefix="PHONE-MIL",
         dev_override="phone", sw_name="SW-MIL-CORE-01")
# Napoli Uffici
make_ips(nap_ufc,  "10.30.1.0",   35, used_ratio=0.65, prefix="WS-NAP",
         dev_override="workstation", sw_name="SW-NAP-CORE-01")
# Napoli Server
make_ips(nap_srv,  "10.30.10.0",  15, used_ratio=0.60, prefix="SRV-NAP",
         dev_override="server", sw_name="SW-NAP-CORE-01")
# DC Produzione
make_ips(dc_prod,  "172.16.1.0",  50, used_ratio=0.85, prefix="PROD-DC",
         dev_override="server", os_override="VMware ESXi 8", sw_name="SW-DC-TOR-01")
# DC Test
make_ips(dc_test,  "172.16.2.0",  30, used_ratio=0.55, prefix="TEST-DC",
         dev_override="server", sw_name="SW-DC-TOR-01")
# DC Backup
make_ips(dc_backup,"172.16.3.0",  20, used_ratio=0.40, prefix="BCK-DC",
         dev_override="storage", sw_name="SW-DC-TOR-01")
# Lab
make_ips(lab,      "192.168.1.0", 30, used_ratio=0.50, prefix="LAB")
# CCTV
make_ips(cctv,     "192.168.10.0",25, used_ratio=0.70, prefix="CAM",
         dev_override="camera", os_override="Embedded Linux")

# ---------- SCAN LOGS ----------
scan_data = [
    ("subnet", "10.10.1.0/24",   ago(0,2), ago(0,1,58), "completed", 60, 48, 48, 0, 118),
    ("subnet", "10.10.2.0/24",   ago(0,1), ago(0,0,55), "completed", 80, 61, 58, 3,  55),
    ("subnet", "10.10.3.0/24",   ago(0,1), ago(0,0,48), "completed", 60, 37, 36, 1,  48),
    ("subnet", "10.10.10.0/24",  ago(1),   ago(1,0,-2), "completed", 20, 14, 14, 0,   2),
    ("subnet", "10.10.20.0/24",  ago(2),   ago(2,0,-3), "completed", 40, 25, 24, 1,   3),
    ("subnet", "10.20.1.0/24",   ago(0,3), ago(0,2,55), "completed", 50, 36, 35, 1,  55),
    ("subnet", "10.30.1.0/24",   ago(0,4), ago(0,3,52), "completed", 35, 23, 23, 0,  52),
    ("subnet", "172.16.1.0/24",  ago(0,1), ago(0,0,58), "completed", 50, 43, 42, 1,  58),
    ("snmp",   "SW-HQ-CORE-01",  ago(1),   ago(1,0,-5), "completed",  0,  0, 12, 0,   5),
    ("snmp",   "SW-HQ-ACCESS-01",ago(1),   ago(1,0,-4), "completed",  0,  0, 28, 0,   4),
    ("snmp",   "SW-MIL-CORE-01", ago(2),   ago(2,0,-3), "completed",  0,  0, 18, 0,   3),
    ("global", "Tutti i siti",   ago(7),   ago(6,20),   "completed",400,298,290, 8, 1440),
    ("subnet", "192.168.1.0/24", ago(5),   ago(5,0,-8), "completed", 30, 15, 14, 1,   8),
    ("subnet", "10.10.100.0/24", ago(1),   ago(1,0,-2), "completed", 30, 15, 15, 0,   2),
]
cur.executemany("""INSERT INTO scan_logs
    (scan_type,target,started_at,completed_at,status,
     hosts_total,hosts_found,hosts_updated,hosts_error,duration_s)
    VALUES(?,?,?,?,?,?,?,?,?,?)""", scan_data)

# ---------- SCAN GROUPS ----------
cur.execute("INSERT INTO scan_groups(name,group_type,schedule_time,enabled) VALUES(?,?,?,?)",
            ("Scansione Notturna — Sede HQ", "subnet", "02:00", 1))
sg1 = cur.lastrowid
cur.execute("INSERT INTO scan_groups(name,group_type,schedule_time,enabled) VALUES(?,?,?,?)",
            ("Scansione Filiali", "subnet", "03:00", 1))
sg2 = cur.lastrowid
cur.execute("INSERT INTO scan_groups(name,group_type,schedule_time,enabled) VALUES(?,?,?,?)",
            ("SNMP Discovery Giornaliera", "snmp", "06:00", 1))
sg3 = cur.lastrowid

for nid in [hq_srv, hq_ufc, hq_ufc2, hq_dmz, hq_voip, hq_mgmt]:
    cur.execute("INSERT INTO scan_group_items(group_id,network_id) VALUES(?,?)", (sg1, nid))
for nid in [mil_ufc, mil_srv, nap_ufc, nap_srv]:
    cur.execute("INSERT INTO scan_group_items(group_id,network_id) VALUES(?,?)", (sg2, nid))

conn.commit()

# Stats finali
cur.execute("SELECT COUNT(*) FROM networks")
n_net = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM ip_records")
n_ip = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM ip_records WHERE status='used'")
n_used = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM vlans")
n_vlan = cur.fetchone()[0]

conn.close()

size = os.path.getsize(DB_PATH) / 1024
print(f"Database demo creato: {DB_PATH}")
print(f"  Reti:       {n_net}")
print(f"  IP totali:  {n_ip}  (usati: {n_used})")
print(f"  VLAN:       {n_vlan}")
print(f"  Dimensione: {size:.1f} KB")
