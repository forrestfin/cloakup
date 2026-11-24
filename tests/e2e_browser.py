"""End-to-end check against a local page with the real CloakBrowser binary (no LLM).

Run:  .venv/bin/python tests/e2e_browser.py [--headed]
First run downloads the CloakBrowser Chromium (~200MB) into ~/.cloakbrowser.
"""

from __future__ import annotations

import asyncio
import http.server
import sys
import tempfile
import threading
import time
from functools import partial
from pathlib import Path

from cloakup.browser import BrowserSession
from cloakup.config import BrowserConfig, PacingConfig

PAGE1 = """<!doctype html><html><head><meta charset="utf-8"><title>Test Shop</title></head><body>
<h1>Test Shop</h1>
<form onsubmit="event.preventDefault(); document.getElementById('out').textContent='searched:'+q.value+'|'+cat.value">
  <label for="q">关键词</label><input id="q" name="q" placeholder="搜索商品">
  <select id="cat"><option value="all">全部</option><option value="kb">键盘</option><option value="ms">鼠标</option></select>
  <button type="submit">搜索</button>
</form>
<div id="out"></div>
<button id="buy" onclick="document.getElementById('out').textContent='BOUGHT'">立即购买</button>
<div style="height:2200px;background:linear-gradient(#fff,#ddd)">长内容区域</div>
<a href="/page2.html">下一页</a>
<script>
  // What a detection script would see:
  window.__wd = navigator.webdriver;
  window.__mutations = 0;
  window.__mlog=[]; new MutationObserver(m => { window.__mutations += m.length; m.forEach(r=>window.__mlog.push([r.type, r.target.nodeName, [...r.addedNodes].map(n=>n.nodeName+":"+(n.textContent||"").slice(0,30)), r.attributeName])); })
    .observe(document, {subtree: true, attributes: true, childList: true});
  window.__trusted = []; document.addEventListener('click', e => window.__trusted.push(e.isTrusted), true);
  window.__moves = 0; document.addEventListener('mousemove', () => window.__moves++, true);
</script>
</body></html>"""

PAGE2 = "<!doctype html><meta charset=\"utf-8\"><title>Page 2</title><h1>第二页</h1><p>到达第二页</p>"


def serve(root: Path) -> tuple[http.server.ThreadingHTTPServer, str]:
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def by_label(obs, text: str) -> int:
    for el in obs.elements:
        if text in el.label:
            return el.index
    raise AssertionError(f"element {text!r} not found in observation")


async def main(headed: bool) -> None:
    root = Path(tempfile.mkdtemp(prefix="cloakup-e2e-"))
    (root / "index.html").write_text(PAGE1, encoding="utf-8")
    (root / "page2.html").write_text(PAGE2, encoding="utf-8")
    srv, base = serve(root)
    fast = PacingConfig(think_median=0.3, think_min=0.1, think_max=0.6, read_min=0.5, read_max=1.0)
    s = BrowserSession(BrowserConfig(profile_dir=str(root / "profile"), headless=not headed), fast)
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= cond
        print(f"{'✅' if cond else '❌'} {name}" + (f"  ({detail})" if detail else ""))

    t0 = time.time()
    await s.start()
    print(f"browser up in {time.time() - t0:.1f}s")
    try:
        await s.navigate(base + "/index.html")
        # The parser itself may record a mutation or two while loading; only
        # mutations caused by our observation count.
        m0 = await s.page.evaluate("window.__mutations")
        obs = await s.observe(screenshot=True)
        check("observe in isolated world", len(obs.elements) >= 5, f"{len(obs.elements)} elements")
        check("screenshot", bool(obs.screenshot) and len(obs.screenshot) > 1000, f"{len(obs.screenshot or b'')} bytes")
        check("navigator.webdriver hidden", await s.page.evaluate("window.__wd") is False)
        check("next-page link flagged below viewport",
              any(e.label == "下一页" and e.raw["pos"] == 1 for e in obs.elements))
        m1 = await s.page.evaluate("window.__mutations")
        check("observation caused no DOM mutations", m1 == m0,
              f"{m1 - m0} new: {await s.page.evaluate('window.__mlog.slice(' + str(m0) + ')')}" if m1 != m0 else "")

        t = time.time()
        await s.type_text(by_label(obs, "关键词"), "机械键盘", clear=True)
        check("type_text (humanized fill)", await s.page.input_value("#q") == "机械键盘", f"{time.time() - t:.1f}s")

        obs = await s.observe(False)
        await s.select_option(by_label(obs, "全部"), "键盘")
        check("select_option", await s.page.eval_on_selector("#cat", "e => e.value") == "kb")

        obs = await s.observe(False)
        await s.click(by_label(obs, "搜索"))
        out = await s.page.text_content("#out")
        check("click submit", out == "searched:机械键盘|kb", out or "")

        trusted = await s.page.evaluate("window.__trusted")
        moves = await s.page.evaluate("window.__moves")
        check("clicks are trusted events", bool(trusted) and all(trusted), str(trusted))
        check("mouse travelled (Bezier path)", moves > 10, f"{moves} mousemove events")

        y0 = await s.page.evaluate("scrollY")
        await s.scroll("down", "page")
        y1 = await s.page.evaluate("scrollY")
        check("wheel scroll", y1 > y0 + 200, f"{y0} -> {y1}")

        obs = await s.observe(False)
        await s.click(by_label(obs, "下一页"))
        check("click link -> navigation + dwell", s.page.url.endswith("/page2.html"), s.page.url)

        await s.go_back()
        check("go_back", s.page.url.endswith("/index.html"), s.page.url)
    finally:
        await s.close()
        srv.shutdown()
    print("\nALL PASSED" if ok else "\nSOME CHECKS FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main("--headed" in sys.argv))
