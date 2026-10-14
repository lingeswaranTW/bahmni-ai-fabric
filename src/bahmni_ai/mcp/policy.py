# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Tool policy: visibility, namespacing, argument validation, privilege checks, confirmation, audit.

Every MCP tool call passes through ``ServerToolset``. The model never chooses a server, a privilege,
or an identity: the namespaced name is looked up in what *we* resolved from configuration.
"""

import asyncio
import time
from dataclasses import dataclass, field, replace
from typing import Any

import jsonschema
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai.toolsets import ToolsetTool, WrapperToolset

from bahmni_ai.agent.deps import AgentDeps
from bahmni_ai.audit.log import Decision, Outcome, ToolAudit, hash_arguments
from bahmni_ai.logging import get_logger, redact_value
from bahmni_ai.mcp.config import TOOL_SEPARATOR, ServerConfig, matches

log = get_logger("bahmni_ai.policy")


@dataclass(frozen=True)
class ResolvedTool:
    access: str  # "read" | "write"
    confirm: bool
    privilege: str | None
    constraints: dict[str, list[str]] = field(default_factory=dict)


def resolve_tool(server: ServerConfig, tool: str, annotations: dict[str, Any] | None) -> ResolvedTool | None:
    """Return the policy for a discovered tool, or None to hide it (discovery grants nothing)."""
    policy = server.tools
    if matches(policy.deny, tool):
        return None
    override = policy.overrides.get(tool)
    if override is not None:
        return ResolvedTool(
            override.access, override.confirm, override.requiredPrivilege or server.requiredPrivilege,
            dict(override.argumentConstraints),
        )  # fmt: skip
    if not matches(policy.allow, tool):
        return None
    # Annotations come from the (untrusted) server, so they may only make us *more* careful.
    if (annotations or {}).get("readOnlyHint") is True and (annotations or {}).get("destructiveHint") is not True:
        return ResolvedTool("read", False, server.requiredPrivilege)
    if server.requiredPrivilege:
        return ResolvedTool("write", True, server.requiredPrivilege)
    return None  # not provably read-only and no privilege to guard it: stay hidden


def namespaced(server: str, tool: str) -> str:
    return f"{server}{TOOL_SEPARATOR}{tool}"


def _denied(reason: str) -> dict[str, str]:
    return {"error": "permission_denied", "message": reason}


@dataclass
class ServerToolset(WrapperToolset[AgentDeps]):
    server: str
    config: ServerConfig
    resolved: dict[str, ResolvedTool] = field(default_factory=dict)

    async def get_tools(self, ctx: RunContext[AgentDeps]) -> dict[str, ToolsetTool[AgentDeps]]:
        out: dict[str, ToolsetTool[AgentDeps]] = {}
        for name, tool in (await super().get_tools(ctx)).items():
            annotations = (tool.tool_def.metadata or {}).get("annotations")
            rule = resolve_tool(self.config, name, annotations)
            if rule is None:
                continue
            new_name = namespaced(self.server, name)
            self.resolved[new_name] = rule
            out[new_name] = replace(tool, toolset=self, tool_def=replace(tool.tool_def, name=new_name))
        return out

    def _audit(
        self,
        ctx: RunContext[AgentDeps],
        tool: str,
        rule: ResolvedTool | None,
        decision: Decision,
        args: dict[str, Any],
        reason: str = "",
        outcome: Outcome = "not_executed",
        started: float = 0.0,
    ) -> None:
        u = ctx.deps.user
        ctx.deps.audit.record(ToolAudit(
            request_id=u.request_id, username=u.username, user_uuid=u.user_uuid, server=self.server, tool=tool,
            access=rule.access if rule else "unknown", decision=decision, reason=reason, outcome=outcome,
            latency_ms=int((time.monotonic() - started) * 1000) if started else 0,
            argument_names=tuple(sorted(args)), arguments_hash=hash_arguments(args),
        ))  # fmt: skip

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[AgentDeps], tool: ToolsetTool[AgentDeps]
    ) -> Any:
        rule = self.resolved.get(name)
        original = name.split(TOOL_SEPARATOR, 1)[1] if TOOL_SEPARATOR in name else name
        if rule is None:  # not something we exposed: refuse, whatever the model asked for
            self._audit(ctx, original, None, "denied", tool_args, "unknown tool")
            return _denied("This tool is not available.")

        # 1. Argument validation against the server's declared JSON schema.
        try:
            jsonschema.validate(tool_args, tool.tool_def.parameters_json_schema)
        except jsonschema.ValidationError as exc:
            self._audit(ctx, original, rule, "invalid_arguments", tool_args, exc.message[:120])
            return {"error": "invalid_arguments", "message": exc.message[:200]}
        for key, allowed in rule.constraints.items():
            if str(tool_args.get(key)) not in allowed:
                self._audit(ctx, original, rule, "denied", tool_args, f"argument '{key}' not allowed")
                return _denied(f"The value for '{key}' is not permitted.")

        # 2. Privileges (gate + tool-specific), verified with OpenMRS, not with the model.
        needed = [p for p in (ctx.deps.gate_privilege, rule.privilege) if p]
        missing = await ctx.deps.privileges.missing(ctx.deps.user, needed)  # SessionExpiredError propagates
        if missing:
            self._audit(ctx, original, rule, "denied", tool_args, "missing privilege")
            return _denied("You do not have the privilege required for this action.")

        # 3. Confirmation for consequential writes. The model asking is not consent.
        if rule.confirm and not ctx.tool_call_approved:
            self._audit(ctx, original, rule, "confirmation_required", tool_args)
            raise ApprovalRequired

        # 4. Right before a write executes, re-check against OpenMRS without the cache.
        if rule.access == "write":
            missing = await ctx.deps.privileges.missing(ctx.deps.user, needed, fresh=True)
            if missing:
                self._audit(ctx, original, rule, "denied", tool_args, "missing privilege at execution")
                return _denied("You do not have the privilege required for this action.")

        # 5. Execute once, with a timeout. Never retried here (writes are not assumed idempotent).
        started = time.monotonic()
        inner = replace(tool, tool_def=replace(tool.tool_def, name=original))
        try:
            async with asyncio.timeout(ctx.deps.call_timeout_seconds):
                result = await super().call_tool(original, tool_args, replace(ctx, tool_name=original), inner)
        except TimeoutError:
            self._audit(ctx, original, rule, "allowed", tool_args, outcome="timeout", started=started)
            log.warning("tool_timeout", server=self.server, tool=original)
            return {"error": "timeout", "message": "The tool did not answer in time; its result is unknown. "
                    "Do not repeat the action automatically."}  # fmt: skip
        except ApprovalRequired:
            raise
        except Exception as exc:
            self._audit(ctx, original, rule, "allowed", tool_args, outcome="error", started=started)
            log.warning("tool_error", server=self.server, tool=original, error=exc.__class__.__name__)
            return {"error": "tool_failed", "message": str(redact_value(str(exc)))[:300]}
        self._audit(ctx, original, rule, "allowed", tool_args, outcome="success", started=started)
        return result
