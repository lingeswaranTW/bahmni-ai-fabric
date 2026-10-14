# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

import pytest

from bahmni_ai.agent.factory import ModelConfigError, build_agent


@pytest.mark.parametrize(
    "model,env",
    [
        ("openai:gpt-4o", {"OPENAI_API_KEY": "sk-test"}),
        ("anthropic:claude-sonnet-5-5", {"ANTHROPIC_API_KEY": "sk-ant-test"}),
        ("ollama:llama3.2", {"OLLAMA_BASE_URL": "http://localhost:11434/v1"}),
    ],
)
def test_provider_is_chosen_by_configuration_string_only(monkeypatch, model, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    agent = build_agent(model)
    assert agent.model is not None and model.split(":")[0] in str(agent.model.system)


def test_missing_provider_key_fails_without_echoing_secrets(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ModelConfigError) as e:
        build_agent("openai:gpt-4o")
    assert "sk-" not in str(e.value)


def test_unknown_provider_fails_closed():
    with pytest.raises(ModelConfigError):
        build_agent("nonsense:model")
