# -*- coding: utf-8 -*-
"""
Test automatici per IPAM.
Usano un database SQLite in memoria — non toccano mai instance/ipam.db.

Variabili d'ambiente impostate PRIMA dell'import di app.py:
  IPAM_DATABASE_URI=sqlite:///:memory:  → tutte le connessioni (ORM + raw sqlite3)
                                           usano il DB in memoria
  IPAM_SEED=0                           → disabilita il seed iniziale

Esecuzione:
    pytest test_app.py -v
"""

import os
import json
import pytest

# ── Configurazione pre-import ──────────────────────────────────────────────────
# DEVE stare prima di qualsiasi import di moduli del progetto.
os.environ.setdefault('IPAM_DATABASE_URI', 'sqlite:///:memory:')
os.environ['IPAM_SEED'] = '0'

# ── Import dell'applicazione ───────────────────────────────────────────────────
from app import app as flask_app, db, LocalUser, Network, IPRecord, VLan, APP_VERSION


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope='module')
def app():
    """
    Flask app già inizializzata con DB in memoria.
    Grazie a IPAM_DATABASE_URI impostato prima dell'import, l'app usa
    sqlite:///:memory: (con StaticPool automatico di Flask-SQLAlchemy 3.x)
    fin dal modulo-level 'with app.app_context(): db.create_all()'.
    Non serve creare l'utente admin: è già stato creato dalla logica di startup.
    """
    flask_app.config.update({
        'TESTING': True,
        'WTF_CSRF_ENABLED': False,
    })
    yield flask_app


@pytest.fixture(scope='module')
def client(app):
    """Client di test con sessione autenticata (admin)."""
    with app.test_client() as c:
        resp = c.post('/login', data={'username': 'admin', 'password': 'admin'},
                      follow_redirects=True)
        assert resp.status_code == 200, "Login iniziale fallito nel fixture"
        yield c


@pytest.fixture(scope='module')
def rete_test(app):
    """Crea una rete di test e la rimuove al termine."""
    with app.app_context():
        net = Network(
            name='Test-Network',
            address='192.168.100.0',
            cidr=24,
            mask='255.255.255.0',
            network_type='subnet',
            status='active',
        )
        db.session.add(net)
        db.session.commit()
        net_id = net.id
    yield net_id
    with app.app_context():
        Network.query.filter_by(id=net_id).delete()
        db.session.commit()


# ── Test: autenticazione ───────────────────────────────────────────────────────

class TestAuth:
    def test_login_page_raggiungibile(self, app):
        """La pagina di login risponde 200 senza autenticazione."""
        with app.test_client() as c:
            resp = c.get('/login')
        assert resp.status_code == 200

    def test_api_senza_auth_ritorna_401(self, app):
        """Le API richiedono autenticazione: senza sessione ritornano 401."""
        with app.test_client() as c:
            resp = c.get('/api/version')
        assert resp.status_code == 401
        data = resp.get_json()
        assert data is not None
        assert 'login' in data

    def test_login_credenziali_errate(self, app):
        """Login con password sbagliata mostra errore."""
        with app.test_client() as c:
            resp = c.post('/login', data={'username': 'admin', 'password': 'sbagliata'},
                          follow_redirects=True)
        assert resp.status_code == 200
        assert 'Credenziali non valide' in resp.get_data(as_text=True)

    def test_login_credenziali_corrette(self, app):
        """Login con credenziali valide va a buon fine."""
        with app.test_client() as c:
            resp = c.post('/login', data={'username': 'admin', 'password': 'admin'},
                          follow_redirects=True)
        assert resp.status_code == 200


# ── Test: API version ─────────────────────────────────────────────────────────

class TestApiVersion:
    def test_version_ritorna_200(self, client):
        resp = client.get('/api/version')
        assert resp.status_code == 200

    def test_version_contiene_campi_obbligatori(self, client):
        data = client.get('/api/version').get_json()
        assert 'version' in data
        assert 'build' in data
        assert 'changelog' in data

    def test_version_corrisponde_a_app(self, client):
        data = client.get('/api/version').get_json()
        assert data['version'] == APP_VERSION

    def test_version_changelog_e_lista(self, client):
        data = client.get('/api/version').get_json()
        assert isinstance(data['changelog'], list)


# ── Test: API reti ────────────────────────────────────────────────────────────

