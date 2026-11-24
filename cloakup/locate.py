"""Resolve a recipe ``target`` against a live observation.

Targets describe elements the way a person would ("the search box", "the link
titled X"), with the recorded CSS path only as a hint. Each candidate element
is scored; a confident, unambiguous winner is used directly. Anything else is
handed to the self-healing path (an AI pick) by the runner.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Optional

from .observe import Element, Observation

ACCEPT = 3.0  # minimum score to act without asking the AI
MARGIN = 1.0  # winner must beat the runner-up by this much (unless `nth` says which)


@dataclass
class Match:
    element: Element
    score: float
    reasons: list[str]


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


def _label_score(want: list[str], have: str) -> tuple[float, str]:
    have_n = _norm(have)
    best, why = 0.0, ""
    for w in want:
        w_n = _norm(w)
        if not w_n or not have_n:
            continue
        if have_n == w_n:
            s, r = 4.0, f'label="{w}"'
        elif w_n in have_n:
            # long labels that merely contain the text are weaker evidence
            s, r = 3.0 * min(1.0, (len(w_n) + 8) / (len(have_n) + 8)) + 0.5, f'label∋"{w}"'
        else:
            ratio = SequenceMatcher(None, w_n, have_n).ratio()
            s, r = (ratio * 3.0, f"label≈{ratio:.2f}") if ratio >= 0.6 else (0.0, "")
        if s > best:
            best, why = s, r
    return best, why


def score(target: dict[str, Any], el: Element) -> Match:
    e = el.raw
    if target.get("frame_url_contains") and target["frame_url_contains"] not in e.get("frame_url", ""):
        return Match(el, -100.0, ["different iframe"])
    total, reasons = 0.0, []
    labels = target.get("label")
    if labels:
        labels = [labels] if isinstance(labels, str) else list(labels)
        s, why = _label_score(labels, el.label)
        total += s
        if why:
            reasons.append(why)
    if target.get("css") and target["css"] == el.css:
        total += 2.5
        reasons.append("css")
    for key, w in (("tag", 1.0), ("type", 1.0), ("role", 1.0)):
        if target.get(key) and _norm(target[key]) == _norm(e.get(key, "")):
            total += w
            reasons.append(key)
        elif target.get(key) and e.get(key):
            total -= 0.5  # explicit mismatch
    if target.get("href_contains"):
        if target["href_contains"] in (e.get("href") or ""):
            total += 2.0
            reasons.append("href")
        else:
            total -= 1.0
    if e.get("disabled"):
        total -= 1.5
    return Match(el, total, reasons)


def locate(target: dict[str, Any], obs: Observation) -> tuple[Optional[Match], list[Match]]:
    """Returns (confident match or None, ranked candidates)."""
    ranked = sorted((score(target, el) for el in obs.elements), key=lambda m: -m.score)
    ranked = [m for m in ranked if m.score > 0]
    if not ranked or ranked[0].score < ACCEPT:
        return None, ranked[:8]
    top = ranked[0].score
    tied = [m for m in ranked if top - m.score < MARGIN]
    if "nth" in target:
        # nth among equally good matches, in document/visual order
        tied.sort(key=lambda m: (m.element.raw.get("pos", 0), m.element.y, m.element.x))
        n = int(target["nth"])
        return (tied[n] if n < len(tied) else None), ranked[:8]
    if len(tied) > 1:
        # Links to the same destination (cover image + title link of one item)
        # are interchangeable: take the best one, preferring what's in view.
        hrefs = {m.element.raw.get("href") for m in tied}
        if len(hrefs) == 1 and None not in hrefs and "" not in hrefs:
            tied.sort(key=lambda m: (-m.score, abs(m.element.raw.get("pos", 0)), m.element.y, m.element.x))
            return tied[0], ranked[:8]
        return None, ranked[:8]  # ambiguous -> let the AI decide
    return ranked[0], ranked[:8]


def describe(el: Element) -> dict[str, Any]:
    """Target descriptor for an element (used when recording and healing)."""
    e = el.raw
    t: dict[str, Any] = {"tag": e["tag"]}
    if el.label:
        t["label"] = el.label.rstrip("…")
    if e.get("type"):
        t["type"] = e["type"]
    if e.get("role"):
        t["role"] = e["role"]
    if e.get("href") and not e["href"].startswith("javascript"):
        t["href_contains"] = e["href"].rstrip("…")
    if e.get("frame_url"):
        from urllib.parse import urlparse
        t["frame_url_contains"] = urlparse(e["frame_url"]).path
    t["css"] = el.css
    return t
