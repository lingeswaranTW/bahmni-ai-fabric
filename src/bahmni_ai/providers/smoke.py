# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Live smoke test of a provider/model through the same path the service uses (docs/PROVIDERS.md).

    uv run python -m bahmni_ai.providers.smoke ollama:llama3.2
    uv run python -m bahmni_ai.providers.smoke "anthropic:claude-sonnet-5-5,openai:gpt-4o" --skip-errors

Needs the provider's credentials in the environment and makes real (small, billable) model calls. Synthetic prompts
only. Exit code 0 when every check passes.
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic_ai import Agent
from pydantic_ai.messages import FunctionToolCallEvent, PartDeltaEvent, PartStartEvent, TextPart, TextPartDelta
from pydantic_ai.toolsets import FunctionToolset

from bahmni_ai.agent.deps import AgentDeps
from bahmni_ai.agent.factory import build_agent
from bahmni_ai.auth.context import UserContext
from bahmni_ai.providers import ProviderSettings

# Environment variable that holds the credential/endpoint of each provider, used to force a failure.
_BREAK_VAR = {
    "openai": ("OPENAI_API_KEY", "sk-invalid"),
    "anthropic": ("ANTHROPIC_API_KEY", "sk-ant-invalid"),
    "gemini": ("GOOGLE_API_KEY", "invalid"),
    "openai-compat": ("OPENAI_COMPAT_BASE_URL", "http://127.0.0.1:1/v1"),
    "ollama": ("OLLAMA_BASE_URL", "http://127.0.0.1:1/v1"),
}


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


def _deps() -> AgentDeps:
    user = UserContext("smoke", "smoke-uuid", frozenset(), session_id="")
    return AgentDeps(user=user, privileges=None, audit=None, gate_privilege="")  # type: ignore[arg-type]


async def _plain(agent: Agent[AgentDeps, object]) -> CheckResult:
    result = await agent.run("Reply with the single word: pong", deps=_deps())
    text = str(result.output)
    return CheckResult("plain answer", bool(text.strip()), text.strip()[:60])


async def _streaming(agent: Agent[AgentDeps, object]) -> CheckResult:
    chunks = 0
    async with agent.run_stream_events("Count from 1 to 5 in words.", deps=_deps()) as events:
        async for ev in events:
            if (isinstance(ev, PartStartEvent) and isinstance(ev.part, TextPart) and ev.part.content) or (
                isinstance(ev, PartDeltaEvent) and isinstance(ev.delta, TextPartDelta)
            ):
                chunks += 1
    return CheckResult("streaming", chunks > 0, f"{chunks} text events")


async def _tool_call(agent: Agent[AgentDeps, object]) -> CheckResult:
    calls: list[tuple[int, int]] = []

    def add(a: int, b: int) -> int:
        """Add two integers."""
        calls.append((a, b))
        return a + b

    seen_call = False
    final = ""
    async with agent.run_stream_events(
        "Use the add tool to compute 17 + 25, then reply with only the number.",
        deps=_deps(),
        toolsets=[FunctionToolset([add])],
    ) as events:
        async for ev in events:
            if isinstance(ev, FunctionToolCallEvent):
                seen_call = True
            elif isinstance(ev, PartStartEvent) and isinstance(ev.part, TextPart):
                final += ev.part.content
            elif isinstance(ev, PartDeltaEvent) and isinstance(ev.delta, TextPartDelta):
                final += ev.delta.content_delta
    ok = seen_call and bool(calls) and "42" in final
    return CheckResult("tool call", ok, f"called={calls} answer={final.strip()[:40]!r}")


async def _forced_failure(spec: str) -> CheckResult:
    """A broken credential/endpoint must surface as a mapped, client-safe error, never a raw exception text."""
    from pydantic_ai.exceptions import FallbackExceptionGroup, ModelAPIError

    from bahmni_ai.agent.service import _provider_error

    first = spec.split(",")[0].strip()
    name = first.partition(":")[0].strip().lower()
    if name not in _BREAK_VAR:
        return CheckResult("forced failure", True, f"skipped (no known way to break '{name}')")
    var, bad = _BREAK_VAR[name]
    saved = os.environ.get(var)
    os.environ[var] = bad
    try:
        agent = build_agent(first, provider_settings=ProviderSettings(max_retries=0))
        await agent.run("hi", deps=_deps())
    except (ModelAPIError, FallbackExceptionGroup) as exc:
        event = _provider_error(exc)
        ok = event.code in {"provider_error", "rate_limited"}
        return CheckResult("forced failure", ok, f"mapped to {event.code}")
    except Exception as exc:
        return CheckResult("forced failure", False, f"unmapped {exc.__class__.__name__}")
    finally:
        if saved is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = saved
    return CheckResult("forced failure", False, "call unexpectedly succeeded with a broken credential")


async def run_smoke(spec: str, *, check_errors: bool = True) -> list[CheckResult]:
    agent = build_agent(spec)
    checks: list[Callable[[Agent[AgentDeps, object]], Awaitable[CheckResult]]] = [_plain, _streaming, _tool_call]
    results: list[CheckResult] = []
    for check in checks:
        try:
            results.append(await check(agent))
        except Exception as exc:  # report, keep going: one failing capability should not hide the others
            results.append(CheckResult(check.__name__.lstrip("_").replace("_", " "), False, exc.__class__.__name__))
    if check_errors:
        results.append(await _forced_failure(spec))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live smoke test of an AI_MODEL value")
    parser.add_argument("model", help='AI_MODEL value, e.g. "ollama:llama3.2"')
    parser.add_argument("--skip-errors", action="store_true", help="skip the forced-failure check")
    args = parser.parse_args(argv)
    results = asyncio.run(run_smoke(args.model, check_errors=not args.skip_errors))
    for r in results:
        print(f"{'PASS' if r.ok else 'FAIL'}  {r.name:15} {r.detail}")  # noqa: T201
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
