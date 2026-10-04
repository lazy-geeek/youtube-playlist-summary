"""Run as root on the target Dokku host; secrets arrive on stdin, never logged."""
import json
import argparse
import re
from pathlib import Path
import subprocess
import sys

parser = argparse.ArgumentParser(description='Configure an explicitly named Dokku app.')
parser.add_argument('--app', default='playlist-briefing')
parser.add_argument('--domain', required=True)
parser.add_argument('--storage-root', default='/var/lib/dokku/data/storage')
args = parser.parse_args()
APP, DOMAIN = args.app, args.domain
if not re.fullmatch(r'[a-z][a-z0-9-]*', APP) or not re.fullmatch(r'[a-zA-Z0-9.-]+', DOMAIN):
    raise SystemExit('Invalid app name or domain.')
values = json.load(sys.stdin)

def run(*args, quiet=False):
    result = subprocess.run(['dokku',*args],stdout=subprocess.DEVNULL if quiet else None,
                            stderr=subprocess.PIPE if quiet else None,text=True)
    if result.returncode:
        raise SystemExit('Dokku operation failed: '+args[0])

exists = subprocess.run(['dokku','apps:exists',APP],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode == 0
if exists:
    # Only reuse this specific app once marked as managed by this project.
    result = subprocess.run(['dokku','config:get',APP,'PLAYLIST_BRIEFING_MANAGED'],capture_output=True,text=True)
    if result.stdout.strip() != '1':
        raise SystemExit('Existing app is not managed by this project; refused to overwrite.')
else:
    run('apps:create',APP)
path = Path(args.storage_root) / APP
path.mkdir(mode=0o700,exist_ok=True)
path.chmod(0o700)
import os
os.chown(path,10001,10001)
run('config:set','--no-restart',APP,'PLAYLIST_BRIEFING_MANAGED=1',
    'APP_URL=https://'+DOMAIN,'APP_USERNAME='+values['username'],'APP_PASSWORD='+values['password'],
    'ENCRYPTION_KEY='+values['encryption_key'],'DATA_DIR=/app/data',quiet=True)
run('domains:set',APP,DOMAIN)
run('ports:set',APP,'http:80:8000')
run('storage:mount',APP,str(path)+':/app/data')
# SQLite plus exclusive worker lock requires stop-before-start, never rolling instances.
run('checks:disable',APP,'web')
run('ps:set',APP,'stop-timeout-seconds','120')
run('nginx:set',APP,'proxy-read-timeout','600s')
run('nginx:set',APP,'client-max-body-size','2m')
run('http-auth:enable',APP,values['username'],values['password'],quiet=True)
print('Dokku app, credentials, persistence and proxy configured.')
