# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Trusted per-request identity. Built only from a session validated against OpenMRS."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class UserContext:
    username: str
    user_uuid: str
    privileges: frozenset[str]
    # Never printed or logged: repr=False keeps it out of tracebacks and log dumps.
    session_id: str = field(repr=False)
    roles: frozenset[str] = frozenset()
    request_id: str = ""

    # OpenMRS grants every privilege to the "System Developer" (super user) role.
    @property
    def is_superuser(self) -> bool:
        return "System Developer" in self.roles

    def has_privilege(self, name: str) -> bool:
        return self.is_superuser or name in self.privileges
