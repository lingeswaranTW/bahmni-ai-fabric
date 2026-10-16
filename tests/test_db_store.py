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
    return PendingConfirmation(pid, user, "conv", [], "[]", time.time() + ttl)


async def test_conversation_roundtrip_and_owner_scoping(stores):
    await stores.conversations.save("alice", "c1", [{"kind": "request", "n": 1}], "first")
    await stores.conversations.save("alice", "c1", [{"kind": "request", "n": 2}], "ignored")
    assert await stores.conversations.load("alice", "c1") == [{"kind": "request", "n": 2}]  # stored as JSON, not bytes
    assert (await stores.conversations.get("alice", "c1")).summary.title == "first"  # title fixed at creation
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


async def test_list_is_owner_scoped_newest_first_and_paginated(stores):
    for cid in ("a", "b", "c"):
        await stores.conversations.save("alice", cid, [], cid)
    await stores.conversations.save("bob", "x", [], "x")
    await stores.conversations.save("alice", "a", [{"n": 1}])  # activity moves it to the top
    assert [c.id for c in await stores.conversations.list("alice", 10, 0)] == ["a", "c", "b"]
    assert [c.id for c in await stores.conversations.list("alice", 1, 1)] == ["c"]
    assert [c.id for c in await stores.conversations.list("bob", 10, 0)] == ["x"]


async def test_soft_delete_hides_and_reuse_starts_fresh(stores):
    s = stores.conversations
    await s.save("alice", "c1", [{"old": True}], "old title")
    assert not await s.delete("bob", "c1", hard=False)  # not the owner: same answer as unknown
    assert await s.delete("alice", "c1", hard=False)
    assert await s.load("alice", "c1") is None and await s.get("alice", "c1") is None
    assert await s.list("alice", 10, 0) == []
    assert not await s.delete("alice", "c1", hard=False)  # already gone
    await s.save("alice", "c1", [{"new": True}], "new title")  # the id is reused: nothing of the old one survives
    rec = await s.get("alice", "c1")
    assert rec.messages_json == [{"new": True}] and rec.summary.title == "new title"


async def test_hard_delete_removes_the_row(stores):
    s = stores.conversations
    await s.save("alice", "c1", [], "t")
    assert await s.delete("alice", "c1", hard=True)
    assert await s.get("alice", "c1") is None and not await s.delete("alice", "c1", hard=True)
    await s.save("alice", "c2", [], "t")
    await s.delete("alice", "c2", hard=False)
    assert await s.delete("alice", "c2", hard=True)  # a soft-deleted conversation can still be purged
    assert not await s.delete("alice", "c2", hard=True)
