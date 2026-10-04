import base64
import importlib
import os
import re
import json
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setenv('APP_PASSWORD','long-password-for-testing')
    monkeypatch.setenv('APP_USERNAME','reader')
    monkeypatch.setenv('ENCRYPTION_KEY',Fernet.generate_key().decode())
    monkeypatch.setenv('DATA_DIR',str(tmp_path))
    monkeypatch.setenv('APP_URL','http://localhost:8765')
    import app.main
    main = importlib.reload(app.main)
    with TestClient(main.app,base_url='http://localhost:8765') as c:
        yield c


def authorize(c):
    c.headers['Authorization'] = 'Basic '+base64.b64encode(b'reader:long-password-for-testing').decode()


def csrf(c):
    page = c.get('/').text
    return re.search('name="csrf" value="([^"]+)"',page).group(1)


def test_all_routes_and_exports_protected(client):
    for url in ['/','/setup','/history','/runs/1/markdown','/health','/static/app.js']:
        assert client.get(url).status_code == 401


def test_setup_and_home_render_without_secrets(client):
    authorize(client)
    client.app.state.store.save_settings({'google_client_secret':'secret-google','refresh_token':'secret-refresh'})
    for url in ['/','/setup','/history']:
        response = client.get(url)
        assert response.status_code == 200
        assert 'secret-google' not in response.text and 'secret-refresh' not in response.text
        assert 'OPENROUTER_API_KEY' in client.get('/setup').text


def test_csrf_and_origin_required(client):
    authorize(client)
    token = csrf(client)
    assert client.post('/runs',data={'csrf':token}).status_code == 403
    assert client.post('/runs',data={'csrf':'bad'},headers={'Origin':'http://localhost:8765'}).status_code == 403


def test_section_disappears_persists_and_export_unchanged(client):
    authorize(client)
    s = client.app.state.store
    rid = s.execute('INSERT INTO runs(created,status,config,markdown) VALUES (?,?,?,?)',('2026-10-04','ready','{}','# Bericht\n\n## KI\n\nDetails'))
    sec = s.execute('INSERT INTO sections(run_id,position,title,markdown) VALUES (?,?,?,?)',(rid,0,'KI','Details'))
    token = csrf(client)
    assert '1 von 1 Abschnitten noch offen' in client.get(f'/runs/{rid}').text
    r = client.post(f'/runs/{rid}/sections/{sec}/read',data={'csrf':token},headers={'Origin':'http://localhost:8765'})
    assert r.status_code == 200
    assert '0 von 1 Abschnitten noch offen' in r.text
    assert 'Gelesene Abschnitte (1)' in r.text
    assert s.run(rid)['read_completed'] is not None
    assert 'reading-section' not in r.text
    assert client.get(f'/runs/{rid}/markdown').text.endswith('Details')
    client.post(f'/runs/{rid}/sections/{sec}/read',data={'csrf':token,'unread':'1'},headers={'Origin':'http://localhost:8765'})
    assert s.run(rid)['unread'] == 1
    assert s.run(rid)['read_completed'] is None


def test_oauth_callback_without_state_rejected(client):
    authorize(client)
    assert 'ungültig' in client.get('/oauth/callback?state=wrong&code=fake').text


def test_report_html_safe(client):
    authorize(client)
    s = client.app.state.store
    rid = s.execute('INSERT INTO runs(created,status,config,markdown) VALUES (?,?,?,?)',('2026','ready','{}','# Safe <script>alert(1)</script>'))
    assert '<script>alert(1)</script>' not in client.get(f'/runs/{rid}').text



def test_count_includes_all_current_playlist_entries(client,monkeypatch):
    authorize(client)
    s = client.app.state.store
    s.save_settings({'source':'source','refresh_token':'token'})
    class YT:
        def __init__(self,store): pass
        def entries(self,source): return [{'source_item':'current-1'},{'source_item':'current-2'}]
        def close(self): pass
    import app.main
    monkeypatch.setattr(app.main,'YouTube',YT)
    assert client.get('/api/count').json() == {'total':2,'pending':2,'held':0}
