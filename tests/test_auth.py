# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

import httpx
import pytest
import respx
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from bahmni_ai.api.deps import current_user
from bahmni_ai.auth.context import UserContext
from bahmni_ai.auth.session import SessionExpiredError, SessionValidator
from bahmni_ai.settings import Settings, get_settings

URL = "http://omrs:8080/openmrs/ws/rest/v1/session"


def session_body(user="alice", privs=("Use AI Chatbot",), roles=()):
    return {
        "authenticated": True,
        "user": {
            "uuid": f"uuid-{user}",
            "username": user,
            "privileges": [{"name": p} for p in privs],
            "roles": [{"name": r} for r in roles],
        },
    }


def make_app(validator):
    app = FastAPI()
    app.state.session_validator = validator
    app.dependency_overrides[get_settings] = lambda: Settings()

    @app.get("/whoami")
    async def whoami(user: UserContext = Depends(current_user)):
        return {"user": user.username}

    return app


@pytest.fixture
async def validator():
    async with httpx.AsyncClient() as c:
        yield SessionValidator(c, "http://omrs:8080", ttl_seconds=30)


@respx.mock
def test_valid_session_gives_identity_and_forwards_cookie():
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=session_body()))
    v = SessionValidator(httpx.AsyncClient(), "http://omrs:8080")
    client = TestClient(make_app(v))
    r = client.get("/whoami", headers={"X-OpenMRS-Session-Id": "SESS1"})
    assert r.status_code == 200 and r.json() == {"user": "alice"}
    assert route.calls.last.request.headers["cookie"] == "JSESSIONID=SESS1"


@respx.mock
def test_spoofed_identity_headers_are_ignored():
    respx.get(URL).mock(return_value=httpx.Response(200, json=session_body("alice")))
    client = TestClient(make_app(SessionValidator(httpx.AsyncClient(), "http://omrs:8080")))
    r = client.get("/whoami", headers={"X-OpenMRS-Session-Id": "S", "X-User-Id": "root", "X-User": "bob"})
    assert r.json() == {"user": "alice"}


def test_missing_header_is_401():
    client = TestClient(make_app(SessionValidator(httpx.AsyncClient(), "http://omrs:8080")))
    r = client.get("/whoami")
    assert r.status_code == 401 and r.json()["detail"]["code"] == "session_expired"


@respx.mock
def test_expired_and_unauthenticated_sessions_are_401():
    client = TestClient(make_app(SessionValidator(httpx.AsyncClient(), "http://omrs:8080")))
    respx.get(URL).mock(return_value=httpx.Response(401))
    assert client.get("/whoami", headers={"X-OpenMRS-Session-Id": "x"}).status_code == 401
    respx.get(URL).mock(return_value=httpx.Response(200, json={"authenticated": False}))
    assert client.get("/whoami", headers={"X-OpenMRS-Session-Id": "y"}).status_code == 401


@respx.mock
def test_missing_gate_privilege_is_403_and_superuser_passes():
    client = TestClient(make_app(SessionValidator(httpx.AsyncClient(), "http://omrs:8080")))
    respx.get(URL).mock(return_value=httpx.Response(200, json=session_body(privs=("Get Patients",))))
    assert client.get("/whoami", headers={"X-OpenMRS-Session-Id": "a"}).status_code == 403
    respx.get(URL).mock(return_value=httpx.Response(200, json=session_body(privs=(), roles=("System Developer",))))
    assert client.get("/whoami", headers={"X-OpenMRS-Session-Id": "b"}).status_code == 200


@respx.mock
def test_openmrs_down_is_502():
    respx.get(URL).mock(side_effect=httpx.ConnectError("boom"))
    client = TestClient(make_app(SessionValidator(httpx.AsyncClient(), "http://omrs:8080")))
    assert client.get("/whoami", headers={"X-OpenMRS-Session-Id": "z"}).status_code == 502


@respx.mock
async def test_cache_and_fresh_bypass(validator):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=session_body()))
    await validator.validate("S")
    await validator.validate("S")
    assert route.call_count == 1
    await validator.validate("S", fresh=True)
    assert route.call_count == 2


@respx.mock
async def test_cache_is_keyed_per_session_so_users_never_mix(validator):
    respx.get(URL).mock(
        side_effect=lambda req: httpx.Response(
            200, json=session_body("alice" if "A" in req.headers["cookie"] else "bob")
        )
    )
    a = await validator.validate("A")
    b = await validator.validate("B")
    assert (a.username, b.username) == ("alice", "bob")
    assert (await validator.validate("A")).username == "alice"


async def test_malformed_session_ids_rejected(validator):
    for bad in ("", None, "a;b", "x" * 300, "bad\nid"):
        with pytest.raises(SessionExpiredError):
            await validator.validate(bad)
