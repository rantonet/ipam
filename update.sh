#!/usr/bin/env bash
# ================================================================
#  update.sh  --  Aggiornamento IPAM (solo file applicazione)
#
#  NON tocca:
#    - instance/ipam.db  (database)
#    - configurazione Apache
#
#  Utilizzo:
#    chmod +x update.sh
#    sudo ./update.sh [INSTALL_DIR]
#
#  Esempio con directory personalizzata:
#    sudo ./update.sh /var/www/html/ipam
# ================================================================
set -euo pipefail

INSTALL_DIR="${1:-/var/www/html/ipam}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${INSTALL_DIR}/venv"
PYTHON_BIN="${VENV_DIR}/bin/python3"
PIP_BIN="${VENV_DIR}/bin/pip"
GUNICORN_BIN="${VENV_DIR}/bin/gunicorn"

if [[ $EUID -ne 0 ]]; then
    echo "[ERRORE] Esegui come root: sudo $0" >&2
    exit 1
fi

if [[ ! -d "${INSTALL_DIR}" ]]; then
    echo "[ERRORE] Directory non trovata: ${INSTALL_DIR}" >&2
    echo "  Per la prima installazione usa install.sh" >&2
    exit 1
fi

if [[ ! -f "${INSTALL_DIR}/instance/ipam.db" ]]; then
    echo "[ERRORE] DB non trovato in ${INSTALL_DIR}/instance/ipam.db" >&2
    echo "  Sicuro di voler aggiornare questa directory?" >&2
    exit 1
fi

if [[ ! -d "${VENV_DIR}" ]]; then
    echo "[ERRORE] Virtualenv non trovato in ${VENV_DIR}" >&2
    echo "  Esegui prima install.sh per creare l'ambiente Python." >&2
    exit 1
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "[ERRORE] Python del venv non trovato o non eseguibile: ${PYTHON_BIN}" >&2
    echo "  Esegui prima install.sh per creare l'ambiente Python." >&2
    exit 1
fi

if [[ ! -x "${GUNICORN_BIN}" ]]; then
    echo "[ERRORE] Gunicorn del venv non trovato o non eseguibile: ${GUNICORN_BIN}" >&2
    echo "  Esegui prima install.sh per installare le dipendenze nel venv." >&2
    exit 1
fi

# ── Controllo versione Python del venv ───────────────────────────
PYTHON_MIN_MAJOR=3
PYTHON_MIN_MINOR=8

VENV_PY_VERSION=$("${PYTHON_BIN}" -c "import sys; print('{}.{}'.format(sys.version_info.major, sys.version_info.minor))" 2>/dev/null || echo "0.0")
VENV_PY_MAJOR=$(echo "${VENV_PY_VERSION}" | cut -d. -f1)
VENV_PY_MINOR=$(echo "${VENV_PY_VERSION}" | cut -d. -f2)

if [[ "${VENV_PY_MAJOR}" -lt "${PYTHON_MIN_MAJOR}" ]] || \
   { [[ "${VENV_PY_MAJOR}" -eq "${PYTHON_MIN_MAJOR}" ]] && [[ "${VENV_PY_MINOR}" -lt "${PYTHON_MIN_MINOR}" ]]; }; then
    echo ""
    echo "  [AVVISO] Versione Python del venv non compatibile!" >&2
    echo "  Rilevata : Python ${VENV_PY_VERSION}" >&2
    echo "  Minima   : Python ${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR}" >&2
    echo "  Il venv potrebbe essere obsoleto. Si consiglia di rieseguire install.sh" >&2
    echo "  per ricreare l'ambiente con una versione Python aggiornata." >&2
    echo "  L'aggiornamento prosegue, ma potrebbero verificarsi errori." >&2
    echo ""
fi

echo "===================================================="
echo "  IPAM -- Aggiornamento file applicazione"
echo "  Dir      : ${INSTALL_DIR}"
echo "  Venv     : ${VENV_DIR}"
echo "  Python   : ${PYTHON_BIN}  (v${VENV_PY_VERSION})"
echo "  Gunicorn : ${GUNICORN_BIN}"
echo "  DB       : PRESERVATO (non verrà toccato)"
echo "===================================================="

