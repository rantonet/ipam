# -*- coding: utf-8 -*-
"""
scanner.py  --  IPAM Network Scanner
=====================================
Esegue DNS PTR lookup + ping per ogni IP di una subnet.
Aggiorna il DB con hostname, stato e last_seen.

Utilizzo standalone:
    python3 scanner.py --subnet-id 42
    python3 scanner.py --all

Viene anche importato da app.py per lo scan asincrono via API.
"""

import os
import sys
import socket
import errno
import subprocess
import ipaddress
import threading
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Logging ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('ipam.scanner')

# ── Configurazione DNS ────────────────────────────────────────────
# Server DNS di default — configurare dalla pagina Impostazioni
DNS_SERVERS = []  # vuoto = usa il resolver di sistema come fallback

# Override runtime impostato da AppConfig tramite app.py
_runtime_dns = None

# Probe TCP: fallback quando il ping ICMP è bloccato da firewall
_runtime_tcp_enabled = True
_runtime_tcp_ports   = [22, 80, 443, 445, 3389, 135, 8080, 8443]


def set_runtime_dns(servers):
    """Aggiorna i server DNS a runtime (letti da AppConfig in app.py)."""
    global _runtime_dns
    _runtime_dns = list(servers) if servers else None


def set_runtime_tcp(enabled, ports=None):
    """Aggiorna le impostazioni TCP probe a runtime (letti da AppConfig in app.py)."""
    global _runtime_tcp_enabled, _runtime_tcp_ports
    _runtime_tcp_enabled = bool(enabled)
    if ports:
        _runtime_tcp_ports = list(ports)

# ── Configurazione SNMP ──────────────────────────────────────────
SNMP_COMMUNITY = 'public'   # community string SNMPv2c (default — configurare dalla pagina Impostazioni)
SNMP_PORT      = 161
SNMP_TIMEOUT   = 3          # secondi timeout SNMP
# Router/switch da interrogare per le ARP table
# Lascia vuoto per usare solo ping+DNS
SNMP_ROUTERS   = []         # es. ['10.112.1.1', '10.114.1.1']

# Timeout e parallelismo
PING_TIMEOUT   = 1     # secondi per ping
DNS_TIMEOUT    = 2     # secondi per query DNS
MAX_WORKERS    = 50    # thread paralleli per subnet
MAX_SUBNET_WORKERS = 4 # subnet parallele (per scan --all)

# ── Stato scan in memoria (condiviso tra thread) ──────────────────
_scan_status = {}   # { network_id: { 'running': bool, 'progress': int, 'total': int, ... } }
_scan_lock   = threading.Lock()


def get_scan_status(network_id=None):
    """Restituisce lo stato dello scan per una rete (o tutti)."""
    with _scan_lock:
        if network_id:
            return _scan_status.get(network_id, {'running': False})
        return dict(_scan_status)


def _set_status(network_id, **kwargs):
    with _scan_lock:
        if network_id not in _scan_status:
            _scan_status[network_id] = {}
        _scan_status[network_id].update(kwargs)


# ── DNS PTR lookup ────────────────────────────────────────────────
def dns_ptr_lookup(ip_str, dns_servers=None, timeout=DNS_TIMEOUT):
    """
    Esegue una query PTR (reverse DNS) per un IP.
    Usa dnspython con i nameserver specificati (non il resolver di sistema).
    Prova tutti i DNS nell'ordine, restituisce il primo hostname trovato.
    """
    servers = dns_servers or DNS_SERVERS

    try:
        import dns.resolver
        import dns.reversename
        import dns.exception

        rev = dns.reversename.from_address(ip_str)

        for nameserver in servers:
            try:
                resolver = dns.resolver.Resolver(configure=False)
                resolver.nameservers = [nameserver]
                resolver.timeout     = timeout
                resolver.lifetime    = timeout
                answers = resolver.resolve(rev, 'PTR')
                if answers:
                    hostname = str(answers[0]).rstrip('.')
                    if hostname and hostname != ip_str:
                        return hostname
            except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer,
                    dns.resolver.NoNameservers, dns.exception.Timeout):
                continue
            except Exception:
                continue

    except ImportError:
        # Fallback a socket se dnspython non e' installato
        # NOTA: socket usa il resolver di sistema, non i DNS specificati
        log.warning("dnspython non installato - usando resolver di sistema (inaccurato)")
        try:
            old_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(timeout)
            try:
                result = socket.gethostbyaddr(ip_str)
                hostname = result[0]
                if hostname and hostname != ip_str:
                    return hostname.rstrip('.')
            except (socket.herror, socket.gaierror):
                pass
            finally:
                socket.setdefaulttimeout(old_timeout)
        except Exception:
            pass

    return None