class TestApiNetworks:
    def test_lista_reti_ritorna_200(self, client):
        resp = client.get('/api/networks')
        assert resp.status_code == 200

    def test_lista_reti_e_json_array(self, client):
        data = client.get('/api/networks').get_json()
        assert isinstance(data, list)

    def test_crea_rete_valida(self, client, app):
        payload = {
            'name': 'Rete-Test-Create',
            'address': '10.99.1.0',
            'cidr': 24,
            'network_type': 'subnet',
            'status': 'active',
        }
        resp = client.post('/api/networks',
                           data=json.dumps(payload),
                           content_type='application/json')
        assert resp.status_code == 201
        data = resp.get_json()
        assert 'id' in data
        with app.app_context():
            Network.query.filter_by(id=data['id']).delete()
            db.session.commit()

    def test_crea_rete_duplicata_ritorna_409(self, client, rete_test):
        payload = {
            'name': 'Duplicate',
            'address': '192.168.100.0',
            'cidr': 24,
            'network_type': 'subnet',
        }
        resp = client.post('/api/networks',
                           data=json.dumps(payload),
                           content_type='application/json')
        assert resp.status_code == 409

    def test_crea_rete_indirizzo_non_valido(self, client):
        payload = {'name': 'Bad', 'address': 'non-valido', 'cidr': 24}
        resp = client.post('/api/networks',
                           data=json.dumps(payload),
                           content_type='application/json')
        assert resp.status_code == 400

    def test_crea_rete_senza_payload_ritorna_400(self, client):
        # Un body JSON vuoto ({}) è falsy in Python → il handler restituisce 400
        resp = client.post('/api/networks',
                           data='{}',
                           content_type='application/json')
        assert resp.status_code == 400

    def test_modifica_rete_esistente(self, client, rete_test):
        payload = {'name': 'Test-Modificata', 'location': 'Milano'}
        resp = client.put(f'/api/networks/{rete_test}',
                          data=json.dumps(payload),
                          content_type='application/json')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['name'] == 'Test-Modificata'

    def test_modifica_rete_inesistente_ritorna_404(self, client):
        resp = client.put('/api/networks/999999',
                          data=json.dumps({'name': 'X'}),
                          content_type='application/json')
        assert resp.status_code == 404

    def test_elimina_rete_esistente(self, client, app):
        with app.app_context():
            tmp = Network(name='Da-Eliminare', address='10.88.88.0',
                          cidr=28, mask='255.255.255.240',
                          network_type='subnet', status='active')
            db.session.add(tmp)
            db.session.commit()
            tmp_id = tmp.id
        resp = client.delete(f'/api/networks/{tmp_id}')
        assert resp.status_code == 200

    def test_elimina_rete_inesistente_ritorna_404(self, client):
        resp = client.delete('/api/networks/999999')
        assert resp.status_code == 404

    def test_filtro_per_tipo_subnet(self, client, rete_test):
        resp = client.get('/api/networks?type=subnet')
        assert resp.status_code == 200
        data = resp.get_json()
        assert all(n['network_type'] == 'subnet' for n in data)

    def test_filtro_ricerca_testuale(self, client, rete_test):
        resp = client.get('/api/networks?q=Test-Modificata')
        assert resp.status_code == 200
        data = resp.get_json()
        nomi = [n['name'] for n in data]
        assert any('Test-Modificata' in nome for nome in nomi)


# ── Test: API tree reti ───────────────────────────────────────────────────────

class TestApiNetworkTree:
    def test_tree_ritorna_200(self, client):
        resp = client.get('/api/networks/tree')
        assert resp.status_code == 200

    def test_tree_ritorna_json_array(self, client):
        data = json.loads(client.get('/api/networks/tree').data)
        assert isinstance(data, list)

    def test_tree_invalidate(self, client):
        resp = client.post('/api/networks/tree/invalidate')
        assert resp.status_code == 200


# ── Test: API IP Records ──────────────────────────────────────────────────────

class TestApiIpRecords:
    def test_lista_ip_ritorna_200(self, client):
        resp = client.get('/api/ip-records')
        assert resp.status_code == 200

    def test_lista_ip_e_json(self, client):
        data = client.get('/api/ip-records').get_json()
        assert isinstance(data, (list, dict))

    def test_crea_ip_record(self, client, rete_test, app):
        payload = {
            'ip_address': '192.168.100.10',
            'hostname': 'test-host',
            'status': 'used',
            'network_id': rete_test,
        }
        resp = client.post('/api/ip-records',
                           data=json.dumps(payload),
                           content_type='application/json')
        assert resp.status_code == 201
        data = resp.get_json()
        assert 'id' in data
        with app.app_context():
            IPRecord.query.filter_by(id=data['id']).delete()
            db.session.commit()

    def test_crea_ip_duplicato_ritorna_409(self, client, rete_test, app):
        with app.app_context():
            rec = IPRecord(ip_address='192.168.100.50', status='used',
                           network_id=rete_test)
            db.session.add(rec)
            db.session.commit()
            rec_id = rec.id
        payload = {'ip_address': '192.168.100.50', 'status': 'used',
                   'network_id': rete_test}
        resp = client.post('/api/ip-records',
                           data=json.dumps(payload),
                           content_type='application/json')
        assert resp.status_code == 409
        with app.app_context():
            IPRecord.query.filter_by(id=rec_id).delete()
            db.session.commit()

    def test_ip_record_inesistente_ritorna_404(self, client):
        resp = client.put('/api/ip-records/999999',
                          data=json.dumps({'hostname': 'x'}),
                          content_type='application/json')
        assert resp.status_code == 404


