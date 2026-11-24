"""``{{name}}`` templating for recipes and task templates.

Deliberately tiny (no expressions, no code execution): a recipe is data that
an LLM writes, so it must not be able to run anything.

* ``{{keyword}}``          -> params["keyword"]
* ``{{item.title}}``       -> dotted lookup into the current scope
* ``"{{max_items}}"``      -> a string that is *only* a placeholder keeps the
                              value's type (int stays int, list stays list)
* ``{{secret:NAME}}``      -> left untouched; resolved at the browser only
"""

from __future__ import annotations

import re
from typing import Any, Mapping

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][\w.]*)\s*\}\}")


class TemplateError(KeyError):
    pass


def lookup(scope: Mapping[str, Any], path: str) -> Any:
    cur: Any = scope
    for part in path.split("."):
        if isinstance(cur, Mapping) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            raise TemplateError(f"未定义的模板变量: {path}")
    return cur


def render(value: Any, scope: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        whole = PLACEHOLDER.fullmatch(value.strip())
        if whole:
            return lookup(scope, whole.group(1))
        return PLACEHOLDER.sub(lambda m: str(lookup(scope, m.group(1))), value)
    if isinstance(value, list):
        return [render(v, scope) for v in value]
    if isinstance(value, dict):
        return {k: render(v, scope) for k, v in value.items()}
    return value


def placeholders(value: Any) -> set[str]:
    """Top-level names referenced anywhere in ``value``."""
    found: set[str] = set()
    if isinstance(value, str):
        found |= {m.group(1).split(".")[0] for m in PLACEHOLDER.finditer(value)}
    elif isinstance(value, list):
        for v in value:
            found |= placeholders(v)
    elif isinstance(value, dict):
        for v in value.values():
            found |= placeholders(v)
    return found
