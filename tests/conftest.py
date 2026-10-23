# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

import os

import pytest

from bahmni_ai.agent.deps import AgentDeps
from bahmni_ai.audit.log import ToolAudit
from bahmni_ai.auth.context import UserContext
from bahmni_ai.auth.privileges import PrivilegeChecker
from bahmni_ai.auth.session import SessionExpiredError
from tests import stubs


class FakeValidator:
    """Stands in for OpenMRS: session id -> UserContext (or expired)."""

    def __init__(self) -> None:
        self.users: dict[str, UserContext] = {}
        self.fresh_calls = 0

    def add(self, session: str, name: str, privileges: tuple[str, ...], uuid: str = "") -> UserContext:
        u = UserContext(name, uuid or f"u-{name}", frozenset(privileges), session_id=session)
        self.users[session] = u
        return u

    async def validate(self, session_id, *, fresh=False, request_id=""):
        if fresh:
            self.fresh_calls += 1
        if session_id not in self.users:
            raise SessionExpiredError("expired")
        return self.users[session_id]


class MemoryAudit:
    def __init__(self) -> None:
        self.entries: list[ToolAudit] = []

    def record(self, entry: ToolAudit) -> None:
        self.entries.append(entry)


@pytest.fixture
def validator():
    return FakeValidator()


@pytest.fixture
def audit():
    return MemoryAudit()


@pytest.fixture
def make_deps(validator, audit):
    def _make(user: UserContext, timeout: float = 5.0) -> AgentDeps:
        return AgentDeps(
            user=user,
            privileges=PrivilegeChecker(validator),
            audit=audit,
            gate_privilege="Use AI Chatbot",
            call_timeout_seconds=timeout,
        )

    return _make


@pytest.fixture(scope="module")
def docs_url():
    with stubs.serve("docs") as url:
        yield url


@pytest.fixture(scope="module")
def jira_url():
    with stubs.serve("jira") as url:
        yield url


@pytest.fixture(autouse=True)
def _hermetic_settings(monkeypatch):
    """Tests must not depend on a developer's .env or exported variables (a real DATABASE_URL made startup tests
    try to reach Postgres). Settings fields are cleared from the environment and .env loading is switched off."""
    from bahmni_ai.settings import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    for name in [n for n in os.environ if n.startswith("OTEL_")]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _reset_calls():
    stubs.CALLS.clear()


@pytest.fixture
def client_factory(docs_url, jira_url, validator, audit):
    from fastapi.testclient import TestClient

    from bahmni_ai.agent.factory import build_agent
    from bahmni_ai.agent.service import ChatService
    from bahmni_ai.db.store import MemoryConfirmationStore, MemoryConversationStore
    from bahmni_ai.main import create_app
    from bahmni_ai.settings import Settings
    from tests.test_registry_policy import registry

    def make(model, **settings_kw):
        settings = Settings(openmrs_url="http://omrs:8080", **settings_kw)
        svc = ChatService(
            build_agent(model),
            registry(docs_url, jira_url),
            MemoryConversationStore(),
            MemoryConfirmationStore(),
            PrivilegeChecker(validator),
            audit,
            settings,
        )
        return TestClient(create_app(settings, chat_service=svc))

    return make
