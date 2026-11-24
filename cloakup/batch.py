"""Run one recipe over many parameter sets.

Each worker owns one browser profile (one persistent identity), optionally
bound to one proxy, and works through the queue one job at a time with a
human-sized rest between jobs. Concurrency = number of profiles, which must
not exceed the session count your CloakBrowser plan allows (free key: 1).
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .browser import BrowserSession
from .config import AgentConfig, BrowserConfig, PacingConfig
from .guard import AskFn, ConfirmFn, terminal_ask, terminal_confirm
from .llm import LLM
from .recipe import Recipe, RecipeError
from .runner import RecipeRunner

log = logging.getLogger("cloakup")


def _coerce(value: str, spec: dict[str, Any]) -> Any:
    t = spec.get("type")
    if value == "" and "default" in spec:
        return spec["default"]
    if t == "integer":
        return int(value)
    if t == "number":
        return float(value)
    if t == "boolean":
        return value.strip().lower() in ("1", "true", "yes", "y", "是")
    if t in ("array", "object"):
        return json.loads(value)
    return value


def load_rows(path: str | Path, recipe: Recipe) -> list[dict[str, Any]]:
    """CSV (header = parameter names, values coerced by the input schema) or JSONL."""
    p = Path(path)
    props = recipe.inputs.get("properties") or {}
    if p.suffix.lower() == ".csv":
        with open(p, newline="", encoding="utf-8-sig") as f:
            rows = []
            for r in csv.DictReader(f):
                row = {}
                for k, v in r.items():
                    if k is None or (v == "" and "default" not in props.get(k, {})):
                        continue
                    row[k] = _coerce(v, props.get(k, {}))
                rows.append(row)
            return rows
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


@dataclass
class Slot:
    profile: str
    proxy: Optional[str] = None


async def run_batch(recipe: Recipe, rows: list[dict[str, Any]], slots: list[Slot], out_path: str | Path, *,
                    llm: Optional[LLM] = None, agent_cfg: Optional[AgentConfig] = None,
                    browser_tpl: Optional[BrowserConfig] = None, pacing: Optional[PacingConfig] = None,
                    heal: bool = True, rest: tuple[float, float] = (20.0, 90.0),
                    confirm: ConfirmFn = terminal_confirm, ask: AskFn = terminal_ask) -> dict[str, int]:
    llm = llm or LLM()
    tpl = browser_tpl or BrowserConfig()
    # Validate everything up front so a typo in row 900 doesn't surface hours in.
    queue: asyncio.Queue[tuple[int, dict[str, Any]]] = asyncio.Queue()
    bad = 0
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    lock = asyncio.Lock()

    async def write(rec: dict[str, Any]) -> None:
        async with lock:
            with open(out, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    for i, row in enumerate(rows):
        try:
            recipe.validate_params(row)
            queue.put_nowait((i, row))
        except RecipeError as exc:
            bad += 1
            await write({"row": i, "success": False, "params": row, "error": str(exc)})
    stats = {"ok": 0, "failed": 0, "invalid": bad}

    async def worker(slot: Slot) -> None:
        cfg = BrowserConfig(**{**tpl.__dict__, "profile_dir": slot.profile, "proxy": slot.proxy or tpl.proxy})
        session: Optional[BrowserSession] = None
        first = True
        try:
            while not queue.empty():
                i, row = queue.get_nowait()
                if not first:
                    await asyncio.sleep(random.uniform(*rest))
                first = False
                if session is None:
                    session = BrowserSession(cfg, pacing or PacingConfig())
                    await session.start()
                runner = RecipeRunner(recipe, session, llm, agent_cfg, heal=heal, confirm=confirm, ask=ask)
                t0 = time.time()
                try:
                    res = await runner.run(row)
                    rec = {"row": i, "profile": slot.profile, **res.to_dict()}
                except Exception as exc:  # browser crashed etc. -> fresh browser for the next job
                    rec = {"row": i, "profile": slot.profile, "success": False, "params": row,
                           "error": f"{type(exc).__name__}: {exc}", "seconds": round(time.time() - t0, 1)}
                    await session.close()
                    session = None
                stats["ok" if rec.get("success") else "failed"] += 1
                log.info("[%s] row %d %s", slot.profile, i, "✅" if rec.get("success") else f"❌ {rec.get('error')}")
                await write(rec)
        finally:
            if session is not None:
                await session.close()

    await asyncio.gather(*(worker(s) for s in slots))
    return stats
