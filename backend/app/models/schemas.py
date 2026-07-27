"""Pydantic response models — the API's public shapes."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.states import CallStatus


class CallListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    status: CallStatus
    error_code: str | None
    created_at: datetime


class CallDetail(CallListItem):
    storage_key: str
    transcript: dict | None
    analysis: dict | None
    tag_overrides: dict | None
    updated_at: datetime


class CallListPage(BaseModel):
    items: list[CallListItem]
    next_cursor: str | None
