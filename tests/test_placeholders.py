# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from bahmni_ai.prompts.placeholders import PromptRenderer
from bahmni_ai.settings import Settings

# 2026-10-22 22:30 UTC is already Friday 2026-10-23 04:00 in Asia/Kolkata.
NOW = datetime(2026, 10, 22, 22, 30, tzinfo=UTC)


def renderer(text: str, tz: str = "Asia/Kolkata", **kw) -> PromptRenderer:
    return PromptRenderer(lambda: text, tz, clock=lambda: NOW, **kw)


def test_placeholder_has_weekday_date_time_and_zone():
    assert renderer("Now: {{current_datetime}}").render() == "Now: Friday, 2026-10-23 04:00 (Asia/Kolkata, UTC+05:30)"


def test_spaces_inside_braces_allowed():
    assert renderer("{{ current_datetime }}").render().startswith("Friday, 2026-10-23")


def test_old_placeholders_are_not_supported():
    out = renderer("{{current_date}}").render()
    assert out.startswith("{{current_date}}")  # left as written, and the automatic line still gives the date
    assert "Current date and time" in out


def test_unknown_placeholder_left_as_written():
    out = renderer("Hi {{user_name}} on {{current_datetime}}").render()
    assert out == "Hi {{user_name}} on Friday, 2026-10-23 04:00 (Asia/Kolkata, UTC+05:30)"


def test_no_automatic_line_when_prompt_uses_the_placeholder():
    assert "Current date and time" not in renderer("Today is {{current_datetime}}.").render()


def test_automatic_line_when_prompt_has_no_placeholder():
    out = renderer("Be brief.").render()
    assert out.startswith("Be brief.\n\n")
    assert out.endswith("Current date and time: Friday, 2026-10-23 04:00 (Asia/Kolkata, UTC+05:30).")


def test_automatic_line_can_be_switched_off():
    assert renderer("Be brief.", auto_datetime=False).render() == "Be brief."


def test_recomputed_on_every_call():
    times = iter([NOW, datetime(2026, 10, 23, 22, 30, tzinfo=UTC)])
    r = PromptRenderer(lambda: "{{current_datetime}}", "UTC", clock=lambda: next(times))
    assert (r.render(), r.render()) == (
        "Thursday, 2026-10-22 22:30 (UTC, UTC+00:00)",
        "Friday, 2026-10-23 22:30 (UTC, UTC+00:00)",
    )


def test_unknown_placeholder_logged_once(capsys):
    r = renderer("{{nope}} {{nope}}")
    r.render()
    r.render()
    assert (capsys.readouterr().out + capsys.readouterr().err).count("prompt_unknown_placeholder") <= 1


def test_invalid_timezone_rejected_at_startup():
    with pytest.raises(ValidationError, match="IANA"):
        Settings(timezone="Mars/Olympus")