# ── Step 1: file Python principali ───────────────────────────────
echo ""
echo "[1/5] Copio i file Python..."

cp "${SCRIPT_DIR}/app.py"           "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/scanner.py"       "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/wsgi_gunicorn.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/main.py"          "${INSTALL_DIR}/" 2>/dev/null || true

echo "  app.py, scanner.py, wsgi_gunicorn.py aggiornati."

# ── Step 2: template HTML e file statici ─────────────────────────
echo ""
echo "[2/5] Copio templates e static..."

cp -r "${SCRIPT_DIR}/templates/." "${INSTALL_DIR}/templates/"
cp -r "${SCRIPT_DIR}/static/."    "${INSTALL_DIR}/static/"

TMPL_COUNT=$(find "${INSTALL_DIR}/templates" -name "*.html" | wc -l)
echo "  ${TMPL_COUNT} template HTML aggiornati."

# ── Step 3: aggiornamento dipendenze nel venv ─────────────────────
echo ""
echo "[3/5] Aggiorno le dipendenze Python nel virtualenv..."

if [[ -f "${SCRIPT_DIR}/requirements.txt" ]]; then
    "${PIP_BIN}" install --quiet -r "${SCRIPT_DIR}/requirements.txt"
    echo "  Dipendenze aggiornate da requirements.txt nel venv."
else
    echo "  [WARN] requirements.txt non trovato -- dipendenze non aggiornate."
fi

# ── Step 4: verifica e aggiornamento ExecStart del servizio systemd ──
echo ""
echo "[4/5] Verifico che il servizio systemd usi il Gunicorn del venv..."

SERVICE_FILE="/etc/systemd/system/ipam.service"
NEEDS_RELOAD=0

if [[ -f "${SERVICE_FILE}" ]]; then
    if grep -q "ExecStart=${GUNICORN_BIN}" "${SERVICE_FILE}"; then
        echo "  ExecStart già punta al venv Gunicorn. Nessuna modifica necessaria."
    else
        echo "  ExecStart non usa il venv Gunicorn. Aggiorno il servizio..."
        sed -i "s|^ExecStart=.*|ExecStart=${GUNICORN_BIN} --bind 127.0.0.1:8000 --workers 1 --timeout 120 wsgi_gunicorn:application|" \
            "${SERVICE_FILE}"
        sed -i "s|^Environment=\"PATH=.*|Environment=\"PATH=${VENV_DIR}/bin:/usr/local/bin:/usr/bin\"|" \
            "${SERVICE_FILE}"
        systemctl daemon-reload
        NEEDS_RELOAD=1
        echo "  Servizio aggiornato per usare: ${GUNICORN_BIN}"
    fi
else
    echo "  [WARN] File servizio non trovato: ${SERVICE_FILE}" >&2
    echo "  Assicurati che il servizio sia stato installato con install.sh" >&2
fi

# ── Step 5: riavvio servizio ──────────────────────────────────────
echo ""
echo "[5/5] Riavvio il servizio IPAM..."

systemctl restart ipam
sleep 2

if systemctl is-active --quiet ipam; then
    echo "  Gunicorn: attivo (${GUNICORN_BIN})."
else
    echo "  [ERRORE] Il servizio non si è avviato. Controlla: journalctl -u ipam -n 50" >&2
    exit 1
fi

# ── Verifica versione ─────────────────────────────────────────────
echo ""
VERSION=$(curl -s --max-time 5 http://127.0.0.1:8000/ipam/api/version 2>/dev/null \
    | "${PYTHON_BIN}" -c "import sys,json; d=json.load(sys.stdin); print(d.get('version','?') + '  (' + d.get('build','?') + ')')" \
    2>/dev/null || echo "non disponibile")

echo "===================================================="
echo "  [OK] Aggiornamento completato!"
echo ""
echo "  Versione attiva : ${VERSION}"
echo "  DB              : ${INSTALL_DIR}/instance/ipam.db  (intatto)"
echo "  Virtualenv      : ${VENV_DIR}"
echo "  Gunicorn        : ${GUNICORN_BIN}"
echo ""
echo "  Verifica online : http://$(hostname -I | awk '{print $1}')/ipam/api/version"
echo "===================================================="
