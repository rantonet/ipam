#!/usr/bin/env bash
# ================================================================
#  install.sh  --  IPAM su Apache + Gunicorn
#
#  Distribuzioni supportate:
#    Debian / Ubuntu
#    RHEL / Rocky Linux / AlmaLinux / CentOS 7+ / Fedora
#
#  Utilizzo:
#    chmod +x install.sh
#    sudo ./install.sh [INSTALL_DIR]
#
#  Esempio con directory personalizzata:
#    sudo ./install.sh /var/www/html/ipam
# ================================================================
set -euo pipefail

#  Directory di installazione (default /var/www/html/ipam) 
INSTALL_DIR="${1:-/var/www/html/ipam}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

#  Controllo root 
if [[ $EUID -ne 0 ]]; then
    echo "[ERRORE] Esegui come root: sudo $0" >&2
    exit 1
fi

# ================================================================
#  RILEVAMENTO DISTRO
# ================================================================
detect_distro() {
    if [[ -f /etc/debian_version ]]; then
        FAMILY="debian"
        PKG_MGR="apt-get"
        APACHE_SVC="apache2"
        APACHE_USER="www-data"
        CONF_DEST="/etc/apache2/conf-available/ipam.conf"
    elif [[ -f /etc/redhat-release ]]; then
        FAMILY="redhat"
        APACHE_SVC="httpd"
        APACHE_USER="apache"
        CONF_DEST="/etc/httpd/conf.d/ipam.conf"
        if grep -qi "fedora" /etc/redhat-release; then
            # Fedora: %{rhel} non è definita, usa sempre dnf, niente EPEL
            IS_FEDORA=true
            PKG_MGR="dnf"
            RHEL_VER=99
        else
            IS_FEDORA=false
            RHEL_VER=$(rpm -E '%{rhel}' 2>/dev/null || echo "0")
            # Protezione: se la macro non è espansa (ritorna stringa) usa 0
            [[ "$RHEL_VER" =~ ^[0-9]+$ ]] || RHEL_VER=0
            if [[ "$RHEL_VER" -ge 8 ]]; then
                PKG_MGR="dnf"
            else
                PKG_MGR="yum"
            fi
        fi
    else
        echo "[ERRORE] Distribuzione non supportata." >&2
        exit 1
    fi
}

detect_distro

if [[ "$FAMILY" == "debian" ]]; then
    DISTRO_LABEL=$(. /etc/os-release && echo "$PRETTY_NAME")
else
    DISTRO_LABEL=$(cat /etc/redhat-release)
fi

echo "===================================================="
echo "  IPAM -- Installazione"
echo "  Distro  : ${DISTRO_LABEL}"
echo "  Dir     : ${INSTALL_DIR}"
echo "  Famiglia: ${FAMILY}"
echo "===================================================="

# ================================================================
#  STEP 1 -- Pacchetti di sistema
# ================================================================
echo ""
echo "[1/6] Installo Apache e dipendenze..."

if [[ "$FAMILY" == "debian" ]]; then
    apt-get update -qq
    apt-get install -y apache2 python3 python3-pip python3-venv

elif [[ "$FAMILY" == "redhat" ]]; then
    if [[ "${IS_FEDORA}" == "true" ]]; then
        # Fedora: repo standard, niente EPEL, python3-virtualenv si chiama python3-virtualenv
        $PKG_MGR install -y httpd python3 python3-pip python3-virtualenv 2>/dev/null \
            || $PKG_MGR install -y httpd python3 python3-pip python3-devel
    else
        # RHEL / CentOS / Rocky / AlmaLinux
        if ! rpm -q epel-release &>/dev/null; then
            $PKG_MGR install -y epel-release
        fi
        if [[ "$RHEL_VER" -ge 8 ]]; then
            dnf config-manager --set-enabled powertools 2>/dev/null \
                || dnf config-manager --set-enabled crb 2>/dev/null \
                || true
        fi
        $PKG_MGR install -y httpd python3 python3-pip python3-virtualenv
    fi
