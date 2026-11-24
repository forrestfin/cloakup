"""Actual browser input/click/readback contract on a network-free fixture."""
import asyncio,json
from types import SimpleNamespace as NS
from playwright.async_api import async_playwright
from cloakup.browser import BrowserSession
from cloakup.config import BrowserConfig,PacingConfig
from cloakup.executor_check import verify,url

async def main():
 async with async_playwright() as pw:
  browser=await pw.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
  page=await browser.new_page();page._stealth_world=NS(evaluate=page.evaluate,invalidate=lambda:None)
  await page.goto(url())
  session=BrowserSession(BrowserConfig(),PacingConfig(think_min=0,think_median=.01,think_max=.01,min_interval=0))
  session.context=page.context;session.page=page
  result=await verify(session)
  assert await page.locator('#executor-input').input_value()=='CLOAKUP_READY'
  assert await page.locator('#executor-count').text_content()=='1'
  print(json.dumps(result,ensure_ascii=False));await browser.close()
asyncio.run(main())
