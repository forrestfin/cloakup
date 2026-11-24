"""Network-free iframe coordinate contract; uses native pointer, never JS click."""
import asyncio
from types import SimpleNamespace as NS
from playwright.async_api import async_playwright
from cloakup.frame_actions import click
from cloakup.dom_reader import read_frame_dom
from cloakup.sourcing import SourcingError

async def main():
 async with async_playwright() as p:
  b=await p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True);page=await b.new_page(viewport={'width':1000,'height':800})
  await page.set_content('<iframe style="margin:40px;width:600px;height:400px" srcdoc="<button id=mode onclick=\'this.dataset.count=Number(this.dataset.count||0)+1\'>按职位设置招呼语</button>"></iframe>')
  frame=page.frames[1]
  async def act(kind,fn):assert kind=='click';await fn()
  session=NS(page=page,context=page.context,_act=act)
  await click(session,frame,'#mode');assert await read_frame_dom(page,frame,"document.querySelector('#mode').dataset.count")=='1'
  await page.evaluate("document.body.insertAdjacentHTML('beforeend','<div style=\"position:fixed;inset:0;background:white\"></div>')")
  try:await click(session,frame,'#mode');raise AssertionError('overlay must block')
  except SourcingError:pass
  assert await read_frame_dom(page,frame,"document.querySelector('#mode').dataset.count")=='1'
  await b.close();print('iframe native pointer: correct frame coordinates, one click, overlay refusal passed')
asyncio.run(main())
