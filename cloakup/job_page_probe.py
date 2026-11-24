"""只读采集 BOSS 职位管理页与推荐页岗位切换控件的页面结构（多岗位任务的页面观察用）。

只做三件事：跳转到这两个页面、读取结构、在推荐页展开一次岗位下拉（不选任何选项，回沟通页时自然收起）。
不点修改按钮、不输入。候选人卡片与会话整块跳过；正文只保留本任务岗位名和界面用词，其余换成字数；
title、aria-label 等属性同样处理；链接、data 属性里的长 ID 换成稳定代号，本任务岗位的代号标出岗位名。
完整结构写到数据目录 analysis/ 下的文件，执行器检查记录里只留摘要。
"""
import asyncio
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .pacing import jitter

CHAT_URL = 'https://www.zhipin.com/web/chat/index'
RECOMMEND_URL = 'https://www.zhipin.com/web/chat/recommend'
RECOMMEND_FRAME = '/web/frame/recommend/'
JOB_MENU_TEXT = '职位管理'
MAX_NODES = 4000
MAX_HTML = 150000
MAX_FRAMES = 4
SKIP = '.candidate-card-wrap,[data-geekid],.geek-item,.card-list,.chat-conversation,.message-item,.chat-message-list'

# 界面用词（整段只由这些词、数字和标点组成才原样保留）；人名、公司名、其他岗位名都会被换成字数。
UI_WORDS = ('上一页','下一页','职位','岗位','状态','城市','地区','经验','学历','薪资','操作','全部','不限','招聘','开放','关闭','审核',
            '发布','编辑','删除','刷新','置顶','下线','上线','推荐','搜索','筛选','确定','取消','沟通','牛人','查看','更多','管理',
            '设置','切换','选择','失败','通过','在线','最近','看过','收藏','简历','打招呼','继续','暂停','过期','名额','热门','普通',
            '急聘','类型','时间','创建','更新','数据','详情','复制','分享','招聘中','审核中','进行中','共','条','页','已','待','未','的','薪')

# 结构骨架：opts={skip,maxNodes,maxHtml,titles,ui,roots}；roots 为空时从 body 开始，否则只取这些元素。
SKELETON_JS = r'''(opts=>{
 const titles=opts.titles||[],ui=new RegExp('^[\\d\\s()（）:：/·|,，.。\\-~～+%kK<>«»‹›…]*(?:(?:'+opts.ui.join('|')+')[\\d\\s()（）:：/·|,，.。\\-~～+%kK]*)*$');
 const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
 const mask=s=>s.replace(/1[3-9]\d{9}/g,'[手机号]').replace(/[\w.+-]+@[\w-]+\.[\w.]+/g,'[邮箱]');
 const txt=s=>{const t=String(s).replace(/\s+/g,' ').trim();if(!t)return '';if(t.length>60)return '[文本 '+t.length+' 字]';
  const hit=titles.filter(x=>t.includes(x));if(!hit.length)return ui.test(t)?mask(t):'[文本 '+t.length+' 字]';
  let rest=t;for(const x of hit)rest=rest.split(x).join(' ');rest=rest.replace(/\s+/g,' ').trim();
  return !rest||ui.test(rest)?mask(t):hit.join(' ')+' [文本 '+rest.length+' 字]';};
 const CJK=/[㐀-鿿豈-﫿]/;
 const RAW=/^(class|id|role|type|name|for|ka|disabled|checked|selected|tabindex)$/,TEXT=/^(title|alt|placeholder|aria-[\w-]+)$/,DATA=/^(value|data-[\w-]+)$/;
 const attr=a=>{const n=a.name,v=String(a.value).slice(0,300);
  if(RAW.test(n))return CJK.test(v)?txt(v):v;
  if(n==='href')return v;
  if(n==='src')return '[src]';
  if(TEXT.test(n))return txt(v);
  if(DATA.test(n))return CJK.test(v)?txt(v):mask(v);
  return null;};
 let count=0,truncated=false,skipped=0;
 const walk=(node,depth)=>{
  if(count>=opts.maxNodes){truncated=true;return '';}
  if(node.nodeType===3){const t=txt(node.textContent||'');if(!t)return '';count++;return esc(t);}
  if(node.nodeType!==1)return '';
  const e=node,tag=e.tagName.toLowerCase();
  if(/^(script|style|noscript|template|link|meta)$/.test(tag))return '';
  count++;
  if(e.matches(opts.skip)){skipped++;return '<'+tag+' class="'+esc(typeof e.className==='string'?e.className:'')+'" x-skipped="1"></'+tag+'>';}
  const r=e.getBoundingClientRect(),st=getComputedStyle(e);
  const visible=r.width>0&&r.height>0&&st.visibility!=='hidden'&&st.display!=='none';
  const attrs=Array.from(e.attributes).map(a=>{const v=attr(a);return v===null?'':' '+a.name+'="'+esc(v)+'"'}).join('');
  const extra=(visible?'':' x-hidden="1"')+(st.cursor==='pointer'?' x-pointer="1"':'');
  if(tag==='svg'||tag==='img'||tag==='iframe')return '<'+tag+attrs+extra+'></'+tag+'>';
  let inner='';if(depth<40)for(const c of e.childNodes)inner+=walk(c,depth+1);
  return '<'+tag+attrs+extra+'>'+inner+'</'+tag+'>';
 };
 const roots=opts.roots?opts.roots.map(p=>document.querySelector(p)).filter(Boolean):[document.body];
 const html=roots.map(r=>walk(r,0)).join('\n');
 const text=((document.body&&document.body.innerText)||'').trim();
 return {html:html.slice(0,opts.maxHtml),truncated:truncated||html.length>opts.maxHtml,nodes:count,skipped,
  text_len:text.length,loading:text.length<200&&text.includes('加载中')};
})'''

