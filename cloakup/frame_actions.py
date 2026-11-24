"""Observed iframe controls -> native pointer/keyboard; no DOM writes or JS clicks."""
import asyncio,json
from .dom_reader import read_frame_dom
from .sourcing import SourcingError

PATH = r'''e=>{const a=[];for(;e&&e.nodeType===1;e=e.parentElement){if(e.id){a.unshift('#'+CSS.escape(e.id));break;}const siblings=e.parentElement?Array.from(e.parentElement.children).filter(n=>n.tagName===e.tagName):[];a.unshift(e.tagName.toLowerCase()+(siblings.length>1?':nth-of-type('+(siblings.indexOf(e)+1)+')':''));}return a.join(' > ')}'''
async def text_control(page,frame,label,scope='body'):
 expr='(()=>{const path=('+PATH+');const root=document.querySelector('+json.dumps(scope)+');if(!root)return null;const nodes=Array.from(root.querySelectorAll("button,a,div,span,label,li")).filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&(e.innerText||" ").trim()==='+json.dumps(label)+'});const matches=nodes.filter(e=>!nodes.some(n=>n!==e&&e.contains(n)));return matches.length===1?path(matches[0]):null})()'
 return await read_frame_dom(page,frame,expr)

async def click(session,frame,css):
 if not css:raise SourcingError('内嵌控件尚未唯一定位；未点击')
 # The platform's settings iframe is a direct child. Reject other frame layouts.
 point=await read_frame_dom(session.page,frame,'(()=>{const e=document.querySelector('+json.dumps(css)+');if(!e)return null;const r=e.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2,h=document.elementFromPoint(x,y);return r.width>0&&r.height>0&&x>=0&&y>=0&&x<innerWidth&&y<innerHeight&&(h===e||e.contains(h))?{x,y,w:innerWidth,h:innerHeight}:null})()')
 if not point:raise SourcingError('内嵌控件被遮挡或不在可见区域；未点击')
 cdp=await session.context.new_cdp_session(session.page)
 try:
  tree=(await asyncio.wait_for(cdp.send('Page.getFrameTree'),6))['frameTree'];matches=[n['frame']['id'] for n in tree.get('childFrames',[]) if n['frame'].get('url')==frame.url]
  if len(matches)!=1:raise SourcingError('内嵌页面层级不唯一；未点击')
  owner=await asyncio.wait_for(cdp.send('DOM.getFrameOwner',{'frameId':matches[0]}),6)
  box=await asyncio.wait_for(cdp.send('DOM.getBoxModel',{'backendNodeId':owner['backendNodeId']}),6);q=box['model']['content']
  x=q[0]+point['x']/point['w']*(q[2]-q[0])+point['y']/point['h']*(q[6]-q[0]);y=q[1]+point['x']/point['w']*(q[3]-q[1])+point['y']/point['h']*(q[7]-q[1])
  check=await asyncio.wait_for(cdp.send('Runtime.evaluate',{'expression':'(()=>{const e=document.elementFromPoint('+str(x)+','+str(y)+');return e?.tagName==="IFRAME"})()','returnByValue':True,'timeout':3000}),6)
  if check.get('result',{}).get('value') is not True:raise SourcingError('内嵌页面被其他界面遮挡；未点击')
 finally:
  await asyncio.wait_for(cdp.detach(),2)
 await session._act('click',lambda:session.page.mouse.click(x,y))
