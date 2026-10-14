# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

from fastapi.testclient import TestClient

from bahmni_ai.logging import REDACTED, redact_value
from bahmni_ai.main import create_app
from bahmni_ai.settings import Settings


def test_health_under_service_path():
    c = TestClient(create_app(Settings()))
    assert c.get("/bahmni-ai/health").json() == {"status": "UP"}


def test_redaction():
    out = redact_value({"Authorization": "Bearer abc", "msg": "cookie JSESSIONID=XYZ; Bearer tok", "ok": 1})
    assert out["Authorization"] == REDACTED
    assert "XYZ" not in out["msg"] and "tok" not in out["msg"]
    assert out["ok"] == 1
