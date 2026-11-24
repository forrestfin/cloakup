"""Recipe: a parameterized, replayable browser workflow (YAML on disk).

A recipe is what the AI "understood" about a task, frozen into data:
inputs (JSON Schema) -> steps with semantic targets and expectations ->
outputs (JSON Schema). See ``RECIPE_SCHEMA`` for the full format.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import jsonschema
import yaml

from .template import placeholders

ACTIONS = ["navigate", "click", "type", "select", "hover", "press", "scroll", "wait", "wait_for",
           "extract", "ai_check", "ai_step", "foreach", "ask_human", "go_back"]

TARGET_SCHEMA = {
    "type": "object",
    "description": "How to find an element. Semantic fields first; css is only a hint.",
    "properties": {
        "label": {"description": "Visible text / aria-label / placeholder. String or list of alternatives; may contain {{params}}.",
                  "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
        "frame_url_contains": {"type": "string", "description": "Stable path of a target iframe, when needed."},
        "tag": {"type": "string"},
        "type": {"type": "string", "description": "input type, e.g. search, text, submit"},
        "role": {"type": "string"},
        "href_contains": {"type": "string"},
        "css": {"type": "string", "description": "CSS path seen when recorded; a hint, may go stale."},
        "nth": {"type": "integer", "minimum": 0, "description": "Pick the nth of equally good matches."},
    },
    "additionalProperties": False,
}

EXPECT_SCHEMA = {
    "type": "object",
    "description": "What must be true after the step. All given conditions must hold.",
    "properties": {
        "url_contains": {"type": "string"},
        "title_contains": {"type": "string"},
        "text_contains": {"type": "string"},
        "element": TARGET_SCHEMA,
        "timeout": {"type": "number", "description": "seconds to wait for it (default 8)"},
    },
    "additionalProperties": False,
}

STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["action", "intent"],
    "properties": {
        "id": {"type": "string"},
        "action": {"enum": ACTIONS},
        "intent": {"type": "string", "description": "What this step achieves, in plain words. Used for self-healing."},
        "when": {"type": "object", "description": "Run only if condition holds.",
                 "properties": {"value": {}, "equals": {}, "not_equals": {}, "truthy": {"type": "boolean"}},
                 "required": ["value"], "additionalProperties": False},
        "optional": {"type": "boolean", "description": "Failure does not fail the run."},
        "target": TARGET_SCHEMA,
        "expect": EXPECT_SCHEMA,
        "url": {"type": "string"},
        "text": {"type": "string"},
        "clear": {"type": "boolean"},
        "submit": {"type": "boolean"},
        "option": {"type": "string"},
        "key": {"type": "string"},
        "direction": {"enum": ["down", "up"]},
        "amount": {"enum": ["small", "half", "page"]},
        "seconds": {"type": "number"},
        "instruction": {"type": "string"},
        "schema": {"type": "object", "description": "JSON Schema of the extracted value."},
        "save_as": {"type": "string", "description": "Store under vars.<name> and result.<name>."},
        "append_to": {"type": "string", "description": "Append to the list vars/result.<name>."},
        "merge": {"type": "boolean",
                  "description": "extract only: merge the extracted object's fields into result (and vars)."},
        "question": {"type": "string"},
        "then": {"type": "array", "items": {"$ref": "#/$defs/step"}},
        "else": {"type": "array", "items": {"$ref": "#/$defs/step"}},
        "items": {"description": "List (or {{placeholder}} resolving to a list) to iterate."},
        "as": {"type": "string"},
        "limit": {},
        "steps": {"type": "array", "items": {"$ref": "#/$defs/step"}},
        "max_steps": {"type": "integer"},
    },
    "additionalProperties": False,
}

RECIPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["id", "goal", "inputs", "steps"],
    "properties": {
        "id": {"type": "string", "pattern": "^[a-z0-9_]+$"},
        "name": {"type": "string"},
        "description": {"type": "string"},
        "version": {"type": "integer"},
        "start_url": {"type": "string"},
        "allowed_domains": {"type": "array", "items": {"type": "string"}},
        "goal": {"type": "string", "description": "Task in natural language with {{params}}; used by the AI fallback."},
        "inputs": {"type": "object", "description": "JSON Schema (type object) of the parameters."},
        "outputs": {"type": "object", "description": "JSON Schema of the result object."},
        "steps": {"type": "array", "items": {"$ref": "#/$defs/step"}, "minItems": 1},
    },
    "additionalProperties": False,
    "$defs": {"step": STEP_SCHEMA},
}


class RecipeError(ValueError):
    pass


@dataclass
class Recipe:
    id: str
    goal: str
    inputs: dict[str, Any]
    steps: list[dict[str, Any]]
    name: str = ""
    description: str = ""
    version: int = 1
    start_url: Optional[str] = None
    allowed_domains: list[str] = field(default_factory=list)
    outputs: Optional[dict[str, Any]] = None
    path: Optional[Path] = None

    # ------------------------------------------------------------- io
    @classmethod
    def from_dict(cls, data: dict[str, Any], path: Optional[Path] = None) -> "Recipe":
        errors = sorted(jsonschema.Draft202012Validator(RECIPE_SCHEMA).iter_errors(data), key=lambda e: list(e.path))
        if errors:
            e = errors[0]
            raise RecipeError(f"流程格式错误 at {'/'.join(map(str, e.path)) or '<root>'}: {e.message}")
        try:
            jsonschema.Draft202012Validator.check_schema(data["inputs"])
            if data.get("outputs"):
                jsonschema.Draft202012Validator.check_schema(data["outputs"])
        except jsonschema.SchemaError as exc:
            raise RecipeError(f"inputs/outputs 不是合法 JSON Schema: {exc.message}") from exc
        data = copy.deepcopy(data)
        _assign_ids(data["steps"], "s")
        r = cls(id=data["id"], goal=data["goal"], inputs=data["inputs"], steps=data["steps"],
                name=data.get("name", ""), description=data.get("description", ""),
                version=int(data.get("version", 1)), start_url=data.get("start_url"),
                allowed_domains=list(data.get("allowed_domains", [])), outputs=data.get("outputs"), path=path)
        r._check_placeholders()
        return r

    @classmethod
    def load(cls, path: str | Path) -> "Recipe":
        p = Path(path)
        return cls.from_dict(yaml.safe_load(p.read_text(encoding="utf-8")), path=p)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "name": self.name, "description": self.description,
                             "version": self.version}
        if self.start_url:
            d["start_url"] = self.start_url
        if self.allowed_domains:
            d["allowed_domains"] = self.allowed_domains
        d.update(goal=self.goal, inputs=self.inputs)
        if self.outputs:
            d["outputs"] = self.outputs
        d["steps"] = self.steps
        return d

    def save(self, path: str | Path | None = None) -> Path:
        p = Path(path or self.path or f"recipes/{self.id}.yaml")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(self.to_dict(), allow_unicode=True, sort_keys=False, width=110),
                     encoding="utf-8")
        self.path = p
        return p

    # ----------------------------------------------------------- params
    def input_names(self) -> set[str]:
        return set((self.inputs.get("properties") or {}).keys())

    def _check_placeholders(self) -> None:
        """Every {{name}} must be an input, a loop variable, or vars/result."""
        known = self.input_names() | {"vars", "result", "item"}

        def walk(steps: list[dict[str, Any]], scope: set[str]) -> None:
            for st in steps:
                inner = scope | ({st["as"]} if st.get("as") else set())
                own = {k: v for k, v in st.items() if k not in ("then", "else", "steps", "schema")}
                unknown = placeholders(own) - scope - ({st["as"]} if st.get("as") else set())
                if unknown:
                    raise RecipeError(f"步骤 {st.get('id') or st['action']} 引用了未定义的参数: {sorted(unknown)}")
                for key in ("then", "else", "steps"):
                    if st.get(key):
                        walk(st[key], inner)

        walk(self.steps, known)
        unknown = placeholders([self.goal, self.start_url or ""]) - known
        if unknown:
            raise RecipeError(f"goal/start_url 引用了未定义的参数: {sorted(unknown)}")

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Apply defaults and validate against ``inputs``. Returns the full param set."""
        full = copy.deepcopy(params)
        for name, spec in (self.inputs.get("properties") or {}).items():
            if name not in full and isinstance(spec, dict) and "default" in spec:
                full[name] = copy.deepcopy(spec["default"])
        try:
            jsonschema.validate(full, self.inputs)
        except jsonschema.ValidationError as exc:
            where = "/".join(map(str, exc.path)) or "参数"
            raise RecipeError(f"参数不合法 ({where}): {exc.message}") from exc
        return full

    def missing_required(self, params: dict[str, Any]) -> list[str]:
        return [n for n in self.inputs.get("required", []) if n not in params]


