# -*- coding: utf-8 -*-
"""
IPAM - IP Address Management System
Backend: Python 3 / Flask
Server:  Apache 2.4 + mod_wsgi
Path:    http://<server>/ipam/
Version: 1.3.0
"""

# ── Versione e changelog ──────────────────────────────────────────────────────
APP_VERSION = "1.9.0"
APP_BUILD   = "2026-05-28"
APP_CHANGELOG = [
    {
        "version": "1.3.0",
        "data":    "2026-05-28",
        "modifiche": [
            "Ordinamento numerico degli IP per ottetto in tutte le viste (dettaglio subnet, pagina IP, API)",
            "Funzione helper _ip_numeric_order() centralizzata per sort numerico",
            "Form creazione rete: aggiunti campi country, site, usage, zone, gateway, status (index e networks)",
            "Form modifica rete (dettaglio subnet): aggiunti campi gateway e tipo rete",
            "Fix scanner.py: riassegna network_id quando un IP viene trovato in una subnet più specifica",
            "Fix Gunicorn: --workers 1 per evitare inconsistenza stato scan tra processi",
            "Fix JS pollScanStatus: mostra errori in UI e ricarica solo a successo",
            "SafeDateTime su tutte le colonne DateTime (compatibilità SQLAlchemy/SQLite)",
        ]
    },
    {
        "version": "1.2.0",
        "data":    "2026-05-26",
        "modifiche": [
            "Import da SolarWinds (reti e IP)",
            "Scanner ICMP/SNMP con stato polling",
            "Gerarchia reti con parent_id e tree cache",
            "Bulk edit IP records",
        ]
    },
]

import os
import sys

# Forza stdout/stderr a UTF-8 anche su sistemi con locale latin-1 (es. CentOS 7)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import ipaddress
from sqlalchemy.types import TypeDecorator, String as SAString
from sqlalchemy import text as sa_text

#  App factory 

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__, static_url_path='/static', static_folder='static')

#  Chiave segreta (in produzione usare variabile d'ambiente)
app.config['SECRET_KEY'] = os.environ.get('IPAM_SECRET', 'ipam-secret-key-changeme')

#  DATABASE: file nella sottocartella instance/ accanto ad app.py
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(BASE_DIR, 'instance', 'ipam.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

#  PREFERRED URL SCHEME (HTTP in LAN, HTTPS in produzione)
app.config['PREFERRED_URL_SCHEME'] = 'http'

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = 'auth_login'

# ── Auth logger (instance/auth.log, 5 MB × 3 file) ────────────────
import logging as _logging
from logging.handlers import RotatingFileHandler as _RFH
_auth_log = _logging.getLogger('ipam.auth')
_auth_log.setLevel(_logging.INFO)
_auth_log.propagate = False
try:
    _ah = _RFH(
        os.path.join(BASE_DIR, 'instance', 'auth.log'),
        maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8',
    )
    _ah.setFormatter(_logging.Formatter('%(asctime)s | %(message)s',
                                        datefmt='%Y-%m-%d %H:%M:%S'))
    _auth_log.addHandler(_ah)
except Exception:
    _auth_log.addHandler(_logging.NullHandler())


def _client_ip():
    xff = request.headers.get('X-Forwarded-For', '')
    return xff.split(',')[0].strip() if xff else (request.remote_addr or '-')


#  MODELS 

class SafeDateTime(TypeDecorator):
    """
    Tipo DateTime robusto per SQLite + Python 3.6.
    Salva sempre con separatore spazio; legge sia 'T' che spazio,
    con o senza microsecondi.  Evita il bug SQLAlchemy 1.x che
    usa isoformat() (con 'T') in scrittura ma un regex con ' ' in lettura.
    """
    impl     = SAString(30)
    cache_ok = True
    _FMTS = (
        '%Y-%m-%d %H:%M:%S.%f',
        '%Y-%m-%dT%H:%M:%S.%f',
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%dT%H:%M:%S',
        '%Y-%m-%d',
    )

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.strftime('%Y-%m-%d %H:%M:%S.%f')
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        for fmt in self._FMTS:
            try:
                return datetime.strptime(value, fmt)
            except (ValueError, TypeError):
                continue
        return None


def _dt(dt):
    """Serializza datetime senza microsecondi (compatibile con tutti i parser JS)."""
    return dt.strftime('%Y-%m-%dT%H:%M:%S') if dt else None


class Network(db.Model):
    __tablename__ = 'networks'
    __table_args__ = (
        db.Index('idx_networks_address_cidr', 'address', 'cidr'),
        db.Index('idx_networks_parent_id',    'parent_id'),
        db.Index('idx_networks_network_type', 'network_type'),
    )
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(120), nullable=False)
    address       = db.Column(db.String(50),  nullable=False)
    cidr          = db.Column(db.Integer,     nullable=False)
    mask          = db.Column(db.String(20))
    vlan_id       = db.Column(db.Integer)
    location      = db.Column(db.String(200))
    description   = db.Column(db.String(500))
    country       = db.Column(db.String(100))
    site          = db.Column(db.String(200))
    usage         = db.Column(db.String(200))
    zone          = db.Column(db.String(100))
    gateway       = db.Column(db.String(50))
    parent_id     = db.Column(db.Integer, db.ForeignKey('networks.id'), nullable=True)
    network_type  = db.Column(db.String(20), default='subnet')   # supernet | subnet | vlan
    status        = db.Column(db.String(20), default='active')   # active | reserved | deprecated
    created_at    = db.Column(SafeDateTime, default=datetime.utcnow)
    updated_at    = db.Column(SafeDateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_scan     = db.Column(SafeDateTime)

    children   = db.relationship('Network', backref=db.backref('parent', remote_side=[id]), lazy='dynamic')
    ip_records = db.relationship('IPRecord', backref='network', lazy='dynamic',
                                 cascade='all, delete-orphan')

    @property
    def network_obj(self):
        try:
            return ipaddress.ip_network(f"{self.address}/{self.cidr}", strict=False)
        except Exception:
            return None

    @property
    def total_hosts(self):
        net = self.network_obj
        if not net:
            return 0
        return net.num_addresses - 2 if self.cidr < 31 else net.num_addresses

    @property
    def used_count(self):
        return self.ip_records.filter_by(status='used').count()

    @property
    def available_count(self):
        return max(0, self.total_hosts - self.used_count)

    @property
    def usage_percent(self):
        return round((self.used_count / self.total_hosts) * 100, 2) if self.total_hosts else 0

    @property
    def broadcast(self):
        net = self.network_obj
        return str(net.broadcast_address) if net else ''

    @property
    def netmask(self):
        net = self.network_obj
        return str(net.netmask) if net else ''

    def to_dict(self):
        return {
            'id':            self.id,
            'name':          self.name,
            'address':       self.address,
            'cidr':          self.cidr,
            'mask':          self.netmask,
            'vlan_id':       self.vlan_id,
            'location':      self.location,
            'description':   self.description,
            'country':       self.country,
            'site':          self.site,
            'usage':         self.usage,
            'zone':          self.zone,
            'gateway':       self.gateway,
            'parent_id':     self.parent_id,
            'network_type':  self.network_type,
            'status':        self.status,
            'total_hosts':   self.total_hosts,
            'used_count':    self.used_count,
            'available_count': self.available_count,
            'usage_percent': self.usage_percent,
            'broadcast':     self.broadcast,
            'last_scan':     _dt(self.last_scan),
            'created_at':    _dt(self.created_at),
        }


class IPRecord(db.Model):
    __tablename__ = 'ip_records'
    __table_args__ = (
        # Indice più critico: ogni query di dettaglio subnet filtra per network_id
        db.Index('idx_ip_records_network_id',     'network_id'),
        # Indice composito: filtra per network_id + status (vista used/free/all)
        db.Index('idx_ip_records_network_status', 'network_id', 'status'),
        # Indice su status: usato nelle statistiche globali
        db.Index('idx_ip_records_status',         'status'),
    )
    id             = db.Column(db.Integer, primary_key=True)
    ip_address     = db.Column(db.String(50),  nullable=False, unique=True)
    ip_int         = db.Column(db.Integer)       # IPv4 come intero 32-bit — per sort O(log n)
    hostname       = db.Column(db.String(200))
    mac_address    = db.Column(db.String(20))
    switch_name    = db.Column(db.String(100))
    switch_port    = db.Column(db.String(50))
    snmp_updated_at = db.Column(SafeDateTime)
    device_type    = db.Column(db.String(50))
    os_type        = db.Column(db.String(100))
    status         = db.Column(db.String(20), default='used')   # used | reserved | dhcp | available
    network_id     = db.Column(db.Integer, db.ForeignKey('networks.id'))
    description    = db.Column(db.String(500))
    last_seen      = db.Column(SafeDateTime)
    created_at     = db.Column(SafeDateTime, default=datetime.utcnow)
    updated_at     = db.Column(SafeDateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id':             self.id,
            'ip_address':     self.ip_address,
            'hostname':       self.hostname,
            'mac_address':    self.mac_address,
            'switch_name':    self.switch_name,
            'switch_port':    self.switch_port,
            'snmp_updated_at': _dt(self.snmp_updated_at),
            'device_type':    self.device_type,
            'os_type':        self.os_type,
            'status':         self.status,
            'network_id':     self.network_id,
            'description':    self.description,
            'last_seen':      _dt(self.last_seen),
            'created_at':     _dt(self.created_at),
        }


class VLan(db.Model):
    __tablename__ = 'vlans'
    id          = db.Column(db.Integer, primary_key=True)
    vlan_id     = db.Column(db.Integer, unique=True, nullable=False)
    name        = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(500))
    status      = db.Column(db.String(20), default='active')
    created_at  = db.Column(SafeDateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id':          self.id,
            'vlan_id':     self.vlan_id,
            'name':        self.name,
            'description': self.description,
            'status':      self.status,
        }


class AppConfig(db.Model):
    __tablename__ = 'app_config'
    key   = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, default='')


class ScanLog(db.Model):
    __tablename__ = 'scan_logs'
    id            = db.Column(db.Integer, primary_key=True)
    scan_type     = db.Column(db.String(20))    # subnet | snmp | global | group
    target        = db.Column(db.String(200))   # CIDR, 'SNMP Discovery', nome gruppo
    started_at    = db.Column(SafeDateTime, default=datetime.utcnow)
    completed_at  = db.Column(SafeDateTime)
    status        = db.Column(db.String(20), default='running')  # running|ok|error
    hosts_total   = db.Column(db.Integer, default=0)
    hosts_found   = db.Column(db.Integer, default=0)
    hosts_updated = db.Column(db.Integer, default=0)
    hosts_error   = db.Column(db.Integer, default=0)
    duration_s    = db.Column(db.Integer, default=0)
    error_msg     = db.Column(db.String(500))
    notes         = db.Column(db.Text)

    def to_dict(self):
        return {
            'id':           self.id,
            'scan_type':    self.scan_type,
            'target':       self.target,
            'started_at':   _dt(self.started_at),
            'completed_at': _dt(self.completed_at),
            'status':       self.status,
            'hosts_total':  self.hosts_total,
            'hosts_found':  self.hosts_found,
            'hosts_updated': self.hosts_updated,
            'hosts_error':  self.hosts_error,
            'duration_s':   self.duration_s,
            'error_msg':    self.error_msg,
            'notes':        self.notes,
        }


