"""Small Claude helpers shared by the recipe runner, compiler and parameter resolver.

``LLM.json`` returns a dict that validates against a JSON Schema. It first tries
structured outputs (``output_config.format``), which guarantee schema-valid JSON;
schemas that use features structured outputs can't express (recursion, some
keywords) fall back to "JSON in text" plus local validation and up to two
self-correction rounds.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any, Optional

import anthropic
import jsonschema

from .client import ModelClient, as_model_client, default_model

log = logging.getLogger("cloakup")



def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Copy of ``schema`` with ``additionalProperties: false`` on every object,
    as structured outputs require."""
    s = copy.deepcopy(schema)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                node.setdefault("additionalProperties", False)
                node.setdefault("properties", {})
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(s)
    return s


def _first_json(text: str) -> Any:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise ValueError("no JSON found in model output")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    return obj


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, client: Any = None, model: Optional[str] = None) -> None:
        self.client: ModelClient = as_model_client(client)
        self.model = model or default_model()
        self.usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}

    def _account(self, resp: Any) -> None:
        u = resp.usage
        self.usage["input_tokens"] += u.input_tokens or 0
        self.usage["output_tokens"] += u.output_tokens or 0
        self.usage["calls"] += 1

    async def _create(self, allow_downgrade: bool = True, **kw: Any) -> Any:
        resp = await self.client.create(allow_downgrade=allow_downgrade, model=self.model, max_tokens=16000,
                                        betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kw)
        self._account(resp)
        if resp.stop_reason == "refusal":
            raise LLMError("model declined the request")
        return resp

    @staticmethod
    def _text(resp: Any) -> str:
        return "".join(b.text for b in resp.content if b.type == "text")

    async def json(self, *, system: str, content: list[dict[str, Any]] | str, schema: dict[str, Any],
                   effort: str = "low", structured: bool = True) -> Any:
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if structured and self.client.structured_outputs:
            try:
                # No downgrade here: a 400 may be about this schema, not the gateway.
                resp = await self._create(
                    allow_downgrade=False, system=system, messages=[{"role": "user", "content": content}],
                    output_config={"effort": effort,
                                   "format": {"type": "json_schema", "schema": strict_schema(schema)}})
                data = json.loads(self._text(resp))
                jsonschema.validate(data, schema)
                return data
            except (anthropic.BadRequestError, anthropic.UnprocessableEntityError) as exc:
                log.debug("structured output rejected (%s); falling back to validated text", exc)
            except (json.JSONDecodeError, jsonschema.ValidationError) as exc:
                # The service accepted output_config.format but didn't honour it.
                log.debug("structured output invalid (%s); disabling and falling back", exc)
                self.client.structured_ok = False

        messages: list[dict[str, Any]] = [{"role": "user", "content": [
            *content,
            {"type": "text", "text": "只输出一个符合下面 JSON Schema 的 JSON 值,不要任何其他文字:\n"
                                     + json.dumps(schema, ensure_ascii=False)}]}]
        last_err = ""
        for _ in range(3):
            resp = await self._create(system=system, messages=messages, output_config={"effort": effort})
            text = self._text(resp)
            try:
                data = _first_json(text)
                jsonschema.validate(data, schema)
                return data
            except (ValueError, jsonschema.ValidationError) as exc:
                last_err = str(exc).splitlines()[0][:500]
                messages += [{"role": "assistant", "content": resp.content},
                             {"role": "user", "content": f"输出不符合 Schema:{last_err}。请修正后重新只输出 JSON。"}]
        raise LLMError(f"could not get schema-valid JSON: {last_err}")