fi

# ================================================================
#  STEP 2 -- Virtualenv Python + dipendenze (Flask + Gunicorn)
#  Il venv viene creato in ${INSTALL_DIR}/venv e isola
#  completamente le dipendenze dal Python di sistema.
# ================================================================
echo ""
echo "[2/6] Creo virtualenv e installo dipendenze Python..."

VENV_DIR="${INSTALL_DIR}/venv"

# Crea il venv (idempotente: non sovrascrive se già esiste)
python3 -m venv "${VENV_DIR}"

# Installa le dipendenze nel venv
if [[ -f "${SCRIPT_DIR}/requirements.txt" ]]; then
    "${VENV_DIR}/bin/pip" install --quiet -r "${SCRIPT_DIR}/requirements.txt"
else
    "${VENV_DIR}/bin/pip" install --quiet \
        flask flask-sqlalchemy flask-login werkzeug gunicorn \
        dnspython apscheduler ldap3
fi

echo "  Virtualenv: ${VENV_DIR}"

# ================================================================
#  STEP 3 -- Copia file applicazione
#  Gestisce layout piatto (tutti i file in SCRIPT_DIR) e
#  layout strutturato (con sottodirectory templates/ e static/).
# ================================================================
echo ""
echo "[3/6] Copio i file in ${INSTALL_DIR}..."

mkdir -p "${INSTALL_DIR}/instance"
mkdir -p "${INSTALL_DIR}/static/css"
mkdir -p "${INSTALL_DIR}/templates"

cp "${SCRIPT_DIR}/app.py"           "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/wsgi_gunicorn.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/scanner.py"       "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/main.py"          "${INSTALL_DIR}/" 2>/dev/null || true

# Template HTML
if [[ -d "${SCRIPT_DIR}/templates" ]]; then
    cp -r "${SCRIPT_DIR}/templates/." "${INSTALL_DIR}/templates/"
