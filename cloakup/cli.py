"""Command line.

    cloakup run "<task with {{params}}>" --params '{...}' --output-schema out.json   AI exploration
    cloakup recipe compile runs/<ts> --id my_flow                                    trace -> recipe
    cloakup recipe run recipes/my_flow.yaml --params '{...}'                         replay
    cloakup recipe verify recipes/my_flow.yaml --samples samples.jsonl               replay w/o healing
    cloakup recipe heals recipes/my_flow.yaml [--apply]                              review self-healing
    cloakup ask "自然语言请求" --recipes recipes/                                      NL -> recipe + params -> run
    cloakup batch recipes/my_flow.yaml inputs.csv --profiles ./profiles/a,./profiles/b
    cloakup open --profile ./profiles/a --url https://...                            manual login / warm-up
    cloakup check-api                                                                model service self-test
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from .browser import BrowserSession
from .client import ModelClient, default_model
from .config import AgentConfig, BrowserConfig, PacingConfig


# ------------------------------------------------------------------ options
def _browser_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("浏览器")
    g.add_argument("--profile", default="./profiles/default", help="持久化浏览器配置目录(一个目录 = 一个身份)")
    g.add_argument("--proxy", help="代理,如 http://user:pass@host:port 或 socks5://...(建议住宅代理)")
    g.add_argument("--no-geoip", action="store_true", help="不按代理出口 IP 自动设置时区/语言")
    g.add_argument("--headless", action="store_true", help="无头模式(更容易被识别,默认有头)")
    g.add_argument("--preset", choices=["default", "careful"], default="careful", help="拟人化预设")
    g.add_argument("--locale")
    g.add_argument("--timezone")


def _api_args(g: Any) -> None:
    g.add_argument("--model", default=default_model(), help="模型名(网关里的名字),默认取 CLOAKUP_MODEL")
    g.add_argument("--base-url", default=None, help="模型服务地址,默认取 ANTHROPIC_BASE_URL")
    g.add_argument("--api-compat", default=None, choices=["auto", "full", "basic", "minimal"],
                   help="接口兼容级别,默认 auto(遇到不支持的功能自动降级),也可用 CLOAKUP_API_COMPAT")


_CLIENT: ModelClient | None = None


def _client(a: argparse.Namespace) -> ModelClient:
    """One client per process, so a compat downgrade is learned only once."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = ModelClient(compat=a.api_compat, base_url=a.base_url)
    return _CLIENT


def _llm(a: argparse.Namespace) -> Any:
    from .llm import LLM

    return LLM(_client(a), model=a.model)


def _agent_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("AI")
    _api_args(g)
    g.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    g.add_argument("--max-steps", type=int, default=40)
    g.add_argument("--allow-domain", action="append", default=[], help="只允许访问这些域名(可多次)")
    g.add_argument("--secret", action="append", default=[],
                   help="NAME=ENV_VAR:代理可用 {{secret:NAME}} 输入该环境变量的值,值不会发给模型")
    g.add_argument("--no-confirm", action="store_true", help="敏感点击(支付/删除等)不再询问")
    g.add_argument("--no-screenshots", action="store_true", help="只用 DOM 文本,不发截图(省 token)")
    g.add_argument("--trace-dir", default="./runs")


def _browser_cfg(a: argparse.Namespace, profile: str | None = None) -> BrowserConfig:
    return BrowserConfig(profile_dir=profile or a.profile, proxy=a.proxy, geoip=not a.no_geoip,
                         headless=a.headless, human_preset=a.preset, locale=a.locale, timezone=a.timezone)


def _agent_cfg(a: argparse.Namespace) -> AgentConfig:
    return AgentConfig(model=a.model, effort=a.effort, max_steps=a.max_steps,
                       screenshots=not a.no_screenshots, allowed_domains=a.allow_domain,
                       confirm_sensitive=not a.no_confirm, secrets=_parse_secrets(a.secret),
                       trace_dir=a.trace_dir)