PATH_JS = r'''e=>{const a=[];for(;e&&e.nodeType===1;e=e.parentElement){if(e.id){a.unshift('#'+CSS.escape(e.id));break;}const s=e.parentElement?Array.from(e.parentElement.children).filter(n=>n.tagName===e.tagName):[];a.unshift(e.tagName.toLowerCase()+(s.length>1?':nth-of-type('+(s.indexOf(e)+1)+')':''));}return a.join(' > ')}'''

# 主页面里文字恰为“职位管理”的可见元素：给出链接与中心点，用于跳转或原生点击。
MENU_JS = r'''((label)=>{const path=(''' + PATH_JS + r''');
 const vis=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0};
 const nodes=Array.from(document.querySelectorAll('a,li,div,span')).filter(e=>vis(e)&&(e.innerText||'').trim()===label);
 const leaves=nodes.filter(e=>!nodes.some(n=>n!==e&&e.contains(n)));
 return leaves.map(e=>{const r=e.getBoundingClientRect(),a=e.closest('a[href]'),x=r.x+r.width/2,y=r.y+r.height/2,h=document.elementFromPoint(x,y);
  return {href:a?a.getAttribute('href'):null,path:path(e),x,y,hit:!!h&&(h===e||e.contains(h))};});
})'''

# 推荐页内嵌页里显示本任务岗位名的最小可见元素（岗位切换控件的候选），排除候选人区域与按钮、输入框。
SWITCH_JS = r'''((titles,skip)=>{const path=(''' + PATH_JS + r''');
 const vis=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0};
 const nodes=Array.from(document.querySelectorAll('body *')).filter(e=>vis(e)&&!e.closest(skip)&&!e.closest('button,input,textarea,[contenteditable="true"]')&&titles.some(t=>{const s=(e.innerText||'').trim();return s.includes(t)&&s.length<=t.length+40}));
 const leaves=nodes.filter(e=>!nodes.some(n=>n!==e&&e.contains(n)));
 return leaves.map(e=>({path:path(e),tag:e.tagName.toLowerCase(),classes:typeof e.className==='string'?e.className:'',pointer:getComputedStyle(e).cursor==='pointer'}));
})'''

# 点击前重新核实：路径指向的仍是显示本任务岗位名的可见元素，且不在候选人区域、按钮或输入框里。
CONTROL_CHECK_JS = r'''((css,titles,skip)=>{const e=document.querySelector(css);if(!e)return false;const r=e.getBoundingClientRect();
 return r.width>0&&r.height>0&&!e.closest(skip)&&!e.closest('button,input,textarea,[contenteditable="true"]')&&titles.some(t=>(e.innerText||'').includes(t));})'''

