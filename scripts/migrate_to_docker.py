"""Import existing local ledgers/artifacts without copying Mac browser profiles.
Run only before using the new Docker workspace. Re-import never replaces DB data.
"""
import json
from pathlib import Path
import subprocess
import urllib.request
root=Path(__file__).resolve().parents[1]
try:
    state=json.load(urllib.request.urlopen('http://127.0.0.1:8765/api/state',timeout=5))
except OSError:
    state=None
if state:
    inflight=[r for r in state['runs'] if r['status'] in ('queued','running','waiting','paused','pausing','closing','cancelling') and r['mode'] not in ('login',)]
    if inflight:raise SystemExit('原本机环境有业务运行，请结束后再迁移，避免发送状态不一致')
try:
    target=json.load(urllib.request.urlopen('http://127.0.0.1:8766/api/state',timeout=5))
except OSError:
    target=None
if target and target.get('busy'):raise SystemExit('Docker 工作空间有运行中的任务，请结束后再迁移')
subprocess.run(['docker','compose','cp',str(root/'runs'),'app:/data/'],cwd=root,check=True,stdout=subprocess.DEVNULL)
subprocess.run(['docker','compose','exec','-T','--user','root','app','chown','-R','1000:1000','/data/runs'],cwd=root,check=True)
subprocess.run(['docker','compose','restart','app'],cwd=root,check=True)
print('已导入本机运行和发送防重发记录；未迁移 Mac 浏览器登录身份，未启动任何业务任务。')