# ── TCP Probe ─────────────────────────────────────────────────────
def tcp_probe(ip_str, ports=None, timeout=0.5):
    """
    Tenta connessioni TCP su porte comuni per rilevare host che bloccano ICMP.
    Restituisce True se l'host risponde su almeno una porta (sia aperta che chiusa
    con RST — entrambe indicano che l'host è raggiungibile).
    """
    probe_ports = ports if ports is not None else _runtime_tcp_ports
    for port in probe_ports:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            err = sock.connect_ex((ip_str, port))
            sock.close()
            # err == 0: porta aperta (connesso)
            # err == ECONNREFUSED: porta chiusa ma host risponde con RST → host UP
            if err == 0 or err == errno.ECONNREFUSED:
                return True
        except (socket.error, OSError):
            pass
    return False


# ── Ping ─────────────────────────────────────────────────────────
def ping(ip_str, timeout=PING_TIMEOUT):
    """
    Invia un ping all'IP. Restituisce True se raggiungibile.
    Usa il comando di sistema (compatibile Linux).
    """
    try:
        result = subprocess.run(
            ['ping', '-c', '1', '-W', str(timeout), ip_str],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout + 1,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def ping_verbose(ip_str, timeout=2):
    """
    Ping con output testuale completo — per diagnostica.
    Restituisce (alive, stdout_text, stderr_text, returncode, error).
    """
    import shutil
    ping_bin = shutil.which('ping') or '/bin/ping'
    try:
        result = subprocess.run(
            [ping_bin, '-c', '3', '-W', str(timeout), ip_str],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout * 3 + 2,
        )
        out = result.stdout.decode('utf-8', errors='replace')
        err = result.stderr.decode('utf-8', errors='replace')
        return result.returncode == 0, out, err, result.returncode, None
    except subprocess.TimeoutExpired:
        return False, '', '', -1, 'timeout'
    except FileNotFoundError:
        return False, '', '', -1, 'ping non trovato'
    except Exception as e:
        return False, '', '', -1, str(e)


def tcp_probe_verbose(ip_str, ports=None, timeout=0.5):
    """
    TCP probe con dettaglio porta — per diagnostica.
    Restituisce lista di dict {'port': N, 'result': 'open'|'refused'|'timeout'|'error'}.
    """
    probe_ports = ports if ports is not None else _runtime_tcp_ports
    results = []
    for port in probe_ports:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            err = sock.connect_ex((ip_str, port))
            sock.close()
            if err == 0:
                results.append({'port': port, 'result': 'open'})
            elif err == errno.ECONNREFUSED:
                results.append({'port': port, 'result': 'refused'})
            elif err in (errno.ETIMEDOUT, 110):
                results.append({'port': port, 'result': 'timeout'})
            else:
                results.append({'port': port, 'result': 'errno:{}'.format(err)})
        except Exception as e:
            results.append({'port': port, 'result': 'error:{}'.format(e)})
    return results



# ── SNMP ARP table ────────────────────────────────────────────────
_arp_cache  = {}
_arp_loaded = False

def load_arp_tables(routers=None, community=None):
    """Legge le ARP table dai router via snmpwalk. Popola _arp_cache."""
    global _arp_cache, _arp_loaded
    if _arp_loaded:
        return _arp_cache

    comm    = community or SNMP_COMMUNITY
    targets = routers   or SNMP_ROUTERS
    if not targets:
        _arp_loaded = True
        return _arp_cache

    import re
    oid = '1.3.6.1.2.1.4.22.1.2'

    for router in targets:
        try:
            result = subprocess.run(
                ['snmpwalk', '-v2c', '-c', comm,
                 '-t', str(SNMP_TIMEOUT), '-r', '1',
                 router, oid],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=SNMP_TIMEOUT + 2
            )
            for line in result.stdout.decode('utf-8', errors='ignore').splitlines():
                # Formato Cisco: IP-MIB::ipNetToMediaPhysAddress.1.10.114.9.157 = STRING: 0:8:e3:ff:fc:28
                # Formato hex:   ... = Hex-STRING: 00 08 E3 FF FC 28
                # Il formato OID puo' essere:
                # .1.10.114.9.1           (indice.a.b.c.d)
                # .83886080.10.114.9.1    (ifIndex.a.b.c.d)
                # Prendiamo sempre gli ultimi 4 ottetti come IP
                m = re.search(
                    r'ipNetToMediaPhysAddress\.\d+\.(\d+\.\d+\.\d+\.\d+)'
                    r'.*?(?:STRING:|Hex-STRING:)\s*([0-9A-Fa-f:. ]+)',
                    line)
                if m:
                    ip      = m.group(1)
                    mac_raw = m.group(2).strip()
                    # Normalizza: separa per : o spazio, zero-pad ogni ottetto
                    parts = re.split(r'[:\s]+', mac_raw)
                    if len(parts) == 6:
                        mac_fmt = ':'.join(p.zfill(2).upper() for p in parts)
                        _arp_cache[ip] = mac_fmt
        except Exception as e:
            log.warning(f"SNMP ARP da {router} fallito: {e}")

    log.info(f"ARP cache: {len(_arp_cache)} entries da {len(targets)} router")
    _arp_loaded = True
    return _arp_cache


# ── Scan singolo IP ───────────────────────────────────────────────
def scan_ip(ip_str):
    """
    Esegue ping + (TCP probe se ping fallisce) + DNS PTR + ARP lookup per un singolo IP.
    Il TCP probe rileva host che bloccano ICMP per regole firewall.
    Restituisce dict con i risultati.
    """
    is_alive    = ping(ip_str)
    detect_mode = 'ping' if is_alive else None

    # Fallback TCP probe se il ping non risponde e la funzione è abilitata
    if not is_alive and _runtime_tcp_enabled:
        is_alive = tcp_probe(ip_str)
        if is_alive:
            detect_mode = 'tcp'

    mac      = _arp_cache.get(ip_str)  # da SNMP ARP table se disponibile

    # DNS PTR lookup — usa server da AppConfig se disponibili, altrimenti default
    hostname = dns_ptr_lookup(ip_str, dns_servers=_runtime_dns or DNS_SERVERS)

    return {
        'ip':          ip_str,
        'alive':       is_alive,
        'detect_mode': detect_mode,   # 'ping' | 'tcp' | None
        'hostname':    hostname,
        'mac':         mac,
        'scanned_at':  datetime.utcnow(),
    }


# ── Scan intera subnet ────────────────────────────────────────────
def scan_subnet(network_id, app_context=None):
    """
    Scansiona tutti gli IP di una subnet.
    - network_id: ID della rete nel DB
    - app_context: Flask app context (necessario se chiamato in thread separato)

    Aggiorna il DB con i risultati.
    """
    # Setup Flask context
    if app_context:
        app_context.push()

    # Import Flask/DB qui (dentro il thread) per evitare problemi di contesto
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    os.environ.setdefault('IPAM_SEED', '0')

    from app import db, Network, IPRecord, ScanLog

    _set_status(network_id, running=True, progress=0, total=0,
                started_at=datetime.utcnow().isoformat(),
                found=0, updated=0, errors=0)

    _log = None

    try:
        # Carica la rete dal DB
        network = Network.query.get(network_id)
        if not network:
            _set_status(network_id, running=False, error='Rete non trovata')
            return

        net_obj = ipaddress.ip_network(f"{network.address}/{network.cidr}", strict=False)
        hosts   = list(net_obj.hosts())

        if not hosts:
            _set_status(network_id, running=False, error='Nessun host nella subnet')
            return

        total = len(hosts)
        _set_status(network_id, total=total,
                    subnet=f"{network.address}/{network.cidr}",
                    name=network.name)

        # Crea voce di log
        try:
            _log = ScanLog(
                scan_type='subnet',
                target=f"{network.address}/{network.cidr}",
                status='running',
                hosts_total=total,
                notes=network.name,
            )
            db.session.add(_log)
            db.session.commit()
        except Exception:
            _log = None

        # Carica ARP table dai router prima dello scan (se configurati)
        if SNMP_ROUTERS:
            log.info(f"Carico ARP table da {len(SNMP_ROUTERS)} router via SNMP...")
            load_arp_tables()
            log.info(f"ARP cache: {len(_arp_cache)} entries")

        log.info(f"Scan avviato: {network.name} ({network.address}/{network.cidr}) "
                 f"- {total} host, {MAX_WORKERS} thread")

        found = updated = errors = 0
        progress = 0

        # Scan parallelo con ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_ip = {
                executor.submit(scan_ip, str(ip)): str(ip)
                for ip in hosts
            }

            for future in as_completed(future_to_ip):
                ip_str = future_to_ip[future]
                progress += 1
                _set_status(network_id, progress=progress)

                try:
                    result = future.result()
                except Exception as e:
                    log.warning(f"Errore scan {ip_str}: {e}")
                    errors += 1
                    continue

                # Aggiorna il DB
                try:
                    existing = IPRecord.query.filter_by(ip_address=ip_str).first()

                    if result['alive'] or result['hostname']:
                        # IP attivo o con record DNS
                        status = 'used' if result['alive'] else 'reserved'

                        if existing:
                            # Se appartiene a un'altra rete, spostalo nella subnet corretta
                            if existing.network_id != network_id:
                                existing.network_id = network_id
                            if result['hostname']:
                                existing.hostname = result['hostname']
                            elif existing.hostname in (None, '', 'None', 'none'):
                                existing.hostname = None
                            if result.get('mac'):
                                existing.mac_address = result['mac']
                            existing.status     = status
                            existing.last_seen  = result['scanned_at']
                            existing.updated_at = datetime.utcnow()
                            updated += 1
                        else:
                            # Crea nuovo record
                            import socket as _s, struct as _st
                            try:
                                _ip_int = _st.unpack('!I', _s.inet_aton(ip_str))[0]
                            except Exception:
                                _ip_int = None
                            record = IPRecord(
                                ip_address  = ip_str,
                                ip_int      = _ip_int,
                                hostname    = result['hostname'],
                                mac_address = result.get('mac'),
                                status      = status,
                                network_id  = network_id,
                                last_seen   = result['scanned_at'],
                            )
                            db.session.add(record)
                            found += 1

                        _set_status(network_id, found=found, updated=updated)

                    elif existing and existing.status == 'used':
                        # Era segnato come usato ma ora non risponde
                        if existing.network_id != network_id:
                            existing.network_id = network_id
                        existing.status = 'available'
                        existing.updated_at = datetime.utcnow()
                        updated += 1

                    # Commit ogni 50 record per evitare transazioni enormi
                    if (progress % 50) == 0:
                        db.session.commit()

                except Exception as e:
                    log.warning(f"Errore DB per {ip_str}: {e}")
                    db.session.rollback()
                    errors += 1

        # Commit finale
        try:
            # Aggiorna last_scan sulla rete
            network.last_scan = datetime.utcnow()
            db.session.commit()
        except Exception as e:
            log.error(f"Errore commit finale: {e}")
            db.session.rollback()

        try:
            started = datetime.strptime(
                _scan_status[network_id]['started_at'], '%Y-%m-%dT%H:%M:%S.%f')
        except ValueError:
            started = datetime.strptime(
                _scan_status[network_id]['started_at'], '%Y-%m-%dT%H:%M:%S')
        duration = (datetime.utcnow() - started).seconds

        log.info(f"Scan completato: {network.name} - "
                 f"Nuovi: {found}, Aggiornati: {updated}, "
                 f"Errori: {errors}, Tempo: {duration}s")

        _set_status(network_id,
                    running=False,
                    found=found,
                    updated=updated,
                    errors=errors,
                    duration=duration,
                    completed_at=datetime.utcnow().isoformat())

        # Aggiorna log
        if _log:
            try:
                _log.status        = 'ok'
                _log.completed_at  = datetime.utcnow()
                _log.hosts_found   = found
                _log.hosts_updated = updated
                _log.hosts_error   = errors
                _log.duration_s    = duration
                db.session.commit()
            except Exception:
                pass

    except Exception as e:
        log.error(f"Errore scan subnet {network_id}: {e}")
        _set_status(network_id, running=False, error=str(e))
        if _log:
            try:
                _log.status    = 'error'
                _log.completed_at = datetime.utcnow()
                _log.error_msg = str(e)[:500]
                db.session.commit()
            except Exception:
                pass
        try:
            db.session.rollback()
        except Exception:
            pass