def _parse_secrets(items: list[str]) -> dict[str, str]:
    secrets: dict[str, str] = {}
    for item in items:
        name, _, env = item.partition("=")
        env = env or name
        value = os.environ.get(env)
        if not value:
            sys.exit(f"--secret {item}: 环境变量 {env} 未设置")
        secrets[name] = value
    return secrets


def _json_arg(value: str | None) -> Any:
    if not value:
        return None
    if value.startswith("@"):
        return json.loads(Path(value[1:]).read_text(encoding="utf-8"))
    return json.loads(value)


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


# ----------------------------------------------------------------- commands
async def cmd_run(a: argparse.Namespace) -> int:
    from .agent import WebAgent

    agent = WebAgent(_agent_cfg(a), _browser_cfg(a), PacingConfig(), client=_client(a))
    res = await agent.run(a.task, start_url=a.url, params=_json_arg(a.params) or {},
                          output_schema=_json_arg(a.output_schema))
    print("\n" + ("✅ 成功" if res.success else "❌ 未完成") + f"  ({res.steps} 步)\n{res.summary}")
    if res.result is not None:
        _print_json(res.result)
    if res.data:
        _print_json(res.data)
    print(f"tokens: {res.usage}  (接口级别: {_client(a).level})")
    if res.trace_dir:
        print(f"trace: {res.trace_dir}")
        if res.success:
            print(f"编译成可回放流程:  cloakup recipe compile {res.trace_dir} --id <流程id>")
    return 0 if res.success else 1


async def cmd_compile(a: argparse.Namespace) -> int:
    from .compile import compile_trace

    recipe = await compile_trace(a.trace, _llm(a), recipe_id=a.id, notes=a.notes or "")
    path = recipe.save(Path(a.out) / f"{recipe.id}.yaml")
    print(f"✅ 已生成流程 {path}  (参数: {', '.join(sorted(recipe.input_names())) or '无'})")
    print(f"请检查后试跑: cloakup recipe verify {path} --samples <样例参数.jsonl>")
    return 0


async def _replay(a: argparse.Namespace, recipe: Any, params: dict[str, Any], heal: bool) -> Any:
    from .runner import RecipeRunner

    session = BrowserSession(_browser_cfg(a), PacingConfig())
    await session.start()
    try:
        runner = RecipeRunner(recipe, session, _llm(a), _agent_cfg(a), heal=heal)
        return await runner.run(params)
    finally:
        await session.close()


def _report(res: Any) -> None:
    print("\n" + ("✅ 成功" if res.success else f"❌ 失败: {res.error}") + f"  ({res.seconds:.0f}s)")
    for s in res.steps:
        mark = {"ok": "✓", "healed": "🩹", "skipped": "·", "optional_failed": "~", "failed": "✗"}[s.status]
        print(f"  {mark} {s.id:<24} {s.action:<9} {s.seconds:5.1f}s  {s.detail[:70]}")
    if res.heals:
        print(f"🩹 {len(res.heals)} 处自愈,已记录;查看/应用: cloakup recipe heals <recipe> --apply")
    _print_json(res.result)
    print(f"LLM: {res.usage}")


async def cmd_recipe_run(a: argparse.Namespace) -> int:
    from .recipe import Recipe

    recipe = Recipe.load(a.recipe)
    res = await _replay(a, recipe, _json_arg(a.params) or {}, heal=not a.no_heal)
    _report(res)
    return 0 if res.success else 1


async def cmd_verify(a: argparse.Namespace) -> int:
    from .batch import load_rows
    from .recipe import Recipe

    recipe = Recipe.load(a.recipe)
    rows = load_rows(a.samples, recipe)
    failures = 0
    for i, row in enumerate(rows):
        print(f"\n=== 样例 {i}: {json.dumps(row, ensure_ascii=False)}")
        res = await _replay(a, recipe, row, heal=False)
        _report(res)
        failures += not res.success
    print(f"\n验证结果: {len(rows) - failures}/{len(rows)} 通过(未开启自愈)")
    return 0 if failures == 0 else 1


