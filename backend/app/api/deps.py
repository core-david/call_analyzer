"""FastAPI dependencies — one place where handlers get their collaborators."""

from collections.abc import AsyncIterator

from arq.connections import ArqRedis
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionFactory


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session


def get_arq_pool(request: Request) -> ArqRedis:
    return request.app.state.arq_pool