class ScanGroup(db.Model):
    __tablename__ = 'scan_groups'
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(200), nullable=False)
    group_type    = db.Column(db.String(20), default='subnet')  # supernet | subnet | ip_range
    schedule_time = db.Column(db.String(5))                     # HH:MM
    enabled       = db.Column(db.Boolean, default=True)
    created_at    = db.Column(SafeDateTime, default=datetime.utcnow)
    items         = db.relationship('ScanGroupItem', backref='group',
                                    cascade='all, delete-orphan')


class ScanGroupItem(db.Model):
    __tablename__ = 'scan_group_items'
    id         = db.Column(db.Integer, primary_key=True)
    group_id   = db.Column(db.Integer, db.ForeignKey('scan_groups.id'), nullable=False)
    network_id = db.Column(db.Integer, db.ForeignKey('networks.id'), nullable=True)
    ip_range   = db.Column(db.String(100), nullable=True)


class LocalUser(db.Model):
    __tablename__ = 'local_users'
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    display_name  = db.Column(db.String(200), default='')
    is_admin      = db.Column(db.Boolean, default=False)
    enabled       = db.Column(db.Boolean, default=True)
    created_at    = db.Column(SafeDateTime, default=datetime.utcnow)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

    def to_dict(self):
        return {
            'id':           self.id,
            'username':     self.username,
            'display_name': self.display_name or '',
            'is_admin':     self.is_admin,
            'enabled':      self.enabled,
        }


# ── Config helpers ────────────────────────────────────────────────
def _cfg_get(key, default=''):
    row = AppConfig.query.get(key)
    return row.value if row else default


def _cfg_set(key, value):
    row = AppConfig.query.get(key)
    if row:
        row.value = str(value)
    else:
        db.session.add(AppConfig(key=key, value=str(value)))
    db.session.commit()


def _update_scanner_dns():
    """Sincronizza i DNS dal DB verso il modulo scanner."""
    try:
        primary   = _cfg_get('dns_primary',   '').strip()
        secondary = _cfg_get('dns_secondary', '').strip()
        servers   = [s for s in [primary, secondary] if s]
        if servers:
            from scanner import set_runtime_dns
            set_runtime_dns(servers)
    except Exception as e:
        import logging
        logging.getLogger('ipam').warning('Impossibile aggiornare DNS scanner: %s', e)


def _update_scanner_tcp():
    """Sincronizza le impostazioni TCP probe dal DB verso il modulo scanner."""
    try:
        enabled  = _cfg_get('tcp_probe_enabled', '1') == '1'
        ports_s  = _cfg_get('tcp_probe_ports', '22,80,443,445,3389,135,8080,8443').strip()
        ports    = [int(p.strip()) for p in ports_s.split(',') if p.strip().isdigit()]
        from scanner import set_runtime_tcp
        set_runtime_tcp(enabled, ports or None)
    except Exception as e:
        import logging
        logging.getLogger('ipam').warning('Impossibile aggiornare TCP probe scanner: %s', e)


#  AUTH ────────────────────────────────────────────────────────────

class AuthUser(UserMixin):
    """Utente autenticato (locale o LDAP) per Flask-Login."""
    def __init__(self, user_id, username, display_name, is_admin, source):
        self.id           = user_id
        self.username     = username
        self.display_name = display_name or username
        self.is_admin     = is_admin
        self.source       = source  # 'local' | 'ldap'


@login_manager.user_loader
def _load_user(user_id):
    if user_id.startswith('local:'):
        uid = int(user_id[6:])
        u = LocalUser.query.get(uid)
        if u and u.enabled:
            return AuthUser(user_id, u.username, u.display_name, u.is_admin, 'local')
    elif user_id.startswith('ldap:'):
        info = session.get('_ldap_user')
        if info and info.get('id') == user_id:
            return AuthUser(user_id, info['username'], info['display_name'], False, 'ldap')
    return None


@login_manager.unauthorized_handler
def _unauthorized():
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Non autenticato', 'login': True}), 401
    return redirect(url_for('auth_login', next=request.full_path))


@app.before_request
def _require_login():
    """Protegge tutte le route tranne login e static."""
    exempt = {'auth_login', 'static'}
    if request.endpoint in exempt or request.endpoint is None:
        return
    if not current_user.is_authenticated:
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Non autenticato', 'login': True}), 401
        return redirect(url_for('auth_login', next=request.full_path))


def _dn_to_cn(dn):
    for part in str(dn).split(','):
        p = part.strip()
        if p.upper().startswith('CN='):
            return p[3:]
    return str(dn)


def _ldap_authenticate(username, password):
    """Autentica via LDAP/AD o OpenLDAP. Restituisce AuthUser o None."""
    ip = _client_ip()
    try:
        from ldap3 import Server, Connection, ALL, SUBTREE
        server_url     = _cfg_get('ldap_server',        '').strip()
        base_dn        = _cfg_get('ldap_base_dn',       '').strip()
        bind_dn        = _cfg_get('ldap_bind_dn',       '').strip()
        bind_pw        = _cfg_get('ldap_bind_password', '').strip()
        user_attr      = _cfg_get('ldap_user_attr',     'sAMAccountName').strip() or 'sAMAccountName'
        allowed_groups = [g.strip() for g in _cfg_get('ldap_allowed_groups', '').split(',') if g.strip()]
        server_type    = _cfg_get('ldap_server_type',   'ad').strip() or 'ad'

        if not server_url or not base_dn:
            return None

        srv = Server(server_url, get_info=ALL, connect_timeout=5)
        try:
            if bind_dn:
                conn = Connection(srv, user=bind_dn, password=bind_pw, auto_bind=True)
            else:
                conn = Connection(srv, auto_bind=True)
        except Exception as e:
            _auth_log.warning('LDAP_ERR   | %-20s | ldap | %s | bind di servizio fallito: %s',
                              username, ip, e)
            return None

        if server_type == 'ad':
            attrs = ['distinguishedName', 'displayName', 'memberOf']
        else:
            attrs = ['displayName', 'cn', 'uid']

        conn.search(base_dn,
                    '({}={})'.format(user_attr, username),
                    search_scope=SUBTREE,
                    attributes=attrs)

        if not conn.entries:
            _auth_log.warning('LDAP_404   | %-20s | ldap | %s | utente non trovato nel dominio',
                              username, ip)
            return None

        entry        = conn.entries[0]
        user_dn      = entry.entry_dn
        display_name = str(entry.displayName) if hasattr(entry, 'displayName') and entry.displayName else username

        # ── Verifica gruppi ──────────────────────────────────────────
        if allowed_groups:
            allowed_low = [ag.lower() for ag in allowed_groups]

            if server_type == 'ad':
                # 1) Gruppi diretti via memberOf — case-insensitive
                member_of  = [str(g) for g in entry.memberOf] if hasattr(entry, 'memberOf') and entry.memberOf else []
                direct_cns = [_dn_to_cn(g) for g in member_of]
                direct_low = [g.lower() for g in direct_cns]
                if any(al in direct_low for al in allowed_low):
                    pass  # ok — appartenenza diretta trovata
                else:
                    # 2) Nested groups via LDAP_MATCHING_RULE_IN_CHAIN (OID AD)
                    try:
                        conn.search(base_dn,
                                    '(&(objectClass=group)(member:1.2.840.113556.1.4.1941:={}))'.format(user_dn),
                                    search_scope=SUBTREE,
                                    attributes=['cn'])
                        nested_low = [str(e.cn).lower() for e in conn.entries if e.cn]
                    except Exception:
                        nested_low = []
                    if not any(al in nested_low for al in allowed_low):
                        _auth_log.warning(
                            'LDAP_GROUP | %-20s | ldap | %s | non appartiene ai gruppi autorizzati',
                            username, ip)
                        return None
            else:
                # OpenLDAP: posixGroup/groupOfNames/groupOfUniqueNames
                found = False
                for grp in allowed_groups:
                    filt = ('(&(|(objectClass=posixGroup)(objectClass=groupOfNames)'
                            '(objectClass=groupOfUniqueNames))(cn={})'
                            '(|(memberUid={})(member={})(uniqueMember={})))').format(
                                grp, username, user_dn, user_dn)
                    conn.search(base_dn, filt, search_scope=SUBTREE, attributes=['cn'])
                    if conn.entries:
                        found = True
                        break
                if not found:
                    _auth_log.warning(
                        'LDAP_GROUP | %-20s | ldap | %s | non appartiene ai gruppi autorizzati',
                        username, ip)
                    return None

        # ── Verifica password (bind come utente) ─────────────────────
        try:
            Connection(srv, user=user_dn, password=password, auto_bind=True)
        except Exception:
            _auth_log.warning('LDAP_BADPW | %-20s | ldap | %s | password errata',
                              username, ip)
            return None

        _auth_log.info('LDAP_OK    | %-20s | ldap | %s | %s', username, ip, display_name)
        user_id = 'ldap:' + username
        session['_ldap_user'] = {
            'id':           user_id,
            'username':     username,
            'display_name': display_name,
        }
        return AuthUser(user_id, username, display_name, False, 'ldap')

    except Exception as e:
        _auth_log.warning('LDAP_ERR   | %-20s | ldap | %s | %s', username, ip, e)
        return None


def _do_login(username, password):
    """Autentica: LDAP (se abilitato) → fallback utenti locali."""
    ip = _client_ip()
    if _cfg_get('ldap_enabled', '0') == '1':
        u = _ldap_authenticate(username, password)
        if u:
            return u
        _auth_log.info('LOCAL_FALL | %-20s | -    | %s | fallback a utenti locali', username, ip)
    local_u = LocalUser.query.filter_by(username=username, enabled=True).first()
    if local_u and local_u.check_password(password):
        return AuthUser('local:' + str(local_u.id),
                        local_u.username, local_u.display_name,
                        local_u.is_admin, 'local')
    # Controlla se l'utente esiste ma è disabilitato
    disabled = LocalUser.query.filter_by(username=username, enabled=False).first()
    if disabled:
        _auth_log.warning('DISABLED   | %-20s | locale | %s | account disabilitato', username, ip)
    return None


# ── Route login / logout ───────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def auth_login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        ip = _client_ip()
        user = _do_login(username, password)
        if user:
            login_user(user)
            _auth_log.info('LOGIN_OK   | %-20s | %-6s | %s', user.username, user.source, ip)
            nxt = request.args.get('next', '').strip().rstrip('?')
            # Segui il redirect solo se punta dentro l'app (non root / di Apache)
            script = request.environ.get('SCRIPT_NAME', '/ipam')
            if (nxt and nxt.startswith('/') and '://' not in nxt
                    and nxt not in ('', '/') and nxt.startswith(script)):
                return redirect(nxt)
            return redirect(url_for('index'))
        _auth_log.warning('LOGIN_FAIL | %-20s | -      | %s | credenziali non valide', username, ip)
        error = 'Credenziali non valide'
    return render_template('login.html', error=error, version=APP_VERSION)


@app.route('/logout')
def auth_logout():
    if current_user.is_authenticated:
        _auth_log.info('LOGOUT     | %-20s | %-6s | %s',
                       current_user.username, current_user.source, _client_ip())
    session.pop('_ldap_user', None)
    logout_user()
    return redirect(url_for('auth_login'))


# ── API: Log autenticazione ────────────────────────────────────────