async def cmd_heals(a: argparse.Namespace) -> int:
    from .recipe import Recipe, apply_heals
    from .runner import HealStore

    recipe = Recipe.load(a.recipe)
    patches = HealStore(recipe).load()
    current = [p for p in patches if p.get("version") == recipe.version]
    if not current:
        print("当前版本没有自愈记录。")
        return 0
    for p in current:
        what = json.dumps(p.get("new_target"), ensure_ascii=False) if p["kind"] == "target" else p.get("summary")
        print(f"- {p['step_id']} [{p['kind']}] {what}")
    if any(p["kind"] == "agent" for p in current):
        print("⚠️ 有步骤是靠 AI 子代理修复的,说明页面流程可能变了:建议重新探索并编译。")
    if a.apply:
        changed = apply_heals(recipe, current, min_count=a.min_count)
        if changed:
            recipe.save()
            print(f"✅ 已更新步骤 {changed},流程版本 -> v{recipe.version}")
        else:
            print(f"没有达到 {a.min_count} 次一致修复的步骤,未修改。")
    return 0


async def cmd_ask(a: argparse.Namespace) -> int:
    from .params import resolve
    from .recipe import load_recipes

    llm = _llm(a)
    recipes = load_recipes(a.recipes)
    r = await resolve(a.request, recipes, llm)
    if r.recipe is None:
        print(f"没有匹配的流程({r.reason}),改用 AI 自主执行。")
        a.task, a.url, a.params, a.output_schema = a.request, None, None, None
        return await cmd_run(a)
    print(f"→ 流程 {r.recipe.id},参数 {json.dumps(r.params, ensure_ascii=False)}")
    for name in r.missing:
        spec = (r.recipe.inputs.get("properties") or {}).get(name, {})
        raw = await asyncio.to_thread(input, f"请提供参数 {name}({spec.get('description', '')}): ")
        r.params[name] = _coerce_cli(raw, spec)
    res = await _replay(a, r.recipe, r.params, heal=True)
    _report(res)
    return 0 if res.success else 1


def _coerce_cli(raw: str, spec: dict[str, Any]) -> Any:
    from .batch import _coerce

    return _coerce(raw, spec)


async def cmd_batch(a: argparse.Namespace) -> int:
    from .batch import Slot, load_rows, run_batch
    from .recipe import Recipe

    recipe = Recipe.load(a.recipe)
    rows = load_rows(a.inputs, recipe)
    profiles = [p.strip() for p in a.profiles.split(",") if p.strip()]
    proxies: list[str | None] = [None] * len(profiles)
    if a.proxies:
        lines = [x.strip() for x in Path(a.proxies).read_text().splitlines() if x.strip()]
        if len(lines) < len(profiles):
            sys.exit(f"--proxies 只有 {len(lines)} 行,少于 {len(profiles)} 个配置")
        proxies = lines[: len(profiles)]
    slots = [Slot(p, x) for p, x in zip(profiles, proxies)]
    print(f"{len(rows)} 组参数,{len(slots)} 个并发身份 -> {a.out}")
    stats = await run_batch(recipe, rows, slots, a.out, llm=_llm(a), agent_cfg=_agent_cfg(a),
                            browser_tpl=_browser_cfg(a), heal=not a.no_heal,
                            rest=(a.rest_min, a.rest_max))
    print(f"完成: {stats}")
    return 0 if stats["failed"] == 0 and stats["invalid"] == 0 else 1


async def cmd_open(a: argparse.Namespace) -> int:
    s = BrowserSession(_browser_cfg(a), PacingConfig())
    await s.start()
    try:
        if a.url:
            await s.page.goto(a.url)
        await asyncio.to_thread(input, "浏览器已打开。手动操作完成后按回车关闭(登录状态会保存到配置目录)…")
    finally:
        await s.close()
    return 0


