"""Call endpoints."""

import base64
import uuid
from datetime import datetime
from pathlib import Path

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_arq_pool, get_session
from app.models.call import Call
from app.models.schemas import CallDetail, CallListItem, CallListPage
from app.models.states import CallStatus
from app.services.storage import storage

router = APIRouter(prefix="/calls", tags=["calls"])

MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # 500 MB

ALLOWED_EXTENSIONS = {".wav", ".mp3"}
ALLOWED_CONTENT_TYPES = {
    "audio/wav", "audio/x-wav", "audio/wave", "audio/mpeg", "audio/mp3",
    # curl and some browsers send a generic type — don't reject them
    "application/octet-stream", None, "",
}


class FileTooLarge(Exception):
    pass


class _CappedReader:
    """File-like wrapper that aborts the stream past max_bytes.

    Enforcing the cap here (not via Content-Length) means a client
    can't dodge it by lying in the headers.
    """

    def __init__(self, fileobj, max_bytes: int) -> None:
        self._f = fileobj
        self._remaining = max_bytes

    def read(self, size: int = -1) -> bytes:
        data = self._f.read(size)
        self._remaining -= len(data)
        if self._remaining < 0:
            raise FileTooLarge
        return data


@router.post("", status_code=202)
async def upload_call(
    file: UploadFile,
    session: AsyncSession = Depends(get_session),
    arq_pool: ArqRedis = Depends(get_arq_pool),
) -> dict:
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(422, detail="only .wav and .mp3 files are accepted")
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(422, detail=f"unsupported content type {file.content_type}")

    # Reject empty files before writing anything.
    if not file.file.read(1):
        raise HTTPException(422, detail="empty file")
    file.file.seek(0)

    # Id generated before the insert: row id and storage key are born together.
    call_id = uuid.uuid4()
    key = f"{call_id}{ext}"

    # Write order: storage -> row -> enqueue. Each step only promises
    # things that already exist; see task doc for the failure analysis.
    try:
        await storage.save(_CappedReader(file.file, MAX_UPLOAD_BYTES), key)
    except FileTooLarge:
        raise HTTPException(413, detail=f"file exceeds {MAX_UPLOAD_BYTES} bytes")

    call = Call(
        id=call_id,
        filename=file.filename or key,
        storage_key=key,
        status=CallStatus.UPLOADED,
    )
    session.add(call)
    await session.commit()

    try:
        await arq_pool.enqueue_job("process_call", str(call_id))
    except Exception:
        # File and row exist but no job ever will — make it visible, not silent.
        call.status = CallStatus.FAILED
        call.error_code = "enqueue_failed"
        await session.commit()
        raise HTTPException(500, detail="upload stored but queueing failed")

    return {"id": str(call_id), "status": str(call.status)}


def _encode_cursor(created_at: datetime, call_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{created_at.isoformat()}|{call_id}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw_ts, raw_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        return datetime.fromisoformat(raw_ts), uuid.UUID(raw_id)
    except Exception:
        raise HTTPException(400, detail="malformed cursor")


@router.get("", response_model=CallListPage)
async def list_calls(
    status: CallStatus | None = None,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> CallListPage:
    stmt = select(Call).order_by(Call.created_at.desc(), Call.id.desc())
    if status is not None:
        stmt = stmt.where(Call.status == status)
    if cursor is not None:
        after_ts, after_id = _decode_cursor(cursor)
        stmt = stmt.where(tuple_(Call.created_at, Call.id) < (after_ts, after_id))

    rows = (await session.execute(stmt.limit(limit + 1))).scalars().all()

    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = _encode_cursor(items[-1].created_at, items[-1].id) if has_more else None
    return CallListPage(
        items=[CallListItem.model_validate(r) for r in items],
        next_cursor=next_cursor,
    )


@router.get("/{call_id}", response_model=CallDetail)
async def get_call(
    call_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> CallDetail:
    call = await session.get(Call, call_id)
    if call is None:
        raise HTTPException(404, detail="call not found")
    return CallDetail.model_validate(call)