# ── Test: API VLAN ────────────────────────────────────────────────────────────

class TestApiVlans:
    def test_lista_vlans_ritorna_200(self, client):
        resp = client.get('/api/vlans')
        assert resp.status_code == 200

    def test_lista_vlans_e_json_array(self, client):
        data = client.get('/api/vlans').get_json()
        assert isinstance(data, list)

    def test_crea_vlan(self, client, app):
        payload = {'vlan_id': 999, 'name': 'VLAN-Test', 'status': 'active'}
        resp = client.post('/api/vlans',
                           data=json.dumps(payload),
                           content_type='application/json')
        assert resp.status_code == 201
        data = resp.get_json()
        assert 'id' in data
        with app.app_context():
            VLan.query.filter_by(id=data['id']).delete()
            db.session.commit()

    def test_crea_vlan_duplicata_ritorna_409(self, client, app):
        with app.app_context():
            v = VLan(vlan_id=998, name='VLAN-Dup')
            db.session.add(v)
            db.session.commit()
            v_id = v.id
        resp = client.post('/api/vlans',
                           data=json.dumps({'vlan_id': 998, 'name': 'VLAN-Dup2'}),
                           content_type='application/json')
        assert resp.status_code == 409
        with app.app_context():
            VLan.query.filter_by(id=v_id).delete()
            db.session.commit()


# ── Test: API statistiche ─────────────────────────────────────────────────────

class TestApiStats:
    def test_stats_ritorna_200(self, client):
        resp = client.get('/api/stats')
        assert resp.status_code == 200

    def test_stats_contiene_campi_obbligatori(self, client):
        data = client.get('/api/stats').get_json()
        campi = [
            'total_networks', 'total_subnets', 'total_vlans',
            'total_ips', 'used_ips', 'free_ips', 'global_usage',
        ]
        for campo in campi:
            assert campo in data, f"Campo mancante nelle stats: {campo}"

    def test_stats_valori_numerici(self, client):
        data = client.get('/api/stats').get_json()
        assert isinstance(data['total_networks'], int)
        assert isinstance(data['total_vlans'], int)
        assert isinstance(data['global_usage'], (int, float))

    def test_stats_global_usage_nel_range(self, client):
        data = client.get('/api/stats').get_json()
        assert 0.0 <= data['global_usage'] <= 100.0


# ── Test: pagine HTML ─────────────────────────────────────────────────────────

class TestPagine:
    def test_home_ritorna_200(self, client):
        resp = client.get('/')
        assert resp.status_code == 200

    def test_pagina_networks_ritorna_200(self, client):
        resp = client.get('/networks')
        assert resp.status_code == 200

    def test_pagina_vlans_ritorna_200(self, client):
        resp = client.get('/vlans')
        assert resp.status_code == 200

    def test_pagina_ip_ritorna_200(self, client):
        resp = client.get('/ip-addresses')
        assert resp.status_code == 200

    def test_pagina_docs_ritorna_200(self, client):
        resp = client.get('/docs')
        assert resp.status_code == 200

    def test_pagina_inesistente_ritorna_404(self, client):
        resp = client.get('/questa-pagina-non-esiste')
        assert resp.status_code == 404


# ── Test: modello Network ─────────────────────────────────────────────────────

class TestModelloNetwork:
    def test_network_to_dict(self, app):
        with app.app_context():
            net = Network(name='Model-Test', address='10.0.0.0', cidr=8,
                          network_type='supernet', status='active')
            db.session.add(net)
            db.session.commit()
            d = net.to_dict()
            assert d['name'] == 'Model-Test'
            assert d['address'] == '10.0.0.0'
            assert d['cidr'] == 8
            assert 'total_hosts' in d
            assert 'usage_percent' in d
            db.session.delete(net)
            db.session.commit()

    def test_network_total_hosts(self, app):
        with app.app_context():
            net = Network(name='Host-Count', address='192.168.1.0', cidr=24,
                          network_type='subnet', status='active')
            db.session.add(net)
            db.session.commit()
            assert net.total_hosts == 254
            db.session.delete(net)
            db.session.commit()

    def test_network_broadcast(self, app):
        with app.app_context():
            net = Network(name='Broadcast-Test', address='10.10.10.0', cidr=24,
                          network_type='subnet', status='active')
            db.session.add(net)
            db.session.commit()
            assert net.broadcast == '10.10.10.255'
            db.session.delete(net)
            db.session.commit()


# ── Test: modello IPRecord ────────────────────────────────────────────────────

class TestModelloIPRecord:
    def test_ip_record_to_dict(self, app):
        with app.app_context():
            rec = IPRecord(ip_address='172.16.0.1', status='used')
            db.session.add(rec)
            db.session.commit()
            d = rec.to_dict()
            assert d['ip_address'] == '172.16.0.1'
            assert d['status'] == 'used'
            db.session.delete(rec)
            db.session.commit()