# 展开前后对比：取展开后新出现的可见元素（只取最外层，最多 20 个），单独抓结构，避免下拉被截断。
VISIBLE_JS = r'''((skip)=>{const path=(''' + PATH_JS + r''');const out=[];
 for(const e of document.querySelectorAll('body *')){if(out.length>=6000)break;const r=e.getBoundingClientRect();if(r.width>0&&r.height>0&&!e.closest(skip))out.push(path(e));}
 return out;})'''

ID_RE = re.compile(r'(?<![\w~-])(?=[\w~-]*\d)(?=[\w~-]*[A-Za-z])[\w~-]{12,}(?![\w~-])|(?<!\d)\d{8,}(?!\d)', re.ASCII)
ATTR_RE = re.compile(r'\b(href|id|value|data-[\w-]+)="([^"]*)"')


class ProbeStopped(Exception):
    def __init__(self, reason, stage):
        super().__init__(reason);self.reason=reason;self.stage=stage


class Tokens:
    """把链接、data 属性与网址里的长 ID 换成稳定代号；同一个值始终同一个代号，本任务岗位标出岗位名。"""
    def __init__(self, known):
        self.known=dict(known);self.names={};self.labels={}
    def sub(self, text):
        def name(m):
            value=m.group(0)
            if value not in self.names:
                self.names[value]='ID'+str(len(self.names)+1)
                if value in self.known:self.labels[self.names[value]]=self.known[value]
            return self.names[value]
        return ID_RE.sub(name, text or '')
    def html(self, text):
        return ATTR_RE.sub(lambda m:m.group(1)+'="'+self.sub(m.group(2))+'"', text or '')


def skeleton_expression(titles, roots=None):
    opts={'skip':SKIP,'maxNodes':MAX_NODES,'maxHtml':MAX_HTML,'titles':list(titles),'ui':list(UI_WORDS),'roots':roots}
    return '(' + SKELETON_JS + ')(' + json.dumps(opts, ensure_ascii=False) + ')'


async def _read_main(session, expression):
    from .dom_reader import read_dom
    return await read_dom(session.page, expression, timeout=8)


async def _read_frame(session, frame, expression):
    from .dom_reader import read_frame_dom
    return await read_frame_dom(session.page, frame, expression, timeout=8)


async def _frame_click(session, frame, css):
    from .frame_actions import click
    await click(session, frame, css)


def _permitted(manager):
    if any(m['status'] not in ('paused','stopped','completed') for m in manager.account_execution.members()):
        raise ValueError('读取职位页面前，请先暂停该账号全部任务')
    owner=manager.busy()
    if owner and (not owner.get('account_runner') or owner.get('manual_takeover')):
        raise ValueError('浏览器正被其他操作或管理员接管')
    if owner and owner.get('active_case_id'):
        raise ValueError('执行器还在处理当前会话，等它停到安全点后再读取')


def _site_frames(session):
    main=getattr(session.page,'main_frame',None)
    return [f for f in session.page.frames if f is not main and urlparse(f.url).hostname=='www.zhipin.com'
            and not re.search(r'captcha|verify|challenge', f.url, re.I)][:MAX_FRAMES]


def _short(url):
    p=urlparse(url or '');return p.path+('?'+p.query if p.query else '')


async def _guard(manager, session, stage):
    """每次跳转、点击前后都核一次：任务仍暂停；登录失效或平台校验就停下，不再跳转。"""
    _permitted(manager)
    from . import page_health
    url, frames = await page_health.read_frames(session, 'chat')
    path=urlparse(url or '').path
    if any(p in path for p in ('/web/user','/login','/passport','/signin')) or any(f.get('login') for f in frames):
        raise ProbeStopped('login_required', stage)
    if any(f.get('challenge') for f in frames):
        raise ProbeStopped('verification_required', stage)


async def _settle(session, ready):
    for _ in range(16):
        if await ready():return True
        await asyncio.sleep(jitter(.5, .2))
    return False


def _clean(value, tokens):
    if not isinstance(value, dict):return {'error':'unreadable'}
    return {k:(tokens.html(v) if k=='html' else v) for k,v in value.items()}


async def _dump_page(session, tokens, titles):
    main=await _read_main(session, skeleton_expression(titles))
    frames=[]
    for frame in _site_frames(session):
        try:value=await _read_frame(session, frame, skeleton_expression(titles))
        except Exception as exc:value={'error':type(exc).__name__}
        frames.append({'url':tokens.sub(_short(frame.url)),**_clean(value, tokens)})
    return {'url':tokens.sub(_short(session.page.url)),'main':_clean(main, tokens),'frames':frames}


