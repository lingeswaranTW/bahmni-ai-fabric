# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Privilege checks against OpenMRS (never against anything the model says)."""

from bahmni_ai.auth.context import UserContext
from bahmni_ai.auth.session import SessionValidator


class PrivilegeChecker:
    """Re-validates the session with OpenMRS and checks the user's privileges.

    ``fresh=True`` bypasses the short session cache; used right before executing writes.
    Raises ``SessionExpiredError`` / ``OpenmrsUnavailableError`` (callers fail closed).
    """

    def __init__(self, validator: SessionValidator):
        self._validator = validator

    async def missing(self, user: UserContext, privileges: list[str], *, fresh: bool = False) -> list[str]:
        current = await self._validator.validate(user.session_id, fresh=fresh, request_id=user.request_id)
        if current.user_uuid != user.user_uuid:  # the session changed hands: treat as expired
            from bahmni_ai.auth.session import SessionExpiredError

            raise SessionExpiredError("session identity changed")
        return [p for p in privileges if not current.has_privilege(p)]
