"""Claude client with graceful degradation for Anthropic-compatible gateways.

Configuration (environment, or the matching CLI flags):

* ``ANTHROPIC_BASE_URL``   gateway address, e.g. ``https://llm.internal.example.com``
* ``ANTHROPIC_API_KEY``    key sent as ``x-api-key``  — or —
  ``ANTHROPIC_AUTH_TOKEN`` key sent as ``Authorization: Bearer``
* ``CLOAKUP_MODEL``        model name as the gateway knows it (default ``claude-opus-5-5``)
* ``CLOAKUP_API_COMPAT``   ``auto`` (default) | ``full`` | ``basic`` | ``minimal``

Compatibility levels, highest first:

``full``    beta Messages endpoint with server-side context editing, server-side
            fallback and automatic prompt caching (official API).
``basic``   GA ``/v1/messages`` with ``output_config`` (effort, structured outputs)
            and a cached system prompt.
``minimal`` GA ``/v1/messages`` with only model / system / tools / messages.

In ``auto`` mode a request that the gateway rejects (400/404/422) is retried one
level lower; when the lower level succeeds the client stays there for the rest
of the process, so the probe costs at most one failed request per level.

Many gateways silently *ignore* fields they don't know instead of rejecting
them, so a rejection-based probe would wrongly report ``full``. ``auto``
therefore starts at ``full`` only for the official API host and at ``basic``
for any other base URL. Likewise, once a structured-output answer comes back
not matching its schema, structured outputs are treated as unsupported.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import anthropic

log = logging.getLogger("cloakup")

LEVELS = ("full", "basic", "minimal")
DEFAULT_MODEL = "claude-opus-5-5"
FULL_BETAS = ["context-management-2025-06-27", "server-side-fallback-2026-07-01"]
_REJECTED = (anthropic.BadRequestError, anthropic.NotFoundError, anthropic.UnprocessableEntityError)


def default_model() -> str:
    return os.environ.get("CLOAKUP_MODEL") or DEFAULT_MODEL


class ModelClient:
    """Wraps an ``AsyncAnthropic``-like client. ``create`` takes the *full* request
    (including beta-only fields) and strips whatever the current level can't send."""

    def __init__(self, raw: Any = None, compat: Optional[str] = None, base_url: Optional[str] = None) -> None:
        compat = (compat or os.environ.get("CLOAKUP_API_COMPAT") or "auto").lower()
        if compat not in ("auto", *LEVELS):
            raise ValueError(f"unknown api compat level {compat!r}")
        if raw is None:
            kw: dict[str, Any] = {"max_retries": 4}
            if base_url:
                kw["base_url"] = base_url
            raw = anthropic.AsyncAnthropic(**kw)
        self.raw = raw
        self.auto = compat == "auto"
        self.level = compat
        if self.auto:
            self.level = "full" if _official(getattr(raw, "base_url", None)) else "basic"
        self.structured_ok = True

    # ------------------------------------------------------------------ shaping
    @staticmethod
    def shape(level: str, kw: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        """Returns (use_beta_endpoint, request kwargs) for ``level``."""
        kw = dict(kw)
        if level == "full":
            kw.setdefault("betas", FULL_BETAS)
            return True, kw
        for k in ("betas", "fallbacks", "context_management", "cache_control"):
            kw.pop(k, None)
        if level == "minimal":
            kw.pop("output_config", None)
            if isinstance(kw.get("system"), list):
                kw["system"] = [{k: v for k, v in blk.items() if k != "cache_control"} for blk in kw["system"]]
        return False, kw

    @property
    def manages_context(self) -> bool:
        """True when the server clears old tool results for us (full level)."""
        return self.level == "full"

    @property
    def structured_outputs(self) -> bool:
        return self.level != "minimal" and self.structured_ok

    # ------------------------------------------------------------------ calls
    async def _send(self, level: str, kw: dict[str, Any]) -> Any:
        beta, req = self.shape(level, kw)
        api = self.raw.beta.messages if beta else self.raw.messages
        return await api.create(**req)

    async def create(self, allow_downgrade: bool = True, **kw: Any) -> Any:
        """Send a request at the current level, stepping down on gateway rejection.

        ``allow_downgrade=False`` is for requests that may legitimately be rejected
        for their own content (e.g. a JSON schema structured outputs can't express),
        which must not be mistaken for a gateway limitation."""
        try:
            return await self._send(self.level, kw)
        except _REJECTED as first:
            if not (self.auto and allow_downgrade):
                raise
            idx = LEVELS.index(self.level)
            for lower in LEVELS[idx + 1:]:
                try:
                    resp = await self._send(lower, kw)
                except _REJECTED:
                    continue
                log.warning("模型服务不支持 %s 级接口(%s),已降级为 %s", self.level, _short(first), lower)
                self.level = lower
                return resp
            raise


def _official(base_url: Any) -> bool:
    """Official API host, or no base_url at all (test doubles)."""
    if base_url is None:
        return True
    from urllib.parse import urlparse

    host = urlparse(str(base_url)).hostname or ""
    return host == "api.anthropic.com"


def _short(exc: Exception) -> str:
    return str(exc).splitlines()[0][:200]


def as_model_client(client: Any) -> ModelClient:
    """Accepts None, a raw SDK client or a ModelClient."""
    if isinstance(client, ModelClient):
        return client
    return ModelClient(client)