async def _open_job_list(manager, session, capture, tokens, titles):
    links=await _read_main(session, '(' + MENU_JS + ')(' + json.dumps(JOB_MENU_TEXT, ensure_ascii=False) + ')') or []
    capture['menu']=[{'path':tokens.sub(item.get('path') or ''),'hit':item.get('hit'),'href':tokens.sub(item.get('href') or '') or None} for item in links]
    if len(links)!=1:raise ProbeStopped('job_menu_not_unique' if links else 'job_menu_missing', 'job_list')
    item=links[0];href=item.get('href')
    target=urljoin(CHAT_URL, href) if href and not href.startswith(('javascript:','#')) else None
    before=session.page.url
    _permitted(manager)
    if target and urlparse(target).hostname=='www.zhipin.com' and urlparse(target).path.startswith('/web/'):
        capture['job_list_entry']='link';await session.navigate(target)
    elif item.get('hit'):
        # 菜单不是普通链接：在元素中心做一次原生点击（只是切换页面）。
        capture['job_list_entry']='menu_click'
        await session._act('click', lambda: session.page.mouse.click(item['x'], item['y']))
    else:
        raise ProbeStopped('job_menu_not_clickable', 'job_list')
    async def ready():
        value=await _read_main(session, '((document.body&&document.body.innerText)||"").trim().length')
        return session.page.url!=CHAT_URL and isinstance(value,int) and value>300 or (session.page.url!=before and bool(_site_frames(session)))
    settled=await _settle(session, ready)
    await asyncio.sleep(jitter(1.5, .2))
    await _guard(manager, session, 'job_list')
    capture['job_list']={'settled':settled,**await _dump_page(session, tokens, titles)}


async def _open_recommend(manager, session, capture, tokens, titles):
    _permitted(manager)
    await session.navigate(RECOMMEND_URL)
    frame=None
    async def ready():
        nonlocal frame
        found=[f for f in session.page.frames if RECOMMEND_FRAME in f.url]
        frame=found[0] if len(found)==1 else None
        return frame is not None
    settled=await _settle(session, ready)
    await _guard(manager, session, 'recommend')
    if not settled:raise ProbeStopped('recommend_frame_missing', 'recommend')
    await asyncio.sleep(jitter(1.5, .2))
    await _guard(manager, session, 'recommend')
    section={'before':await _dump_page(session, tokens, titles),'frame_url_before':tokens.sub(_short(frame.url))}
    capture['recommend']=section
    controls=await _read_frame(session, frame, '(' + SWITCH_JS + ')(' + json.dumps(titles, ensure_ascii=False) + ',' + json.dumps(SKIP) + ')') or []
    section['switch_candidates']=[{**c,'path':tokens.sub(c.get('path') or '')} for c in controls]
    if len(controls)!=1:
        section['expanded']={'performed':False,'reason':'switch_control_not_unique' if controls else 'switch_control_missing'};return
    css=controls[0]['path']
    visible_before=set(await _read_frame(session, frame, '(' + VISIBLE_JS + ')(' + json.dumps(SKIP) + ')') or [])
    await _guard(manager, session, 'recommend_switch')
    if not await _read_frame(session, frame, '(' + CONTROL_CHECK_JS + ')(' + json.dumps(css) + ',' + json.dumps(titles, ensure_ascii=False) + ',' + json.dumps(SKIP) + ')'):
        section['expanded']={'performed':False,'reason':'switch_control_changed'};return
    await _frame_click(session, frame, css)
    await asyncio.sleep(jitter(1.2, .2))
    await _guard(manager, session, 'recommend_switch')
    current=next((f for f in session.page.frames if RECOMMEND_FRAME in f.url), None)
    expanded={'performed':True,'collapsed':False,'frame_url':tokens.sub(_short(current.url)) if current else None}
    section['expanded']=expanded
    if current is not None:
        visible_after=await _read_frame(session, current, '(' + VISIBLE_JS + ')(' + json.dumps(SKIP) + ')') or []
        fresh=[p for p in visible_after if p not in visible_before]
        roots=[p for p in fresh if not any(p!=q and p.startswith(q+' > ') for q in fresh)][:20]
        expanded['new_visible']=len(fresh)
        if roots:expanded['new_parts']=_clean(await _read_frame(session, current, skeleton_expression(titles, roots)), tokens)
        expanded['dump']=_clean(await _read_frame(session, current, skeleton_expression(titles)), tokens)
    after=next((f for f in session.page.frames if RECOMMEND_FRAME in f.url), None)
    section['frame_url_after']=tokens.sub(_short(after.url)) if after else None


