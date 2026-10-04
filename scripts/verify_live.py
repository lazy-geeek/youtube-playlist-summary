import json
import argparse
from urllib.parse import urlsplit
from pathlib import Path
import httpx

parser = argparse.ArgumentParser(description='Verify an explicitly configured deployment.')
parser.add_argument('--url', required=True)
parser.add_argument('--credentials', default=str(Path(__file__).resolve().parents[1] / 'data/deploy-secrets.json'))
args = parser.parse_args()
url = args.url.rstrip('/')
parsed = urlsplit(url)
if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
    raise SystemExit('Provide an HTTPS base URL without credentials or a path.')
values = json.loads(Path(args.credentials).read_text())
with httpx.Client(base_url=url,timeout=25,follow_redirects=False) as c:
    for route in ['/','/setup','/history','/health','/runs/1/markdown','/static/app.js']:
        r = c.get(route)
        assert r.status_code == 401, (route,r.status_code)
        print('Protected:',route,r.status_code)
    c.auth = (values['username'],values['password'])
    for route in ['/','/setup','/history','/health','/static/app.js']:
        r = c.get(route)
        assert r.status_code == 200, (route,r.status_code)
        assert values['password'] not in r.text and values['encryption_key'] not in r.text
        print('Authenticated:',route,r.status_code)
    response = c.get('/setup')
    assert url + '/oauth/callback' in response.text
    assert 'OPENROUTER_API_KEY' in response.text
    assert 'HttpOnly' in c.get('/').headers.get('set-cookie','') or c.cookies.get('briefing_session')
    assert 'frame-ancestors' in response.headers['content-security-policy']
    assert c.post('/runs',data={'csrf':'bad'},headers={'Origin':url}).status_code == 403
    assert c.get('/api/count').status_code in (200, 409, 503)
    print('TLS, setup URL, secret redaction, CSP and CSRF verified. Real integration tests require configured provider access.')
