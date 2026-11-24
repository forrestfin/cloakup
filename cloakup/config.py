"""Configuration dataclasses."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal, Optional


@dataclass
class BrowserConfig:
    # A persistent profile keeps cookies, history and the fingerprint seed across
    # runs, so the site sees the same returning "person" instead of a fresh
    # incognito-looking browser every time.
    profile_dir: str = "./profiles/default"
    headless: bool = False  # some sites flag headless even with C++ patches
    proxy: Optional[str] = None  # residential proxy recommended: http://user:pass@host:port
    geoip: bool = True  # match timezone/locale/WebRTC IP to the proxy exit IP (only when proxy set)
    human_preset: Literal["default", "careful"] = "careful"
    locale: Optional[str] = None
    timezone: Optional[str] = None
    license_key: Optional[str] = field(default_factory=lambda: os.environ.get("CLOAKBROWSER_LICENSE_KEY"))
    extra_args: list[str] = field(default_factory=list)


@dataclass
class PacingConfig:
    """Human rhythm between actions. All times in seconds."""

    # Pause before each action. The LLM's own latency counts toward it, so the
    # harness only sleeps the remainder.
    think_median: float = 1.6
    think_min: float = 0.5
    think_max: float = 6.0
    min_interval: float = 0.8
    max_actions_per_minute: int = 18
    # Dwell after a page load, proportional to the amount of visible text.
    read_chars_per_sec: float = 60.0
    read_min: float = 1.5
    read_max: float = 9.0
    # Chance per dwell slice of an idle mouse drift (people move the mouse while reading).
    idle_move_chance: float = 0.45


@dataclass
class AgentConfig:
    model: str = field(default_factory=lambda: os.environ.get("CLOAKUP_MODEL") or "claude-opus-5-5")
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    max_steps: int = 40
    screenshots: bool = True  # send a screenshot with each observation (vision)
    max_elements: int = 220
    # Empty list = no restriction. Otherwise navigation outside these domains
    # (and their subdomains) is refused.
    allowed_domains: list[str] = field(default_factory=list)
    # Ask the operator in the terminal before clicks that look like payments,
    # orders, deletions, etc.
    confirm_sensitive: bool = True
    # Secrets the model may reference as {{secret:NAME}}; values never enter the
    # model context. Maps NAME -> value.
    secrets: dict[str, str] = field(default_factory=dict)
    trace_dir: Optional[str] = "./runs"
    # Only used when the model service can't clear old tool results server-side
    # (gateway compat level basic/minimal): once a request's input exceeds this,
    # the conversation restarts from a progress log plus the current page.
    # Restarting (rather than trimming old turns) keeps each conversation
    # append-only, which preserved thinking requires.
    context_reset_tokens: int = 60000
