import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.states import CallStatus


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    filename: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default=CallStatus.UPLOADED)
    error_code: Mapped[str | None] = mapped_column(String(50))

    # Payloads whose shape evolves through M2/M3 — JSONB, not columns.
    transcript: Mapped[dict | None] = mapped_column(JSONB)
    analysis: Mapped[dict | None] = mapped_column(JSONB)
    tag_overrides: Mapped[dict | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # Serves the keyset pagination in GET /api/calls.
        Index("ix_calls_created_at_id", "created_at", "id"),
        # Serves the status filter and "anything in flight?" polling.
        Index("ix_calls_status", "status"),
    )