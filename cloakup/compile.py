"""Compile a successful exploration trace into a parameterized recipe.

What the AI does here is the "understanding" step: from one concrete run
(task + parameter values + the actions that worked) it works out which typed
values were parameters, which elements were meant (described semantically),
what each step was for, what the page should look like afterwards, and where
run-specific choices need an extract/foreach or a bounded ai_step instead of a
fixed click.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from .llm import LLM
from .recipe import RECIPE_SCHEMA, Recipe, RecipeError

log = logging.getLogger("cloakup")

COMPILE_SYSTEM = """你是网页自动化流程的编译器。输入是 AI 代理一次成功执行任务的记录(带本次参数值),输出是可以换参数反复回放的流程(recipe)JSON。

要求:
1. 参数化:把记录里出现的参数值(输入的文字、选择的选项、URL 片段)替换成 {{参数名}}。inputs 是 JSON Schema(type: object),为每个参数写 description 和合适的类型;本次执行用到的参数都放进 required,除非它明显可选(可选参数给 default)。
2. 只保留通往结果的有效动作:去掉失败的尝试、走错又返回的路径、重复的滚动。
3. 每一步都要有 id(小写英文加下划线)和 intent(这一步要达成什么,自愈时依赖它)。
4. target 用语义描述:label(可见文字/占位符,可给多个备选;依赖参数的文字用模板,如 "{{keyword}}")、tag、type、role、href_contains;css 只作为提示保留。不要把价格、数量、日期等易变文字写进 label。
5. 在页面会发生可观察变化的地方加 expect(url_contains 用稳定片段,或 text_contains / element),不要写依赖本次具体数据的检查,除非用模板。
6. 依赖本次页面内容的选择(例如"点开第一个搜索结果"):用 extract(save_as 列表)+ foreach(items: "{{vars.xxx}}",子步骤里用 "{{item.title}}" 之类的模板)或 target.nth;日期选择器、复杂自绘控件、内容高度可变的交互用 ai_step,并写清 instruction 与 max_steps。
7. 数据提取:把 save_data / done.result 的位置改写成 extract 步骤(写 schema、instruction 和 save_as/append_to/merge),使最终 result 对象符合 outputs Schema。extract 的 schema 顶层是对象:save_as X 会把整个对象存为 result.X;append_to X 把对象追加到列表 result.X;merge: true 把对象的各字段直接并入 result(例如 schema 是 {books: [...]} 而 outputs 要求 result.books 是数组时用 merge)。
   expect.element 只能指向可交互元素(链接、按钮、输入框、下拉框);检查列表、卡片、正文是否出现用 text_contains 或 url_contains。extract 步骤不改变页面,一般不需要 expect。
