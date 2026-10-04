from pathlib import Path
import json
import argparse
from urllib.parse import urlsplit
import secrets
import tarfile
from cryptography.fernet import Fernet

parser = argparse.ArgumentParser(description='Prepare a portable deployment archive.')
parser.add_argument('--url', required=True)
parser.add_argument('--output', default='data/playlist-briefing.tar.gz')
args = parser.parse_args()
parsed = urlsplit(args.url)
if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
    raise SystemExit('Provide an HTTPS base URL without credentials or a path.')
url = args.url.rstrip('/')
root = Path(__file__).resolve().parents[1]
data = root / 'data'
data.mkdir(mode=0o700,exist_ok=True)
data.chmod(0o700)
config = data / 'deploy-secrets.json'
if not config.exists():
    values = {'username':'owner','password':secrets.token_urlsafe(32),'encryption_key':Fernet.generate_key().decode()}
    config.write_text(json.dumps(values))
    config.chmod(0o600)
else:
    values = json.loads(config.read_text())
access = data / 'server-access.txt'
access.write_text('URL: '+url+'\nBenutzername: '+values['username']+'\nPasswort: '+values['password']+'\n')
access.chmod(0o600)
output = Path(args.output)
output.parent.mkdir(parents=True, exist_ok=True)
with tarfile.open(output,'w:gz') as archive:
    for p in ['Dockerfile','pyproject.toml','uv.lock','app.json','.dockerignore','app']:
        archive.add(root / p,arcname=p,filter=lambda info: None if '__pycache__' in info.name else info)
print('Deployment archive ready:', output)
print('Access details are stored only in data/server-access.txt.')
