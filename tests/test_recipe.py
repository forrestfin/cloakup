"""Offline tests for parameterized execution (P1+P2): no browser, no API calls."""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace as NS
from typing import Any

import pytest

from cloakup import batch as batch_mod
from cloakup.batch import Slot, load_rows, run_batch
from cloakup.compile import compile_trace, summarize_trace
from cloakup.config import AgentConfig
from cloakup.locate import locate
from cloakup.observe import build_observation
from cloakup.params import resolve
from cloakup.recipe import Recipe, RecipeError, apply_heals
from cloakup.runner import RecipeRunner
from cloakup.template import TemplateError, render

# ----------------------------------------------------------- fake website
def _el(tag, label, css, **kw):
    return {"tag": tag, "type": kw.pop("type", ""), "role": "", "label": label, "css": css, "x": 10, "y": 10,
            "w": 50, "h": 20, "pos": 0, "disabled": False, **kw}


class FakeSite:
    """Home with a search box -> results page listing products -> product pages."""

    PRODUCTS = {"键盘A": 199, "键盘B": 299, "键盘C": 399}

    def __init__(self, search_css="#q"):
        self.search_css = search_css
        self.url = "https://shop.test/"
        self.typed = ""
        self.history: list[str] = []

    def elements(self):
        if self.url == "https://shop.test/":
            return [_el("input", "搜索商品", self.search_css, type="search", value=self.typed),
                    _el("a", "帮助", "a.help", href="/help")]
        if self.url.startswith("https://shop.test/search"):
            return [_el("a", name, f"li:nth-of-type({i + 1}) > a", href=f"/p/{i}")
                    for i, name in enumerate(self.PRODUCTS)]
        return [_el("a", "返回", "a.back", href="/")]

    def text(self):
        if self.url.startswith("https://shop.test/p/"):
            name = list(self.PRODUCTS)[int(self.url.rsplit("/", 1)[1])]
            return f"{name} 价格 ¥{self.PRODUCTS[name]}"
        if self.url.startswith("https://shop.test/search"):
            return "搜索结果 " + " ".join(self.PRODUCTS)
        return "欢迎光临"

    def go(self, url):
        self.history.append(self.url)
        self.url = url if url.startswith("http") else "https://shop.test" + url


class FakeSession:
    def __init__(self, site: FakeSite):
        self.site = site
        self.last_obs = None
        self.actions: list[tuple] = []
        self.page = NS(url=None, title=self._title)
        self._sync()

    def _sync(self):
        self.page.url = self.site.url

    async def _title(self):
        return "Shop"

    async def start(self):
        pass

    async def close(self):
        pass

    async def observe(self, screenshot=True, text_limit=3500):
        els = self.site.elements()
        raw = {"url": self.site.url, "title": "Shop", "vw": 1280, "vh": 720, "scrollY": 0, "scrollH": 800,
               "total": len(els), "frames": 0, "challenge": [], "text": self.site.text(),
               "textLen": len(self.site.text()), "elements": els}
        self.last_obs = build_observation(raw)
        return self.last_obs

    async def navigate(self, url):
        self.site.go(url)
        self._sync()
        self.actions.append(("navigate", url))
        return "ok"

    async def click(self, i):
        el = self.last_obs.find(i)
        self.actions.append(("click", el.label))
        if el.raw.get("href"):
            self.site.go(el.raw["href"])
            self._sync()
        return "clicked"

    async def type_text(self, i, text, clear=True, submit=False):
        self.actions.append(("type", text, submit))
        self.site.typed = text
        if submit:
            self.site.go(f"/search?q={text}")
            self._sync()
        return "typed"

    async def go_back(self):
        self.site.url = self.site.history.pop()
        self._sync()
        self.actions.append(("back",))
        return "back"

    async def text_contains(self, needle):
        return needle in self.site.text()

    async def idle(self):
        pass

    async def select_option(self, i, o):
        return "sel"

    async def hover(self, i):
        return "hover"

    async def press_key(self, k):
        return "key"

    async def scroll(self, d, a):
        return "scroll"

    async def wait(self, s):
        return "wait"


