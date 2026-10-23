# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Fills the date placeholder in the system prompt on every run (docs/PROMPTS.md#placeholder).

The model cannot know the server's date. There is one placeholder, ``{{current_datetime}}``, holding the weekday, date,
time and clinic time zone. It is filled here, per request, not by the prompt source: the source's prompt is cached, so a
date filled in there would go stale. Other ``{{names}}`` are left as written and logged once.
"""

import re
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from bahmni_ai.logging import get_logger

log = get_logger("bahmni_ai.prompts")

PLACEHOLDER = "current_datetime"
_TOKEN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class PromptRenderer:
    def __init__(
        self,
        text: Callable[[], str],
        timezone: str,
        *,
        auto_datetime: bool = True,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._text, self._zone, self._zone_name = text, ZoneInfo(timezone), timezone
        self._auto, self._clock = auto_datetime, clock
        self._warned: set[str] = set()

    def _now(self) -> str:
        now = self._clock().astimezone(self._zone)
        offset = now.strftime("%z")
        return f"{now:%A, %Y-%m-%d %H:%M} ({self._zone_name}, UTC{offset[:3]}:{offset[3:]})"

    def render(self) -> str:
        text, now, used = self._text(), self._now(), False

        def fill(match: re.Match[str]) -> str:
            nonlocal used
            name = match.group(1)
            if name == PLACEHOLDER:
                used = True
                return now
            if name not in self._warned:
                self._warned.add(name)
                log.warning("prompt_unknown_placeholder", name=name)
            return match.group(0)

        text = _TOKEN.sub(fill, text)
        if self._auto and not used:
            text = f"{text.rstrip()}\n\nCurrent date and time: {now}."
        return text