def _write_capture(manager, capture):
    root=Path(getattr(manager,'root','.'))/'analysis';root.mkdir(parents=True, exist_ok=True)
    path=root/('job-pages-'+time.strftime('%Y%m%d-%H%M%S')+'.json')
    path.write_text(json.dumps(capture, ensure_ascii=False, indent=1), encoding='utf-8')
    return 'analysis/'+path.name


async def read_job_pages(manager, task_id, now=None):
    from .boss_recommend import job_identity
    from .task_policy import window_status
    from . import page_health
    definition=manager.definitions.get(task_id)
    if definition['config']['engine']!='boss_sourcing':raise ValueError('仅支持 BOSS 寻访任务')
    _permitted(manager)
    if not window_status(definition['config'].get('schedule'), now)['allowed']:raise ValueError('当前不在任务执行时段，不操作网站')
    session=manager.boss_session
    if not manager.browser_alive(session):raise ValueError('没有可用的执行浏览器')
    cases=[manager.cases.load(cid) for cid in definition['config'].get('case_ids',[])]
    titles=[c['job']['label'] for c in cases]
    known={}
    for c in cases:
        try:known[job_identity(c)]=c['job']['label']
        except Exception:continue
    start=await page_health.check(session, 'chat')
    if start.get('has_draft'):raise ValueError('沟通页有未发送的草稿，未离开页面')
    if start.get('reason') in ('unreadable','login_required','verification_required'):
        raise ValueError({'unreadable':'沟通页读取失败，无法确认没有未发送草稿，未离开页面'}.get(start['reason'],'页面需要登录或平台校验，未读取'))
    tokens=Tokens(known)
    capture={'task_id':task_id,'started_at':time.time()}
    report={'task_id':task_id,'business_writes':0,'settings_writes':0,'stages':[]}
    stage='job_list'
    try:
        await _open_job_list(manager, session, capture, tokens, titles);report['stages'].append('job_list')
        stage='recommend'
        await _open_recommend(manager, session, capture, tokens, titles);report['stages'].append('recommend')
        report['completed']=True
    except ProbeStopped as stop:
        report.update(completed=False, reason=stop.reason, stage=stop.stage)
    except ValueError as exc:
        report.update(completed=False, reason='not_permitted', stage=stage, error=str(exc)[:200])
    except Exception as exc:
        report.update(completed=False, reason='error', stage=stage, error_type=type(exc).__name__, error=str(exc)[:200])
    finally:
        # 推荐页缓存作废：执行器恢复后会重新打开推荐页，而不是沿用这次诊断留下的页面。
        session._boss_recommend_list=None;session._boss_recommend_detail=None
        if report.get('reason') not in ('login_required','verification_required'):
            try:
                await _guard(manager, session, 'return')
                await session.navigate(CHAT_URL);report['returned_to_chat']=True
            except ProbeStopped as stop:
                report.update(returned_to_chat=False, return_blocked=stop.reason)
            except Exception as exc:
                report.update(returned_to_chat=False, return_error=type(exc).__name__)
    capture['report']=dict(report);capture['known_ids']=tokens.labels
    report['known_ids']=tokens.labels
    expanded=capture.get('recommend',{}).get('expanded',{})
    report['summary']={'menu':capture.get('menu'),'job_list_entry':capture.get('job_list_entry'),'job_list_url':capture.get('job_list',{}).get('url'),
                       'job_list_frames':[f['url'] for f in capture.get('job_list',{}).get('frames',[])],
                       'recommend_frame_before':capture.get('recommend',{}).get('frame_url_before'),'recommend_frame_after':capture.get('recommend',{}).get('frame_url_after'),
                       'switch_candidates':len(capture.get('recommend',{}).get('switch_candidates',[])),'expanded':expanded.get('performed',False)}
    try:report['capture_file']=_write_capture(manager, capture)
    except OSError as exc:report['capture_error']=type(exc).__name__
    report['duration_ms']=round((time.time()-capture['started_at'])*1000)
    return report
