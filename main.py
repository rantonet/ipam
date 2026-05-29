# Avvio sviluppo locale (porta 5000)
#
# Prima di eseguire, attivare il virtualenv:
#   source venv/bin/activate   (Linux / macOS)
#   venv\Scripts\activate      (Windows)
#
# Poi avviare con:
#   python3 main.py
#
# In alternativa, senza attivare il venv:
#   venv/bin/python3 main.py

from werkzeug.serving import run_simple
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.wrappers import Response
from app import app

def redirect_to_ipam(environ, start_response):
    response = Response(status=302, headers={'Location': '/ipam/'})
    return response(environ, start_response)

application = DispatcherMiddleware(redirect_to_ipam, {'/ipam': app})

if __name__ == '__main__':
    run_simple('0.0.0.0', 5000, application, use_reloader=True, use_debugger=False)
