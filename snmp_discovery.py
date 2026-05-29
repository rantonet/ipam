# -*- coding: utf-8 -*-
"""
snmp_discovery.py — IPAM SNMP Discovery
========================================
Interroga router e switch via SNMP per raccogliere:
  - Tabella ARP (IP → MAC) dai router
  - Tabella FDB (MAC → porta switch) dagli switch 802.1Q/Cisco
  - Nomi interfacce (ifName/ifDescr)

Richiede snmpwalk (net-snmp):
  RHEL/CentOS: yum install net-snmp-utils
  Debian/Ubuntu: apt install snmp
"""

import re
import subprocess
import logging
import threading
from datetime import datetime

log = logging.getLogger('ipam.snmp')

SNMP_TIMEOUT = 5
SNMP_RETRIES = 1

# ── Stato discovery (condiviso tra thread) ─────────────────────────
_disc_status = {
    'running':       False,
    'started_at':    None,
    'completed_at':  None,
    'error':         None,
    'updated':       0,
    'arp_entries':   0,
    'mac_entries':   0,
    'current_step':  '',
}
_disc_lock = threading.Lock()


def _upd(**kw):
    with _disc_lock:
        _disc_status.update(kw)


def get_discovery_status():
    """Restituisce una copia dello stato corrente della discovery."""
    with _disc_lock:
        return dict(_disc_status)


# ── Wrapper snmpwalk ───────────────────────────────────────────────

def _snmpwalk(host, community, oid, version='2c', timeout=SNMP_TIMEOUT):
    """Esegue snmpwalk e restituisce lista di righe output."""
    try:
        res = subprocess.run(
            ['snmpwalk', '-v' + version, '-c', community,
             '-t', str(timeout), '-r', str(SNMP_RETRIES), host, oid],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout + 3,
        )
        return res.stdout.decode('utf-8', errors='ignore').splitlines()
    except FileNotFoundError:
        log.error('snmpwalk non trovato — installare net-snmp-utils (yum/apt)')
        return []
    except Exception as e:
        log.warning('snmpwalk %s %s: %s', host, oid, e)
        return []


# ── ARP table dai router ───────────────────────────────────────────

def get_arp_table(router, community, version='2c'):
    """
    Legge la tabella ARP da un router/L3-switch.
    OID: ipNetToMediaPhysAddress (1.3.6.1.2.1.4.22.1.2)
    Restituisce dict {ip_str: mac_str}.
    """
    result = {}
    for line in _snmpwalk(router, community, '1.3.6.1.2.1.4.22.1.2', version):
        m = re.search(
            r'ipNetToMediaPhysAddress\.\d+\.(\d+\.\d+\.\d+\.\d+)'
            r'.*?(?:STRING:|Hex-STRING:)\s*([0-9A-Fa-f:. ]+)',
            line)
        if m:
            ip      = m.group(1)
            mac_raw = m.group(2).strip()
            parts   = re.split(r'[:\s]+', mac_raw)
            if len(parts) == 6:
                result[ip] = ':'.join(p.zfill(2).upper() for p in parts)
    return result


# ── FDB table dagli switch ─────────────────────────────────────────

def get_fdb_table(switch, community, vlan=None, version='2c'):
    """
    Legge la FDB (Forwarding Database) di uno switch.
    Per switch 802.1Q usa community string nel formato community@vlan.
    OID: dot1dTpFdbPort (1.3.6.1.2.1.17.4.3.1.2)
    Restituisce dict {mac_str: bridge_port_int}.
    """
    comm = '{}@{}'.format(community, vlan) if vlan else community
    result = {}
    for line in _snmpwalk(switch, comm, '1.3.6.1.2.1.17.4.3.1.2', version):
        m = re.search(r'17\.4\.3\.1\.2\.(\d+(?:\.\d+){5})\s*=\D*(\d+)\s*$', line)
        if m:
            octets = [int(x) for x in m.group(1).split('.')]
            port   = int(m.group(2))
            if len(octets) == 6 and port > 0:
                mac = ':'.join('{:02X}'.format(o) for o in octets)
                result[mac] = port
    return result


def get_bridge_to_ifindex(switch, community, vlan=None, version='2c'):
    """
    Mappa bridge_port_number → ifIndex.
    OID: dot1dBasePortIfIndex (1.3.6.1.2.1.17.1.4.1.2)
    """
    comm = '{}@{}'.format(community, vlan) if vlan else community
    result = {}
    for line in _snmpwalk(switch, comm, '1.3.6.1.2.1.17.1.4.1.2', version):
        m = re.search(r'17\.1\.4\.1\.2\.(\d+)\s*=\D*(\d+)\s*$', line)
        if m:
            result[int(m.group(1))] = int(m.group(2))
    return result


