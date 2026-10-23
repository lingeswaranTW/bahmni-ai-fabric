# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Validate the OpenMRS session forwarded by bahmni-core and build a trusted UserContext.

bahmni-core sends the caller's JSESSIONID as ``X-OpenMRS-Session-Id``. We never trust that header
on its own: identity and privileges come from OpenMRS ``GET /ws/rest/v1/session``.
"""

import hashlib
import time
from typing import Any

import httpx

from bahmni_ai.auth.context import UserContext
from bahmni_ai.logging import get_logger

SESSION_PATH = "/openmrs/ws/rest/v1/session"
# Ask OpenMRS for exactly what we need. Privileges are listed per user; roles let us spot super users.
_REP = "custom:(authenticated,user:(uuid,username,privileges:(name),roles:(name)))"

log = get_logger("bahmni_ai.auth")


class SessionExpiredError(Exception):
    """The session is missing, expired, or not authenticated."""


class OpenmrsUnavailableError(Exception):
    """OpenMRS could not be reached or answered unexpectedly."""


def _names(items: Any) -> frozenset[str]:
    if not isinstance(items, list):
        return frozenset()
    return frozenset(i["name"] for i in items if isinstance(i, dict) and isinstance(i.get("name"), str))


class SessionValidator:
    def __init__(self, client: httpx.AsyncClient, base_url: str, ttl_seconds: float = 30.0, max_entries: int = 1024):
        self._client = client
        self._base = base_url.rstrip("/")
        self._ttl = ttl_seconds
        self._max = max_entries
        # key: sha256(session id) -> (expires_at, UserContext). In memory only, never persisted.
        self._cache: dict[str, tuple[float, UserContext]] = {}

    @staticmethod
    def _key(session_id: str) -> str:
        return hashlib.sha256(session_id.encode()).hexdigest()

    async def validate(self, session_id: str | None, *, fresh: bool = False, request_id: str = "") -> UserContext:
        if not session_id or len(session_id) > 256 or not session_id.isprintable() or ";" in session_id:
            raise SessionExpiredError("missing or malformed session")
        key = self._key(session_id)
        now = time.monotonic()
        if not fresh:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        user = await self._fetch(session_id, request_id)
        if len(self._cache) >= self._max:
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            if len(self._cache) >= self._max:
                self._cache.clear()
        self._cache[key] = (now + self._ttl, user)
        return user

    def invalidate(self, session_id: str) -> None:
        self._cache.pop(self._key(session_id), None)

    async def _fetch(self, session_id: str, request_id: str) -> UserContext:
        try:
            resp = await self._client.get(
                f"{self._base}{SESSION_PATH}",
                params={"v": _REP},
                headers={"Cookie": f"JSESSIONID={session_id}", "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            log.warning("openmrs_unreachable", error=exc.__class__.__name__)
            raise OpenmrsUnavailableError("OpenMRS is unreachable") from exc
        if resp.status_code in (401, 403):
            raise SessionExpiredError("session rejected by OpenMRS")
        if resp.status_code != 200:
            log.warning("openmrs_unexpected_status", status=resp.status_code)
            raise OpenmrsUnavailableError(f"OpenMRS returned {resp.status_code}")
        try:
            body = resp.json()
            user = body["user"] if body.get("authenticated") else None
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise OpenmrsUnavailableError("unexpected OpenMRS session response") from exc
        log.debug("openmrs_session_response", request_id=request_id, authenticated=bool(user))  # no user data
        if not isinstance(user, dict) or not user.get("uuid"):
            raise SessionExpiredError("session is not authenticated")
        return UserContext(
            username=str(user.get("username") or user["uuid"]),
            user_uuid=str(user["uuid"]),
            privileges=_names(user.get("privileges")),
            roles=_names(user.get("roles")),
            session_id=session_id,
            request_id=request_id,
        )
