#!/bin/bash
set -e

python3 -m pip install --quiet flask flask-sqlalchemy flask-login werkzeug gunicorn dnspython apscheduler ldap3