8. 凭据保持 {{secret:NAME}} 原样。不要加入记录里没有的不可逆操作(支付、下单、删除、发布)。
9. goal 写成带 {{参数}} 的自然语言任务描述,AI 兜底时使用。id 用小写英文加下划线。start_url 用记录中的起始页(可参数化)。"""


# ------------------------------------------------------------ static checks
# Interactive tags/roles the observer lists; expect.element can only see these.
_INTERACTIVE_TAGS = {"a", "button", "input", "select", "textarea", "summary", "label", "option"}
_INTERACTIVE_ROLES = {"button", "link", "checkbox", "radio", "tab", "menuitem", "option", "switch",
                      "combobox", "textbox", "searchbox"}
# Implicit roles the observer reports as "" (only explicit role="" attributes are read).
_IMPLICIT_ROLE = {"a": "link", "button": "button", "select": "combobox", "textarea": "textbox", "input": "textbox"}


def _walk(steps: list[dict[str, Any]]):
    for st in steps or []:
        yield st
        for k in ("then", "else", "steps"):
            yield from _walk(st.get(k) or [])


def _fix_target(t: dict[str, Any], where: str, notes: list[str]) -> None:
    labels = t.get("label")
    templated = any("{{" in str(x) for x in (labels if isinstance(labels, list) else [labels]))
    if templated and t.get("css"):
        # A css path recorded for one parameter value points at the wrong element for others.
        notes.append(f"{where}: label 依赖参数,去掉只对本次有效的 css {t.pop('css')!r}")
    tag, role = t.get("tag"), t.get("role")
    if role and tag and _IMPLICIT_ROLE.get(tag) == role:
        t.pop("role")  # the observer only reports explicit roles; an implicit one would cost score
        notes.append(f"{where}: 去掉隐式 role={role}")


def sanitize(data: dict[str, Any]) -> list[str]:
    """Deterministic fixes for common compiler slips. Mutates ``data``; returns notes."""
    notes: list[str] = []
    for st in _walk(data.get("steps") or []):
        sid = st.get("id", "?")
        if isinstance(st.get("target"), dict):
            _fix_target(st["target"], f"{sid}.target", notes)
        exp = st.get("expect")
        if isinstance(exp, dict) and isinstance(exp.get("element"), dict):
            el = exp["element"]
            visible = el.get("tag") in _INTERACTIVE_TAGS or el.get("role") in _INTERACTIVE_ROLES or (
                not el.get("tag") and not el.get("role"))
            if not visible:
                notes.append(f"{sid}.expect: 去掉指向非交互元素的 element 检查 {el}")
                exp.pop("element")
                if not any(k in exp for k in ("url_contains", "title_contains", "text_contains", "element")):
                    st.pop("expect")
            else:
                _fix_target(el, f"{sid}.expect.element", notes)
    return notes


def _type_of(schema: dict[str, Any]) -> set[str]:
    t = (schema or {}).get("type")
    return set(t) if isinstance(t, list) else ({t} if t else set())


def lint(data: dict[str, Any]) -> list[str]:
    """Static check that the steps can produce the declared outputs."""
    outputs = data.get("outputs") or {}
    props = outputs.get("properties") or {}
    if not props:
        return []
    produced: set[str] = set()
    problems: list[str] = []
    for st in _walk(data.get("steps") or []):
        sid, act = st.get("id", "?"), st.get("action")
        stored = _type_of(st.get("schema") or {"type": "object"}) if act == "extract" else set()
        if st.get("merge"):
            keys = set((st.get("schema") or {}).get("properties") or {})
            produced |= keys
            for k in keys & set(props):
                want, got = _type_of(props[k]), _type_of(st["schema"]["properties"][k])
                if want and got and not (want & got):
                    problems.append(f"步骤 {sid} merge 的字段 {k} 类型是 {sorted(got)},outputs.{k} 要求 {sorted(want)}")
        name = st.get("save_as")
        if name:
            produced.add(name)
            want = _type_of(props.get(name, {}))
            if name in props and stored and want and not (want & stored):
                problems.append(
                    f"步骤 {sid} 用 save_as {name} 保存了 {sorted(stored)},但 outputs.{name} 要求 {sorted(want)};"
                    f"如果 extract 的 schema 里已经有 {name} 字段,改用 merge: true")
        name = st.get("append_to")
        if name:
            produced.add(name)
            want = _type_of(props.get(name, {}))
            if name in props and want and "array" not in want:
                problems.append(f"步骤 {sid} 用 append_to {name} 生成列表,但 outputs.{name} 要求 {sorted(want)}")
    missing = [k for k in outputs.get("required") or [] if k not in produced]
    if missing:
        problems.append(f"outputs 要求的字段 {missing} 没有任何步骤产生(用 extract 的 save_as/append_to/merge)")
    return problems


def load_trace(trace_dir: str | Path) -> list[dict[str, Any]]:
    p = Path(trace_dir) / "trace.jsonl"
    if not p.exists():
        raise FileNotFoundError(f"找不到执行记录: {p}")
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def _short(v: Any, n: int = 160) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "…"


def summarize_trace(events: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    """(task event, readable action log with parameter-binding hints)."""
    task = next((e for e in events if e.get("type") == "task"), {})
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    if not result.get("success"):
        raise RecipeError("这次执行没有成功完成,不能编译成流程。请用成功的执行记录。")
    params: dict[str, Any] = task.get("params") or {}
    lines: list[str] = []
    n = 0
    for e in events:
        if e.get("type") == "observe" and e.get("url"):
            lines.append(f"   页面: {e['url']}  「{_short(e.get('title', ''), 60)}」")
        if e.get("type") != "action":
            continue
        n += 1
        status = "✗失败" if e.get("error") else "✓"
        args = {k: v for k, v in (e.get("input") or {}).items() if k != "element_id"}
        line = f"#{n} {status} {e['tool']} {_short(args)}"
        if e.get("element"):
            line += f"\n   元素: {_short(e['element'], 240)}"
        if e.get("error"):
            line += f"\n   错误: {_short(e.get('result', ''), 120)}"
        lines.append(line)
    hints = []
    log_text = "\n".join(lines)
    for name, value in params.items():
        sval = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if sval and sval in log_text:
            hints.append(f"- 参数 {name}={sval!r} 出现在执行记录中,应替换为 {{{{{name}}}}}")
    final = {"summary": result.get("summary"), "result": result.get("result"), "data": result.get("data")}
    text = (f"任务: {task.get('task')}\n起始 URL: {task.get('start_url')}\n"
            f"本次参数: {json.dumps(params, ensure_ascii=False)}\n"
            f"输出 Schema: {json.dumps(task.get('output_schema'), ensure_ascii=False)}\n\n"
            f"执行记录:\n{log_text}\n\n最终结果: {_short(final, 1500)}\n\n"
            + ("参数绑定提示:\n" + "\n".join(hints) if hints else "参数绑定提示: (无直接匹配)"))
    return task, text


async def compile_trace(trace_dir: str | Path, llm: LLM, recipe_id: Optional[str] = None,
                        notes: str = "") -> Recipe:
    events = load_trace(trace_dir)
    task, text = summarize_trace(events)
    prompt = text + (f"\n\n额外说明: {notes}" if notes else "")
    if recipe_id:
        prompt += f"\n\n流程 id 必须是: {recipe_id}"
    if task.get("output_schema"):
        prompt += "\n\noutputs 直接使用上面的输出 Schema。"

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    last_error = ""
    for _ in range(3):
        if last_error:
            content = [{"type": "text", "text": prompt + f"\n\n上一次输出的问题,请修正: {last_error}"}]
        data = await llm.json(system=COMPILE_SYSTEM, content=content, schema=RECIPE_SCHEMA,
                              effort="high", structured=False)
        if recipe_id:
            data["id"] = recipe_id
        if task.get("output_schema"):
            data["outputs"] = task["output_schema"]
        data.setdefault("version", 1)
        for note in sanitize(data):
            log.info("编译修正: %s", note)
        try:
            recipe = Recipe.from_dict(data)
            recipe.validate_params(task.get("params") or {})  # the recorded run must be a valid input
            problems = lint(data)
            if problems:
                raise RecipeError(";".join(problems))
            return recipe
        except RecipeError as exc:
            last_error = str(exc)
            log.info("编译结果未通过检查,重试: %s", last_error[:300])
    raise RecipeError(f"编译失败: {last_error}")
