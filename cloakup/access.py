"""Local browser access classification. Never returns page contents to a model."""
import re
from urllib.parse import urlparse


def is_boss_url(url):
    host=urlparse(url).hostname or ''
    return host=='zhipin.com' or host.endswith('.zhipin.com')


def boss_login_required(obs):
    if not is_boss_url(obs.url): return False
    path=urlparse(obs.url).path.lower()
    elements=getattr(obs,'elements',[])
    labels={e.label.strip() for e in elements}
    return (any(token in path for token in ('/web/user','/login','/passport','/signin'))
            or bool(labels & {'登录/注册','扫码登录','密码登录','短信登录'})
            or any(getattr(e,'raw',{}).get('type')=='password' for e in elements))


def boss_verification_required(obs):
    raw=getattr(obs,'raw',{})
    return bool(raw.get('challenge') or any(t in raw.get('text','') for t in ('环境存在异常','操作过于频繁','账号存在异常')))


def boss_access_ready(obs):
    return (is_boss_url(obs.url) and urlparse(obs.url).path.startswith('/web/chat/')
            and not boss_login_required(obs) and not boss_verification_required(obs)
            and any(re.fullmatch(r'(沟通|推荐牛人|推荐|职位管理|我的岗位|职位)(?:\s*\d+)?',e.label.strip()) for e in obs.elements))


def can_recover_login(job):
    # Only restart the read-only exploration of legacy failed runs. Never replay
    # implementation or a possibly successful send as an authentication retry.
    error=job.get('error') or ''
    return (job.get('mode')=='automation_task' and job.get('status')=='failed'
            and job.get('phase') in ('readiness','exploration')
            and not any(p.get('phase')=='implementation' for p in job.get('phases',[]))
            and (job.get('failure_kind')=='browser_login'
                 or 'BOSS 已显示登录表单' in error
                 or any('BOSS 已显示登录表单' in (r.get('reason') or '') for r in job.get('recoveries',[]))))
