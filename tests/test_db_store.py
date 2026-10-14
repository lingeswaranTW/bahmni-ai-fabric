# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

import asyncio
import os
import time

import pytest

from bahmni_ai.db.postgres import Base, init_postgres_stores
from bahmni_ai.store import PendingConfirmation

PG = os.environ.get("TEST_DATABASE_URL")  # e.g. postgresql+asyncpg://user:pw@localhost:55432/db


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(not PG, reason="no TEST_DATABASE_URL"))])
async def stores(request, tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path}/t.db" if request.param == "sqlite" else PG
    s = await init_postgres_stores(url)
    async with s.engine.begin() as conn:
        for t in reversed(Base.metadata.sorted_tables):
            await conn.execute(t.delete())
    yield s
    await s.aclose()


def pending(pid="c1", user="alice", ttl=60):
    return PendingConfirmation(pid, user, "conv", b"[]", "[]", time.time() + ttl)


async def test_conversation_roundtrip_and_owner_scoping(stores):
    await stores.conversations.save("alice", "c1", b"one")
    await stores.conversations.save("alice", "c1", b"two")
    assert await stores.conversations.load("alice", "c1") == b"two"
    assert await stores.conversations.load("bob", "c1") is None


async def test_confirmation_is_single_use_owner_bound_and_expires(stores):
    await stores.confirmations.put(pending())
    assert await stores.confirmations.take("bob", "c1") is None  # wrong owner does not consume it
    assert (await stores.confirmations.take("alice", "c1")).id == "c1"
    assert await stores.confirmations.take("alice", "c1") is None
    await stores.confirmations.put(pending("c2", ttl=-1))
    assert await stores.confirmations.take("alice", "c2") is None


async def test_concurrent_take_only_one_wins(stores):
    await stores.confirmations.put(pending())
    got = await asyncio.gather(*[stores.confirmations.take("alice", "c1") for _ in range(5)])
    assert sum(g is not None for g in got) == 1
