"""ModelClient: gateway compat levels, auto-downgrade, and agent restart without server context editing."""

from types import SimpleNamespace as NS

import anthropic
import httpx
import pytest

from cloakup.client import ModelClient
from cloakup.config import AgentConfig
from cloakup.agent import WebAgent
from cloakup.llm import LLM

from test_agent import FakeBrowser, resp, tool_use


def _rejected(msg="Extra inputs are not permitted"):
    r = httpx.Response(400, request=httpx.Request("POST", "http://gw/v1/messages"))
    return anthropic.BadRequestError(msg, response=r, body=None)


class Gateway:
    """Fake Anthropic-compatible gateway: no beta endpoint; optionally no output_config."""

    def __init__(self, responses, beta=False, output_config=True):
        self.responses, self.calls = list(responses), []
        self.accept_beta, self.accept_oc = beta, output_config
        self.beta = NS(messages=NS(create=self._beta))
        self.messages = NS(create=self._ga)

    async def _beta(self, **kw):
        self.calls.append(("beta", {**kw, "messages": list(kw.get("messages", []))}))
        if not self.accept_beta:
            raise _rejected("unknown beta")
        return self.responses.pop(0)

    async def _ga(self, **kw):
        self.calls.append(("ga", {**kw, "messages": list(kw.get("messages", []))}))
        bad = {"betas", "fallbacks", "context_management", "cache_control"} & kw.keys()
        if bad or ("output_config" in kw and not self.accept_oc):
            raise _rejected(f"Extra inputs are not permitted: {sorted(bad) or 'output_config'}")
        return self.responses.pop(0)


def text_resp(t):
    return NS(content=[NS(type="text", text=t)], stop_reason="end_turn",
              usage=NS(input_tokens=1, output_tokens=1))


def test_shape_levels():
    kw = {"model": "m", "betas": ["x"], "fallbacks": "default", "cache_control": {}, "context_management": {},
          "output_config": {"effort": "low"},
          "system": [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}]}
    beta, full = ModelClient.shape("full", kw)
    assert beta and full == kw
    beta, basic = ModelClient.shape("basic", kw)
    assert not beta and set(basic) == {"model", "output_config", "system"}
    assert basic["system"][0]["cache_control"]
    _, minimal = ModelClient.shape("minimal", kw)
    assert set(minimal) == {"model", "system"} and "cache_control" not in minimal["system"][0]


async def test_auto_downgrade_latches():
    gw = Gateway([text_resp("a"), text_resp("b")])
    c = ModelClient(gw)
    await c.create(model="m", messages=[], betas=["x"], fallbacks="default", output_config={"effort": "low"})
    assert c.level == "basic"
    await c.create(model="m", messages=[], betas=["x"], output_config={"effort": "low"})
    # second request goes straight to the GA endpoint, no beta probe
    assert [k for k, _ in gw.calls] == ["beta", "ga", "ga"]


async def test_downgrade_to_minimal_and_text_json():
    gw = Gateway([text_resp('{"value": 49}')], output_config=False)
    llm = LLM(gw, model="m")
    d = await llm.json(system="s", content="q", schema={"type": "object", "properties": {"value": {"type": "integer"}}})
    assert d == {"value": 49} and llm.client.level == "minimal"
    # structured path was attempted once at full (no downgrade allowed), then text path walked down
    assert [k for k, _ in gw.calls] == ["beta", "beta", "ga", "ga"]


async def test_no_downgrade_for_schema_rejection():
    """A schema the API rejects must not be read as a gateway limitation."""
    gw = Gateway([text_resp('{"value": 1}')], beta=True)
    calls = []

    async def beta(**kw):
        calls.append(kw)
        if "format" in kw.get("output_config", {}):
            raise _rejected("schema too complex")
        return gw.responses.pop(0)

    gw.beta.messages.create = beta
    llm = LLM(gw, model="m")
    await llm.json(system="s", content="q", schema={"type": "object"})
    assert llm.client.level == "full" and len(calls) == 2


async def test_fixed_level_does_not_downgrade():
    c = ModelClient(Gateway([]), compat="full")
    with pytest.raises(anthropic.BadRequestError):
        await c.create(model="m", messages=[])
    assert c.level == "full"


async def test_agent_restarts_conversation_without_context_editing():
    big = NS(input_tokens=70000, output_tokens=5, cache_read_input_tokens=0)
    r1 = resp(tool_use("t1", "scroll", direction="down"))
    r1.usage = big
    gw = Gateway([r1, resp(tool_use("t2", "done", success=True, summary="ok"))])
    agent = WebAgent(AgentConfig(trace_dir=None), client=ModelClient(gw, compat="basic"))
    async def scripted(messages):
        response=await gw.messages.create(messages=messages)
        agent._last_input=response.usage.input_tokens
        return response
    agent._call_model=scripted
    agent.reference_plan={'steps':['test'],'success_criteria':['test'],'risks':[]}
    fb = FakeBrowser()
    agent.browser = agent.executor.browser = fb
    res = await agent.run("任务 X", start_url="https://example.com/")
    assert res.success and agent.resets == 1
    second = gw.calls[1][1]["messages"]
    # a fresh one-message conversation (append-only: nothing earlier was edited)
    assert len(second) == 1 and second[0]["role"] == "user"
    text = second[0]["content"][0]["text"]
    assert "任务 X" in text and "scroll" in text and "已清空" in text


async def test_agent_full_level_keeps_history():
    big = NS(input_tokens=70000, output_tokens=5, cache_read_input_tokens=0)
    r1 = resp(tool_use("t1", "scroll", direction="down"))
    r1.usage = big
    gw = Gateway([r1, resp(tool_use("t2", "done", success=True, summary="ok"))], beta=True)
    agent = WebAgent(AgentConfig(trace_dir=None), client=ModelClient(gw))
    async def scripted(messages):
        response=await gw.messages.create(messages=messages)
        agent._last_input=response.usage.input_tokens
        return response
    agent._call_model=scripted
    agent.reference_plan={'steps':['test'],'success_criteria':['test'],'risks':[]}
    fb = FakeBrowser()
    agent.browser = agent.executor.browser = fb
    await agent.run("任务 X", start_url="https://example.com/")
    assert agent.resets == 1 and len(gw.calls[1][1]["messages"]) == 1


def test_auto_starts_at_basic_for_gateways():
    gw = Gateway([])
    gw.base_url = "https://ark.example.com/api/plan/"
    assert ModelClient(gw).level == "basic"
    gw.base_url = "https://api.anthropic.com"
    assert ModelClient(gw).level == "full"
    assert ModelClient(gw, compat="minimal").level == "minimal"


async def test_ignored_structured_output_is_disabled():
    """Gateway accepts output_config.format but returns prose: switch to text JSON for good."""
    gw = Gateway([text_resp("sure! the answer is 49"), text_resp('{"value": 49}'), text_resp('{"value": 1}')])
    gw.base_url = "https://gw.example.com"
    llm = LLM(gw, model="m")
    schema = {"type": "object", "properties": {"value": {"type": "integer"}}}
    assert await llm.json(system="s", content="q", schema=schema) == {"value": 49}
    assert not llm.client.structured_outputs
    await llm.json(system="s", content="q", schema=schema)
    assert len(gw.calls) == 3  # the second call skipped the structured attempt
