# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Postgres-backed stores (any SQLAlchemy async dialect works; tests use SQLite).

Schema is created on startup with ``create_all``. Rows hold only: conversation id, owner, redacted
model-context JSON, and short-lived pending confirmations. Never credentials or session ids.
"""

import time
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Float, Index, String, Text, delete, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from bahmni_ai.store import ConversationRecord, ConversationSummary, Messages, PendingConfirmation


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


# JSONB on Postgres (queryable, validated by the database), plain JSON elsewhere (SQLite in tests).
_JSON = JSON().with_variant(JSONB(), "postgresql")


class ConversationRow(Base):
    __tablename__ = "ai_conversation"
    user_uuid: Mapped[str] = mapped_column(String(64), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    messages_json: Mapped[Messages] = mapped_column(_JSON)
    title: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    __table_args__ = (Index("ix_ai_conversation_user_updated", "user_uuid", "updated_at"),)


class PendingRow(Base):
    __tablename__ = "ai_pending_confirmation"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_uuid: Mapped[str] = mapped_column(String(64), index=True)
    conversation_id: Mapped[str] = mapped_column(String(64))
    messages_json: Mapped[Messages] = mapped_column(_JSON)
    actions_json: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[float] = mapped_column(Float, index=True)


def _summary(row: ConversationRow) -> ConversationSummary:
    return ConversationSummary(row.id, row.title, row.created_at, row.updated_at)


class PostgresConversationStore:
    def __init__(self, sessions: async_sessionmaker):  # type: ignore[type-arg]
        self._sessions = sessions

    async def _live(self, s: AsyncSession, user_uuid: str, conversation_id: str) -> ConversationRow | None:
        row = await s.get(ConversationRow, (user_uuid, conversation_id))
        return None if row is None or row.deleted_at is not None else row

    async def load(self, user_uuid: str, conversation_id: str) -> Messages | None:
        async with self._sessions() as s:
            row = await self._live(s, user_uuid, conversation_id)
            return row.messages_json if row else None

    async def save(
        self, user_uuid: str, conversation_id: str, messages_json: Messages, title: str | None = None
    ) -> None:
        async with self._sessions.begin() as s:
            row = await s.get(ConversationRow, (user_uuid, conversation_id))
            if row is None:
                s.add(ConversationRow(
                    user_uuid=user_uuid, id=conversation_id, messages_json=messages_json, title=title or "",
                ))  # fmt: skip
            elif row.deleted_at is not None:  # a deleted id reused: a fresh conversation, nothing of the old one kept
                row.messages_json, row.title = messages_json, title or ""
                row.created_at = row.updated_at = _now()
                row.deleted_at = None
            else:
                row.messages_json = messages_json

    async def list(self, user_uuid: str, limit: int, offset: int) -> list[ConversationSummary]:
        async with self._sessions() as s:
            rows = await s.scalars(
                select(ConversationRow)
                .where(ConversationRow.user_uuid == user_uuid, ConversationRow.deleted_at.is_(None))
                .order_by(ConversationRow.updated_at.desc())
                .limit(limit)
                .offset(offset)
            )
            return [_summary(r) for r in rows]

    async def get(self, user_uuid: str, conversation_id: str) -> ConversationRecord | None:
        async with self._sessions() as s:
            row = await self._live(s, user_uuid, conversation_id)
            return ConversationRecord(_summary(row), row.messages_json) if row else None

    async def delete(self, user_uuid: str, conversation_id: str, hard: bool) -> bool:
        where = (ConversationRow.user_uuid == user_uuid, ConversationRow.id == conversation_id)
        async with self._sessions.begin() as s:
            if hard:  # also purges an already soft-deleted row
                result = await s.execute(delete(ConversationRow).where(*where))
            else:
                live = ConversationRow.deleted_at.is_(None)
                result = await s.execute(update(ConversationRow).where(*where, live).values(deleted_at=_now()))
            return bool(result.rowcount)


class PostgresConfirmationStore:
    def __init__(self, sessions: async_sessionmaker):  # type: ignore[type-arg]
        self._sessions = sessions

    async def put(self, p: PendingConfirmation) -> None:
        async with self._sessions.begin() as s:
            await s.execute(delete(PendingRow).where(PendingRow.expires_at < time.time()))
            s.add(
                PendingRow(
                    id=p.id,
                    user_uuid=p.user_uuid,
                    conversation_id=p.conversation_id,
                    messages_json=p.messages_json,
                    actions_json=p.actions_json,
                    expires_at=p.expires_at,
                )
            )

    async def take(self, user_uuid: str, confirmation_id: str) -> PendingConfirmation | None:
        # One atomic DELETE ... RETURNING makes the confirmation single-use even across instances.
        async with self._sessions.begin() as s:
            row = (
                await s.execute(
                    delete(PendingRow)
                    .where(PendingRow.id == confirmation_id, PendingRow.user_uuid == user_uuid)
                    .returning(PendingRow)
                )
            ).scalar_one_or_none()
        if row is None or row.expires_at <= time.time():
            return None
        return PendingConfirmation(row.id, row.user_uuid, row.conversation_id, row.messages_json,
                                   row.actions_json, row.expires_at)  # fmt: skip


class PostgresStores:
    def __init__(self, engine: AsyncEngine):
        self.engine = engine
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.conversations = PostgresConversationStore(sessions)
        self.confirmations = PostgresConfirmationStore(sessions)

    async def aclose(self) -> None:
        await self.engine.dispose()

    async def exists_check(self) -> None:
        async with self.engine.connect() as c:
            await c.execute(select(1))


async def init_postgres_stores(database_url: str) -> PostgresStores:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return PostgresStores(engine)