def get_ifnames(switch, community, version='2c'):
    """
    Legge i nomi delle interfacce.
    Prova ifName (1.3.6.1.2.1.31.1.1.1.1), fallback a ifDescr (1.3.6.1.2.1.2.2.1.2).
    Restituisce dict {ifindex: name_str}.
    """
    result = {}
    specs = [
        ('1.3.6.1.2.1.31.1.1.1.1', r'31\.1\.1\.1\.1\.(\d+)\s*=\D*:\s*(.+)$'),
        ('1.3.6.1.2.1.2.2.1.2',    r'2\.2\.1\.2\.(\d+)\s*=\D*:\s*(.+)$'),
    ]
    for oid, pat in specs:
        if result:
            break
        for line in _snmpwalk(switch, community, oid, version):
            m = re.search(pat, line)
            if m:
                result[int(m.group(1))] = m.group(2).strip()
    return result


def get_sys_name(host, community, version='2c'):
    """Legge sysName.0 — hostname del dispositivo."""
    for line in _snmpwalk(host, community, '1.3.6.1.2.1.1.5.0', version, timeout=3):
        for pat in [r'sysName\.0\s*=\D*:\s*(.+)$', r'1\.1\.5\.0\s*=\D*:\s*(.+)$']:
            m = re.search(pat, line)
            if m:
                return m.group(1).strip()
    return host


def get_vlan_list(switch, community, version='2c'):
    """
    Lista VLAN attive sullo switch.
    Prova Cisco VTP (1.3.6.1.4.1.9.9.46.1.3.1.1.2), poi dot1qVlanStaticName.
    """
    vlans = set()
    for oid in ['1.3.6.1.4.1.9.9.46.1.3.1.1.2', '1.3.6.1.2.1.17.7.1.4.3.1.1']:
        for line in _snmpwalk(switch, community, oid, version, timeout=3):
            m = re.search(r'\.(\d+)\s*=', line)
            if m:
                vid = int(m.group(1))
                if 1 <= vid <= 4094:
                    vlans.add(vid)
        if vlans:
            break
    return sorted(vlans) if vlans else [1]


# ── Mappa MAC → switch + porta ─────────────────────────────────────

def discover_mac_to_port(switches, community, version='2c'):
    """
    Costruisce la mappa MAC → {switch_name, switch_ip, port, vlan}
    interrogando la FDB di ogni switch per ogni VLAN.
    """
    mac_map = {}
    for sw_ip in switches:
        try:
            sw_name = get_sys_name(sw_ip, community, version)
            _upd(current_step='Switch {}'.format(sw_name))
            log.info('Switch %s (%s): lettura interfacce...', sw_ip, sw_name)

            ifnames    = get_ifnames(sw_ip, community, version)
            vlans      = get_vlan_list(sw_ip, community, version)
            vlans_scan = vlans[:50]
            log.info('  %d VLAN, scan prime %d', len(vlans), len(vlans_scan))

            for vlan in vlans_scan:
                try:
                    fdb    = get_fdb_table(sw_ip, community, vlan, version)
                    bp_map = get_bridge_to_ifindex(sw_ip, community, vlan, version)
                    for mac, bport in fdb.items():
                        if mac not in mac_map:
                            ifidx     = bp_map.get(bport)
                            port_name = (ifnames.get(ifidx, 'port{}'.format(bport))
                                         if ifidx else 'port{}'.format(bport))
                            mac_map[mac] = {
                                'switch':    sw_name,
                                'switch_ip': sw_ip,
                                'port':      port_name,
                                'vlan':      vlan,
                            }
                except Exception as ex:
                    log.debug('  VLAN %s su %s: %s', vlan, sw_ip, ex)

            sw_count = sum(1 for v in mac_map.values() if v['switch_ip'] == sw_ip)
            log.info('  %d MAC trovati su %s', sw_count, sw_name)
        except Exception as ex:
            log.warning('Errore switch %s: %s', sw_ip, ex)

    log.info('MAC table: %d entries da %d switch', len(mac_map), len(switches))
    return mac_map


# ── Discovery completa (da eseguire in thread) ─────────────────────