# ── Avvia scan asincrono ──────────────────────────────────────────
def start_scan_async(network_id, flask_app):
    """
    Avvia lo scan in un thread separato.
    Restituisce immediatamente.
    """
    # Controlla se c'e' gia' uno scan in corso
    status = get_scan_status(network_id)
    if status.get('running'):
        return False, 'Scan gia in corso per questa rete'

    ctx = flask_app.app_context()

    thread = threading.Thread(
        target=scan_subnet,
        args=(network_id, ctx),
        daemon=True,
        name=f'scan-{network_id}',
    )
    thread.start()
    return True, 'Scan avviato'


# ── Scan di tutte le subnet ───────────────────────────────────────
def scan_all_subnets(flask_app, skip_recent_hours=6):
    """
    Scansiona tutte le subnet attive.
    Salta le subnet scansionate nelle ultime skip_recent_hours ore.
    Usato dal cron job notturno.
    """
    from app import db, Network
    from datetime import timedelta

    with flask_app.app_context():
        cutoff = datetime.utcnow() - timedelta(hours=skip_recent_hours)
        subnets = Network.query.filter(
            Network.network_type == 'subnet',
            Network.status == 'active',
            db.or_(
                Network.last_scan == None,
                Network.last_scan < cutoff,
            )
        ).all()

        subnet_ids = [s.id for s in subnets]
        total = len(subnet_ids)
        log.info(f"Scan notturno: {total} subnet da scansionare")

    # Scansiona MAX_SUBNET_WORKERS subnet in parallelo
    completed = 0
    for i in range(0, len(subnet_ids), MAX_SUBNET_WORKERS):
        batch = subnet_ids[i:i + MAX_SUBNET_WORKERS]
        threads = []
        for net_id in batch:
            ctx = flask_app.app_context()
            t = threading.Thread(
                target=scan_subnet,
                args=(net_id, ctx),
                daemon=True,
                name=f'scan-{net_id}',
            )
            threads.append(t)
            t.start()

        for t in threads:
            t.join()
            completed += 1
            log.info(f"Scan notturno: {completed}/{total} completate")

    log.info("Scan notturno completato")


