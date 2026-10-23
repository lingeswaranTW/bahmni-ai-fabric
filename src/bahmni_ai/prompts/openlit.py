# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""OpenLIT Prompt Hub source (docs/PROMPTS.md).

``POST {url}/api/prompt/get-compiled`` with ``Authorization: Bearer $OPENLIT_API_KEY``. The server answers HTTP 200 with
``{"err": ..., "res": {...}}``, errors included, so the body is checked, not only the status.
"""

import os
from dataclasses import dataclass
from typing import Any

import httpx

from bahmni_ai.prompts.base import (
    FetchedPrompt,
    PromptRef,
    PromptSource,
    PromptSourceError,
    PromptSourceSettings,
)
from bahmni_ai.urlsafety import is_private_or_tls

API_KEY_VAR = "OPENLIT_API_KEY"
ENDPOINT = "/api/prompt/get-compiled"


class OpenLITPromptSource:
    def __init__(self, base_url: str, api_key: str, timeout_seconds: float, client: httpx.AsyncClient | None = None):
        self._endpoint = base_url.rstrip("/") + ENDPOINT
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False)

    async def fetch(self, ref: PromptRef) -> FetchedPrompt:
        payload: dict[str, Any] = {"name": ref.name, "shouldCompile": True, "source": "bahmni-ai"}
        if ref.version:
            payload["version"] = ref.version
        if ref.variables:
            payload["variables"] = ref.variables
        try:
            resp = await self._client.post(self._endpoint, json=payload, headers=self._headers)
        except httpx.HTTPError as exc:
            raise PromptSourceError(f"OpenLIT is unreachable ({exc.__class__.__name__})") from exc
        if resp.status_code != 200:
            raise PromptSourceError(f"OpenLIT returned HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise PromptSourceError("OpenLIT returned a response that is not JSON") from exc
        if not isinstance(body, dict):
            raise PromptSourceError("OpenLIT returned an unexpected response")
        if body.get("err"):
            raise PromptSourceError(f"OpenLIT reported an error: {str(body['err'])[:200]}")
        res = body.get("res")
        if not isinstance(res, dict):
            raise PromptSourceError("OpenLIT returned no prompt")
        text = res.get("compiledPrompt") or res.get("prompt")
        if not isinstance(text, str) or not text.strip():
            raise PromptSourceError("OpenLIT returned an empty prompt")
        version = res.get("version")
        return FetchedPrompt(text=text.strip(), version=str(version) if version is not None else None)

    async def aclose(self) -> None:
        await self._client.aclose()


@dataclass(frozen=True)
class OpenLITAdapter:
    name: str = "openlit"

    def create(self, settings: PromptSourceSettings) -> PromptSource:
        if not settings.url.startswith(("http://", "https://")):
            raise PromptSourceError("PROMPT_HUB_URL must be an http(s) URL")
        if not is_private_or_tls(settings.url):
            raise PromptSourceError(
                "PROMPT_HUB_URL must be https, or http to localhost, host.docker.internal or a container name"
            )
        api_key = os.environ.get(API_KEY_VAR, "").strip()
        if not api_key:
            raise PromptSourceError(f"{API_KEY_VAR} is not set")
        return OpenLITPromptSource(settings.url, api_key, settings.timeout_seconds)
