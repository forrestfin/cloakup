"""Operator safeguards: domain allowlist, confirmation for risky clicks, secrets."""

from __future__ import annotations

import asyncio
import re
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse

SENSITIVE = re.compile(
    r"(支付|付款|购买|立即购买|下单|提交订单|确认订单|结算|转账|充值|删除|注销|退订|发送|发布|提交申请|"
    r"\bpay\b|purchase|buy now|place order|checkout|confirm order|delete|transfer|unsubscribe|send|publish|submit)",
    re.I,
)
SECRET_REF = re.compile(r"\{\{secret:([A-Za-z0-9_]+)\}\}")

ConfirmFn = Callable[[str], Awaitable[bool]]
AskFn = Callable[[str], Awaitable[str]]


async def terminal_confirm(prompt: str) -> bool:
    answer = await asyncio.to_thread(input, f"\n⚠️  {prompt}\n确认执行? [y/N] ")
    return answer.strip().lower() in ("y", "yes")


async def terminal_ask(question: str) -> str:
    return await asyncio.to_thread(input, f"\n🙋 代理请求协助: {question}\n> ")


class Guard:
    def __init__(self, allowed_domains: list[str], confirm_sensitive: bool,
                 secrets: dict[str, str], confirm: ConfirmFn = terminal_confirm) -> None:
        self.allowed = [d.lower().lstrip(".") for d in allowed_domains]
        self.confirm_sensitive = confirm_sensitive
        self.secrets = secrets
        self._confirm = confirm

    def check_url(self, url: str) -> Optional[str]:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return f"只允许 http/https URL,拒绝: {url}"
        if not self.allowed:
            return None
        host = (parsed.hostname or "").lower()
        if any(host == d or host.endswith("." + d) for d in self.allowed):
            return None
        return f"域名 {host} 不在允许列表 {self.allowed} 中"

    async def approve_click(self, label: str) -> bool:
        if self.confirm_sensitive and SENSITIVE.search(label or ""):
            return await self._confirm(f'代理想点击 "{label}"(疑似支付/下单/删除等不可逆操作)')
        return True

    def resolve_secrets(self, text: str) -> str:
        def sub(m: re.Match) -> str:
            name = m.group(1)
            if name not in self.secrets:
                raise KeyError(f"未配置的 secret: {name}")
            return self.secrets[name]

        return SECRET_REF.sub(sub, text)
