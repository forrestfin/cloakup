"""Natural language -> (recipe, parameters).

"帮我查一下上海到北京明天的经济舱" -> recipe flight_lowest_price,
{"from": "上海", "to": "北京", "date": "2026-11-12", "cabin": "经济舱"}.

Two structured-output calls: pick the recipe (enum of ids), then fill that
recipe's input schema. Required fields are *not* forced in the extraction
schema, so the model reports what is missing instead of inventing it.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any, Optional

from .llm import LLM
from .recipe import Recipe

SYSTEM = ("你把用户的自然语言请求映射到自动化流程和参数。只使用请求里明确给出或可以确定推出的信息"
          "(例如把'明天'换算成具体日期);不确定的参数不要猜,列入 missing。")


@dataclass
class Resolution:
    recipe: Optional[Recipe]
    params: dict[str, Any] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    reason: str = ""


def _relaxed(schema: dict[str, Any]) -> dict[str, Any]:
    s = copy.deepcopy(schema)
    s.pop("required", None)
    for spec in (s.get("properties") or {}).values():
        if isinstance(spec, dict):
            spec.pop("default", None)
    return s


async def resolve(request: str, recipes: list[Recipe], llm: LLM, today: Optional[dt.date] = None) -> Resolution:
    if not recipes:
        return Resolution(None, reason="没有可用的流程")
    today = today or dt.date.today()
    catalog = "\n".join(
        f"- {r.id}: {r.name or r.goal} | 参数: {', '.join(sorted(r.input_names())) or '无'}"
        + (f" | {r.description}" if r.description else "") for r in recipes)
    pick = await llm.json(
        system=SYSTEM, content=f"今天是 {today.isoformat()}。\n可用流程:\n{catalog}\n\n用户请求:{request}\n\n"
                               "选择最匹配的流程;都不匹配就选 none。",
        schema={"type": "object", "properties": {
            "recipe_id": {"enum": [r.id for r in recipes] + ["none"]},
            "reason": {"type": "string"}}, "required": ["recipe_id", "reason"]})
    recipe = next((r for r in recipes if r.id == pick["recipe_id"]), None)
    if recipe is None:
        return Resolution(None, reason=pick.get("reason", ""))
    if not recipe.input_names():
        return Resolution(recipe, reason=pick.get("reason", ""))

    out = await llm.json(
        system=SYSTEM,
        content=(f"今天是 {today.isoformat()}({['周一','周二','周三','周四','周五','周六','周日'][today.weekday()]})。\n"
                 f"流程: {recipe.id} — {recipe.goal}\n参数 Schema: {json.dumps(recipe.inputs, ensure_ascii=False)}\n\n"
                 f"用户请求:{request}"),
        schema={"type": "object", "properties": {
            "params": _relaxed(recipe.inputs),
            "missing": {"type": "array", "items": {"type": "string"}}}, "required": ["params", "missing"]})
    params = {k: v for k, v in (out.get("params") or {}).items() if v not in (None, "")}
    missing = sorted(set(out.get("missing") or []) | set(recipe.missing_required(
        {**{k: s["default"] for k, s in (recipe.inputs.get("properties") or {}).items()
            if isinstance(s, dict) and "default" in s}, **params})))
    return Resolution(recipe, params, [m for m in missing if m in recipe.input_names()], pick.get("reason", ""))
