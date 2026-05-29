# -*- coding: utf-8 -*-
# wsgi_gunicorn.py -- Entry point per Gunicorn
#
# Architettura:
#   Browser -> Apache :80 -> ProxyPass /ipam -> Gunicorn :8000/ipam -> Flask
#
# Richiede in Apache (VirtualHost o httpd.conf):
#   ProxyPreserveHost On
#   RequestHeader set X-Forwarded-Host %{HTTP_HOST}s
#   RequestHeader set X-Forwarded-Proto %{REQUEST_SCHEME}s
#
# Il middleware ReverseProxied:
#   - Ripristina HTTP_HOST dall'header X-Forwarded-Host (hostname reale del browser)
#   - Imposta SCRIPT_NAME=/ipam in modo che url_for() generi link corretti

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app


class ReverseProxied(object):
    def __init__(self, wsgi_app, script_name):
        self.app = wsgi_app
        self.script_name = script_name

    def __call__(self, environ, start_response):
        # Ripristina l'hostname reale (richiede RequestHeader in Apache)
        fwd_host = environ.get('HTTP_X_FORWARDED_HOST', '').split(',')[0].strip()
        if fwd_host:
            environ['HTTP_HOST'] = fwd_host

        # Ripristina lo schema reale (http/https)
        fwd_proto = environ.get('HTTP_X_FORWARDED_PROTO', '').split(',')[0].strip()
        if fwd_proto:
            environ['wsgi.url_scheme'] = fwd_proto

        # Imposta il prefisso /ipam
        environ['SCRIPT_NAME'] = self.script_name
        path = environ.get('PATH_INFO', '')
        if path.startswith(self.script_name):
            environ['PATH_INFO'] = path[len(self.script_name):] or '/'

        return self.app(environ, start_response)


application = ReverseProxied(app, '/ipam')
