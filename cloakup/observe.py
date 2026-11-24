"""Page observation: turn the live DOM into a compact, indexed element list.

The script runs in CloakBrowser's CDP *isolated world* (``page._stealth_world``),
the same one its humanize engine uses. It shares the DOM with the page but has
its own JS globals, so the page's scripts can't see our registry, can't hook
our function calls, and we never add attributes or nodes to the DOM —
observation leaves no trace a MutationObserver or a patched prototype could
catch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

OBSERVE_JS = r"""
(() => {
  const MAX = __MAX__;
  const vw = window.innerWidth, vh = window.innerHeight;
  const SEL = [
    'a[href]', 'button', 'input:not([type=hidden])', 'select', 'textarea', 'summary',
    '[role=button]', '[role=link]', '[role=checkbox]', '[role=radio]', '[role=tab]',
    '[role=menuitem]', '[role=option]', '[role=switch]', '[role=combobox]',
    '[role=textbox]', '[role=searchbox]', '[contenteditable=""]', '[contenteditable=true]',
    '[onclick]', '[tabindex]:not([tabindex="-1"])', '.tab-item'
  ].join(',');
  const idOk = (id) => /^[A-Za-z][\w-]*$/.test(id);
  function cssPath(el) {
    if (el.id && idOk(el.id) && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1)
      return '#' + CSS.escape(el.id);
    const parts = [];
    let n = el;
    while (n && n.nodeType === 1 && n !== document.documentElement) {
      if (n !== el && n.id && idOk(n.id) && document.querySelectorAll('#' + CSS.escape(n.id)).length === 1) {
        parts.unshift('#' + CSS.escape(n.id));
        break;
      }
      let p = n.tagName.toLowerCase();
      const par = n.parentElement;
      if (par) {
        const same = Array.from(par.children).filter(c => c.tagName === n.tagName);
        if (same.length > 1) p += ':nth-of-type(' + (same.indexOf(n) + 1) + ')';
      }
      parts.unshift(p);
      n = par;
    }
    return parts.join(' > ');
  }
  function shown(el) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return null;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none' || parseFloat(s.opacity) === 0) return null;
    return r;
  }
  const clip = (t, n) => { t = (t || '').replace(/\s+/g, ' ').trim(); return t.length > n ? t.slice(0, n) + '…' : t; };
  function label(el) {
    const tag = el.tagName.toLowerCase();
    let lab = el.getAttribute('aria-label') || '';
    if (!lab && el.labels && el.labels.length) lab = el.labels[0].innerText;
    if (!lab && (tag === 'input' || tag === 'textarea')) lab = el.placeholder || el.name || '';
    if (!lab) lab = el.innerText || '';
    if (!lab) lab = el.title || el.getAttribute('alt') || '';
    if (!lab) { const img = el.querySelector && el.querySelector('img[alt]'); if (img) lab = img.alt; }
    return clip(lab, 80);
  }
  const nodes = Array.from(document.querySelectorAll(SEL));
  // Vue/custom-component UIs often expose neither native controls nor roles.
  // Include the smallest visible text-bearing pointer targets, avoiding large
  // cards that mix a harmless detail link with edit/close/contact actions.
  const pointer = el => {
    const r = el.getBoundingClientRect();
    return r.width >= 2 && r.height >= 2 && getComputedStyle(el).cursor === 'pointer'
      && (el.innerText || '').trim().length > 0;
  };
  for (const el of document.querySelectorAll('div,span,li,p,[class]')) {
    if (!pointer(el) || (el.innerText || '').trim().length > 180) continue;
    if (Array.from(el.children).some(pointer)) continue;
    nodes.push(el);
  }
  const items = [];
  const seen = new Set();
  for (const el of nodes) {
    if (seen.has(el)) continue;
    // Skip elements nested inside an already-listed interactive ancestor
    // (e.g. <span> inside <a>) to keep the list short.
    let p = el.parentElement, nested = false;
    while (p) { if (seen.has(p)) { nested = true; break; } p = p.parentElement; }
    if (nested) continue;
    const r = shown(el);
    if (!r) continue;
    seen.add(el);
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const inView = r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw;
    const it = {
      tag, type, role: el.getAttribute('role') || '', label: label(el), css: cssPath(el),
      x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2),
      w: Math.round(r.width), h: Math.round(r.height),
      pos: inView ? 0 : (r.top >= vh ? 1 : -1),
      disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'),
    };
    if (tag === 'a') it.href = clip(el.getAttribute('href'), 80);
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
      if (el.maxLength > 0) it.max_length = el.maxLength;
      it.value = type === 'password' ? (el.value ? '••••' : '') : clip(el.value, tag === 'textarea' ? __TEXT__ : 60);
      if (type === 'checkbox' || type === 'radio') it.checked = el.checked;
    }
    if (tag === 'select') it.options = Array.from(el.options).slice(0, 25).map(o => clip(o.text, 30));
    items.push(it);
  }
  // In-viewport elements first, then those just below / above.
  items.sort((a, b) => (Math.abs(a.pos) - Math.abs(b.pos)) || (a.pos - b.pos));
  const elements = items.slice(0, MAX);
  const frames = Array.from(document.querySelectorAll('iframe')).map(f => f.src || '').filter(Boolean);
  const challenge = frames.filter(s => /captcha|challenges\.cloudflare|turnstile|hcaptcha|geetest|arkoselabs|funcaptcha/i.test(s));
  const bodyText = document.body ? document.body.innerText : '';
  return {
    url: location.href, title: document.title,
    vw, vh, scrollY: Math.round(window.scrollY),
    scrollH: Math.round(document.documentElement.scrollHeight),
    elements, total: items.length,
    frames: frames.length, challenge,
    text: clip(bodyText, __TEXT__), textLen: bodyText.length,
  };
})()
"""


@dataclass
class Element:
    index: int
    tag: str
    css: str
    label: str
    x: int
    y: int
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Observation:
    url: str
    title: str
    elements: list[Element]
    raw: dict[str, Any]
    screenshot: Optional[bytes] = None
    notes: list[str] = field(default_factory=list)

    @property
    def text_len(self) -> int:
        return int(self.raw.get("textLen", 0))

    def find(self, index: int) -> Optional[Element]:
        if 0 <= index < len(self.elements):
            return self.elements[index]
        return None

    def render(self, mask: Optional[dict[str, str]] = None) -> str:
        r = self.raw
        lines = [
            f"URL: {r['url']}",
            f"标题: {r['title']}",
            f"视口: {r['vw']}x{r['vh']} | 滚动位置: {r['scrollY']}/{r['scrollH']}",
        ]
        if r.get("challenge"):
            lines.append("⚠️ 检测到人机验证/验证码 iframe: " + ", ".join(r["challenge"][:3])
                         + " —— 不要尝试自动破解,调用 ask_human 交给操作员处理。")
        for n in self.notes:
            lines.append(f"ℹ️ {n}")
        shown = len(self.elements)
        lines.append(f"\n可交互元素(共 {r['total']} 个,列出 {shown} 个;↓=在视口下方,↑=在视口上方):")
        for el in self.elements:
            e = el.raw
            tag = e["tag"] + (f"[type={e['type']}]" if e.get("type") else "")
            if e.get("role"):
                tag += f" role={e['role']}"
            parts = [f"[{el.index}]", tag]
            if e.get("frame_url"):
                parts.append("iframe=" + e["frame_url"])
            if el.label:
                parts.append(f'"{el.label}"')
            if "value" in e:
                parts.append(f'value="{e["value"]}"')
            if "checked" in e:
                parts.append("☑" if e["checked"] else "☐")
            if e.get("options"):
                parts.append("options=" + "|".join(e["options"]))
            if e.get("href"):
                parts.append(f"href={e['href']}")
            if e.get("disabled"):
                parts.append("(disabled)")
            if e["pos"] == 1:
                parts.append("↓")
            elif e["pos"] == -1:
                parts.append("↑")
            lines.append(" ".join(parts))
        lines.append("\n页面文本摘录:\n" + (r.get("text") or "(空)"))
        out = "\n".join(lines)
        for name, value in (mask or {}).items():
            if value:
                out = out.replace(value, "{{secret:%s}}" % name)
        return out


def build_observation(raw: dict[str, Any]) -> Observation:
    elements = [
        Element(index=i, tag=e["tag"], css=e["css"], label=e.get("label", ""),
                x=e["x"], y=e["y"], raw=e)
        for i, e in enumerate(raw.get("elements", []))
    ]
    return Observation(url=raw["url"], title=raw.get("title", ""), elements=elements, raw=raw)


def observe_script(max_elements: int, text_limit: int = 3500) -> str:
    return OBSERVE_JS.replace("__MAX__", str(int(max_elements))).replace("__TEXT__", str(int(text_limit)))