# ── Esecuzione standalone ─────────────────────────────────────────
if __name__ == '__main__':
    import argparse

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    os.environ.setdefault('IPAM_SEED', '0')

    parser = argparse.ArgumentParser(
        description='IPAM Network Scanner',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''Esempi:
  python3 scanner.py --subnet-id 58
  python3 scanner.py --subnet 10.114.1.0 --cidr 24
  python3 scanner.py --subnet-prefix 10.114 --cidr 24
  python3 scanner.py --all
  python3 scanner.py --all --skip-recent 6
''')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--subnet-id',     type=int,
                       help='ID numerico della subnet nel DB')
    group.add_argument('--subnet',        type=str,
                       help='Indirizzo esatto della subnet (es. 10.114.1.0)')
    group.add_argument('--subnet-prefix', type=str,
                       help='Prefisso indirizzo subnet (es. 10.114 scansiona tutte le /cidr con quel prefisso)')
    group.add_argument('--all',           action='store_true',
                       help='Scansiona tutte le subnet attive')

    parser.add_argument('--cidr',         type=int,
                       help='CIDR della subnet (obbligatorio con --subnet e --subnet-prefix)')
    parser.add_argument('--skip-recent',  type=int, default=0,
                       help='Salta subnet scansionate nelle ultime N ore (default: 0=tutte)')
    args = parser.parse_args()

    from app import app, db, Network

    # Validazione argomenti
    if (args.subnet or args.subnet_prefix) and not args.cidr:
        parser.error('--cidr e obbligatorio con --subnet e --subnet-prefix')

    with app.app_context():

        if args.subnet_id:
            # Scan per ID
            n = Network.query.get(args.subnet_id)
            if not n:
                print(f"[ERRORE] Subnet ID {args.subnet_id} non trovata nel DB")
                sys.exit(1)
            print(f"Subnet: {n.name} ({n.address}/{n.cidr})")
            scan_subnet(args.subnet_id)

        elif args.subnet:
            # Scan per indirizzo esatto + cidr
            n = Network.query.filter_by(
                address=args.subnet, cidr=args.cidr,
                network_type='subnet').first()
            if not n:
                print(f"[ERRORE] Subnet {args.subnet}/{args.cidr} non trovata nel DB")
                # Mostra suggerimenti
                similar = Network.query.filter(
                    Network.address.like(args.subnet.rsplit('.',1)[0] + '%'),
                    Network.network_type == 'subnet'
                ).limit(5).all()
                if similar:
                    print("Subnet simili trovate:")
                    for s in similar:
                        print(f"  ID={s.id}  {s.address}/{s.cidr}  {s.name}")
                sys.exit(1)
            print(f"Subnet: {n.name} ({n.address}/{n.cidr})")
            scan_subnet(n.id)

        elif args.subnet_prefix:
            # Scan per prefisso + cidr
            nets = Network.query.filter(
                Network.address.like(args.subnet_prefix + '.%'),
                Network.cidr == args.cidr,
                Network.network_type == 'subnet',
                Network.status == 'active'
            ).order_by(Network.address).all()

            if not nets:
                # Prova anche senza il punto finale (es. "10.114" matcha "10.1140")
                nets = Network.query.filter(
                    Network.address.like(args.subnet_prefix + '%'),
                    Network.cidr == args.cidr,
                    Network.network_type == 'subnet',
                    Network.status == 'active'
                ).order_by(Network.address).all()

            if not nets:
                print(f"[ERRORE] Nessuna subnet /{args.cidr} trovata con prefisso '{args.subnet_prefix}'")
                sys.exit(1)

            print(f"Trovate {len(nets)} subnet /{args.cidr} con prefisso '{args.subnet_prefix}'")
            for i, n in enumerate(nets, 1):
                print(f"  [{i}/{len(nets)}] {n.address}/{n.cidr} - {n.name}")
                scan_subnet(n.id)

        elif args.all:
            scan_all_subnets(app, skip_recent_hours=args.skip_recent)
