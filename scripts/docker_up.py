"""Start the local stack; model credentials stay in environment.
Optional --license prompts without echo. The launcher never writes a license file.
"""
import json
import getpass
import os
from pathlib import Path
import secrets
import subprocess
import sys
root=Path(__file__).resolve().parents[1]
password=root/'data/docker/postgres-password'
password.parent.mkdir(parents=True,exist_ok=True)
# 日志与日报目录绑定到容器 /data/logs、/data/reports，先在本机建好。
for folder in ('logs','reports'):(root/'data'/folder).mkdir(parents=True,exist_ok=True)
if not password.exists():
    fd=os.open(password,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:f.write(secrets.token_urlsafe(32))
env=os.environ.copy()
env['BUILDKIT_PROGRESS']='plain'
env['COMPOSE_PROGRESS']='plain'
# 飞书凭据只留在本机 data 目录。调用方未显式设置时从这里补上，避免容器里的空值在重建时把它们清掉。
lark_file=root/'data/docker/lark.env'
if lark_file.exists():
    for line in lark_file.read_text().splitlines():
        line=line.strip()
        if not line or line.startswith('#') or '=' not in line:continue
        key,value=line.split('=',1)
        if key in ('LARK_APP_ID','LARK_APP_SECRET') and key not in os.environ and value:
            env[key]=value
if '--reuse-runtime' in sys.argv:
    # Reuse only this local app's already-authorized credentials during updates.
    # 已停止（docker compose stop）的容器也保留着原来的环境变量，同样可以复用；
    # 只有容器被删除（docker compose down）后才需要 --license 重新输入。
    container=subprocess.check_output(['docker','compose','ps','-a','-q','app'],cwd=root,text=True).strip().splitlines()
    if not container:raise SystemExit('找不到 app 容器（可能已被 docker compose down 删除）；请使用 --license 启动，并在环境变量里提供模型凭据')
    container=container[0]
    metadata=json.loads(subprocess.check_output(['docker','inspect',container],text=True))[0]
    settings=dict(item.split('=',1) for item in metadata['Config']['Env'] if '=' in item)
    for name in ('CLOAKBROWSER_LICENSE_KEY','ANTHROPIC_AUTH_TOKEN','ANTHROPIC_API_KEY','ANTHROPIC_BASE_URL','CLOAKUP_MODEL','CLOAKUP_API_COMPAT','LARK_APP_ID','LARK_APP_SECRET'):
        # Explicit caller configuration can install, rotate, or clear Feishu
        # credentials; an old empty container value must never overwrite it.
        # 调用方或本机 lark.env 已有值时，不用容器里的空值覆盖。
        if name in ('LARK_APP_ID','LARK_APP_SECRET') and (name in os.environ or env.get(name)):continue
        if name in settings:env[name]=settings[name]
if '--license' in sys.argv:

    env['CLOAKBROWSER_LICENSE_KEY']=getpass.getpass('CloakBrowser license (hidden): ')
raise SystemExit(subprocess.call(['docker','compose','up','-d','--build','--wait'],cwd=root,env=env))