class FakeLLM:
    """Answers LLM.json calls by inspecting the request."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[dict[str, Any]] = []
        self.usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
        self.client = None

    async def json(self, *, system, content, schema, effort="low", structured=True):
        text = content if isinstance(content, str) else "\n".join(b.get("text", "") for b in content)
        self.calls.append({"system": system, "text": text, "schema": schema})
        self.usage["calls"] += 1
        return self.handler(system, text, schema)


def extract_handler(system, text, schema):
    if "提取" in system or "提取要求" in text:
        if "商品名称列表" in text:
            return {"names": ["键盘A", "键盘B", "键盘C"]}
        page = text.split("页面文本摘录:")[-1]
        for name, price in FakeSite.PRODUCTS.items():
            if name in page:
                return {"name": name, "price": price}
    if "是/否" in system:
        return {"answer": "搜索结果" in text, "reason": "看到了结果"}
    raise AssertionError("unexpected LLM call: " + text[:200])


RECIPE = {
    "id": "shop_prices",
    "goal": "在商店搜索 {{keyword}},记录前 {{max_items}} 个商品的价格",
    "start_url": "https://shop.test/",
    "allowed_domains": ["shop.test"],
    "inputs": {"type": "object", "properties": {
        "keyword": {"type": "string", "description": "搜索词"},
        "max_items": {"type": "integer", "default": 2, "minimum": 1}}, "required": ["keyword"]},
    "outputs": {"type": "object", "required": ["items"], "properties": {"items": {"type": "array", "items": {
        "type": "object", "required": ["name", "price"],
        "properties": {"name": {"type": "string"}, "price": {"type": "number"}}}}}},
    "steps": [
        {"id": "search", "action": "type", "intent": "在搜索框输入关键词并搜索",
         "target": {"tag": "input", "type": "search", "label": ["搜索商品", "Search"], "css": "#q"},
         "text": "{{keyword}}", "submit": True, "expect": {"url_contains": "/search"}},
        {"id": "has_results", "action": "ai_check", "intent": "确认有结果", "question": "页面是否显示了搜索结果?",
         "else": [{"id": "no_results", "action": "ask_human", "intent": "无结果", "question": "没有结果"}]},
        {"id": "list", "action": "extract", "intent": "提取结果里的商品名称", "instruction": "商品名称列表",
         "schema": {"type": "object", "properties": {"names": {"type": "array", "items": {"type": "string"}}},
                    "required": ["names"]}, "save_as": "names"},
        {"id": "each", "action": "foreach", "intent": "逐个打开商品", "items": "{{vars.names.names}}",
         "limit": "{{max_items}}", "as": "name", "steps": [
            {"id": "open", "action": "click", "intent": "打开商品 {{name}}", "target": {"tag": "a", "label": "{{name}}"},
             "expect": {"url_contains": "/p/"}},
            {"id": "price", "action": "extract", "intent": "提取名称和价格",
             "schema": {"type": "object", "properties": {"name": {"type": "string"}, "price": {"type": "number"}},
                        "required": ["name", "price"]}, "append_to": "items"},
            {"id": "back", "action": "go_back", "intent": "回到结果页", "expect": {"url_contains": "/search"}},
         ]},
    ],
}


async def _no_confirm(_):
    return True


async def _ask(_):
    return "ok"


def make_runner(site=None, handler=extract_handler, recipe=None, heal=True, tmp_path=None):
    r = Recipe.from_dict(recipe or RECIPE)
    if tmp_path is not None:
        r.path = tmp_path / f"{r.id}.yaml"
    sess = FakeSession(site or FakeSite())
    llm = FakeLLM(handler)
    runner = RecipeRunner(r, sess, llm, AgentConfig(trace_dir=None), heal=heal, confirm=_no_confirm, ask=_ask)
    return runner, sess, llm


# ---------------------------------------------------------------- template
def test_render_keeps_types_and_errors():
    scope = {"n": 3, "kw": "键盘", "item": {"title": "A"}}
    assert render("{{n}}", scope) == 3
    assert render("找 {{kw}} x{{n}}", scope) == "找 键盘 x3"
    assert render({"a": ["{{item.title}}"]}, scope) == {"a": ["A"]}
    assert render("{{secret:PW}}", scope) == "{{secret:PW}}"
    with pytest.raises(TemplateError):
        render("{{nope}}", scope)


# ------------------------------------------------------------------ recipe
def test_recipe_validation_and_params(tmp_path):
    r = Recipe.from_dict(RECIPE)
    assert r.validate_params({"keyword": "x"}) == {"keyword": "x", "max_items": 2}
    with pytest.raises(RecipeError):
        r.validate_params({"max_items": 2})
    with pytest.raises(RecipeError):
        r.validate_params({"keyword": "x", "max_items": 0})
    bad = json.loads(json.dumps(RECIPE))
    bad["steps"][0]["text"] = "{{undefined_param}}"
    with pytest.raises(RecipeError, match="undefined_param"):
        Recipe.from_dict(bad)
    bad2 = {**RECIPE, "steps": [{"action": "teleport", "intent": "x"}]}
    with pytest.raises(RecipeError):
        Recipe.from_dict(bad2)
    # round trip through YAML keeps everything and assigns no duplicate ids
    p = r.save(tmp_path / "r.yaml")
    r2 = Recipe.load(p)
    assert r2.to_dict()["steps"] == r.to_dict()["steps"]


# ------------------------------------------------------------------ locate
def test_locate_semantic_and_ambiguity():
    obs = build_observation({"url": "u", "title": "", "vw": 1, "vh": 1, "scrollY": 0, "scrollH": 1, "total": 3,
                             "frames": 0, "challenge": [], "text": "", "textLen": 0, "elements": [
                                 _el("input", "搜索商品", "#new-id", type="search"),
                                 _el("a", "详情", "a:nth-of-type(1)"), _el("a", "详情", "a:nth-of-type(2)")]})
    # css changed, but label + type still identify the search box
    m, _ = locate({"tag": "input", "type": "search", "label": "搜索", "css": "#q"}, obs)
    assert m and m.element.css == "#new-id"
    # two identical "详情" links: ambiguous unless nth is given
    assert locate({"tag": "a", "label": "详情"}, obs)[0] is None
    assert locate({"tag": "a", "label": "详情", "nth": 1}, obs)[0].element.css == "a:nth-of-type(2)"
    assert locate({"label": "完全不相关的东西"}, obs)[0] is None



def test_locate_same_destination_is_not_ambiguous():
    """Cover-image link and title link of one book: same href, either click is right."""
    obs = build_observation({"url": "u", "title": "", "vw": 1, "vh": 1, "scrollY": 0, "scrollH": 1, "total": 4,
                             "frames": 0, "challenge": [], "text": "", "textLen": 0, "elements": [
                                 _el("a", "The Black Maria", "a.img", href="/maria", pos=1),
                                 _el("a", "The Black Maria", "a.title", href="/maria"),
                                 _el("a", "详情", "a.d1", href="/x"), _el("a", "详情", "a.d2", href="/y")]})
    m, _ = locate({"tag": "a", "label": "The Black Maria"}, obs)
    assert m and m.element.css == "a.title"  # the one in view
    assert locate({"tag": "a", "label": "详情"}, obs)[0] is None  # different destinations stay ambiguous

# ------------------------------------------------------------------ runner
async def test_replay_full_flow(tmp_path):
    runner, sess, llm = make_runner(tmp_path=tmp_path)
    res = await runner.run({"keyword": "机械键盘"})
    assert res.success, res.error
    assert res.result["items"] == [{"name": "键盘A", "price": 199}, {"name": "键盘B", "price": 299}]
    assert set(res.result) == {"items"}  # the "names" listing stays an intermediate var
    assert ("type", "机械键盘", True) in sess.actions
    assert [a for a in sess.actions if a[0] == "click"] == [("click", "键盘A"), ("click", "键盘B")]
    assert not res.heals
    # deterministic steps used no LLM; only ai_check + extracts did (1 + 1 + 2)
    assert llm.usage["calls"] == 4


async def test_replay_param_changes_loop_count(tmp_path):
    runner, sess, _ = make_runner(tmp_path=tmp_path)
    res = await runner.run({"keyword": "k", "max_items": 3})
    assert res.success and len(res.result["items"]) == 3


async def test_heal_when_page_changed(tmp_path):
    # The site renamed the search box: label and css no longer match.
    site = FakeSite(search_css="#search-v2")
    site_elements = site.elements

    def renamed():
        els = site_elements()
        if site.url == "https://shop.test/":
            els[0]["label"] = "输入你想找的宝贝"
            els[0]["type"] = "text"
        return els

    site.elements = renamed

    def handler(system, text, schema):
        if "修复" in system:
            return {"element_id": 0, "reason": "唯一的输入框就是搜索框"}
        return extract_handler(system, text, schema)

    runner, sess, _ = make_runner(site=site, handler=handler, tmp_path=tmp_path)
    res = await runner.run({"keyword": "k", "max_items": 1})
    assert res.success, res.error
    assert [s.status for s in res.steps if s.id == "search"] == ["healed"]
    assert res.heals[0]["new_target"]["css"] == "#search-v2"
    # Without healing the same run fails at that step.
    runner2, _, _ = make_runner(site=FakeSite(search_css="#search-v2"), handler=handler, heal=False,
                                recipe={**RECIPE, "steps": [{**RECIPE["steps"][0],
                                                             "target": {"label": "不存在的框"}}]},
                                tmp_path=tmp_path)
    res2 = await runner2.run({"keyword": "k"})
    assert not res2.success and "search" in res2.error


async def test_apply_heals_keeps_templates(tmp_path):
    r = Recipe.from_dict(RECIPE)
    patches = [{"kind": "target", "version": 1, "step_id": "search",
                "new_target": {"tag": "input", "label": "输入你想找的宝贝", "css": "#search-v2"}}] * 2 + [
               {"kind": "target", "version": 1, "step_id": "open",
                "new_target": {"tag": "a", "label": "键盘A", "css": "li > a.title"}}] * 2
    changed = apply_heals(r, patches, min_count=2)
    assert set(changed) == {"search", "open"} and r.version == 2
    from cloakup.recipe import find_step
    assert find_step(r.steps, "search")["target"]["label"][0] == "输入你想找的宝贝"
    assert "搜索商品" in find_step(r.steps, "search")["target"]["label"]
    assert find_step(r.steps, "open")["target"]["label"] == "{{name}}"  # template kept
    assert find_step(r.steps, "open")["target"]["css"] == "li > a.title"


async def test_domain_guard_in_replay(tmp_path):
    bad = {**RECIPE, "start_url": "https://evil.test/"}
    runner, sess, _ = make_runner(recipe=bad, tmp_path=tmp_path, heal=False)
    res = await runner.run({"keyword": "k"})
    assert not res.success and "evil.test" in res.error and sess.actions == []


async def test_output_schema_enforced(tmp_path):
    rec = {**RECIPE, "steps": RECIPE["steps"][:1]}  # never extracts items
    runner, _, _ = make_runner(recipe=rec, tmp_path=tmp_path)
    res = await runner.run({"keyword": "k"})
    assert not res.success and "outputs" in res.error


# ----------------------------------------------------------------- compile
TRACE = [
    {"type": "task", "task": "搜索 机械键盘 并记录第一个商品价格", "params": {"keyword": "机械键盘"},
     "output_schema": None, "start_url": "https://shop.test/"},
    {"type": "observe", "url": "https://shop.test/", "title": "Shop"},
    {"type": "action", "tool": "click", "input": {"element_id": 1}, "error": True, "result": "timeout",
     "element": {"tag": "a", "label": "帮助"}},
    {"type": "action", "tool": "type_text", "input": {"element_id": 0, "text": "机械键盘", "submit": True},
     "error": False, "element": {"tag": "input", "type": "search", "label": "搜索商品", "css": "#q"}},
    {"type": "result", "success": True, "summary": "ok", "result": {"price": 199}},
]


def test_summarize_trace_hints():
    _, text = summarize_trace(TRACE)
    assert "参数 keyword='机械键盘'" in text and "✗失败" in text and "#q" in text
    with pytest.raises(RecipeError):
        summarize_trace(TRACE[:-1] + [{"type": "result", "success": False}])


async def test_compile_retries_until_valid(tmp_path):
    (tmp_path / "trace.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in TRACE))
    good = {"id": "x", "goal": "搜索 {{keyword}}", "start_url": "https://shop.test/",
            "inputs": {"type": "object", "properties": {"keyword": {"type": "string"}}, "required": ["keyword"]},
            "steps": [{"id": "search", "action": "type", "intent": "搜索", "text": "{{keyword}}",
                       "target": {"label": "搜索商品"}, "submit": True}]}
    bad = {**good, "steps": [{**good["steps"][0], "text": "{{kw}}"}]}  # undefined placeholder
    outputs = [bad, good]
    llm = FakeLLM(lambda s, t, sc: outputs.pop(0))
    recipe = await compile_trace(tmp_path, llm, recipe_id="shop_search")
    assert recipe.id == "shop_search" and recipe.input_names() == {"keyword"}
    assert "kw" in llm.calls[1]["text"]  # the error was fed back


# ------------------------------------------------------------------ params
async def test_resolve_request_reports_missing():
    r = Recipe.from_dict(RECIPE)

    def handler(system, text, schema):
        if "recipe_id" in schema["properties"]:
            return {"recipe_id": "shop_prices", "reason": "购物比价"}
        assert "required" not in schema["properties"]["params"]  # never forces invention
        return {"params": {"max_items": 5}, "missing": ["keyword"]}

    res = await resolve("帮我比较前5个商品价格", [r], FakeLLM(handler), today=dt.date(2026, 11, 11))
    assert res.recipe.id == "shop_prices" and res.params == {"max_items": 5} and res.missing == ["keyword"]


# ------------------------------------------------------------------- batch
def test_load_rows_coerces_csv(tmp_path):
    p = tmp_path / "in.csv"
    p.write_text("keyword,max_items\n键盘,3\n鼠标,\n", encoding="utf-8")
    rows = load_rows(p, Recipe.from_dict(RECIPE))
    assert rows == [{"keyword": "键盘", "max_items": 3}, {"keyword": "鼠标", "max_items": 2}]


async def test_run_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(batch_mod, "BrowserSession", lambda cfg, pacing: FakeSession(FakeSite()))
    r = Recipe.from_dict(RECIPE)
    r.path = tmp_path / "r.yaml"
    out = tmp_path / "out.jsonl"
    rows = [{"keyword": "a"}, {"keyword": "b", "max_items": 1}, {"max_items": 1}]  # last row invalid
    stats = await run_batch(r, rows, [Slot("p1"), Slot("p2")], out, llm=FakeLLM(extract_handler),
                            agent_cfg=AgentConfig(trace_dir=None), rest=(0, 0), confirm=_no_confirm, ask=_ask)
    assert stats == {"ok": 2, "failed": 0, "invalid": 1}
    recs = [json.loads(line) for line in out.read_text().splitlines()]
    assert sorted(r["row"] for r in recs) == [0, 1, 2]


# ------------------------------------------------------- compile static checks
def _compiled_like_deepseek():
    """Shape of the recipe deepseek-v4.1-flash compiled on 2026-11-12 (both defects included)."""
    books = {"type": "array", "items": {"type": "object", "properties": {"title": {"type": "string"}}}}
    return {
        "id": "t", "version": 1, "goal": "g", "start_url": "https://books.toscrape.com/",
        "inputs": {"type": "object", "properties": {"category": {"type": "string"}}, "required": ["category"]},
        "outputs": {"type": "object", "required": ["books"], "properties": {"books": books}},
        "steps": [
            {"id": "open", "action": "click", "intent": "i",
             "target": {"tag": "a", "role": "link", "label": "{{category}}", "css": "li:nth-of-type(1) > a"},
             "expect": {"url_contains": "/category/"}},
            {"id": "ext", "action": "extract", "intent": "i", "save_as": "books",
             "schema": {"type": "object", "properties": {"books": books}},
             "expect": {"element": {"tag": "article", "role": "product", "css": "article.product_pod"}}},
        ]}


def test_sanitize_fixes_instance_specific_bits():
    from cloakup.compile import sanitize

    d = _compiled_like_deepseek()
    notes = sanitize(d)
    assert d["steps"][0]["target"] == {"tag": "a", "label": "{{category}}"}
    assert "expect" not in d["steps"][1]
    assert len(notes) == 3


def test_lint_catches_nested_output_and_merge_fixes_it():
    from cloakup.compile import lint

    d = _compiled_like_deepseek()
    problems = lint(d)
    assert problems and "merge" in problems[0]
    st = d["steps"][1]
    st.pop("save_as")
    st["merge"] = True
    assert lint(d) == []
    st.pop("merge")
    assert any("没有任何步骤产生" in p for p in lint(d))


async def test_compile_retries_until_lint_passes(tmp_path):
    from cloakup.compile import compile_trace

    trace = tmp_path / "run"
    trace.mkdir()
    events = [{"step": 0, "type": "task", "task": "t", "params": {"category": "Travel"}, "start_url": "https://books.toscrape.com/"},
              {"step": 1, "type": "action", "tool": "click", "input": {"element_id": 3}, "result": "ok", "error": False},
              {"step": 2, "type": "result", "success": True, "summary": "ok", "result": {"books": []}}]
    (trace / "trace.jsonl").write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    bad, good = _compiled_like_deepseek(), _compiled_like_deepseek()
    good["steps"][1].pop("save_as")
    good["steps"][1]["merge"] = True
    replies = [bad, good]
    seen = []

    def reply(system, text, schema):
        seen.append(text)
        return replies.pop(0)

    recipe = await compile_trace(trace, FakeLLM(reply))
    assert recipe.steps[1].get("merge") is True
    assert "merge" in seen[1]  # the lint problem was fed back


def test_generalize_heal_target_back_to_template():
    from cloakup.runner import generalize_target

    raw = {"tag": "button", "label": "{{category}}", "href_contains": "/genre/"}
    new = {"tag": "a", "label": "Mystery", "href_contains": "../books/mystery_3/index.html", "css": "li:nth-of-type(4) > a"}
    assert generalize_target(new, raw, {"category": "Mystery"}) == {"tag": "a", "label": "{{category}}"}
    # two different parameter values now vote for the same patch
    other = {"tag": "a", "label": "Poetry", "href_contains": "../books/poetry_23/index.html", "css": "x"}
    assert generalize_target(other, raw, {"category": "Poetry"}) == {"tag": "a", "label": "{{category}}"}
    # nested scope + non-parameterized targets untouched
    assert generalize_target({"tag": "a", "label": "Olio"}, {"label": "{{item.t}}"}, {"item": {"t": "Olio"}}) \
        == {"tag": "a", "label": "{{item.t}}"}
    assert generalize_target({"label": "Go", "css": "#go"}, {"label": "Search"}, {}) == {"label": "Go", "css": "#go"}
