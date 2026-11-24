"""Tool schemas exposed to Claude and the executor that maps them onto the browser."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .browser import BrowserSession
from .guard import AskFn, Guard, terminal_ask
from .locate import describe

log = logging.getLogger("cloakup")


def _tool(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "input_schema": {"type": "object", "properties": props, "required": required,
                         "additionalProperties": False},
    }


EID = {"type": "integer", "description": "Index shown in [brackets] in the latest observation."}

TOOLS: list[dict[str, Any]] = [
    _tool("click", "Click an element from the latest observation. The browser moves the mouse "
          "along a human path, scrolling it into view first if needed.",
          {"element_id": EID}, ["element_id"]),
    _tool("type_text", "Type into an input/textarea/contenteditable. clear=true replaces the current "
          "value. submit=true presses Enter afterwards. Use {{secret:NAME}} placeholders for "
          "passwords or other credentials instead of literal values.",
          {"element_id": EID, "text": {"type": "string"},
           "clear": {"type": "boolean"}, "submit": {"type": "boolean"}},
          ["element_id", "text"]),
    _tool("select_option", "Choose an option of a native <select> by its visible label or value.",
          {"element_id": EID, "option": {"type": "string"}}, ["element_id", "option"]),
    _tool("hover", "Move the mouse over an element, e.g. to open a hover menu.",
          {"element_id": EID}, ["element_id"]),
    _tool("scroll", "Scroll the page with the mouse wheel. Use this to read further or to reveal "
          "elements marked ↓ / ↑.",
          {"direction": {"type": "string", "enum": ["down", "up"]},
           "amount": {"type": "string", "enum": ["small", "half", "page"]}},
          ["direction", "amount"]),
    _tool("press_key", "Press a single key or chord, e.g. Enter, Escape, Tab, ArrowDown, Control+A.",
          {"key": {"type": "string"}}, ["key"]),
    _tool("click_at", "Click at viewport coordinates (CSS px) read from the screenshot. Only for "
          "targets missing from the element list (canvas, custom widgets).",
          {"x": {"type": "number"}, "y": {"type": "number"}}, ["x", "y"]),
    _tool("navigate", "Open a URL in the current tab. Prefer clicking links and using the site's "
          "own search the way a visitor would; navigate directly only to start or when no link exists.",
          {"url": {"type": "string"}}, ["url"]),
    _tool("go_back", "Browser back button.", {}, []),
    _tool("wait", "Wait for content to load or an animation to finish (1-30 s).",
          {"seconds": {"type": "number"}}, ["seconds"]),
    _tool("ask_human", "Hand control to the human operator and wait for their reply. Call this for "
          "CAPTCHAs / human-verification challenges, logins or 2FA codes you don't have, "
          "ambiguous instructions, or before anything irreversible the task didn't clearly authorize.",
          {"question": {"type": "string"}}, ["question"]),
    _tool("save_data", "Record a piece of extracted information under a key; it is returned in the "
          "final result.",
          {"key": {"type": "string"}, "value": {"description": "Any JSON value."}}, ["key", "value"]),
    _tool("done", "Finish the task. Call exactly once, when the task is complete or cannot be completed. "
          "If an output schema was given, put the final structured result in `result`.",
          {"success": {"type": "boolean"}, "summary": {"type": "string"},
           "result": {"description": "Final result object matching the output schema, when one was given."}},
          ["success", "summary"]),
]

# Tools after which there is nothing new on the page worth observing.
NO_OBSERVE = {"save_data", "done"}


@dataclass
class ToolOutcome:
    text: str
    is_error: bool = False
    observe: bool = True
    finished: bool = False
    success: bool = False
    # What happened, for the trace the recipe compiler learns from.
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Executor:
    browser: BrowserSession
    guard: Guard
    ask: AskFn = terminal_ask
    data: dict[str, Any] = field(default_factory=dict)

    async def run(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        page = getattr(self.browser, "page", None)
        detail: dict[str, Any] = {"url_before": getattr(page, "url", None)}
        if "element_id" in args and getattr(self.browser, "last_obs", None) is not None:
            el = self.browser.last_obs.find(int(args["element_id"]))
            if el is not None:
                detail["element"] = describe(el)
        try:
            out = await self._dispatch(name, args)
            page = getattr(self.browser, "page", None)
            out.detail = {**detail, "url_after": getattr(page, "url", None), **out.detail}
            return out
        except KeyError as exc:
            return ToolOutcome(f"参数错误: {exc}", is_error=True)
        except Exception as exc:  # Playwright timeouts, stale selectors, ...
            msg = str(exc).splitlines()[0][:300]
            return ToolOutcome(f"{name} 失败: {type(exc).__name__}: {msg}。页面可能已变化,请查看最新观察后重试或换一种方式。",
                               is_error=True)

    async def _dispatch(self, name: str, a: dict[str, Any]) -> ToolOutcome:
        b = self.browser
        if name == "click":
            _, el = b.element(int(a["element_id"]))
            href=el.raw.get('href')
            if href and not href.lower().startswith('javascript:'):
                from urllib.parse import urljoin
                error=self.guard.check_url(urljoin(getattr(b.page,'url','') if getattr(b,'page',None) else '',href))
                if error:return ToolOutcome(error,is_error=True)
            if not await self.guard.approve_click(el.label):
                return ToolOutcome("操作员拒绝了这次点击。不要重试同一操作;换个方案或调用 done。", is_error=True)
            return ToolOutcome(await b.click(int(a["element_id"])))
        if name == "type_text":
            text = self.guard.resolve_secrets(str(a["text"]))
            return ToolOutcome(await b.type_text(int(a["element_id"]), text,
                                                 clear=bool(a.get("clear", True)),
                                                 submit=bool(a.get("submit", False))))
        if name == "select_option":
            return ToolOutcome(await b.select_option(int(a["element_id"]), str(a["option"])))
        if name == "hover":
            return ToolOutcome(await b.hover(int(a["element_id"])))
        if name == "scroll":
            return ToolOutcome(await b.scroll(str(a["direction"]), str(a["amount"])))
        if name == "press_key":
            return ToolOutcome(await b.press_key(str(a["key"])))
        if name == "click_at":
            if not await self.guard.approve_click(f"坐标 ({a['x']},{a['y']}) 处的元素"):
                return ToolOutcome("操作员拒绝了这次点击。", is_error=True)
            return ToolOutcome(await b.click_at(float(a["x"]), float(a["y"])))
        if name == "navigate":
            url = str(a["url"])
            err = self.guard.check_url(url)
            if err:
                return ToolOutcome(err, is_error=True)
            return ToolOutcome(await b.navigate(url))
        if name == "go_back":
            return ToolOutcome(await b.go_back())
        if name == "wait":
            return ToolOutcome(await b.wait(float(a["seconds"])))
        if name == "ask_human":
            reply = await self.ask(str(a["question"]))
            return ToolOutcome(f"操作员回复: {reply or '(无回复,已继续)'}")
        if name == "save_data":
            self.data[str(a["key"])] = a["value"]
            return ToolOutcome(f"已保存 {a['key']}", observe=False)
        if name == "done":
            return ToolOutcome("任务结束", observe=False, finished=True, success=bool(a["success"]))
        return ToolOutcome(f"未知工具: {name}", is_error=True)
