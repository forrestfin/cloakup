"""Read database secret at runtime; no credentials in image layers or argv."""
import os
from pathlib import Path
from urllib.parse import quote
for name in ('ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN','ANTHROPIC_BASE_URL','CLOAKBROWSER_LICENSE_KEY'):
    if not os.environ.get(name): os.environ.pop(name,None)
secret = Path('/run/secrets/postgres_password').read_text().strip()
os.environ['DATABASE_URL'] = 'postgresql+psycopg://cloakup:'+quote(secret,safe='')+'@db:5432/cloakup'
# This entrypoint runs before any Chromium process in this single-app container.
# Profile locks from the previous container hostname must not survive recreation.
for profile in Path('/data/profiles').rglob('SingletonLock'):
    for name in ('SingletonLock','SingletonCookie','SingletonSocket'):
        (profile.parent/name).unlink(missing_ok=True)
os.execvp('supervisord',['supervisord','-n','-c','/etc/supervisor/conf.d/cloakup.conf'])
