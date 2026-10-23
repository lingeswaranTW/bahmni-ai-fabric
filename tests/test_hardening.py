# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import respx

from bahmni_ai.mcp.config import load_config
from bahmni_ai.mcp.credentials import EnvCredentialResolver
from tests.test_chat_api import SESSION_URL, scripted, sse, user_json
from tests.test_registry_policy import ALL

SAMPLE = Path(__file__).parent / "fixtures/mcp-servers.example.json"


def test_shipped_sample_config_is_valid():
    env = {
        k: v
        for k, v in {
            "MCP_DOCUMENTS_URL": "https://d/mcp",
            "MCP_DOCUMENTS_TOKEN": "t",
            "MCP_JIRA_URL": "https://j/mcp",
            "MCP_JIRA_TOKEN": "t",
            "MCP_OPENMRS_URL": "https://o/mcp",
        }.items()
    }
    cfg = load_config(SAMPLE, EnvCredentialResolver(environ=env))
    assert set(cfg.mcpServers) == {"documents", "jira", "openmrs"}
    text = SAMPLE.read_text()
    assert "Bearer " not in text and "sk-" not in text  # references only, never values


@respx.mock
def test_session_ids_never_appear_in_logs_or_responses(client_factory, validator, capsys):
    validator.add("TOPSECRET-SESSION-1", "alice", ALL)
    respx.get(SESSION_URL).mock(return_value=httpx.Response(200, json=user_json()))
    with client_factory(scripted("docs__search", {"query": "q"})) as c:
        r = c.post("/bahmni-ai/chat", json={"message": "x"}, headers={"X-OpenMRS-Session-Id": "TOPSECRET-SESSION-1"})
    out = capsys.readouterr().out
    assert "TOPSECRET-SESSION-1" not in out and "TOPSECRET-SESSION-1" not in r.text
    assert "docs-secret" not in out and "docs-secret" not in r.text  # service bearer token never surfaces either


@respx.mock
def test_parallel_users_through_the_api_never_see_each_others_session(client_factory, validator):
    users = {f"SESS-{i}": f"user{i}" for i in range(6)}
    for s, n in users.items():
        validator.add(s, n, ALL)
    respx.get(SESSION_URL).mock(
        side_effect=lambda req: httpx.Response(200, json=user_json(users[req.headers["cookie"].split("=")[1]]))
    )
    with client_factory(scripted("openmrs__whoami")) as c:

        def call(session):
            ev = sse(c.post("/bahmni-ai/chat", json={"message": "who"}, headers={"X-OpenMRS-Session-Id": session}))
            return session, "".join(d["delta"] for n, d in ev if n == "text")

        with ThreadPoolExecutor(6) as pool:
            results = list(pool.map(call, users))
    for session, text in results:
        assert session in text, (session, text)
        assert not any(other in text for other in users if other != session)
