"""Call endpoints."""

import uuid
from pathlib import Path

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_arq_pool, get_session
from app.models.call import Call
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