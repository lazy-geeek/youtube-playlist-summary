from pathlib import Path
import secrets
from cryptography.fernet import Fernet
p = Path('.env')
if p.exists():
    raise SystemExit('.env existiert bereits und bleibt unverändert.')
p.write_text('APP_URL=http://localhost:8765\nAPP_USERNAME=owner\nOPENROUTER_API_KEY=\nAPP_PASSWORD=' + secrets.token_urlsafe(32) + '\nENCRYPTION_KEY=' + Fernet.generate_key().decode() + '\nDATA_DIR=./data\nGOOGLE_ALLOWED_EMAIL=\n')
p.chmod(0o600)
print('.env erstellt. Passwort und berechtigte Google-Adresse lokal konfigurieren.')
