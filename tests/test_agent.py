"""Offline tests: no browser binary, no API calls."""

from __future__ import annotations

import time
from types import SimpleNamespace as NS

import pytest

from cloakup import AgentConfig, WebAgent
from cloakup.config import PacingConfig
from cloakup.guard import Guard
from cloakup.observe import build_observation
from cloakup.pacing import Pacer, lognormal

RAW = {
    "url": "https://shop.test/", "title": "Shop", "vw": 1280, "vh": 720, "scrollY": 0, "scrollH": 3000,
    "total": 3, "frames": 0, "challenge": [], "text": "Welcome hunter2", "textLen": 15,
    "elements": [
        {"tag": "input", "type": "search", "role": "", "label": "搜索", "css": "#q", "x": 10, "y": 10,
         "w": 100, "h": 20, "pos": 0, "disabled": False, "value": ""},
        {"tag": "button", "type": "", "role": "", "label": "立即购买", "css": "#buy", "x": 50, "y": 50,
         "w": 80, "h": 30, "pos": 0, "disabled": False},
        {"tag": "a", "type": "", "role": "", "label": "下一页", "css": "a.next", "x": 50, "y": 900,
         "w": 40, "h": 20, "pos": 1, "disabled": False, "href": "/p2"},
    ],
}


# ---------------------------------------------------------------- pure units
def test_lognormal_bounds():
    vals = [lognormal(1.5, 0.6, 0.5, 4.0) for _ in range(2000)]
    assert min(vals) >= 0.5 and max(vals) <= 4.0


async def test_pacer_counts_llm_latency():
    p = Pacer(PacingConfig(think_median=0.2, think_min=0.1, think_max=0.3, min_interval=0.1))
    p.mark_action()
    time.sleep(0.35)  # "LLM latency" already exceeds the think time
    assert await p.before_action("click") == 0


def test_guard_domains_and_secrets():
    g = Guard(["shop.test"], True, {"PW": "hunter2"})
    assert g.check_url("https://shop.test/x") is None
    assert g.check_url("https://a.shop.test/x") is None
    assert g.check_url("https://evil.test/") is not None
    assert g.check_url("file:///etc/passwd") is not None
    assert g.resolve_secrets("pw={{secret:PW}}") == "pw=hunter2"
    with pytest.raises(KeyError):
        g.resolve_secrets("{{secret:NOPE}}")


def test_render_masks_secrets_and_flags():
    obs = build_observation({**RAW, "challenge": ["https://challenges.cloudflare.com/x"]})
    text = obs.render(mask={"PW": "hunter2"})
    assert "hunter2" not in text and "{{secret:PW}}" in text
    assert "[2] a" in text and "↓" in text
    assert "ask_human" in text


# ------------------------------------------------------------ agent loop
class FakeBrowser:
    def __init__(self):
        self.calls = []
        self.last_obs = None

    async def start(self):
        pass

    async def close(self):
        pass

    async def observe(self, screenshot=True):
        self.last_obs = build_observation(RAW)
        return self.last_obs

    def element(self, i):
        el = self.last_obs.find(i)
        if el is None:
            raise ValueError(f"element [{i}] not in the latest observation")
        return None, el

    async def navigate(self, url):
        self.calls.append(("navigate", url))
        return "ok"

    async def type_text(self, i, text, clear=True, submit=False):
        self.calls.append(("type", i, text, submit))
        return "typed"

    async def click(self, i):
        self.calls.append(("click", i))
        return "clicked"


def tool_use(id_, name, **inp):
    return NS(type="tool_use", id=id_, name=name, input=inp)


def resp(*blocks, stop="tool_use"):
    return NS(content=list(blocks), stop_reason=stop,
              usage=NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0))


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = NS(messages=NS(create=self._create))

    async def _create(self, **kw):
        self.requests.append({**kw, "messages": list(kw["messages"])})  # snapshot; the agent keeps appending
        return self.responses.pop(0)


def make_agent(responses, confirm_answer=True, **cfg):
    async def confirm(_):
        return confirm_answer

    async def ask(_):
        return "done by human"

    client = FakeClient(responses)
    agent = WebAgent(AgentConfig(trace_dir=None, secrets={"PW": "hunter2"}, **cfg),
                     confirm=confirm, ask=ask, client=client)
    async def scripted(messages):
        return await client._create(messages=messages)
    agent._call_model=scripted
    agent.reference_plan={'steps':['执行测试任务'],'success_criteria':['核验测试结果'],'risks':['测试边界']}
    fb = FakeBrowser()
    agent.browser = fb
    agent.executor.browser = fb
    return agent, fb, client


async def test_loop_happy_path():
    agent, fb, client = make_agent([
        resp(NS(type="text", text="搜索"), tool_use("t1", "type_text", element_id=0, text="{{secret:PW}}", submit=True)),
        resp(tool_use("t2", "save_data", key="price", value=42),
             tool_use("t3", "done", success=True, summary="found")),
    ])
    res = await agent.run("find price", start_url="https://shop.test/")
    assert res.success and res.summary == "found" and res.data == {"price": 42}
    assert ("type", 0, "hunter2", True) in fb.calls  # secret resolved only at the browser
    # The model never saw the secret value.
    assert "hunter2" not in repr(client.requests[-1]["messages"])
    # The observation is attached to the type_text result (last result of that turn).
    tr = client.requests[1]["messages"][-1]["content"][-1]
    assert tr["type"] == "tool_result" and any(b.get("type") == "text" and "URL:" in b["text"] for b in tr["content"])


async def test_sensitive_click_refused_and_followups_skipped():
    agent, fb, client = make_agent([
        resp(tool_use("t1", "click", element_id=1), tool_use("t2", "click", element_id=2)),
        resp(tool_use("t3", "done", success=False, summary="refused")),
    ], confirm_answer=False)
    res = await agent.run("buy")
    assert not res.success
    assert fb.calls == []  # neither click ran
    results = client.requests[1]["messages"][-1]["content"]
    assert results[0]["is_error"] and results[1]["is_error"]
    assert "跳过" in results[1]["content"][0]["text"]


async def test_domain_block_and_bad_index():
    agent, fb, client = make_agent([
        resp(tool_use("t1", "navigate", url="https://evil.test/")),
        resp(tool_use("t2", "click", element_id=99)),
        resp(tool_use("t3", "done", success=False, summary="x")),
    ], allowed_domains=["shop.test"])
    await agent.run("t")
    msgs = client.requests[2]["messages"]
    assert msgs[-3]["content"][0]["is_error"]  # navigate blocked
    assert msgs[-1]["content"][0]["is_error"]  # unknown element
    assert fb.calls == []


async def test_nudge_then_give_up():
    agent, _, _ = make_agent([resp(NS(type="text", text="hmm"), stop="end_turn")] * 3)
    res = await agent.run("t")
    assert not res.success and res.steps == 3


async def test_request_shape():
    agent, _, client = make_agent([resp(tool_use("t", "done", success=True, summary="ok"))])
    await agent.run("t")
    kw = client.requests[0]
    assert kw["messages"][0]["role"]=="user"
    assert agent.reference_plan["steps"]==["执行测试任务"]
    assert "temperature" not in kw and "thinking" not in kw
