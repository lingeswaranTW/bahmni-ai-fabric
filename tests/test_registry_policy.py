# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

import asyncio

import pytest
from pydantic_ai import Agent, DeferredToolRequests, DeferredToolResults
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from bahmni_ai.agent.deps import AgentDeps
from bahmni_ai.auth.session import SessionExpiredError
from bahmni_ai.mcp.config import parse_config
from bahmni_ai.mcp.credentials import EnvCredentialResolver
from bahmni_ai.mcp.registry import McpRegistry
from tests import stubs

ENV = {"MCP_JIRA_TOKEN": "jira-secret", "MCP_DOCS_TOKEN": "docs-secret"}


def registry(docs_url, jira_url, **overrides):
    cfg = {
        "mcpServers": {
            "docs": {
                "url": docs_url,
                "auth": {"type": "bearer", "credentialRef": "MCP_DOCS_TOKEN"},
                "tools": {"allow": ["search", "whoami"]},
                "trustToolAnnotations": True,
                "requiredPrivilege": "Get Documents",
            },
            "jira": {
                "url": jira_url,
                "auth": {"type": "bearer", "credentialRef": "MCP_JIRA_TOKEN"},
                "tools": {
                    "allow": ["search"],
                    "deny": ["delete_*"],
                    "overrides": {
                        "create_issue": {
                            "access": "write",
                            "confirm": True,
                            "requiredPrivilege": "AI: Create Issue",
                            "argumentConstraints": {"project": ["BAH", "OPS"]},
                        },
                        "flaky_write": {"access": "write", "confirm": True, "requiredPrivilege": "AI: Create Issue"},
                        "slow_write": {"access": "write", "confirm": True, "requiredPrivilege": "AI: Create Issue"},
                    },
                },
                "requiredPrivilege": "Get Issues",
                "trustToolAnnotations": True,
            },
            "openmrs": {
                "url": docs_url,
                "auth": {"type": "openmrs-session"},
                "tools": {"allow": ["whoami"]},
                "requiredPrivilege": "Get Patients",
                "trustToolAnnotations": True,
            },
            **overrides,
        }
    }
    r = EnvCredentialResolver(environ=ENV)
    return McpRegistry(parse_config(cfg, r), r, connect_timeout_seconds=2, call_timeout_seconds=5)


def model_calling(tool: str, args: dict | None = None):
    def fn(messages, info):
        rets = [p for m in messages for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart)]
        if rets:
            return ModelResponse(parts=[TextPart(str(rets[-1].content))])
        return ModelResponse(parts=[ToolCallPart(tool, args or {})])

    return FunctionModel(fn)


async def run(reg, user, deps, tool, args=None, **kw):
    async with reg.open(user) as opened:
        agent = Agent(model_calling(tool, args), deps_type=AgentDeps, output_type=[str, DeferredToolRequests])
        return await agent.run("go", deps=deps, toolsets=opened.toolsets, **kw)


async def run_approved(reg, user, deps, tool, args=None):
    async with reg.open(user) as opened:
        agent = Agent(model_calling(tool, args), deps_type=AgentDeps, output_type=[str, DeferredToolRequests])
        r = await agent.run("go", deps=deps, toolsets=opened.toolsets)
        assert isinstance(r.output, DeferredToolRequests)
        approvals = {c.tool_call_id: True for c in r.output.approvals}
        return await agent.run(
            message_history=r.all_messages(),
            deps=deps,
            toolsets=opened.toolsets,
            deferred_tool_results=DeferredToolResults(approvals=approvals),
        )


ALL = ("Use AI Chatbot", "Get Documents", "Get Issues", "Get Patients", "AI: Create Issue")


async def tools_visible(reg, deps):
    from pydantic_ai import RunContext
    from pydantic_ai.models.test import TestModel
    from pydantic_ai.usage import RunUsage

    async with reg.open(deps.user) as o:
        ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage())
        names = set()
        for ts in o.toolsets:
            names |= set((await ts.get_tools(ctx)).keys())
        return names, o.unavailable


async def test_tools_from_two_servers_are_namespaced_and_unlisted_hidden(docs_url, jira_url, validator, make_deps):
    u = validator.add("S1", "alice", ALL)
    names, unavailable = await tools_visible(registry(docs_url, jira_url), make_deps(u))
    assert {"docs__search", "jira__search", "docs__whoami", "jira__create_issue"} <= names
    assert "jira__delete_everything" not in names and "docs__create_issue" not in names
    assert unavailable == []


async def test_unlisted_tool_refused_even_if_model_asks(docs_url, jira_url, validator, make_deps):
    u = validator.add("S1", "alice", ALL)
    with pytest.raises(UnexpectedModelBehavior):  # the framework itself rejects names we never exposed
        await run(registry(docs_url, jira_url), u, make_deps(u), "jira__delete_everything")
    assert stubs.CALLS.get("jira:delete_everything") is None


async def test_read_tool_works_and_is_audited(docs_url, jira_url, validator, make_deps, audit):
    u = validator.add("S1", "alice", ALL)
    res = await run(registry(docs_url, jira_url), u, make_deps(u), "docs__search", {"query": "malaria"})
    assert "docs:search:malaria" in res.output
    e = audit.entries[-1]
    assert (e.server, e.tool, e.decision, e.outcome, e.username) == ("docs", "search", "allowed", "success", "alice")
    assert "malaria" not in repr(e) and e.argument_names == ("query",)