@app.route('/api/settings/auth-log')
def api_auth_log():
    try:
        n = min(int(request.args.get('lines', 200)), 1000)
    except (ValueError, TypeError):
        n = 200
    log_path = os.path.join(BASE_DIR, 'instance', 'auth.log')
    if not os.path.exists(log_path):
        return jsonify({'lines': [], 'exists': False})
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            all_lines = f.readlines()
        lines = [l.rstrip('\n') for l in all_lines[-n:]]
        lines.reverse()
        return jsonify({'lines': lines, 'exists': True, 'total': len(all_lines)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── API: Utenti Locali ─────────────────────────────────────────────

@app.route('/api/settings/local-users', methods=['GET'])
def api_local_users_list():
    users = LocalUser.query.order_by(LocalUser.id).all()
    return jsonify([u.to_dict() for u in users])


@app.route('/api/settings/local-users', methods=['POST'])
def api_local_users_create():
    data     = request.get_json() or {}
    username = (data.get('username') or '').strip()
    password = (data.get('password') or '').strip()
    if not username or not password:
        return jsonify({'error': 'Username e password obbligatori'}), 400
    if LocalUser.query.filter_by(username=username).first():
        return jsonify({'error': 'Username già esistente'}), 400
    u = LocalUser(
        username     = username,
        display_name = (data.get('display_name') or '').strip(),
        is_admin     = bool(data.get('is_admin', False)),
        enabled      = bool(data.get('enabled', True)),
    )
    u.set_password(password)
    db.session.add(u)
    db.session.commit()
    return jsonify(u.to_dict()), 201


@app.route('/api/settings/local-users/<int:uid>', methods=['PUT'])
def api_local_users_update(uid):
    u    = LocalUser.query.get_or_404(uid)
    data = request.get_json() or {}
    if 'display_name' in data:
        u.display_name = (data['display_name'] or '').strip()
    if 'is_admin' in data:
        u.is_admin = bool(data['is_admin'])
    if 'enabled' in data:
        u.enabled = bool(data['enabled'])
    if data.get('password'):
        u.set_password(data['password'].strip())
    db.session.commit()
    return jsonify(u.to_dict())


@app.route('/api/settings/local-users/<int:uid>', methods=['DELETE'])
def api_local_users_delete(uid):
    u = LocalUser.query.get_or_404(uid)
    if u.is_admin:
        n_admins = LocalUser.query.filter_by(is_admin=True, enabled=True).count()
        if n_admins <= 1:
            return jsonify({'error': "Impossibile eliminare l'unico amministratore"}), 400
    db.session.delete(u)
    db.session.commit()
    return jsonify({'ok': True})


# ── API: Configurazione LDAP ───────────────────────────────────────

_LDAP_KEYS = ['ldap_enabled', 'ldap_server_type', 'ldap_server', 'ldap_base_dn',
               'ldap_bind_dn', 'ldap_bind_password',
               'ldap_user_attr', 'ldap_allowed_groups']

_SNMP_KEYS = ['snmp_enabled', 'snmp_community', 'snmp_version',
              'snmp_routers', 'snmp_switches']


@app.route('/api/settings/ldap', methods=['GET'])
def api_ldap_get():
    return jsonify({k: _cfg_get(k, '') for k in _LDAP_KEYS})


@app.route('/api/settings/ldap', methods=['PUT'])
def api_ldap_save():
    data = request.get_json() or {}
    for k in _LDAP_KEYS:
        if k in data:
            _cfg_set(k, data[k] or '')
    return jsonify({'ok': True})


@app.route('/api/settings/ldap/test', methods=['POST'])
def api_ldap_test():
    data     = request.get_json() or {}
    username = (data.get('username') or '').strip()
    password = (data.get('password') or '').strip()
    if not username or not password:
        return jsonify({'error': 'Inserire username e password di test'}), 400
    try:
        from ldap3 import Server, Connection, ALL, SUBTREE
        server_url     = _cfg_get('ldap_server',        '').strip()
        base_dn        = _cfg_get('ldap_base_dn',       '').strip()
        bind_dn        = _cfg_get('ldap_bind_dn',       '').strip()
        bind_pw        = _cfg_get('ldap_bind_password', '').strip()
        user_attr      = _cfg_get('ldap_user_attr', 'sAMAccountName').strip() or 'sAMAccountName'
        allowed_groups = [g.strip() for g in _cfg_get('ldap_allowed_groups', '').split(',') if g.strip()]
        server_type    = _cfg_get('ldap_server_type', 'ad').strip() or 'ad'
        if not server_url or not base_dn:
            return jsonify({'ok': False, 'detail': 'Server LDAP e Base DN obbligatori'}), 400

        srv = Server(server_url, get_info=ALL, connect_timeout=5)
        try:
            if bind_dn:
                conn = Connection(srv, user=bind_dn, password=bind_pw, auto_bind=True)
            else:
                conn = Connection(srv, auto_bind=True)
        except Exception as e:
            return jsonify({'ok': False, 'detail': 'Bind di servizio fallito: ' + str(e)})

        if server_type == 'ad':
            attrs = ['distinguishedName', 'displayName', 'memberOf']
        else:
            attrs = ['displayName', 'cn', 'uid']

        conn.search(base_dn,
                    '({}={})'.format(user_attr, username),
                    search_scope=SUBTREE,
                    attributes=attrs)
        if not conn.entries:
            return jsonify({'ok': False, 'detail': 'Utente non trovato nel dominio'})

        entry   = conn.entries[0]
        user_dn = entry.entry_dn
        name    = str(entry.displayName) if hasattr(entry, 'displayName') and entry.displayName else username

        # Verifica password
        try:
            Connection(srv, user=user_dn, password=password, auto_bind=True)
        except Exception:
            return jsonify({'ok': False, 'detail': 'Password errata'})

        # Verifica gruppi
        lines = ['Autenticato come: ' + name]
        if allowed_groups:
            allowed_low = [ag.lower() for ag in allowed_groups]

            if server_type == 'ad':
                # AD — gruppi diretti
                member_of  = [str(g) for g in entry.memberOf] if hasattr(entry, 'memberOf') and entry.memberOf else []
                user_cns   = [_dn_to_cn(g) for g in member_of]
                direct_low = [g.lower() for g in user_cns]
                matched_direct = [g for g in allowed_groups if g.lower() in direct_low]
                if matched_direct:
                    lines.append('Gruppo OK (diretto): ' + ', '.join(matched_direct))
                    lines.append('Gruppi AD utente: ' + (', '.join(user_cns) if user_cns else '(nessuno)'))
                    return jsonify({'ok': True, 'detail': '\n'.join(lines)})
                # AD — nested groups
                lines.append('Nessun gruppo diretto — verifico gruppi annidati...')
                try:
                    conn.search(base_dn,
                                '(&(objectClass=group)(member:1.2.840.113556.1.4.1941:={}))'.format(user_dn),
                                search_scope=SUBTREE, attributes=['cn'])
                    nested_cns = [str(e.cn) for e in conn.entries if e.cn]
                except Exception as ex:
                    nested_cns = []
                    lines.append('Errore ricerca nested: ' + str(ex))
                matched_nested = [g for g in allowed_groups if g.lower() in [c.lower() for c in nested_cns]]
                if matched_nested:
                    lines.append('Gruppo OK (annidato): ' + ', '.join(matched_nested))
                    lines.append('Gruppi diretti AD: ' + (', '.join(user_cns) if user_cns else '(nessuno)'))
                    return jsonify({'ok': True, 'detail': '\n'.join(lines)})
                lines.append('ATTENZIONE: nessun gruppo corrisponde!')
                lines.append('Gruppi configurati: ' + ', '.join(allowed_groups))
                lines.append('Diretti AD:         ' + (', '.join(user_cns)   if user_cns   else '(nessuno)'))
                lines.append('Nested AD:          ' + (', '.join(nested_cns) if nested_cns else '(nessuno o non supportato)'))
                return jsonify({'ok': False, 'detail': '\n'.join(lines)})
            else:
                # OpenLDAP — posixGroup / groupOfNames / groupOfUniqueNames
                matched_ol = []
                for grp in allowed_groups:
                    filt = ('(&(|(objectClass=posixGroup)(objectClass=groupOfNames)'
                            '(objectClass=groupOfUniqueNames))(cn={})'
                            '(|(memberUid={})(member={})(uniqueMember={})))').format(
                                grp, username, user_dn, user_dn)
                    conn.search(base_dn, filt, search_scope=SUBTREE, attributes=['cn'])
                    if conn.entries:
                        matched_ol.append(grp)
                if matched_ol:
                    lines.append('Gruppo OK: ' + ', '.join(matched_ol))
                    return jsonify({'ok': True, 'detail': '\n'.join(lines)})
                lines.append('ATTENZIONE: nessun gruppo corrisponde!')
                lines.append('Gruppi configurati: ' + ', '.join(allowed_groups))
                lines.append('(Verificati con posixGroup/groupOfNames/groupOfUniqueNames)')
                return jsonify({'ok': False, 'detail': '\n'.join(lines)})
        else:
            lines.append('Nessun gruppo richiesto — accesso aperto a tutti gli utenti')

        return jsonify({'ok': True, 'detail': '\n'.join(lines)})
    except Exception as e:
        return jsonify({'ok': False, 'detail': str(e)})


# ── API: Configurazione SNMP Discovery ────────────────────────────

@app.route('/api/settings/snmp', methods=['GET'])
def api_snmp_get():
    return jsonify({k: _cfg_get(k, '') for k in _SNMP_KEYS})


@app.route('/api/settings/snmp', methods=['PUT'])
def api_snmp_save():
    data = request.get_json() or {}
    for k in _SNMP_KEYS:
        if k in data:
            _cfg_set(k, data[k] or '')
    return jsonify({'ok': True})


# ── API: SNMP Discovery ────────────────────────────────────────────

@app.route('/api/snmp/discover', methods=['POST'])
def api_snmp_discover_start():
    try:
        from snmp_discovery import run_discovery, _disc_status
        import threading
        if _disc_status.get('running'):
            return jsonify({'ok': False, 'error': 'Discovery già in esecuzione'}), 409
        t = threading.Thread(target=run_discovery, args=(app.app_context(),), daemon=True)
        t.start()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/snmp/discover/status', methods=['GET'])
def api_snmp_discover_status():
    try:
        from snmp_discovery import _disc_status
        return jsonify(dict(_disc_status))
    except Exception as e:
        return jsonify({'running': False, 'error': str(e)})


# ── API: Log Scansioni ────────────────────────────────────────────

@app.route('/api/logs', methods=['GET'])
def api_logs():
    scan_type = request.args.get('type', '').strip()
    limit     = min(int(request.args.get('limit', 200)), 500)
    q = ScanLog.query
    if scan_type:
        q = q.filter_by(scan_type=scan_type)
    logs = q.order_by(ScanLog.started_at.desc()).limit(limit).all()
    return jsonify([l.to_dict() for l in logs])


@app.route('/api/logs/<int:log_id>', methods=['DELETE'])
def api_log_delete(log_id):
    entry = ScanLog.query.get_or_404(log_id)
    db.session.delete(entry)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/logs/clear', methods=['POST'])
def api_logs_clear():
    data      = request.get_json() or {}
    scan_type = data.get('type', '').strip()
    q = ScanLog.query.filter(ScanLog.status != 'running')
    if scan_type:
        q = q.filter_by(scan_type=scan_type)
    q.delete(synchronize_session=False)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/logs')
def scan_logs_page():
    return render_template('logs.html')


#  ROUTES  PAGES 

@app.route('/')
def index():
    # Statistiche globali con query aggregate (una sola query invece di N)
    total_networks = Network.query.count()
    total_vlans    = VLan.query.count()

    # Conteggi IP per status in una sola query
    from sqlalchemy import func
    ip_stats = dict(db.session.query(
        IPRecord.status, func.count(IPRecord.id)
    ).group_by(IPRecord.status).all())
    total_ips  = sum(ip_stats.values())
    used_ips   = ip_stats.get('used', 0) + ip_stats.get('reserved', 0) + ip_stats.get('dhcp', 0)
    free_ips   = ip_stats.get('available', 0)

    # Utilizzo globale calcolato direttamente in SQL
    result = db.session.execute(db.text(
        "SELECT COUNT(*) as total, "
        "SUM(CASE WHEN status != 'available' THEN 1 ELSE 0 END) as used "
        "FROM ip_records"
    )).fetchone()
    total_hosts  = result[0] or 0
    total_used   = result[1] or 0
    global_usage = round(total_used / total_hosts * 100, 1) if total_hosts else 0

    # Top 5 subnet per utilizzo con query SQL diretta
    top_rows = db.session.execute(db.text("""
        SELECT n.id, n.name, n.address, n.cidr, n.location,
               COUNT(r.id) as total_r,
               SUM(CASE WHEN r.status != 'available' THEN 1 ELSE 0 END) as used_r
        FROM networks n
        LEFT JOIN ip_records r ON r.network_id = n.id
        WHERE n.network_type = 'subnet'
        GROUP BY n.id
        HAVING total_r > 0
        ORDER BY CAST(used_r AS FLOAT) / total_r DESC
        LIMIT 5
    """)).fetchall()

    top_networks = []
    for row in top_rows:
        pct = round(row[6] / row[5] * 100, 1) if row[5] else 0
        top_networks.append({
            'id': row[0], 'name': row[1], 'address': row[2],
            'cidr': row[3], 'location': row[4] or '',
            'used_count': row[6] or 0, 'total_hosts': row[5] or 0,
            'usage_percent': pct,
        })

    return render_template('index.html',
        total_networks = total_networks,
        total_ips      = total_ips,
        used_ips       = used_ips,
        total_vlans    = total_vlans,
        total_hosts    = total_hosts,
        total_used     = total_used,
        global_usage   = global_usage,
        top_networks   = top_networks,
    )


@app.route('/networks')
def networks():
    search   = request.args.get('q', '')
    location = request.args.get('location', '')
    net_type = request.args.get('type', '')
    page     = request.args.get('page', 1, type=int)

    q = Network.query
    if search:
        # Cerca anche negli hostname degli IP record tramite subquery
        subnet_ids_with_hostname = db.session.query(IPRecord.network_id).filter(
            IPRecord.hostname.ilike(f'%{search}%')
        ).distinct().subquery()

        q = q.filter(db.or_(
            Network.name.ilike(f'%{search}%'),
            Network.address.ilike(f'%{search}%'),
            Network.description.ilike(f'%{search}%'),
            Network.location.ilike(f'%{search}%'),
            Network.id.in_(subnet_ids_with_hostname),
        ))
    if location:
        q = q.filter(Network.location.ilike(f'%{location}%'))
    if net_type:
        q = q.filter(Network.network_type == net_type)

    pagination = q.order_by(Network.name).paginate(page=page, per_page=20, error_out=False)
    locations  = [r[0] for r in
                  db.session.query(Network.location).distinct().filter(Network.location != None).all()
                  if r[0]]

    # Se la ricerca ha trovato reti tramite hostname, mostra anche quanti IP corrispondono
    hostname_matches = {}
    if search:
        matches = db.session.query(
            IPRecord.network_id,
            IPRecord.ip_address,
            IPRecord.hostname
        ).filter(
            IPRecord.hostname.ilike(f'%{search}%')
        ).all()
        for net_id, ip, hostname in matches:
            if net_id not in hostname_matches:
                hostname_matches[net_id] = []
            hostname_matches[net_id].append({'ip': ip, 'hostname': hostname})

    return render_template('networks.html',
        networks         = pagination.items,
        pagination       = pagination,
        search           = search,
        location         = location,
        net_type         = net_type,
        locations        = locations,
        hostname_matches = hostname_matches,
    )


def _ip_numeric_order(direction='asc'):
    """Ordina per ip_int (indice INTEGER su IPv4 — O(log n))."""
    col = IPRecord.ip_int
    if direction == 'desc':
        return [col.desc()]
    return [col.asc()]


@app.route('/networks/<int:net_id>')
def network_detail(net_id):
    import socket, struct
    network   = Network.query.get_or_404(net_id)
    page      = request.args.get('page',  1,       type=int)
    view_mode = request.args.get('view',  'used')  # used | free | all
    sort_by   = request.args.get('sort',  'ip_address')
    sort_dir  = request.args.get('dir',   'asc')
    children  = network.children.order_by(Network.address, Network.cidr).all()

    # ── Statistiche figli in una sola query (evita N+1) ──────────
    child_stats = {}
    if children:
        child_ids = [c.id for c in children]
        id_list = ','.join(str(i) for i in child_ids)
        rows = db.session.execute(db.text(
            '''SELECT network_id,
                      SUM(CASE WHEN status != 'available' THEN 1 ELSE 0 END) AS used,
                      COUNT(*) AS total_rec
               FROM ip_records
               WHERE network_id IN ({})
               GROUP BY network_id'''.format(id_list)
        )).fetchall()
        db_stats = {}
        for row in rows:
            db_stats[row[0]] = row[1] or 0  # used count
        # Arricchisci con total_hosts (calcolo puro, nessuna query DB) e usage %
        for child in children:
            used  = db_stats.get(child.id, 0)
            total = child.total_hosts
            avail = max(0, total - used)
            pct   = round(used * 100.0 / total, 1) if total > 0 else 0.0
            child_stats[child.id] = {
                'used':          used,
                'total_hosts':   total,
                'available':     avail,
                'usage_percent': pct,
            }

    # ── Ordinamento ──────────────────────────────────────────────
    if sort_by == 'ip_address':
        order = _ip_numeric_order(sort_dir)
    else:
        sort_map = {
            'hostname':    IPRecord.hostname,
            'mac_address': IPRecord.mac_address,
            'device_type': IPRecord.device_type,
            'status':      IPRecord.status,
            'last_seen':   IPRecord.last_seen,
        }
        col   = sort_map.get(sort_by, IPRecord.ip_address)
        order = [col.desc() if sort_dir == 'desc' else col.asc()]

    # ── Filtro vista ─────────────────────────────────────────────
    q = IPRecord.query.filter_by(network_id=net_id)

    if view_mode == 'used':
        # Solo IP occupati: tutto tranne available
        q = q.filter(IPRecord.status != 'available')
    elif view_mode == 'free':
        # Solo IP liberi: status = available
        q = q.filter(IPRecord.status == 'available')
    # view_mode == 'all': nessun filtro, tutti i record

    ip_pag = q.order_by(*order).paginate(page=page, per_page=50, error_out=False)
    return render_template('network_detail.html',
        network      = network,
        ip_records   = ip_pag.items,
        pagination   = ip_pag,
        children     = children,
        child_stats  = child_stats,
        view_mode    = view_mode,
        sort_by      = sort_by,
        sort_dir     = sort_dir,
    )


@app.route('/vlans')
def vlans():
    all_vlans = VLan.query.order_by(VLan.vlan_id).all()

    # Carica tutte le subnet in una sola query con statistiche aggregate
    subnet_rows = db.session.execute(db.text("""
        SELECT n.id, n.name, n.address, n.cidr, n.location, n.status,
               n.vlan_id,
               COUNT(r.id) as total_r,
               SUM(CASE WHEN r.status != 'available' THEN 1 ELSE 0 END) as used_r
        FROM networks n
        LEFT JOIN ip_records r ON r.network_id = n.id
        WHERE n.network_type = 'subnet'
        GROUP BY n.id
        ORDER BY n.address, n.cidr
    """)).fetchall()

    # Crea oggetti leggeri per il template
    class SubnetInfo:
        def __init__(self, row):
            self.id            = row[0]
            self.name          = row[1]
            self.address       = row[2]
            self.cidr          = row[3]
            self.location      = row[4]
            self.status        = row[5]
            self.vlan_id       = row[6]
            self._total        = row[7] or 0
            self._used         = row[8] or 0
        @property
        def used_count(self):      return self._used
        @property
        def available_count(self): return self._total - self._used
        @property
        def usage_percent(self):
            return round(self._used / self._total * 100, 1) if self._total else 0

    # Raggruppa per vlan_id
    vlan_subnets    = {}
    no_vlan_subnets = []
    for row in subnet_rows:
        s = SubnetInfo(row)
        if s.vlan_id:
            vlan_subnets.setdefault(s.vlan_id, []).append(s)
        else:
            no_vlan_subnets.append(s)

    return render_template('vlans.html',
        vlans           = all_vlans,
        vlan_subnets    = vlan_subnets,
        no_vlan_subnets = no_vlan_subnets,
    )


@app.route('/ip-addresses')
def ip_addresses():
    search      = request.args.get('q',           '').strip()
    status      = request.args.get('status',      '')
    device_type = request.args.get('device_type', '')
    os_type_f   = request.args.get('os_type',     '')
    switch_f    = request.args.get('switch_name', '').strip()
    network_q   = request.args.get('network_q',   '').strip()
    sort_by     = request.args.get('sort',        'ip_address')
    sort_dir    = request.args.get('dir',         'asc')
    page        = request.args.get('page',         1, type=int)

    valid_sorts = {'ip_address', 'hostname', 'mac_address', 'switch_name', 'switch_port',
                   'device_type', 'os_type', 'status', 'last_seen', 'network'}
    if sort_by not in valid_sorts:
        sort_by = 'ip_address'
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'asc'

    q = IPRecord.query
    _net_joined = False

    if search:
        q = q.filter(db.or_(
            IPRecord.ip_address.ilike(f'%{search}%'),
            IPRecord.hostname.ilike(f'%{search}%'),
            IPRecord.mac_address.ilike(f'%{search}%'),
        ))
    if status:
        q = q.filter(IPRecord.status == status)
    if device_type:
        q = q.filter(IPRecord.device_type == device_type)
    if os_type_f:
        q = q.filter(IPRecord.os_type.ilike(f'%{os_type_f}%'))
    if switch_f:
        q = q.filter(IPRecord.switch_name.ilike(f'%{switch_f}%'))
    if network_q:
        q = q.join(Network, IPRecord.network_id == Network.id).filter(
            db.or_(
                Network.address.ilike(f'%{network_q}%'),
                Network.name.ilike(f'%{network_q}%'),
            )
        )
        _net_joined = True

    if sort_by == 'ip_address':
        order = _ip_numeric_order(sort_dir)
    elif sort_by == 'network':
        if not _net_joined:
            q = q.outerjoin(Network, IPRecord.network_id == Network.id)
        if sort_dir == 'desc':
            order = [Network.address.desc(), Network.cidr.desc()]
        else:
            order = [Network.address.asc(),  Network.cidr.asc()]
    else:
        sort_map = {
            'hostname':    IPRecord.hostname,
            'mac_address': IPRecord.mac_address,
            'switch_name': IPRecord.switch_name,
            'switch_port': IPRecord.switch_port,
            'device_type': IPRecord.device_type,
            'os_type':     IPRecord.os_type,
            'status':      IPRecord.status,
            'last_seen':   IPRecord.last_seen,
        }
        col = sort_map[sort_by]
        order = [col.desc() if sort_dir == 'desc' else col.asc()]

    pagination = q.order_by(*order).paginate(page=page, per_page=50, error_out=False)

    import time as _time
    _now = _time.time()
    if _now - _ip_filter_cache['ts'] > _IP_FILTER_TTL:
        _ip_filter_cache['device_types'] = [r[0] for r in db.session.execute(db.text(
            "SELECT DISTINCT device_type FROM ip_records"
            " WHERE device_type IS NOT NULL AND device_type != '' ORDER BY device_type"
        )).fetchall()]
        _ip_filter_cache['os_types'] = [r[0] for r in db.session.execute(db.text(
            "SELECT DISTINCT os_type FROM ip_records"
            " WHERE os_type IS NOT NULL AND os_type != '' ORDER BY os_type"
        )).fetchall()]
        _ip_filter_cache['ts'] = _now
    device_types = _ip_filter_cache['device_types']
    os_types     = _ip_filter_cache['os_types']

    return render_template('ip_addresses.html',
        ip_records   = pagination.items,
        pagination   = pagination,
        search       = search,
        status       = status,
        device_type  = device_type,
        os_type      = os_type_f,
        switch_name  = switch_f,
        network_q    = network_q,
        sort_by      = sort_by,
        sort_dir     = sort_dir,
        device_types = device_types,
        os_types     = os_types,
    )


@app.route('/docs')
def documentation():
    return render_template('docs.html')


@app.route('/bulk-edit')
def bulk_edit():
    return render_template('bulk_edit.html')


#  API 

@app.route('/api/version', methods=['GET'])
def api_version():
    return jsonify({
        'version':   APP_VERSION,
        'build':     APP_BUILD,
        'changelog': APP_CHANGELOG,
    })


@app.route('/api/networks', methods=['GET'])
def api_networks():
    import json as _json
    net_type = request.args.get('type', '').strip()
    q_str    = request.args.get('q',    '').strip()

    where_parts = []
    params      = {}
    if net_type in ('supernet', 'subnet'):
        where_parts.append("network_type = :ntype")
        params['ntype'] = net_type
    if q_str:
        where_parts.append(
            "(name LIKE :q OR address LIKE :q OR location LIKE :q "
            "OR country LIKE :q OR site LIKE :q OR usage LIKE :q "
            "OR zone LIKE :q OR gateway LIKE :q)")
        params['q'] = f'%{q_str}%'

    where = ('WHERE ' + ' AND '.join(where_parts)) if where_parts else ''
    sql   = f'''SELECT id,name,address,cidr,vlan_id,location,description,
                       country,site,usage,zone,gateway,
                       network_type,status,parent_id
                FROM networks {where}
                ORDER BY address,cidr'''
    rows = db.session.execute(db.text(sql), params).fetchall()
    result = []
    for r in rows:
        result.append({
            'id': r[0], 'name': r[1], 'address': r[2], 'cidr': r[3],
            'vlan_id': r[4], 'location': r[5], 'description': r[6],
            'country': r[7], 'site': r[8], 'usage': r[9],
            'zone': r[10], 'gateway': r[11],
            'network_type': r[12], 'status': r[13], 'parent_id': r[14],
        })
    return app.response_class(
        response=_json.dumps(result, separators=(',', ':')),
        mimetype='application/json')


# Cache albero reti in memoria — TTL 5 secondi (sicuro con Gunicorn multi-worker)
import time as _time
_tree_cache      = {'data': None, 'ts': 0.0}
_ip_filter_cache = {'device_types': [], 'os_types': [], 'ts': 0.0}
_IP_FILTER_TTL   = 300   # secondi — ricalcola dropdown ogni 5 minuti
_TREE_TTL = 5.0  # secondi

@app.route('/api/networks/tree', methods=['GET'])
def api_network_tree():
    import socket, struct, json as _json
    # Restituisce la cache solo se e' recente (max 5 secondi)
    now = _time.time()
    if _tree_cache['data'] and (now - _tree_cache['ts']) < _TREE_TTL:
        return app.response_class(
            response=_tree_cache['data'], mimetype='application/json')

    # Query SQL diretta: piu' veloce di ORM + evita il calcolo usage_percent
    rows = db.session.execute(db.text(
        'SELECT id, name, address, cidr, network_type, parent_id '
        'FROM networks ORDER BY address, cidr'
    )).fetchall()

    # Costruisci dizionario nodi (solo campi essenziali per la sidebar)
    nodes = {}
    for r in rows:
        nodes[r[0]] = {
            'id': r[0], 'name': r[1], 'address': r[2],
            'cidr': r[3], 'network_type': r[4],
            'parent_id': r[5], 'children': [],
        }

    # Costruisci albero collegando figli ai padri
    roots = []
    for r in rows:
        pid = r[5]
        if pid and pid in nodes:
            nodes[pid]['children'].append(nodes[r[0]])
        else:
            roots.append(nodes[r[0]])

    # Ordina per indirizzo IP numerico
    def ip_key(node):
        try:
            return struct.unpack('!I', socket.inet_aton(node['address']))[0]
        except Exception:
            return 0

    def sort_tree(lst):
        lst.sort(key=ip_key)
        for n in lst:
            if n['children']:
                sort_tree(n['children'])

    sort_tree(roots)

    # Serializza con JSON compatto e salva in cache
    data = _json.dumps(roots, separators=(',', ':'))
    _tree_cache['data'] = data
    _tree_cache['ts']   = _time.time()
    return app.response_class(response=data, mimetype='application/json')


@app.route('/api/networks/tree/invalidate', methods=['POST'])
def api_tree_invalidate():
    _tree_cache['ts'] = 0.0
    return jsonify({'message': 'Cache invalidata'})


@app.route('/api/networks', methods=['POST'])
def api_create_network():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Payload JSON mancante'}), 400
        try:
            net_obj = ipaddress.ip_network(
                '{}/{}'.format(data['address'], data['cidr']), strict=False)
        except Exception as e:
            return jsonify({'error': 'Indirizzo non valido: {}'.format(e)}), 400

        norm_address = str(net_obj.network_address)
        existing = Network.query.filter_by(address=norm_address, cidr=int(data['cidr'])).first()
        if existing:
            return jsonify({'error': 'Rete già presente', 'id': existing.id}), 409

        parent_id = data.get('parent_id')
        if parent_id is None:
            best_parent = None
            best_cidr   = -1
            for candidate in Network.query.all():
                if candidate.id is None:
                    continue
                try:
                    cand_net = ipaddress.ip_network(
                        '{}/{}'.format(candidate.address, candidate.cidr), strict=False)
                    # subnet_of() richiede Python >= 3.7 — confronto manuale (compatibile 3.6)
                    if (cand_net != net_obj
                            and int(cand_net.network_address) <= int(net_obj.network_address)
                            and int(cand_net.broadcast_address) >= int(net_obj.broadcast_address)):
                        if candidate.cidr > best_cidr:
                            best_cidr   = candidate.cidr
                            best_parent = candidate
                except Exception:
                    continue
            if best_parent:
                parent_id = best_parent.id

        network = Network(
            name         = data['name'],
            address      = norm_address,
            cidr         = int(data['cidr']),
            mask         = str(net_obj.netmask),
            vlan_id      = data.get('vlan_id'),
            location     = data.get('location'),
            description  = data.get('description'),
            parent_id    = parent_id,
            network_type = data.get('network_type', 'subnet'),
            status       = data.get('status', 'active'),
        )
        db.session.add(network)
        db.session.flush()   # ottieni network.id senza chiudere la transazione

        # ── Creazione automatica degli IP con stato 'available' ──────────────
        # Limite: reti con più di 65534 host (CIDR <= 16) vengono saltate.
        # Per /31 e /32 hosts() comprende tutti gli indirizzi.
        MAX_AUTO_IPS = 65534
        cidr_int     = int(data['cidr'])
        auto_ips_created = 0
        auto_ips_skipped = False

        # Calcolo corretto del numero di host per tutti i CIDR
        if cidr_int >= 31:
            n_hosts = net_obj.num_addresses          # /31=2, /32=1
        else:
            n_hosts = net_obj.num_addresses - 2      # escludi network e broadcast

        if n_hosts > MAX_AUTO_IPS:
            auto_ips_skipped = True
        else:
            now_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S.%f')
            sql_ip  = sa_text(
                "INSERT OR IGNORE INTO ip_records "
                "(ip_address, status, network_id, created_at, updated_at) "
                "VALUES (:ip, 'available', :nid, :now, :now)"
            )
            # Usa la connessione della sessione corrente per compatibilità
            # con SQLAlchemy 1.x (produzione) e 2.x (dev)
            conn  = db.session.connection()
            batch = []
            for ip in net_obj.hosts():
                batch.append({'ip': str(ip), 'nid': network.id, 'now': now_str})
                if len(batch) >= 500:
                    conn.execute(sql_ip, batch)
                    batch = []
            if batch:
                conn.execute(sql_ip, batch)
            # Conta le righe effettivamente inserite (INSERT OR IGNORE può saltarne)
            row = conn.execute(
                sa_text("SELECT COUNT(*) FROM ip_records WHERE network_id = :nid"),
                {'nid': network.id}
            )
            auto_ips_created = row.scalar() or 0

        db.session.commit()
        _tree_cache['ts'] = 0.0

        result = network.to_dict()
        result['auto_ips_created'] = auto_ips_created
        result['auto_ips_skipped'] = auto_ips_skipped
        return jsonify(result), 201

    except Exception as e:
        import traceback
        import logging as _lg
        _lg.getLogger('ipam').error('api_create_network: %s\n%s', e, traceback.format_exc())
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({'error': str(e)}), 500


@app.route('/api/networks/<int:net_id>', methods=['PUT'])
def api_update_network(net_id):
    network = Network.query.get_or_404(net_id)
    data    = request.get_json()
    for field in ['name', 'location', 'description', 'country', 'site', 'usage', 'zone', 'gateway', 'vlan_id', 'status', 'network_type', 'parent_id']:
        if field in data:
            setattr(network, field, data[field])
    network.updated_at = datetime.utcnow()
    db.session.commit()
    _tree_cache['ts'] = 0.0
    return jsonify(network.to_dict())


@app.route('/api/networks/<int:net_id>', methods=['DELETE'])
def api_delete_network(net_id):
    network   = Network.query.get_or_404(net_id)
    force     = request.args.get('force', '0') == '1'
    child_nets = Network.query.filter_by(parent_id=net_id).count()
    if child_nets > 0:
        return jsonify({'error': 'Impossibile eliminare: contiene ' + str(child_nets) + ' subnet figlie. Eliminale prima.'}), 409
    ip_count = IPRecord.query.filter_by(network_id=net_id).count()
    if ip_count > 0 and not force:
        return jsonify({'error': 'Impossibile eliminare: contiene ' + str(ip_count) + ' indirizzi IP. Eliminali prima.', 'ip_count': ip_count}), 409
    if force and ip_count > 0:
        IPRecord.query.filter_by(network_id=net_id).delete()
    db.session.delete(network)
    db.session.commit()
    _tree_cache['ts'] = 0.0
    return jsonify({'message': 'Rete eliminata', 'ip_deleted': ip_count if force else 0})


# ── Bulk edit ─────────────────────────────────────────────────────
_NET_EDITABLE = ['name','location','description','country','site','usage',
                 'zone','gateway','vlan_id','status','network_type']
_IP_EDITABLE  = ['hostname','mac_address','device_type','status','description']

@app.route('/api/bulk-edit/networks', methods=['PUT'])
def api_bulk_edit_networks():
    data   = request.get_json()
    ids    = data.get('ids', [])
    fields = {k: v for k, v in data.get('fields', {}).items()
              if k in _NET_EDITABLE and v not in (None, '')}
    if not ids:
        return jsonify({'error': 'Nessun record selezionato'}), 400
    if not fields:
        return jsonify({'error': 'Nessun campo da aggiornare'}), 400
    updated = 0
    for net_id in ids:
        net = Network.query.get(net_id)
        if not net:
            continue
        for k, v in fields.items():
            setattr(net, k, v)
        net.updated_at = datetime.utcnow()
        updated += 1
    db.session.commit()
    _tree_cache['ts'] = 0.0
    return jsonify({'updated': updated})


@app.route('/api/bulk-edit/ip-records', methods=['PUT'])
def api_bulk_edit_ip_records():
    data   = request.get_json()
    ids    = data.get('ids', [])
    fields = {k: v for k, v in data.get('fields', {}).items()
              if k in _IP_EDITABLE and v not in (None, '')}
    if not ids:
        return jsonify({'error': 'Nessun record selezionato'}), 400
    if not fields:
        return jsonify({'error': 'Nessun campo da aggiornare'}), 400
    updated = 0
    for rec_id in ids:
        rec = IPRecord.query.get(rec_id)
        if not rec:
            continue
        for k, v in fields.items():
            setattr(rec, k, v)
        updated += 1
    db.session.commit()
    return jsonify({'updated': updated})


@app.route('/api/ip-records', methods=['GET'])
def api_ip_records():
    network_id = request.args.get('network_id', type=int)
    q_str      = request.args.get('q',      '').strip()
    status_f   = request.args.get('status', '').strip()
    limit      = request.args.get('limit',  500, type=int)
    offset     = request.args.get('offset', 0,   type=int)
    q = IPRecord.query
    if network_id:
        q = q.filter_by(network_id=network_id)
    if q_str:
        like = f'%{q_str}%'
        q = q.filter(db.or_(
            IPRecord.ip_address.ilike(like),
            IPRecord.hostname.ilike(like),
            IPRecord.mac_address.ilike(like),
            IPRecord.device_type.ilike(like),
            IPRecord.description.ilike(like),
        ))
    if status_f:
        q = q.filter(IPRecord.status == status_f)
    total = q.count()
    items = q.order_by(*_ip_numeric_order()).offset(offset).limit(limit).all()
    return jsonify({'total': total, 'items': [r.to_dict() for r in items]})


@app.route('/api/ip-records', methods=['POST'])
def api_create_ip():
    import socket as _s, struct as _st
    data = request.get_json()
    try:
        ipaddress.ip_address(data['ip_address'])
    except Exception as e:
        return jsonify({'error': f'IP non valido: {e}'}), 400
    if IPRecord.query.filter_by(ip_address=data['ip_address']).first():
        return jsonify({'error': 'IP già presente'}), 409
    try:
        _ip_int = _st.unpack('!I', _s.inet_aton(data['ip_address']))[0]
    except Exception:
        _ip_int = None
    record = IPRecord(
        ip_address  = data['ip_address'],
        ip_int      = _ip_int,
        hostname    = data.get('hostname'),
        mac_address = data.get('mac_address'),
        device_type = data.get('device_type'),
        os_type     = data.get('os_type'),
        status      = data.get('status', 'used'),
        network_id  = data.get('network_id'),
        description = data.get('description'),
        last_seen   = datetime.utcnow() if data.get('status', 'used') == 'used' else None,
    )
    db.session.add(record)
    db.session.commit()
    return jsonify(record.to_dict()), 201


@app.route('/api/ip-records/<int:rec_id>', methods=['PUT'])
def api_update_ip(rec_id):
    record = IPRecord.query.get_or_404(rec_id)
    data   = request.get_json()
    for field in ['hostname', 'mac_address', 'device_type', 'os_type', 'status', 'description']:
        if field in data:
            setattr(record, field, data[field])
    record.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify(record.to_dict())


@app.route('/api/ip-records/<int:rec_id>', methods=['DELETE'])
def api_delete_ip(rec_id):
    record = IPRecord.query.get_or_404(rec_id)
    db.session.delete(record)
    db.session.commit()
    return jsonify({'message': 'Record eliminato'})


@app.route('/api/ip-records/<int:rec_id>/clear', methods=['PUT'])
def api_clear_ip(rec_id):
    record = IPRecord.query.get_or_404(rec_id)
    record.hostname    = None
    record.mac_address = None
    record.device_type = None
    record.os_type     = None
    record.description = None
    record.status      = 'free'
    record.last_seen   = None
    record.updated_at  = datetime.utcnow()
    db.session.commit()
    return jsonify(record.to_dict())


@app.route('/api/vlans', methods=['GET'])
def api_vlans():
    return jsonify([v.to_dict() for v in VLan.query.order_by(VLan.vlan_id).all()])


@app.route('/api/vlans', methods=['POST'])
def api_create_vlan():
    data = request.get_json()
    if VLan.query.filter_by(vlan_id=data['vlan_id']).first():
        return jsonify({'error': 'VLAN ID già esistente'}), 409
    vlan = VLan(
        vlan_id     = data['vlan_id'],
        name        = data['name'],
        description = data.get('description'),
        status      = data.get('status', 'active'),
    )
    db.session.add(vlan)
    db.session.commit()
    return jsonify(vlan.to_dict()), 201


@app.route('/api/vlans/<int:v_id>', methods=['PUT'])
def api_update_vlan(v_id):
    vlan = VLan.query.get_or_404(v_id)
    data = request.get_json()
    for field in ['name', 'description', 'status']:
        if field in data:
            setattr(vlan, field, data[field])
    db.session.commit()
    return jsonify(vlan.to_dict())


@app.route('/api/vlans/<int:v_id>', methods=['DELETE'])
def api_delete_vlan(v_id):
    vlan = VLan.query.get_or_404(v_id)
    db.session.delete(vlan)
    db.session.commit()
    return jsonify({'message': 'VLAN eliminata'})


@app.route('/api/stats')
def api_stats():
    from sqlalchemy import func
    # Tutto in 2 query aggregate invece di N query per ogni subnet
    ip_stats = dict(db.session.query(
        IPRecord.status, func.count(IPRecord.id)
    ).group_by(IPRecord.status).all())

    total_ips    = sum(ip_stats.values())
    used_ips     = ip_stats.get('used', 0)
    reserved_ips = ip_stats.get('reserved', 0)
    free_ips     = ip_stats.get('available', 0)
    total_used   = used_ips + reserved_ips + ip_stats.get('dhcp', 0)
    global_usage = round(total_used / total_ips * 100, 1) if total_ips else 0

    return jsonify({
        'total_networks': Network.query.count(),
        'total_subnets':  Network.query.filter_by(network_type='subnet').count(),
        'total_vlans':    VLan.query.count(),
        'total_ips':      total_ips,
        'used_ips':       used_ips,
        'reserved_ips':   reserved_ips,
        'free_ips':       free_ips,
        'total_hosts':    total_ips,
        'total_used':     total_used,
        'global_usage':   global_usage,
    })




# ── SCAN API ─────────────────────────────────────────────────────

@app.route('/api/networks/<int:net_id>/scan', methods=['POST'])
def api_start_scan(net_id):
    """Avvia lo scan asincrono di una subnet."""
    network = Network.query.get_or_404(net_id)
    if network.network_type == 'supernet':
        return jsonify({'error': 'Non e possibile scansionare una supernet direttamente'}), 400
    try:
        from scanner import start_scan_async
        ok, msg = start_scan_async(net_id, app)
        if ok:
            return jsonify({'message': msg, 'network_id': net_id})
        else:
            return jsonify({'error': msg}), 409
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/networks/<int:net_id>/scan/status', methods=['GET'])
def api_scan_status(net_id):
    """Restituisce lo stato dello scan per una subnet."""
    try:
        from scanner import get_scan_status
        status = get_scan_status(net_id)
        return jsonify(status)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/scan/status', methods=['GET'])
def api_scan_status_all():
    """Restituisce lo stato di tutti gli scan in corso."""
    try:
        from scanner import get_scan_status
        return jsonify(get_scan_status())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/scan/test-ip', methods=['GET'])
def api_scan_test_ip():
    """
    Diagnostica scanner su un singolo IP.
    Esegue ping verbose, TCP probe per porta, DNS PTR e mostra la configurazione attiva.
    Parametri: ?ip=10.x.x.x
    """
    import os as _os, shutil, subprocess as _sp
    ip_str = request.args.get('ip', '').strip()
    if not ip_str:
        return jsonify({'error': 'Parametro ?ip= obbligatorio'}), 400

    try:
        import ipaddress as _ipa
        _ipa.ip_address(ip_str)
    except ValueError:
        return jsonify({'error': 'IP non valido: {}'.format(ip_str)}), 400

    from scanner import (ping_verbose, tcp_probe_verbose, dns_ptr_lookup,
                         _runtime_dns, _runtime_tcp_enabled, _runtime_tcp_ports,
                         DNS_SERVERS)

    # ── Ping verbose ─────────────────────────────────────────────
    p_alive, p_out, p_err, p_rc, p_exc = ping_verbose(ip_str)

    # ── TCP probe verbose ─────────────────────────────────────────
    tcp_results = tcp_probe_verbose(ip_str) if _runtime_tcp_enabled else []
    tcp_alive   = any(r['result'] in ('open', 'refused') for r in tcp_results)

    # ── DNS PTR lookup ────────────────────────────────────────────
    dns_servers = _runtime_dns or DNS_SERVERS
    try:
        hostname = dns_ptr_lookup(ip_str, dns_servers=dns_servers)
    except Exception as e:
        hostname = None

    # ── Info sistema ──────────────────────────────────────────────
    ping_bin  = shutil.which('ping') or '/bin/ping'
    ping_perm = None
    try:
        st = _os.stat(ping_bin)
        ping_perm = oct(st.st_mode)
    except Exception:
        pass

    proc_user = None
    try:
        import pwd
        proc_user = pwd.getpwuid(_os.getuid()).pw_name
    except Exception:
        proc_user = str(_os.getuid())

    return jsonify({
        'ip':             ip_str,
        'ping': {
            'alive':      p_alive,
            'returncode': p_rc,
            'output':     p_out.strip(),
            'stderr':     p_err.strip(),
            'error':      p_exc,
            'binary':     ping_bin,
            'perms':      ping_perm,
        },
        'tcp_probe': {
            'enabled':    _runtime_tcp_enabled,
            'alive':      tcp_alive,
            'ports':      tcp_results,
        },
        'dns': {
            'servers':    dns_servers,
            'hostname':   hostname,
        },
        'process': {
            'user':       proc_user,
            'pid':        _os.getpid(),
        },
        'overall_alive': p_alive or tcp_alive,
    })


#  IMPOSTAZIONI 

@app.route('/settings')
def settings():
    return render_template('settings.html')


@app.route('/api/settings', methods=['GET'])
def api_settings_get():
    return jsonify({
        'dns_primary':          _cfg_get('dns_primary',   ''),
        'dns_secondary':        _cfg_get('dns_secondary', ''),
        'global_scan_enabled':  _cfg_get('global_scan_enabled',  '0') == '1',
        'global_scan_time':     _cfg_get('global_scan_time',     '02:00'),
        'tcp_probe_enabled':    _cfg_get('tcp_probe_enabled', '1') == '1',
        'tcp_probe_ports':      _cfg_get('tcp_probe_ports', '22,80,443,445,3389,135,8080,8443'),
    })


@app.route('/api/settings', methods=['PUT'])
def api_settings_put():
    data = request.get_json() or {}
    if 'dns_primary' in data:
        _cfg_set('dns_primary',   data['dns_primary'].strip())
    if 'dns_secondary' in data:
        _cfg_set('dns_secondary', data['dns_secondary'].strip())
    if 'global_scan_enabled' in data:
        _cfg_set('global_scan_enabled', '1' if data['global_scan_enabled'] else '0')
    if 'global_scan_time' in data:
        _cfg_set('global_scan_time', data['global_scan_time'].strip())
    if 'tcp_probe_enabled' in data:
        _cfg_set('tcp_probe_enabled', '1' if data['tcp_probe_enabled'] else '0')
    if 'tcp_probe_ports' in data:
        _cfg_set('tcp_probe_ports', data['tcp_probe_ports'].strip())
    _update_scanner_dns()
    _update_scanner_tcp()
    _reload_scheduler_jobs()
    return jsonify({'ok': True})


@app.route('/api/settings/dns/test', methods=['GET'])
def api_settings_dns_test():
    primary   = _cfg_get('dns_primary',   '').strip()
    secondary = _cfg_get('dns_secondary', '').strip()
    results   = {}
    test_ip   = '8.8.8.8'   # Google — ha quasi sempre un PTR
    try:
        import dns.resolver, dns.reversename, dns.exception
        rev = dns.reversename.from_address(test_ip)
        for label, srv in [('primary', primary), ('secondary', secondary)]:
            if not srv:
                results[label] = {'server': srv, 'ok': False, 'detail': 'non configurato'}
                continue
            try:
                r = dns.resolver.Resolver(configure=False)
                r.nameservers = [srv]
                r.timeout     = 3
                r.lifetime    = 3
                answers = r.resolve(rev, 'PTR')
                results[label] = {'server': srv, 'ok': True,
                                  'detail': str(answers[0]).rstrip('.')}
            except dns.resolver.NXDOMAIN:
                results[label] = {'server': srv, 'ok': True, 'detail': 'NXDOMAIN (raggiungibile)'}
            except Exception as e:
                results[label] = {'server': srv, 'ok': False, 'detail': str(e)}
    except ImportError:
        results = {'error': 'dnspython non installato'}
    return jsonify(results)


# ── Scan Groups CRUD ──────────────────────────────────────────────

def _group_to_dict(g):
    items = []
    for it in g.items:
        d = {'id': it.id, 'network_id': it.network_id, 'ip_range': it.ip_range}
        if it.network_id:
            net = Network.query.get(it.network_id)
            d['label'] = '{} ({}/{})'.format(net.name, net.address, net.cidr) if net else str(it.network_id)
        else:
            d['label'] = it.ip_range or ''
        items.append(d)
    return {
        'id':            g.id,
        'name':          g.name,
        'group_type':    g.group_type,
        'schedule_time': g.schedule_time or '',
        'enabled':       g.enabled,
        'items':         items,
    }


@app.route('/api/settings/scan-groups', methods=['GET'])
def api_scan_groups_list():
    groups = ScanGroup.query.order_by(ScanGroup.id).all()
    return jsonify([_group_to_dict(g) for g in groups])


@app.route('/api/settings/scan-groups', methods=['POST'])
def api_scan_groups_create():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Nome obbligatorio'}), 400
    g = ScanGroup(
        name          = name,
        group_type    = data.get('group_type', 'subnet'),
        schedule_time = (data.get('schedule_time') or '').strip() or None,
        enabled       = bool(data.get('enabled', True)),
    )
    db.session.add(g)
    db.session.commit()
    _reload_scheduler_jobs()
    return jsonify(_group_to_dict(g)), 201


@app.route('/api/settings/scan-groups/<int:gid>', methods=['PUT'])
def api_scan_groups_update(gid):
    g    = ScanGroup.query.get_or_404(gid)
    data = request.get_json() or {}
    if 'name' in data:
        g.name = data['name'].strip()
    if 'group_type' in data:
        g.group_type = data['group_type']
    if 'schedule_time' in data:
        g.schedule_time = (data['schedule_time'] or '').strip() or None
    if 'enabled' in data:
        g.enabled = bool(data['enabled'])
    db.session.commit()
    _reload_scheduler_jobs()
    return jsonify(_group_to_dict(g))


@app.route('/api/settings/scan-groups/<int:gid>', methods=['DELETE'])
def api_scan_groups_delete(gid):
    g = ScanGroup.query.get_or_404(gid)
    db.session.delete(g)
    db.session.commit()
    _reload_scheduler_jobs()
    return jsonify({'ok': True})


@app.route('/api/settings/scan-groups/<int:gid>/items', methods=['POST'])
def api_scan_group_items_add(gid):
    ScanGroup.query.get_or_404(gid)
    data       = request.get_json() or {}
    network_id = data.get('network_id')
    ip_range   = (data.get('ip_range') or '').strip()
    if not network_id and not ip_range:
        return jsonify({'error': 'Specificare network_id o ip_range'}), 400
    it = ScanGroupItem(group_id=gid,
                       network_id=int(network_id) if network_id else None,
                       ip_range=ip_range or None)
    db.session.add(it)
    db.session.commit()
    net  = Network.query.get(it.network_id) if it.network_id else None
    label = '{} ({}/{})'.format(net.name, net.address, net.cidr) if net else (it.ip_range or '')
    return jsonify({'id': it.id, 'network_id': it.network_id,
                    'ip_range': it.ip_range, 'label': label}), 201


@app.route('/api/settings/scan-groups/<int:gid>/items/<int:iid>', methods=['DELETE'])
def api_scan_group_items_delete(gid, iid):
    it = ScanGroupItem.query.filter_by(id=iid, group_id=gid).first_or_404()
    db.session.delete(it)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/venv-info')
def api_venv_info():
    import sys, subprocess, os, re
    python_version = sys.version

    try:
        result = subprocess.run(
            [sys.executable, '-m', 'pip', 'list', '--format=json'],
            capture_output=True, text=True, timeout=15
        )
        import json as _json
        installed_list = _json.loads(result.stdout) if result.returncode == 0 else []
        installed = {p['name'].lower(): p['version'] for p in installed_list}
    except Exception as e:
        installed = {}
        installed_list = []

    req_path = os.path.join(os.path.dirname(__file__), 'requirements.txt')
    required = []
    missing = []
    version_ok = []
    version_mismatch = []

    if os.path.exists(req_path):
        with open(req_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                m = re.match(r'^([A-Za-z0-9_\-\.]+)\s*([><=!].+)?$', line)
                if not m:
                    continue
                pkg_name = m.group(1)
                spec     = (m.group(2) or '').strip()
                req_entry = {'name': pkg_name, 'spec': spec or 'any', 'raw': line}
                key = pkg_name.lower()
                if key not in installed:
                    missing.append(req_entry)
                else:
                    req_entry['installed'] = installed[key]
                    version_ok.append(req_entry)
                required.append(req_entry)

    return jsonify({
        'python_version': python_version,
        'python_executable': sys.executable,
        'packages': installed_list,
        'requirements': required,
        'missing': missing,
        'ok': len(missing) == 0,
    })


@app.route('/api/settings/scan-groups/<int:gid>/run', methods=['POST'])
def api_scan_group_run(gid):
    g = ScanGroup.query.get_or_404(gid)
    try:
        _update_scanner_dns()
        _execute_scan_group(g, app)
        return jsonify({'ok': True, 'message': 'Scansione avviata'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Esecuzione gruppi ─────────────────────────────────────────────

def _execute_scan_group(group, flask_app):
    from scanner import start_scan_async
    if group.group_type == 'supernet':
        for item in group.items:
            if item.network_id:
                subnets = Network.query.filter_by(
                    parent_id=item.network_id,
                    network_type='subnet',
                    status='active',
                ).all()
                for subnet in subnets:
                    start_scan_async(subnet.id, flask_app)
    elif group.group_type == 'subnet':
        for item in group.items:
            if item.network_id:
                start_scan_async(item.network_id, flask_app)
    elif group.group_type == 'ip_range':
        for item in group.items:
            if item.ip_range:
                _scan_by_range(item.ip_range, flask_app)


def _scan_by_range(range_str, flask_app):
    """Trova la subnet nel DB che corrisponde al range e la scansiona."""
    import ipaddress as _ia
    from scanner import start_scan_async
    range_str = range_str.strip()
    try:
        net_obj = _ia.ip_network(range_str, strict=False)
        subnet  = Network.query.filter_by(
            address=str(net_obj.network_address),
            cidr=net_obj.prefixlen,
            network_type='subnet',
        ).first()
        if subnet:
            start_scan_async(subnet.id, flask_app)
    except ValueError:
        pass


# ── APScheduler (un solo worker Gunicorn tiene il lock) ───────────
_scheduler     = None
_sched_lock_fd = None


def _init_scheduler(flask_app):
    global _scheduler, _sched_lock_fd
    try:
        import fcntl
        from apscheduler.schedulers.background import BackgroundScheduler
        lock_path = os.path.join(BASE_DIR, 'instance', 'scheduler.lock')
        fd = open(lock_path, 'w')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _sched_lock_fd = fd
        _scheduler = BackgroundScheduler(timezone='Europe/Rome')
        _scheduler.start()
        import atexit
        atexit.register(_shutdown_scheduler)
        with flask_app.app_context():
            _reload_scheduler_jobs()
        import logging
        logging.getLogger('ipam').info('APScheduler avviato')
    except (IOError, OSError):
        pass   # altro worker ha il lock
    except Exception as e:
        import logging
        logging.getLogger('ipam').warning('APScheduler non avviato: %s', e)


def _shutdown_scheduler():
    global _scheduler, _sched_lock_fd
    if _scheduler and _scheduler.running:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
    if _sched_lock_fd:
        try:
            import fcntl
            fcntl.flock(_sched_lock_fd, fcntl.LOCK_UN)
            _sched_lock_fd.close()
        except Exception:
            pass


def _reload_scheduler_jobs():
    if not _scheduler:
        return
    try:
        from apscheduler.triggers.cron import CronTrigger
        _scheduler.remove_all_jobs()

        # Scansione globale
        if _cfg_get('global_scan_enabled', '0') == '1':
            t = _cfg_get('global_scan_time', '02:00')
            if t and ':' in t:
                h, m = t.split(':', 1)
                _scheduler.add_job(
                    _job_global_scan, CronTrigger(hour=h, minute=m),
                    id='global_scan', replace_existing=True, args=[app])

        # Gruppi
        groups = ScanGroup.query.filter_by(enabled=True).all()
        for g in groups:
            if g.schedule_time and ':' in g.schedule_time:
                h, m = g.schedule_time.split(':', 1)
                _scheduler.add_job(
                    _job_group_scan, CronTrigger(hour=h, minute=m),
                    id='group_{}'.format(g.id), replace_existing=True,
                    args=[g.id, app])
    except Exception as e:
        import logging
        logging.getLogger('ipam').warning('_reload_scheduler_jobs: %s', e)


def _job_global_scan(flask_app):
    try:
        from scanner import scan_all_subnets
        with flask_app.app_context():
            _update_scanner_dns()
            scan_all_subnets(flask_app=flask_app)
    except Exception as e:
        import logging
        logging.getLogger('ipam').error('job global scan: %s', e)


def _job_group_scan(group_id, flask_app):
    try:
        with flask_app.app_context():
            _update_scanner_dns()
            g = ScanGroup.query.get(group_id)
            if g and g.enabled:
                _execute_scan_group(g, flask_app)
    except Exception as e:
        import logging
        logging.getLogger('ipam').error('job group %s scan: %s', group_id, e)


#  SEED DATA 

def seed_data():
    if Network.query.count() > 0:
        return

    vlans_data = [
        (1,    'Management',    'Network management VLAN'),
        (2,    'Server Farm',   'Production servers'),
        (8,    'Storage',       'SAN/NAS traffic'),
        (14,   'Backup',        'Backup network'),
        (20,   'DMZ',           'Demilitarized zone'),
        (24,   'VoIP',          'Voice over IP'),
        (30,   'Wireless',      'WiFi clients'),
        (40,   'Guests',        'Guest network'),
        (41,   'IoT',           'IoT devices'),
        (51,   'Operatori TIF', 'TIF operators'),
        (52,   'DMZ TIF',       'TIF DMZ'),
        (260,  'VLAN 260',      'Legacy VLAN'),
        (280,  'VLAN 280',      'WAN interfaces'),
        (1270, 'AP MGMT',       'Access Point Management'),
    ]
    for vid, name, desc in vlans_data:
        db.session.add(VLan(vlan_id=vid, name=name, description=desc))

    supernet = Network(
        name='Supernet Cagliari', address='10.122.0.0', cidr=19,
        location='Cagliari - CA', description='Main Cagliari supernet',
        network_type='supernet', status='active',
    )
    db.session.add(supernet)
    db.session.flush()

    import random
    random.seed(42)

    cagliari_subnets = [
        ('10.122.1.0',    24, 'VLAN 1',     1,    'Cagliari', 'Management network'),
        ('10.122.2.0',    24, 'VLAN 2',     2,    'Cagliari', 'Server Farm'),
        ('10.122.3.0',    24, 'VLAN 3',     None, 'Cagliari', 'General use'),
        ('10.122.4.0',    24, 'VLAN 8',     8,    'Cagliari', 'Storage network'),
        ('10.122.5.0',    24, 'VLAN 14',    14,   'Cagliari', 'Backup network'),
        ('10.122.6.0',    24, 'VLAN 20',    20,   'Cagliari', 'DMZ'),
        ('10.122.7.0',    24, 'VLAN 30',    30,   'Cagliari', 'Wireless'),
        ('10.122.8.0',    24, 'VLAN 24',    24,   'Cagliari', 'VoIP'),
        ('10.122.9.0',    24, 'VLAN 40',    40,   'Cagliari', 'Guests'),
        ('10.122.10.0',   24, 'VLAN 41',    41,   'Cagliari', 'IoT devices'),
        ('10.122.11.0',   24, 'VLAN 280',   280,  'Cagliari', 'WAN interfaces'),
        ('10.122.12.192', 26, 'VLAN 260',   260,  'Cagliari', 'Legacy segment'),
        ('10.122.13.0',   24, 'Subnet 13',  None, 'Cagliari', ''),
        ('10.122.14.0',   24, 'Wharehouse clients', None, 'Cagliari', 'Wharehouse clients [15]'),
        ('10.122.15.0',   24, 'Ap_mgmt',   1270,  'Cagliari', 'AP Management [1270]'),
        ('10.122.24.0',   24, 'Operatori_TIF', 51,'Cagliari', 'TIF Operators [51]'),
        ('10.122.25.0',   24, 'DMZ_TIF',    52,   'Cagliari', 'DMZ TIF [52]'),
    ]
    for addr, cidr, name, vlan, loc, desc in cagliari_subnets:
        net = Network(name=name, address=addr, cidr=cidr, vlan_id=vlan,
                      location=loc, description=desc,
                      parent_id=supernet.id, network_type='subnet', status='active')
        db.session.add(net)
        db.session.flush()
        _add_random_ips(net, addr, cidr, random)

    remote_subnets = [
        ('192.168.10.0', 24, 'Sede Centrale',      None, 'Sede Centrale',      'HQ'),
        ('192.168.20.0', 24, 'Filiale Nord - SDWAN', None,'Filiale Nord - SDWAN','North branch SDWAN'),
        ('172.16.1.0',   24, 'Filiale A',          None, 'Filiale A - SDWAN',  'Branch A SDWAN'),
        ('172.16.2.0',   24, 'Filiale B',          None, 'Filiale B',          'Branch B'),
        ('172.16.3.0',   24, 'Filiale C',          None, 'Filiale C',          'Branch C office'),
        ('172.16.4.0',   24, 'Filiale D',          None, 'Filiale D',          'Branch D office'),
        ('172.16.5.0',   24, 'Filiale E - SDWAN',  None, 'Filiale E - SDWAN',  'Branch E'),
        ('172.16.6.0',   24, 'Filiale F',          None, 'Filiale F',          'Branch F'),
        ('172.16.7.0',   24, 'Filiale G',          None, 'Filiale G',          'Branch G'),
        ('172.16.8.0',   24, 'Filiale H - SDWAN',  None, 'Filiale H - SDWAN',  'Branch H SDWAN'),
        ('172.16.9.0',   24, 'Filiale I',          None, 'Filiale I',          'Branch I office'),
        ('172.16.10.0',  24, 'Filiale L',          None, 'Filiale L',          'Branch L office'),
        ('172.16.11.0',  24, 'Filiale M',          None, 'Filiale M',          'Branch M'),
    ]
    for addr, cidr, name, vlan, loc, desc in remote_subnets:
        net = Network(name=name, address=addr, cidr=cidr, vlan_id=vlan,
                      location=loc, description=desc, network_type='subnet', status='active')
        db.session.add(net)
        db.session.flush()
        _add_random_ips(net, addr, cidr, random)

    db.session.commit()
    import sys
    msg = "[OK] Seed data inserito."
    print(msg.encode("ascii", errors="replace").decode("ascii"))


def _add_random_ips(net, addr, cidr, random_inst):
    try:
        net_obj  = ipaddress.ip_network(f"{addr}/{cidr}", strict=False)
        hosts    = list(net_obj.hosts())
        n_used   = random_inst.randint(int(len(hosts) * 0.10), int(len(hosts) * 0.42))
        selected = random_inst.sample(hosts, min(n_used, len(hosts)))
        for ip in selected:
            db.session.add(IPRecord(
                ip_address = str(ip),
                hostname   = f"host-{str(ip).replace('.', '-')}.local",
                status     = 'used',
                network_id = net.id,
                last_seen  = datetime.utcnow(),
            ))
    except Exception:
        pass


#  INIT 

def _ensure_indexes():
    """Crea gli indici mancanti e migra ip_int (idempotente)."""
    import sqlite3 as _sq3, socket as _sock, struct as _st, logging as _lg
    _log = _lg.getLogger('ipam')
    try:
        _db_path = os.path.join(BASE_DIR, 'instance', 'ipam.db')
        _con = _sq3.connect(_db_path)

        # ── Migrazione colonna ip_int ──────────────────────────────────────────
        _existing = {r[1] for r in _con.execute('PRAGMA table_info(ip_records)')}
        if 'ip_int' not in _existing:
            _con.execute('ALTER TABLE ip_records ADD COLUMN ip_int INTEGER')
            _con.commit()
            _log.info('Colonna ip_int aggiunta a ip_records.')

        # ── Backfill ip_int per righe ancora NULL ──────────────────────────────
        _null = _con.execute('SELECT COUNT(*) FROM ip_records WHERE ip_int IS NULL').fetchone()[0]
        if _null > 0:
            _log.info('Backfill ip_int: %d righe...', _null)
            _rows = _con.execute('SELECT id, ip_address FROM ip_records WHERE ip_int IS NULL').fetchall()
            _batch = []
            for _rid, _ip in _rows:
                try:
                    _iv = _st.unpack('!I', _sock.inet_aton(_ip))[0]
                except Exception:
                    _iv = 0
                _batch.append((_iv, _rid))
                if len(_batch) >= 2000:
                    _con.executemany('UPDATE ip_records SET ip_int = ? WHERE id = ?', _batch)
                    _batch = []
            if _batch:
                _con.executemany('UPDATE ip_records SET ip_int = ? WHERE id = ?', _batch)
            _con.commit()
            _log.info('Backfill ip_int completato.')

        # ── Indici ────────────────────────────────────────────────────────────
        for _ddl in [
            'CREATE INDEX IF NOT EXISTS idx_ip_records_ip_int        ON ip_records(ip_int)',
            'CREATE INDEX IF NOT EXISTS idx_ip_records_network_id     ON ip_records(network_id)',
            'CREATE INDEX IF NOT EXISTS idx_ip_records_network_status ON ip_records(network_id, status)',
            'CREATE INDEX IF NOT EXISTS idx_ip_records_status         ON ip_records(status)',
            'CREATE INDEX IF NOT EXISTS idx_ip_records_device_type    ON ip_records(device_type)',
            'CREATE INDEX IF NOT EXISTS idx_ip_records_switch_name    ON ip_records(switch_name)',
            'CREATE INDEX IF NOT EXISTS idx_ip_records_os_type        ON ip_records(os_type)',
            'CREATE INDEX IF NOT EXISTS idx_ip_records_last_seen      ON ip_records(last_seen)',
            'CREATE INDEX IF NOT EXISTS idx_networks_address_cidr     ON networks(address, cidr)',
            'CREATE INDEX IF NOT EXISTS idx_networks_parent_id        ON networks(parent_id)',
            'CREATE INDEX IF NOT EXISTS idx_networks_network_type     ON networks(network_type)',
        ]:
            _con.execute(_ddl)
        _con.commit()
        _con.close()
        _log.info('Indici DB verificati/creati.')
    except Exception as _e:
        _lg.getLogger('ipam').warning('ensure_indexes: %s', _e)


with app.app_context():
    db.create_all()
    _ensure_indexes()

    # Migrazione colonne SNMP (aggiunge se non esistono)
    try:
        import sqlite3 as _sq3
        _db_path = os.path.join(BASE_DIR, 'instance', 'ipam.db')
        _con = _sq3.connect(_db_path)
        _cur = _con.cursor()
        _existing = {r[1] for r in _cur.execute('PRAGMA table_info(ip_records)')}
        for _col, _def in [('switch_name',     'VARCHAR(100)'),
                            ('switch_port',     'VARCHAR(50)'),
                            ('snmp_updated_at', 'DATETIME')]:
            if _col not in _existing:
                _cur.execute('ALTER TABLE ip_records ADD COLUMN {} {}'.format(_col, _def))
        _con.commit()
        _con.close()
    except Exception as _e:
        import logging as _lg
        _lg.getLogger('ipam').warning('Migrazione SNMP columns: %s', _e)

    if os.environ.get('IPAM_SEED', '1') == '1':
        seed_data()
    _update_scanner_dns()
    # Crea utente admin locale di default se non esiste
    if not LocalUser.query.filter_by(username='admin').first():
        _admin = LocalUser(username='admin', display_name='Amministratore',
                           is_admin=True, enabled=True)
        _admin.set_password('admin')
        db.session.add(_admin)
        db.session.commit()

_init_scheduler(app)


#  DEV SERVER (non usato con Apache) 
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
