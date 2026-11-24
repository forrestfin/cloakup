"""Browser session on CloakBrowser: launch, observe, and act like a person.

Every pointer/keyboard action goes through CloakBrowser's humanized Playwright
API (``humanize=True``): locator clicks scroll with wheel bursts, travel along
Bezier curves, aim at a realistic point and pass a hit-target check; typing has
per-key timing and occasional self-corrected typos. This module adds what the
humanize layer doesn't decide: *when* to act (via ``Pacer``), dwelling on new
pages, idle mouse drift, and free scrolling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from typing import Any, Optional

from .config import BrowserConfig, PacingConfig
from .observe import Observation, build_observation, observe_script
from .pacing import Pacer, lognormal

log = logging.getLogger("cloakup")

ACTION_TIMEOUTS = {"click":45,"type":60,"navigate":60}
WHEEL_NOTCH_PX = 100


class BrowserSession:
    def __init__(self, cfg: BrowserConfig, pacing: PacingConfig, max_elements: int = 220) -> None:
        self.cfg = cfg
        self.pacer = Pacer(pacing)
        self.max_elements = max_elements
        self.context: Any = None
        self.page: Any = None
        self.last_obs: Optional[Observation] = None
        self._notes: list[str] = []
        self._observed_frames: dict[str, Any] = {}

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        from cloakbrowser import launch_persistent_context_async

        kwargs: dict[str, Any] = dict(
            headless=self.cfg.headless,
            humanize=True,
            human_preset=self.cfg.human_preset,
            # Bound pointer sampling overhead in remote/headed Docker sessions.
            # Keep the native humanized trajectory and timing, with fewer frames.
            human_config={"mouse_max_steps":24,"idle_between_actions":False},
        )
        if self.cfg.proxy:
            kwargs["proxy"] = self.cfg.proxy
            kwargs["geoip"] = self.cfg.geoip
        if self.cfg.locale:
            kwargs["locale"] = self.cfg.locale
        if self.cfg.timezone:
            kwargs["timezone"] = self.cfg.timezone
        if self.cfg.license_key:
            kwargs["license_key"] = self.cfg.license_key
        if self.cfg.extra_args:
            kwargs["args"] = list(self.cfg.extra_args)

        self.context = await launch_persistent_context_async(self.cfg.profile_dir, **kwargs)
        self.context.on("page", self._on_new_page)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        if '--restore-last-session' in self.cfg.extra_args:
            for _ in range(10):
                restored = [p for p in self.context.pages if '://www.zhipin.com/' in p.url]
                if restored:
                    self.page = restored[0]
                    break
                await asyncio.sleep(.2)

    async def close(self) -> None:
        if self.context is not None:
            try:
                await self.context.close()
            except Exception:  # browser may already be gone
                pass
            self.context = None

    def _on_new_page(self, page: Any) -> None:
        # target=_blank links and popups: follow them like a person would.
        self.page = page
        self._notes.append("打开了新标签页,已切换到新标签页。")

    # ---------------------------------------------------------------- observation
    async def observe(self, screenshot: bool = True, text_limit: int = 3500) -> Observation:
        page = self.page
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        world = getattr(page, "_stealth_world", None)
        if world is None:
            # New tab not humanized yet -- give the context patch a moment.
            await asyncio.sleep(0.5)
            world = getattr(page, "_stealth_world", None)
        if world is None:
            raise RuntimeError("isolated world unavailable (humanize layer not attached)")
        from .dom_reader import read_dom
        raw = await read_dom(page,observe_script(self.max_elements,text_limit),timeout=15)
        self._observed_frames = {}
        # Business apps often keep navigation in the main page and their actual
        # form/list in a same-origin iframe. Reuse CloakBrowser's isolated worlds
        # so observing child frames follows the same no-DOM-mutation contract.
        human = getattr(world, "_human", None)
        if human is not None:
            for i, frame in enumerate(getattr(page, "frames", [])[:9]):
                if frame == page.main_frame or re.search(r"captcha|challenges\.cloudflare|turnstile|hcaptcha", frame.url, re.I):
                    continue
                try:
                    child = await asyncio.wait_for(human.worlds.evaluate(frame._impl_obj, observe_script(self.max_elements, text_limit)), timeout=10)
                    key = f"frame_{i}"
                    self._observed_frames[key] = frame
                    for el in child.get("elements", []):
                        el.update(frame_key=key, frame_url=frame.url)
                    raw["elements"].extend(child.get("elements", []))
                    raw["total"] += child.get("total", 0)
                    raw["textLen"] += child.get("textLen", 0)
                    raw["text"] += f"\n[内嵌页面 {frame.url}]\n" + child.get("text", "")
                    raw["challenge"].extend(child.get("challenge", []))
                except Exception as exc:
                    self._notes.append(f"内嵌页面暂不可读: {frame.url} ({type(exc).__name__})")
        raw["elements"] = raw["elements"][: self.max_elements]
        raw["text"] = raw["text"][:text_limit]
        obs = build_observation(raw)
        obs.notes, self._notes = self._notes, []
        if screenshot:
            try:
                # caret="initial": Playwright's default caret hiding writes inline
                # styles onto inputs, a DOM mutation page scripts can observe.
                obs.screenshot = await page.screenshot(type="jpeg", quality=55, scale="css", caret="initial",
                                                       animations="allow")
            except Exception as exc:
                log.debug("screenshot failed: %s", exc)
        self.last_obs = obs
        return obs

    async def text_contains(self, needle: str) -> bool:
        """Whole-page text search, in the isolated world (no 3,500-char cap)."""
        expr = f"(document.body ? document.body.innerText : '').includes({json.dumps(needle)})"
        if bool(await asyncio.wait_for(self.page._stealth_world.evaluate(expr), timeout=10)):
            return True
        human = getattr(self.page._stealth_world, "_human", None)
        if human is not None:
            for frame in getattr(self.page, "frames", []):
                if frame == self.page.main_frame:
                    continue
                try:
                    if await asyncio.wait_for(human.worlds.evaluate(frame._impl_obj, expr), timeout=5):
                        return True
                except Exception:
                    continue
        return False

    async def idle(self) -> None:
        """A small unprompted mouse drift, so replays don't move in lockstep."""
        await self._idle_move()

    def element(self, index: int) -> Any:
        """Locator for an element from the latest observation."""
        if self.last_obs is None:
            raise ValueError("no observation yet")
        el = self.last_obs.find(index)
        if el is None:
            raise ValueError(f"element [{index}] not in the latest observation")
        key = el.raw.get("frame_key")
        owner = self._observed_frames.get(key) if key else self.page
        if owner is None:
            raise ValueError("目标内嵌页面已变化，请重新观察")
        return owner.locator(el.css).first, el

    # -------------------------------------------------------------------- actions
    async def _settle(self, url_before: str) -> None:
        """Wait for the result of an action; dwell on the page if it navigated."""
        await asyncio.sleep(random.uniform(0.4, 1.0))
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        if self.page.url != url_before:
            try:
                await self.page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:
                pass
            await self.dwell_on_page()

    async def dwell_on_page(self) -> None:
        """Spend a human amount of time 'reading' a fresh page, drifting the mouse."""
        try:
            text_len = await asyncio.wait_for(
                self.page._stealth_world.evaluate("document.body ? document.body.innerText.length : 0"),
                timeout=5,
            )
        except Exception:
            text_len = 500
        total = self.pacer.reading_time(int(text_len or 0))
        remaining = total
        while remaining > 0:
            chunk = min(remaining, lognormal(1.6, 0.4, 0.6, 3.5))
            await asyncio.sleep(chunk)
            remaining -= chunk
            if remaining > 0 and random.random() < self.pacer.cfg.idle_move_chance:
                await self._idle_move()

    async def _idle_move(self) -> None:
        vp = self.page.viewport_size or {"width": 1280, "height": 720}
        x = random.uniform(0.15, 0.85) * vp["width"]
        y = random.uniform(0.2, 0.8) * vp["height"]
        try:
            await asyncio.wait_for(self.page.mouse.move(x, y), timeout=3)  # humanized; idle drift must never block work
        except Exception:
            pass

    async def _act(self, kind: str, fn) -> None:
        started=time.monotonic()
        await self.pacer.before_action(kind)
        native_started=time.monotonic();url_before=self.page.url
        completed=False
        try:
            await asyncio.wait_for(fn(),timeout=ACTION_TIMEOUTS.get(kind,45))
            completed=True
        finally:
            self.pacer.mark_action()
            self.last_action_timing={'kind':kind,'pacing_ms':round((native_started-started)*1000),
                'native_ms':round((time.monotonic()-native_started)*1000),'completed':completed}
        settle_started=time.monotonic()
        await self._settle(url_before)
        self.last_action_timing['settle_ms']=round((time.monotonic()-settle_started)*1000)

    async def click(self, index: int) -> str:
        loc, el = self.element(index)
        await self._act("click", lambda: loc.click(timeout=15000))
        return f'clicked [{index}] "{el.label}"'

    async def hover(self, index: int) -> str:
        loc, el = self.element(index)
        await self._act("hover", lambda: loc.hover(timeout=15000))
        return f'hovered [{index}] "{el.label}"'

    async def type_text(self, index: int, text: str, clear: bool = True, submit: bool = False) -> str:
        loc, el = self.element(index)

        async def go() -> None:
            if clear:
                await loc.fill(text, timeout=20000)  # humanized: clears, then types per key
            else:
                await loc.click(timeout=15000)
                await self.page.keyboard.type(text)
            if submit:
                await asyncio.sleep(lognormal(0.5, 0.4, 0.2, 1.5))
                await self.page.keyboard.press("Enter")

        await self._act("type", go)
        return f'typed into [{index}] "{el.label}"' + (" and pressed Enter" if submit else "")

    async def select_option(self, index: int, option: str) -> str:
        loc, el = self.element(index)

        async def go() -> None:
            try:
                await loc.select_option(label=option, timeout=10000)
            except Exception:
                await loc.select_option(value=option, timeout=10000)

        await self._act("select", go)
        return f'selected "{option}" in [{index}]'

    async def press_key(self, key: str) -> str:
        await self._act("key", lambda: self.page.keyboard.press(key))
        return f"pressed {key}"

    async def click_at(self, x: float, y: float) -> str:
        await self._act("click", lambda: self.page.mouse.click(x, y))
        return f"clicked at ({x:.0f},{y:.0f})"

    async def scroll(self, direction: str, amount: str) -> str:
        vp = self.page.viewport_size or {"width": 1280, "height": 720}
        frac = {"small": 0.3, "half": 0.5, "page": 0.85}.get(amount, 0.5)
        px = vp["height"] * frac * random.uniform(0.85, 1.15)
        sign = 1 if direction == "down" else -1

        async def go() -> None:
            # Pointer over the content first: wheel events go where the mouse is.
            await self.page.mouse.move(random.uniform(0.3, 0.7) * vp["width"],
                                       random.uniform(0.35, 0.65) * vp["height"])
            notches = max(1, round(px / WHEEL_NOTCH_PX))
            for i in range(notches):
                await self.page.mouse.wheel(0, sign * WHEEL_NOTCH_PX)
                # accelerate -> cruise -> decelerate
                edge = min(i, notches - 1 - i)
                await asyncio.sleep(random.uniform(0.05, 0.09) if edge >= 2 else random.uniform(0.09, 0.18))

        await self._act("scroll", go)
        return f"scrolled {direction} ({amount})"

    async def navigate(self, url: str) -> str:
        async def go() -> None:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=45000)

        await asyncio.wait_for(self._act("navigate", go), timeout=75)  # include humanized wrapper and settle
        return f"navigated to {self.page.url}"

    async def go_back(self) -> str:
        await self._act("navigate", lambda: self.page.go_back(wait_until="domcontentloaded", timeout=30000))
        return f"went back to {self.page.url}"

    async def wait(self, seconds: float) -> str:
        await asyncio.sleep(max(0.5, min(30.0, seconds)))
        return f"waited {seconds:.1f}s"