async def cmd_check_api(a: argparse.Namespace) -> int:
    """Tiny requests: text, tool use + image input, schema-valid JSON."""
    client = _client(a)
    llm = _llm(a)
    raw = client.raw
    print(f"服务地址: {getattr(raw, 'base_url', '?')}\n模型: {a.model}\n兼容设置: {a.api_compat or os.environ.get('CLOAKUP_API_COMPAT') or 'auto'}")
    ok = True

    async def step(name: str, coro: Any) -> Any:
        nonlocal ok
        try:
            out = await coro
            print(f"  ✓ {name}: {out}")
            return out
        except Exception as exc:  # noqa: BLE001 - report every failure kind
            ok = False
            print(f"  ✗ {name}: {type(exc).__name__}: {str(exc).splitlines()[0][:300]}")
            return None

    async def text() -> str:
        r = await llm._create(system="只回复 OK", messages=[{"role": "user", "content": "ping"}],
                              output_config={"effort": "low"})
        return f"{llm._text(r).strip()[:40]!r}  → 接口级别 {client.level}"

    tool = {"name": "report", "description": "报告图片颜色", "input_schema": {
        "type": "object", "properties": {"color": {"type": "string", "enum": ["red", "green", "blue", "other"]}},
        "required": ["color"]}}

    async def tool_image() -> str:
        import base64
        r = await llm._create(
            system="用 report 工具回答。", tools=[tool],
            messages=[{"role": "user", "content": [
                {"type": "text", "text": "这张图片是什么颜色?调用 report。"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                             "data": base64.standard_b64encode(_solid_png()).decode()}}]}],
            output_config={"effort": "low"})
        calls = [b for b in r.content if b.type == "tool_use"]
        if not calls:
            raise RuntimeError("模型没有调用工具(网关可能不支持 tool use)")
        color = calls[0].input.get("color")
        if color != "red":
            raise RuntimeError(f"图片识别结果 {color!r},期望 'red'(网关可能丢弃了图片)")
        return "tool_use 正常,图片识别正确 (red)"

    async def structured() -> str:
        d = await llm.json(system="按要求输出。", content="给出数字 7 的平方", effort="low",
                           schema={"type": "object", "required": ["value"],
                                   "properties": {"value": {"type": "integer"}}})
        if d.get("value") != 49:
            raise RuntimeError(f"结果 {d}")
        return f"{d}  (结构化输出{'可用' if client.structured_outputs else '不可用,已用文本 JSON + 校验'})"

    await step("文本请求", text())
    await step("工具调用 + 图片", tool_image())
    await step("JSON 输出", structured())
    print(f"最终接口级别: {client.level}"
          + ("" if client.manages_context else "(无服务端上下文清理:代理超过阈值时会带进度摘要重开对话)"))
    print(f"tokens: {llm.usage}")
    print("✅ 模型服务可用" if ok else "❌ 有检查未通过")
    return 0 if ok else 1


def _solid_png(w: int = 64, h: int = 64, rgb: tuple[int, int, int] = (220, 30, 30)) -> bytes:
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


