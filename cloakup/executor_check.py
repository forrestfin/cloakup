"""End-to-end executor contract on a network-free synthetic page only."""
import asyncio,time
from urllib.parse import quote
from .dom_reader import read_dom
from .locate import locate

CONTRACT='executor-v1'
HTML='''<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>Cloakup executor check</title></head><body>
<label>执行器测试输入<input id="executor-input"></label>
<button id="executor-confirm" onclick="document.querySelector('#executor-result').textContent=document.querySelector('#executor-input').value;document.querySelector('#executor-count').textContent=String(Number(document.querySelector('#executor-count').textContent)+1)">确认执行器测试</button>
<button>重复测试按钮</button><button>重复测试按钮</button><button disabled>禁用测试按钮</button>
<output id="executor-result"></output><output id="executor-count">0</output></body></html>'''


class ExecutorCheckError(ValueError):
    def __init__(self,message,steps):
        super().__init__(message)
        self.report={'contract':CONTRACT,'status':'failed','steps':steps,'reason':message,'business_writes':0}


async def verify(session):
    if not session.page.url.startswith('data:text/html,'):raise ValueError('执行器核验仅允许隔离合成页面')
    started=time.monotonic();steps=[]
    async def step(name,fn):
        begin=time.monotonic()
        try:value=await fn()
        except Exception as exc:
            steps.append({'name':name,'status':'failed','duration_ms':round((time.monotonic()-begin)*1000),'error_type':type(exc).__name__})
            if name in ('原生输入','原生点击') and getattr(session,'last_action_timing',None):
                steps[-1]['timing']=dict(session.last_action_timing)
            raise ExecutorCheckError(name+'未通过',steps) from exc
        steps.append({'name':name,'status':'passed','duration_ms':round((time.monotonic()-begin)*1000)})
        if name in ('原生输入','原生点击') and getattr(session,'last_action_timing',None):steps[-1]['timing']=dict(session.last_action_timing)
        return value
    async def observe():
        value=await session.observe(False)
        if value.title!='Cloakup executor check':raise ValueError('合成测试页面身份变化')
        return value
    if hasattr(session.page,'bring_to_front'):
        await step('激活隔离测试页',lambda:asyncio.wait_for(session.page.bring_to_front(),timeout=8))
    obs=await step('读取隔离页面',observe)
    target,_=locate({'label':'执行器测试输入','tag':'input'},obs)
    button,_=locate({'label':'确认执行器测试','tag':'button'},obs)
    ambiguous,_=locate({'label':'重复测试按钮','tag':'button'},obs)
    if not target or not button or ambiguous:raise ExecutorCheckError('元素唯一定位或歧义拒绝未通过',steps)
    steps.append({'name':'唯一定位与歧义拒绝','status':'passed','duration_ms':0})
    await step('原生输入',lambda:session.type_text(target.element.index,'CLOAKUP_READY'))
    obs=await step('输入后重新观察',observe)
    button,_=locate({'label':'确认执行器测试','tag':'button'},obs)
    if not button:raise ValueError('点击前元素已变化')
    await step('原生点击',lambda:session.click(button.element.index))
    async def receipt():
        result=await read_dom(session.page,"(()=>({value:document.querySelector('#executor-result')?.textContent,count:Number(document.querySelector('#executor-count')?.textContent)}))()")
        if result!={'value':'CLOAKUP_READY','count':1}:raise ValueError('动作结果或单次执行回执不符')
    await step('结果回读与单次执行核验',receipt)
    warnings=[{'step':s['name'],'duration_ms':s['duration_ms'],'reason':'操作成功但耗时偏长，不自动重放，请查看阶段耗时'} for s in steps if s['name'] in ('原生输入','原生点击') and s['duration_ms']>15000]
    return {'warnings':warnings,'contract':CONTRACT,'status':'passed','scope':'synthetic_page_only','steps':steps,'duration_ms':round((time.monotonic()-started)*1000),'business_writes':0}


def url():return 'data:text/html,'+quote(HTML)
