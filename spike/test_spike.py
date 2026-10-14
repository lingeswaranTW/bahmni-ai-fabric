"""Stage 0 spike: prove Pydantic AI can do what the design needs."""
import asyncio
import socket
import threading
import time

import pytest
import uvicorn
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from pydantic_ai import Agent, DeferredToolRequests, DeferredToolResults
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.toolsets import ApprovalRequiredToolset


def make_server(label):
    mcp = FastMCP(label)

    @mcp.tool
    async def whoami() -> str:
        await asyncio.sleep(0.2)  # force overlap between concurrent runs
        return get_http_headers(include={"x-openmrs-session-id"}).get("x-openmrs-session-id", "none")

    @mcp.tool
    def create_issue(title: str) -> str:
        return f"{label}:created:{title}"

    return mcp


@pytest.fixture(scope="module")
def server_url():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    app = make_server("jira").http_app(stateless_http=True)
    srv = uvicorn.Server(uvicorn.Config(app, port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True); t.start()
    while not srv.started: time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    srv.should_exit = True


def calls_tool_once(tool, args=None):
    def fn(messages, info):
        if any(isinstance(p, ToolReturnPart) for m in messages for p in getattr(m, "parts", [])):
            ret = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)][-1]
            return ModelResponse(parts=[TextPart(str(ret.content))])
        return ModelResponse(parts=[ToolCallPart(tool, args or {})])
    return FunctionModel(fn)


async def run_as(url, session, tool="whoami"):
    ts = MCPToolset(url, headers={"X-OpenMRS-Session-Id": session})
    agent = Agent(calls_tool_once(tool), toolsets=[ts])
    return (await agent.run("hi")).output


async def test_concurrent_users_get_own_credentials(server_url):
    results = await asyncio.gather(*[run_as(server_url, f"sess-{i}") for i in range(8)])
    assert results == [f"sess-{i}" for i in range(8)]


async def test_prefix_and_approval_pause_resume(server_url):
    ts = ApprovalRequiredToolset(MCPToolset(server_url).prefixed("jira"),
                                 approval_required_func=lambda ctx, td, a: td.name.endswith("create_issue"))
    agent = Agent(calls_tool_once("jira_create_issue", {"title": "T"}), toolsets=[ts],
                  output_type=[str, DeferredToolRequests])
    r = await agent.run("go")
    assert isinstance(r.output, DeferredToolRequests)
    approvals = r.output.approvals
    assert approvals and approvals[0].tool_name == "jira_create_issue"
    res = DeferredToolResults(approvals={approvals[0].tool_call_id: True})
    r2 = await agent.run(message_history=r.all_messages(), deferred_tool_results=res)
    assert "jira:created:T" in r2.output


async def test_process_tool_call_can_deny(server_url):
    async def deny(ctx, call_tool, name, args):
        raise PermissionError("denied by policy")
    ts = MCPToolset(server_url, process_tool_call=deny, tool_error_behavior="error")
    agent = Agent(calls_tool_once("whoami"), toolsets=[ts])
    with pytest.raises(PermissionError):
        await agent.run("hi")


def test_provider_switch_by_string(monkeypatch):
    from pydantic_ai.models import infer_model
    monkeypatch.setenv("OPENAI_API_KEY", "x"); monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    names = {infer_model(m).system for m in ("openai:gpt-4o", "anthropic:claude-sonnet-5-5", "ollama:llama3.2")}
    assert len(names) == 3


async def test_cancellation_is_clean(server_url):
    t = asyncio.create_task(run_as(server_url, "s-cancel"))
    await asyncio.sleep(0.1); t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert await run_as(server_url, "after") == "after"
