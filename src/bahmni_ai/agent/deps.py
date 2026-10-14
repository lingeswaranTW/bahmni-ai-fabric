# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

from dataclasses import dataclass

from bahmni_ai.audit.log import AuditSink
from bahmni_ai.auth.context import UserContext
from bahmni_ai.auth.privileges import PrivilegeChecker


@dataclass
class AgentDeps:
    """Trusted per-run context. Identity comes from the validated session, never from the model."""

    user: UserContext
    privileges: PrivilegeChecker
    audit: AuditSink
    gate_privilege: str
    call_timeout_seconds: float = 30.0