def run_discovery(app_context=None):
    """
    Discovery completa:
      1. ARP dai router  → IP → MAC
      2. FDB dagli switch → MAC → switch + porta
      3. Aggiorna IPRecord nel DB
    """
    if app_context:
        app_context.push()

    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from app import db, IPRecord, ScanLog, _cfg_get

    _log = None

    try:
        _upd(running=True, started_at=datetime.utcnow().isoformat(),
             completed_at=None, error=None, updated=0,
             arp_entries=0, mac_entries=0, current_step='Avvio...')

        try:
            _log = ScanLog(scan_type='snmp', target='SNMP Discovery', status='running')
            db.session.add(_log)
            db.session.commit()
        except Exception:
            _log = None

        community = _cfg_get('snmp_community', 'public').strip() or 'public'
        version   = _cfg_get('snmp_version',   '2c').strip()    or '2c'
        routers   = [r.strip() for r in re.split(r'[,\n\r]+', _cfg_get('snmp_routers',  '')) if r.strip()]
        switches  = [s.strip() for s in re.split(r'[,\n\r]+', _cfg_get('snmp_switches', '')) if s.strip()]

        if not routers and not switches:
            _upd(running=False, error='Nessun router/switch configurato nelle impostazioni SNMP')
            return

        # ── 1. ARP tables ────────────────────────────────────────────
        arp_map = {}
        for router in routers:
            _upd(current_step='ARP da {}'.format(router))
            log.info('ARP table da %s...', router)
            arp_map.update(get_arp_table(router, community, version))
        log.info('ARP: %d entries da %d router', len(arp_map), len(routers))
        _upd(arp_entries=len(arp_map))

        # ── 2. FDB tables ────────────────────────────────────────────
        mac_map = {}
        if switches:
            _upd(current_step='FDB dagli switch...')
            mac_map = discover_mac_to_port(switches, community, version)
        _upd(mac_entries=len(mac_map))

        # ── 3. Aggiorna DB ───────────────────────────────────────────
        _upd(current_step='Aggiornamento DB...')
        updated = 0
        now = datetime.utcnow()

        # ARP: IP → MAC → cerca switch info
        for ip, mac in arp_map.items():
            try:
                rec = IPRecord.query.filter_by(ip_address=ip).first()
                if not rec:
                    continue
                changed = False
                if mac and rec.mac_address != mac:
                    rec.mac_address = mac
                    changed = True
                sw = mac_map.get(mac)
                if sw:
                    if rec.switch_name != sw['switch']:
                        rec.switch_name = sw['switch']
                        changed = True
                    port_val = '{} (VLAN {})'.format(sw['port'], sw['vlan']) if sw.get('vlan') and sw['vlan'] != 1 else sw['port']
                    if rec.switch_port != port_val:
                        rec.switch_port = port_val
                        changed = True
                if changed:
                    rec.snmp_updated_at = now
                    updated += 1
            except Exception as ex:
                log.warning('DB %s: %s', ip, ex)

        # Sweep MAC diretti (non nell'ARP ma trovati in FDB)
        for mac, sw in mac_map.items():
            try:
                rec = IPRecord.query.filter_by(mac_address=mac).first()
                if rec and (rec.switch_name != sw['switch'] or
                            not rec.switch_port or rec.switch_port.split(' ')[0] != sw['port']):
                    rec.switch_name     = sw['switch']
                    port_val = '{} (VLAN {})'.format(sw['port'], sw['vlan']) if sw.get('vlan') and sw['vlan'] != 1 else sw['port']
                    rec.switch_port     = port_val
                    rec.snmp_updated_at = now
                    updated += 1
            except Exception as ex:
                log.warning('DB MAC %s: %s', mac, ex)

        try:
            db.session.commit()
        except Exception as ex:
            db.session.rollback()
            log.error('Commit: %s', ex)

        log.info('Discovery completata: %d record aggiornati', updated)
        _upd(running=False, completed_at=datetime.utcnow().isoformat(),
             updated=updated, current_step='')

        if _log:
            try:
                _log.status        = 'ok'
                _log.completed_at  = datetime.utcnow()
                _log.hosts_updated = updated
                _log.hosts_found   = len(arp_map)
                _log.hosts_total   = len(arp_map) + len(mac_map)
                _log.notes         = 'ARP: {} entries, MAC: {} entries'.format(
                                         len(arp_map), len(mac_map))
                db.session.commit()
            except Exception:
                pass

    except Exception as ex:
        log.error('Discovery: %s', ex)
        _upd(running=False, error=str(ex), current_step='')
        if _log:
            try:
                _log.status    = 'error'
                _log.completed_at = datetime.utcnow()
                _log.error_msg = str(ex)[:500]
                db.session.commit()
            except Exception:
                pass
