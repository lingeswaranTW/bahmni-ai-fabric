# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Request authentication. Identity comes only from a session validated against OpenMRS."""

import re
import uuid

import structlog
from fastapi import Depends, HTTPException, Request

from bahmni_ai.auth.context import UserContext
from bahmni_ai.auth.session import OpenmrsUnavailableError, SessionExpiredError, SessionValidator
from bahmni_ai.mcp.auth import SESSION_HEADER
from bahmni_ai.settings import Settings, get_settings

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def get_validator(request: Request) -> SessionValidator:
    validator: SessionValidator = request.app.state.session_validator
    return validator


def request_id_of(request: Request) -> str:
    rid = request.headers.get("X-Request-Id", "")
    return rid if _REQUEST_ID.match(rid) else uuid.uuid4().hex


async def current_user(
    request: Request,
    validator: SessionValidator = Depends(get_validator),
    settings: Settings = Depends(get_settings),
) -> UserContext:
    """Other headers such as X-User-Id are ignored on purpose."""
    rid = request_id_of(request)
    try:
        user = await validator.validate(request.headers.get(SESSION_HEADER), request_id=rid)
    except SessionExpiredError as exc:
        raise error(401, "session_expired", "Session expired. Please log in again.") from exc
    except OpenmrsUnavailableError as exc:
        raise error(502, "openmrs_unavailable", "Cannot verify the session right now.") from exc
    if not user.has_privilege(settings.gate_privilege):
        raise error(403, "forbidden", "You are not allowed to use the AI assistant.")
    structlog.contextvars.bind_contextvars(request_id=rid, user=user.username)
    return user