# --------------------------------------------------------------------- main
def cmd_report(a) -> None:
    from datetime import date, datetime
    from types import SimpleNamespace
    from .daily_report import ensure, generate, report_timezone
    from .sourcing import CaseStore
    from .storage import Database
    root = Path(a.root).resolve()
    db = Database(root)
    manager = SimpleNamespace(root=root, db=db, cases=CaseStore(root, db))
    if a.backfill:
        made = ensure(manager)
        print("已生成：" + (", ".join(made) if made else "无缺失日期"))
        return
    day = date.fromisoformat(a.day) if a.day else datetime.now(report_timezone()).date()
    print(generate(manager, day)["markdown"])


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="cloakup", description="CloakBrowser + Claude 拟人化网页自动化")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("web", help="启动本机网页工作台")
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("--root", default=".", help="项目数据目录（流程、浏览器身份、运行记录）")
    w.set_defaults(fn=None)

    r = sub.add_parser("run", help="AI 自主完成一个(可带参数的)任务")
    r.add_argument("task", help="任务描述,可含 {{参数}}")
    r.add_argument("--url", help="起始 URL,可含 {{参数}}")
    r.add_argument("--params", help="参数 JSON,或 @文件")
    r.add_argument("--output-schema", help="结果 JSON Schema,或 @文件")
    _browser_args(r)
    _agent_args(r)
    r.set_defaults(fn=cmd_run)

    rc = sub.add_parser("recipe", help="流程:编译/回放/验证/自愈").add_subparsers(dest="sub", required=True)
    c = rc.add_parser("compile", help="把一次成功的执行记录编译成参数化流程")
    c.add_argument("trace", help="runs/<时间戳> 目录")
    c.add_argument("--id", help="流程 id(小写英文下划线)")
    c.add_argument("--out", default="recipes")
    c.add_argument("--notes", help="给编译器的补充说明,例如哪些值应该是参数")
    _api_args(c.add_argument_group("AI"))
    c.set_defaults(fn=cmd_compile)

    rr = rc.add_parser("run", help="按参数回放流程")
    rr.add_argument("recipe")
    rr.add_argument("--params", help="参数 JSON,或 @文件")
    rr.add_argument("--no-heal", action="store_true", help="失败时不调用 AI 自愈")
    _browser_args(rr)
    _agent_args(rr)
    rr.set_defaults(fn=cmd_recipe_run)

    v = rc.add_parser("verify", help="用多组样例参数回放(关闭自愈),检查流程是否可靠")
    v.add_argument("recipe")
    v.add_argument("--samples", required=True, help="样例参数 .jsonl 或 .csv")
    _browser_args(v)
    _agent_args(v)
    v.set_defaults(fn=cmd_verify)

    h = rc.add_parser("heals", help="查看自愈记录,--apply 写回流程")
    h.add_argument("recipe")
    h.add_argument("--apply", action="store_true")
    h.add_argument("--min-count", type=int, default=2, help="同一修复出现至少几次才写回")
    h.set_defaults(fn=cmd_heals)

    k = sub.add_parser("ask", help="自然语言请求 -> 选流程 + 抽取参数 -> 执行")
    k.add_argument("request")
    k.add_argument("--recipes", default="recipes")
    _browser_args(k)
    _agent_args(k)
    k.set_defaults(fn=cmd_ask)

    b = sub.add_parser("batch", help="同一流程跑多组参数")
    b.add_argument("recipe")
    b.add_argument("inputs", help=".csv(表头=参数名)或 .jsonl")
    b.add_argument("--profiles", default="./profiles/default", help="逗号分隔的配置目录;数量 = 并发数")
    b.add_argument("--proxies", help="代理列表文件,每行一个,与配置一一对应")
    b.add_argument("--out", default="results.jsonl")
    b.add_argument("--no-heal", action="store_true")
    b.add_argument("--rest-min", type=float, default=20.0, help="同一身份两次任务间最少休息秒数")
    b.add_argument("--rest-max", type=float, default=90.0)
    _browser_args(b)
    _agent_args(b)
    b.set_defaults(fn=cmd_batch)

    o = sub.add_parser("open", help="手动打开某个配置(登录、养号)")
    o.add_argument("--url")
    _browser_args(o)
    o.set_defaults(fn=cmd_open)

    ck = sub.add_parser("check-api", help="检查模型服务:文本、工具调用、图片、JSON 输出(3 项检查,几次很小的请求)")
    _api_args(ck)
    ck.set_defaults(fn=cmd_check_api)

    rp = sub.add_parser("report", help="生成执行日报（只读本地记录，不访问网站、不调用模型）")
    rp.add_argument("--root", default=".", help="项目数据目录，与 web --root 相同")
    rp.add_argument("--day", help="日期 YYYY-MM-DD，默认今天（截至当前）")
    rp.add_argument("--backfill", action="store_true", help="补生成所有已结束但缺少报告的日期")

    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(message)s")
    for noisy in ("httpx", "httpx2", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if a.cmd == "web":
        from .workbench import serve
        serve(Path(a.root), a.port)
        return
    if a.cmd == "report":
        cmd_report(a)
        return
    sys.exit(asyncio.run(a.fn(a)))


if __name__ == "__main__":
    main()
