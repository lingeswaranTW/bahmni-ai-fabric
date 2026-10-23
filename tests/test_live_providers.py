# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Real-provider smoke tests. Skipped unless LIVE_PROVIDER_MODELS is set, e.g.

    LIVE_PROVIDER_MODELS="ollama:llama3.2,anthropic:claude-sonnet-5-5" uv run pytest -m live

Credentials come from the provider's usual environment variables. These make real model calls.
"""

import os

import pytest

from bahmni_ai.providers.smoke import run_smoke

MODELS = [m.strip() for m in os.environ.get("LIVE_PROVIDER_MODELS", "").split(",") if m.strip()]

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not MODELS, reason="set LIVE_PROVIDER_MODELS to run live provider tests"),
]


@pytest.mark.parametrize("model", MODELS or ["skipped"])
async def test_provider_smoke(model):
    results = await run_smoke(model)
    failed = [f"{r.name}: {r.detail}" for r in results if not r.ok]
    assert not failed, failed