async def test_user_without_privilege_cannot_run_tool(docs_url, jira_url, validator, make_deps, audit):
    u = validator.add("S2", "bob", ("Use AI Chatbot",))  # no "Get Documents"
    res = await run(registry(docs_url, jira_url), u, make_deps(u), "docs__search", {"query": "x"})
    assert "privilege" in res.output.lower() and "docs:search" not in res.output
    assert audit.entries[-1].decision == "denied"


async def test_invalid_arguments_rejected_before_calling_server(docs_url, jira_url, validator, make_deps):
    u = validator.add("S1", "alice", ALL)
    res = await run(registry(docs_url, jira_url), u, make_deps(u), "docs__search", {"query": 123})
    assert "invalid" in res.output.lower() or "query" in res.output.lower()


async def test_write_requires_confirmation_then_executes_once(docs_url, jira_url, validator, make_deps):
    u = validator.add("S1", "alice", ALL)
    reg = registry(docs_url, jira_url)
    deps = make_deps(u)
    async with reg.open(u) as opened:
        agent = Agent(
            model_calling("jira__create_issue", {"title": "T", "project": "BAH"}),
            deps_type=AgentDeps,
            output_type=[str, DeferredToolRequests],
        )
        r = await agent.run("go", deps=deps, toolsets=opened.toolsets)
        assert isinstance(r.output, DeferredToolRequests) and stubs.CALLS.get("jira:create_issue") is None
        call = r.output.approvals[0]
        r2 = await agent.run(
            message_history=r.all_messages(),
            deps=deps,
            toolsets=opened.toolsets,
            deferred_tool_results=DeferredToolResults(approvals={call.tool_call_id: True}),
        )
    assert "jira:created:BAH:T" in r2.output and stubs.CALLS["jira:create_issue"] == 1


async def test_write_rechecks_privilege_fresh_after_approval(docs_url, jira_url, validator, make_deps):
    u = validator.add("S1", "alice", ALL)
    reg = registry(docs_url, jira_url)
    deps = make_deps(u)
    async with reg.open(u) as opened:
        agent = Agent(
            model_calling("jira__create_issue", {"title": "T", "project": "BAH"}),
            deps_type=AgentDeps,
            output_type=[str, DeferredToolRequests],
        )
        r = await agent.run("go", deps=deps, toolsets=opened.toolsets)
        validator.add("S1", "alice", ("Use AI Chatbot", "Get Issues"))  # privilege revoked in between
        call = r.output.approvals[0]
        r2 = await agent.run(
            message_history=r.all_messages(),
            deps=deps,
            toolsets=opened.toolsets,
            deferred_tool_results=DeferredToolResults(approvals={call.tool_call_id: True}),
        )
    assert "privilege" in r2.output.lower() and stubs.CALLS.get("jira:create_issue") is None


async def test_argument_constraint_blocks_other_projects(docs_url, jira_url, validator, make_deps):
    u = validator.add("S1", "alice", ALL)
    res = await run(
        registry(docs_url, jira_url), u, make_deps(u), "jira__create_issue", {"title": "T", "project": "SECRET"}
    )
    assert (
        isinstance(res.output, str) and "not permitted" in res.output and stubs.CALLS.get("jira:create_issue") is None
    )


async def test_failed_write_runs_once_and_error_is_redacted(docs_url, jira_url, validator, make_deps):
    u = validator.add("S1", "alice", ALL)
    res = await run_approved(registry(docs_url, jira_url), u, make_deps(u), "jira__flaky_write", {"title": "T"})
    assert stubs.CALLS["jira:flaky_write"] == 1
    assert "sekret-token-123" not in res.output


async def test_write_timeout_is_not_retried_and_reports_unknown(docs_url, jira_url, validator, make_deps, audit):
    u = validator.add("S1", "alice", ALL)
    res = await run_approved(
        registry(docs_url, jira_url), u, make_deps(u, timeout=0.5), "jira__slow_write", {"title": "T"}
    )
    assert "unknown" in res.output.lower() and stubs.CALLS["jira:slow_write"] == 1
    assert audit.entries[-1].outcome == "timeout"


async def test_server_down_is_isolated(docs_url, jira_url, validator, make_deps):
    u = validator.add("S1", "alice", ALL)
    reg = registry(
        docs_url, jira_url, dead={"url": f"http://127.0.0.1:{stubs.free_port()}/mcp", "tools": {"allow": ["x"]}}
    )
    names, unavailable = await tools_visible(reg, make_deps(u))
    assert unavailable == ["dead"] and "docs__search" in names


async def test_concurrent_users_never_share_openmrs_session_or_service_token(docs_url, jira_url, validator, make_deps):
    reg = registry(docs_url, jira_url)
    users = [validator.add(f"SESSION-{i}", f"user{i}", ALL) for i in range(6)]
    outs = await asyncio.gather(*[run(reg, u, make_deps(u), "openmrs__whoami") for u in users])
    assert [o.output.split("|")[0] for o in outs] == [f"SESSION-{i}" for i in range(6)]
    # the OpenMRS session is only sent to openmrs-session servers, never to the bearer-auth docs server
    docs = await run(reg, users[0], make_deps(users[0]), "docs__whoami")
    assert docs.output == "none|Bearer docs-secret"


async def test_session_expiry_mid_run_propagates(docs_url, jira_url, validator, make_deps):
    u = validator.add("S1", "alice", ALL)
    reg = registry(docs_url, jira_url)
    validator.users.clear()
    with pytest.raises(SessionExpiredError):
        await run(reg, u, make_deps(u), "docs__search", {"query": "x"})


async def test_openmrs_session_server_without_user_is_unavailable(docs_url, jira_url):
    reg = registry(docs_url, jira_url)
    async with reg.open(None) as opened:
        assert "openmrs" in opened.unavailable