else
    HTML_COUNT=$(find "${SCRIPT_DIR}" -maxdepth 1 -name "*.html" | wc -l)
    if [[ "$HTML_COUNT" -eq 0 ]]; then
        echo "[ERRORE] Nessun file .html trovato in ${SCRIPT_DIR}" >&2
        exit 1
    fi
    cp "${SCRIPT_DIR}"/*.html "${INSTALL_DIR}/templates/"
    echo "  Template: ${HTML_COUNT} file .html -> templates/ (layout piatto)"
fi

# CSS / file statici
if [[ -d "${SCRIPT_DIR}/static" ]]; then
    cp -r "${SCRIPT_DIR}/static/." "${INSTALL_DIR}/static/"
elif [[ -f "${SCRIPT_DIR}/main.css" ]]; then
    cp "${SCRIPT_DIR}/main.css" "${INSTALL_DIR}/static/css/main.css"
    echo "  CSS: main.css -> static/css/ (layout piatto)"
else
    echo "  [WARN] main.css non trovato."
fi

# Assicura coding declaration UTF-8 (necessaria su Python 2.x / CentOS 7)
for pyfile in "${INSTALL_DIR}/app.py" "${INSTALL_DIR}/wsgi_gunicorn.py"; do
    if ! head -1 "$pyfile" | grep -q "coding"; then
        sed -i '1s/^/# -*- coding: utf-8 -*-\n/' "$pyfile"
    fi
done

echo "  Struttura installata:"
find "${INSTALL_DIR}" \
    -not -path "*/instance/*" \
    -not -path "*/__pycache__/*" \
    -type f | sed "s|${INSTALL_DIR}/||" | sort | sed 's/^/    /'

# ================================================================
#  STEP 4 -- Database SQLite
#  Se instance/ipam.db e' presente nello zip, viene copiato con i
#  dati esistenti. Altrimenti viene creato un DB vuoto.
# ================================================================
echo ""
echo "[4/6] Inizializzo il database..."

if [[ -f "${INSTALL_DIR}/instance/ipam.db" ]]; then
    echo "  DB esistente rilevato in ${INSTALL_DIR}/instance/ipam.db -- non sovrascritto."
elif [[ -f "${SCRIPT_DIR}/instance/ipam.db" ]]; then
    cp "${SCRIPT_DIR}/instance/ipam.db" "${INSTALL_DIR}/instance/ipam.db"
    echo "  DB copiato dall'archivio."
else
    IPAM_SEED=0 "${INSTALL_DIR}/venv/bin/python3" - << PYEOF
import sys, os
sys.path.insert(0, '${INSTALL_DIR}')
os.environ['IPAM_SEED'] = '0'
from app import app, db
with app.app_context():
    db.create_all()
print("  DB creato (vuoto).")
PYEOF
fi

# ================================================================
#  STEP 5 -- Permessi + SELinux (solo Red Hat)
# ================================================================
echo ""
echo "[5/6] Imposto permessi..."

chown -R root:root "${INSTALL_DIR}"
chmod -R 755 "${INSTALL_DIR}"
chmod 750 "${INSTALL_DIR}/instance"
chmod 640 "${INSTALL_DIR}/instance/ipam.db" 2>/dev/null || true

if [[ "$FAMILY" == "redhat" ]]; then
    SELINUX_STATUS=$(getenforce 2>/dev/null || echo "Disabled")
    if [[ "$SELINUX_STATUS" != "Disabled" ]]; then
        echo "  SELinux attivo (${SELINUX_STATUS}) — configuro contesti e policy..."

        if command -v semanage &>/dev/null && command -v restorecon &>/dev/null; then
            # Contenuto app: lettura httpd
            semanage fcontext -a -t httpd_sys_content_t \
                "${INSTALL_DIR}(/.*)?" 2>/dev/null \
                || semanage fcontext -m -t httpd_sys_content_t \
                "${INSTALL_DIR}(/.*)?" 2>/dev/null || true
            # Directory instance: scrittura httpd (DB SQLite)
            semanage fcontext -a -t httpd_sys_rw_content_t \
                "${INSTALL_DIR}/instance(/.*)?" 2>/dev/null \
                || semanage fcontext -m -t httpd_sys_rw_content_t \
                "${INSTALL_DIR}/instance(/.*)?" 2>/dev/null || true
            # Binari venv (gunicorn, python3): devono essere eseguibili
            semanage fcontext -a -t bin_t \
                "${INSTALL_DIR}/venv/bin(/.*)?" 2>/dev/null \
                || semanage fcontext -m -t bin_t \
                "${INSTALL_DIR}/venv/bin(/.*)?" 2>/dev/null || true
            restorecon -Rv "${INSTALL_DIR}" >/dev/null 2>&1
            echo "  SELinux: contesti applicati (bin_t su venv/bin, rw su instance)."
        else
            # Fallback con chcon (non persistente ma funziona subito)
            chcon -R -t bin_t "${INSTALL_DIR}/venv/bin/" 2>/dev/null || true
            chcon -R -t httpd_sys_rw_content_t "${INSTALL_DIR}/instance/" 2>/dev/null || true
            echo "  [WARN] semanage non trovato — usato chcon (non persistente)."
            echo "         Installa policycoreutils-python-utils per la configurazione permanente."
        fi

        # Boolean: consente a httpd/gunicorn di connettersi alla rete e usare execmem
        setsebool -P httpd_can_network_connect 1 2>/dev/null || true
        setsebool -P httpd_execmem 1 2>/dev/null || true

        # Genera policy personalizzata da eventuali denial nel log audit
        if command -v ausearch &>/dev/null && command -v audit2allow &>/dev/null; then
            DENIALS=$(ausearch -c 'gunicorn' --raw 2>/dev/null | wc -l)
            if [[ "$DENIALS" -gt 0 ]]; then
                ausearch -c 'gunicorn' --raw 2>/dev/null \
                    | audit2allow -M my-gunicorn 2>/dev/null \
                    && semodule -X 300 -i my-gunicorn.pp 2>/dev/null \
                    && echo "  SELinux: policy my-gunicorn applicata da log audit." \
                    || true
            fi
        fi
    else
        echo "  SELinux disabilitato — nessuna configurazione necessaria."
    fi
fi

# ================================================================
#  STEP 6 -- Servizio Gunicorn (systemd) + Apache reverse proxy HTTPS
#
#  Architettura:
#    Browser -> Apache :443 (TLS) -> ProxyPass /ipam -> Gunicorn :8000 -> Flask
#    Apache serve /ipam/static direttamente dal filesystem.
#    Il VirtualHost :80 reindirizza tutto il traffico su HTTPS.
#
#  Gunicorn usa Python 3 nativo, evitando il problema mod_wsgi/Python 2.7.
#  Il middleware ReverseProxied in wsgi_gunicorn.py imposta SCRIPT_NAME=/ipam
#  in modo che Flask generi tutti gli URL con il prefisso corretto.
# ================================================================
echo ""
echo "[6/6] Configuro Gunicorn (systemd) e Apache (HTTPS)..."

VENV_DIR="${INSTALL_DIR}/venv"
GUNICORN_BIN="${VENV_DIR}/bin/gunicorn"
PYTHON_BIN="${VENV_DIR}/bin/python3"
SERVER_IP=$(hostname -I | awk '{print $1}')
SERVER_FQDN=$(hostname -f 2>/dev/null || echo "${SERVER_IP}")

# -- Certificato TLS: genera autofirmato se non esiste ----------
TLS_KEY="/etc/ssl/private/ipam.key"
TLS_CERT="/etc/ssl/certs/ipam.crt"
if [[ ! -f "$TLS_KEY" ]] || [[ ! -f "$TLS_CERT" ]]; then
    echo "  Generazione certificato TLS autofirmato (3650 giorni)..."
    mkdir -p /etc/ssl/private
    openssl req -x509 -newkey rsa:2048 \
        -keyout "$TLS_KEY" -out "$TLS_CERT" \
        -days 3650 -nodes \
        -subj "/CN=${SERVER_FQDN}/O=IPAM/C=IT" 2>/dev/null
    chmod 600 "$TLS_KEY"
    echo "  Certificato autofirmato: ${TLS_CERT}"
    echo "  [WARN] Sostituire con certificato firmato (Let's Encrypt: certbot --apache)"
else
    echo "  Certificato TLS esistente riutilizzato: ${TLS_CERT}"
fi

# -- Servizio systemd -------------------------------------------
cat > /etc/systemd/system/ipam.service << SVCEOF
[Unit]
Description=IPAM Gunicorn daemon
After=network.target

[Service]
User=root
Group=root
WorkingDirectory=${INSTALL_DIR}
Environment="PATH=${INSTALL_DIR}/venv/bin:/usr/local/bin:/usr/bin"
Environment="IPAM_SEED=0"
Environment="IPAM_COOKIE_SECURE=1"
ExecStart=${GUNICORN_BIN} --bind 127.0.0.1:8000 --workers 1 --timeout 120 wsgi_gunicorn:application
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable ipam
systemctl restart ipam
sleep 2
if systemctl is-active --quiet ipam; then
    echo "  Gunicorn: riavviato su 127.0.0.1:8000"
else
    echo "  [WARN] Gunicorn non avviato. Controlla: journalctl -u ipam -n 30"
fi

# -- Configurazione Apache: HTTP redirect + HTTPS proxy ---------
cat > "${CONF_DEST}" << APACHECONF
# ipam.conf -- generato da install.sh
# Distro: ${FAMILY} | Dir: ${INSTALL_DIR}
# SICUREZZA: HTTP redirige su HTTPS; tutto il traffico e' cifrato.

# Redirect HTTP -> HTTPS
<VirtualHost *:80>
    ServerName ${SERVER_FQDN}
    Redirect permanent / https://${SERVER_FQDN}/
</VirtualHost>

# Reverse proxy HTTPS verso Gunicorn
<VirtualHost *:443>
    ServerName ${SERVER_FQDN}

    SSLEngine on
    SSLCertificateFile    ${TLS_CERT}
    SSLCertificateKeyFile ${TLS_KEY}

    # Informa Flask dello schema reale (richiesto per SESSION_COOKIE_SECURE)
    RequestHeader set X-Forwarded-Proto "https"

    # File statici serviti direttamente da Apache
    Alias /ipam/static ${INSTALL_DIR}/static
    <Directory ${INSTALL_DIR}/static>
        Options -Indexes
        AllowOverride None
        Require all granted
    </Directory>

    # Proteggi il database SQLite
    <Directory ${INSTALL_DIR}/instance>
        Require all denied
    </Directory>

    # Reverse proxy verso Gunicorn (esclude /static)
    ProxyPass        /ipam/static !
    ProxyPass        /ipam  http://127.0.0.1:8000/ipam
    ProxyPassReverse /ipam  http://127.0.0.1:8000/ipam
</VirtualHost>
APACHECONF

echo "  ipam.conf generato in ${CONF_DEST}"

# -- Attiva moduli e servizio Apache ----------------------------
if [[ "$FAMILY" == "debian" ]]; then
    a2enmod proxy proxy_http ssl headers 2>/dev/null || true
    a2enconf ipam 2>/dev/null || true
    systemctl enable --now apache2
    systemctl reload apache2
    if command -v ufw &>/dev/null && ufw status | grep -q "^Status: active"; then
        ufw allow 80/tcp
        ufw allow 443/tcp
        echo "  Firewall (ufw): porte 80/tcp e 443/tcp aperte."
    fi

elif [[ "$FAMILY" == "redhat" ]]; then
    # Installa mod_ssl se assente
    if ! rpm -q mod_ssl &>/dev/null; then
        yum install -y mod_ssl 2>/dev/null || dnf install -y mod_ssl 2>/dev/null || true
    fi
    systemctl enable --now httpd
    systemctl reload httpd
    if systemctl is-active --quiet firewalld; then
        firewall-cmd --permanent --add-service=http
        firewall-cmd --permanent --add-service=https
        firewall-cmd --reload
        echo "  Firewall (firewalld): porte 80/tcp e 443/tcp aperte."
    else
        echo "  [WARN] firewalld non attivo."
    fi
fi

# ================================================================
#  FINE
# ================================================================
echo ""
echo "===================================================="
echo "  [OK] Installazione completata con HTTPS abilitato!"
echo ""
echo "  URL: https://${SERVER_FQDN}/ipam/"
echo "  (HTTP porta 80 reindirizza automaticamente su HTTPS)"
echo ""
echo "  Certificato TLS: ${TLS_CERT}"
echo "  [WARN] Se autofirmato, il browser mostrera' un avviso."
echo "  Sostituire con certificato firmato (es. Let's Encrypt):"
echo "    certbot --apache -d ${SERVER_FQDN}"
echo ""
echo "  Comandi utili:"
if [[ "$FAMILY" == "debian" ]]; then
echo "  Gunicorn : systemctl status ipam"
echo "  Log app  : journalctl -u ipam -f"
echo "  Apache   : systemctl status apache2"
echo "  Log http : tail -f /var/log/apache2/error.log"
else
echo "  Gunicorn : systemctl status ipam"
echo "  Log app  : journalctl -u ipam -f"
echo "  Apache   : systemctl status httpd"
echo "  Log http : tail -f /etc/httpd/logs/error_log"
fi
echo ""
echo "  Per aggiungere le tue reti:"
echo "  -> https://${SERVER_FQDN}/ipam/networks"
echo ""
echo "  Virtualenv Python: ${INSTALL_DIR}/venv"
echo "  Gunicorn:          ${INSTALL_DIR}/venv/bin/gunicorn"
echo "===================================================="