def _assign_ids(steps: list[dict[str, Any]], prefix: str) -> None:
    taken: set[str] = set()

    def collect(ss: list[dict[str, Any]]) -> None:
        for s in ss:
            if s.get("id"):
                if s["id"] in taken:
                    raise RecipeError(f"重复的步骤 id: {s['id']}")
                taken.add(s["id"])
            for k in ("then", "else", "steps"):
                collect(s.get(k) or [])

    def fill(ss: list[dict[str, Any]], pre: str) -> None:
        for i, s in enumerate(ss, 1):
            if not s.get("id"):
                cand = f"{pre}{i}"
                while cand in taken:
                    cand += "_"
                s["id"] = cand
                taken.add(cand)
            for k in ("then", "else", "steps"):
                fill(s.get(k) or [], f"{s['id']}.{k}.")

    collect(steps)
    fill(steps, prefix)


def find_step(steps: list[dict[str, Any]], step_id: str) -> Optional[dict[str, Any]]:
    for s in steps:
        if s.get("id") == step_id:
            return s
        for k in ("then", "else", "steps"):
            hit = find_step(s.get(k) or [], step_id)
            if hit:
                return hit
    return None


def apply_heals(recipe: Recipe, patches: list[dict[str, Any]], min_count: int = 1) -> list[str]:
    """Fold repeated ``target`` heals into the recipe (in place, version + 1).

    A step is updated only when the same new element was chosen at least
    ``min_count`` times for this recipe version. Heals of parameterized
    targets arrive already generalized (``runner.generalize_target``), so a
    templated new label replaces the old one; a concrete new label never
    replaces a template. Returns changed ids.
    """
    from collections import Counter

    votes: Counter[tuple[str, str]] = Counter()
    chosen: dict[tuple[str, str], dict[str, Any]] = {}
    for p in patches:
        if p.get("kind") != "target" or p.get("version") != recipe.version:
            continue
        key = (p["step_id"], json.dumps(p["new_target"], sort_keys=True, ensure_ascii=False))
        votes[key] += 1
        chosen[key] = p["new_target"]
    changed: list[str] = []
    for (step_id, _), n in votes.most_common():
        if n < min_count or step_id in changed:
            continue
        step = find_step(recipe.steps, step_id)
        if step is None:
            continue
        old = step.get("target") or {}
        new = dict(chosen[(step_id, _)])
        if placeholders(old.get("label", "")) and not placeholders(new.get("label", "")):
            # New label is a concrete value from one run: keep the template.
            new["label"] = old["label"]
        elif old.get("label") and new.get("label") and old["label"] != new["label"]:
            alts = old["label"] if isinstance(old["label"], list) else [old["label"]]
            new["label"] = [new["label"], *[a for a in alts if a != new["label"]]]
        if "nth" in old:
            new["nth"] = old["nth"]
        step["target"] = new
        changed.append(step_id)
    if changed:
        recipe.version += 1
    return changed


def load_recipes(directory: str | Path) -> list[Recipe]:
    return [Recipe.load(p) for p in sorted(Path(directory).glob("*.y*ml"))]
