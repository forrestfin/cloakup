"""Capture visible professional sections locally, never forward raw resumes."""
import asyncio,re,time
from urllib.parse import urlparse
from . import boss_chat

HEADINGS=('个人优势','自我评价','求职期望','工作经历','项目经历','教育经历','专业技能','技能','资格证书','语言能力','培训经历','教育背景','工作经验','项目经验','技能特长')

def professional_sections(text):
    pattern=r'(?m)^\s*('+ '|'.join(HEADINGS)+r')\s*$'
    matches=list(re.finditer(pattern,text));sections={}
    for i,m in enumerate(matches):
        value=text[m.end():matches[i+1].start() if i+1<len(matches) else len(text)].strip()
        value=re.split(r'(?m)^\s*(?:举报|不合适|立即沟通|打招呼|发送消息|求简历)\s*$',value)[0].strip()
        if value:sections[m.group(1)]=value
    return sections

async def capture_online_resume(manager,job,session,case_id,candidate_id):
    store=manager.cases;case=store.load(case_id);candidate=store.candidate(case,candidate_id)
    if time.time()-candidate.get('online_resume_checked_at',0)<86400:return
    binding=candidate['conversation'];original=session.page;created=[]
    result={'sections':{},'coverage':'当前沟通页职业摘要；未读取到完整在线简历','source_url':boss_chat.CHAT_URL,'collected_at':time.time()}
    try:
        snap=await boss_chat.open_conversation(session,**binding)
        from .autopilot import adopt_platform_identity
        case=store.load(case_id);candidate=adopt_platform_identity(store,case,store.candidate(case,candidate_id),snap)
        binding=candidate['conversation']
        identity=await boss_chat.current_identity(session,binding['job_title'])
        if not identity or identity['chat_id']!=binding['chat_id']:return
        obs=await session.observe(False,text_limit=12000)
        buttons=[e for e in obs.elements if e.label.strip()=='在线简历' and not e.raw.get('disabled')]
        if len(buttons)!=1:
            result['reason']='未找到唯一且可用的在线简历入口'
        else:
            before=list(session.context.pages)
            await session.click(buttons[0].index)
            await asyncio.sleep(1)
            created=[p for p in session.context.pages if p not in before and (urlparse(p.url).hostname or '').endswith('zhipin.com')]
            if len(created)>1:result['reason']='出现多个资料页面，无法唯一归属'
            else:
                session.page=created[0] if created else original
                observed=await session.observe(False,text_limit=40000)
                # The resume must identify the verified receiver. A modal is
                # scoped; background conversation text must never become CV data.
                script=r'''(()=>{const roots=Array.from(document.querySelectorAll('.resume-dialog,.geek-resume,.resume-content,.online-resume,.resume-container,.resume-detail,.resume-box')).filter(e=>{const r=e.getBoundingClientRect();return r.width&&r.height});const text=roots.map(e=>e.innerText).sort((a,b)=>b.length-a.length)[0]||'';return text})()'''
                text=await session.page._stealth_world.evaluate(script)
                if created and not text:text=observed.raw.get('text','')
                if binding['receiver'] not in text:result['reason']='简历页面姓名与已核验会话未能对应，未采集正文'
                else:
                    result['sections']=professional_sections(text)
                    result['source_url']=session.page.url
                    result['coverage']='在线简历当前已加载、可见的职业信息；不代表完整简历或已获得附件'
                    if not result['sections']:result['reason']='尚未识别职业章节，保留已有沟通页摘要'
    except Exception as exc:
        result['reason']='在线简历读取未完成：'+type(exc).__name__
    finally:
        for page in created:
            if not page.is_closed():await page.close()
        session.page=original
        if not created:
            try:await session.press_key('Escape')
            except Exception:pass
        session.last_obs=None;session._observed_frames={}
    case=store.load(case_id);candidate=store.candidate(case,candidate_id)
    candidate['online_resume_checked_at']=time.time()
    if result['sections'] or not candidate.get('online_resume'):candidate['online_resume']=result
    candidate['online_resume_attempt']=result.get('reason')
    store.audit(case,'online_resume_captured',candidate_id=candidate_id,sections=list(result['sections']),coverage=result['coverage'],reason=result.get('reason'),model_disclosure=False)
