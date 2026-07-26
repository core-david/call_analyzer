"""The pipeline task: walks a call through the state machine with
DB-checkpointed stages. Stage work is skipped when its output already
exists (idempotent retries — the M2 contract, honored from day one)."""

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionFactory
from app.models.call import Call
from app.models.states import CallStatus, assert_transition
from app.services.analysis import analyze
from app.services.storage import storage
from app.services.transcription import transcribe

logger = logging.getLogger(__name__)


async def _advance(session: AsyncSession, call: Call, to_status: CallStatus) -> None:
    """Move the call to `to_status` — legality checked, then checkpointed."""
    assert_transition(CallStatus(call.status), to_status)
    call.status = to_status
    await session.commit()


async def process_call(ctx: dict, call_id: str) -> None:
    async with SessionFactory() as session:
        call = await session.get(Call, uuid.UUID(call_id))
        if call is None:
            logger.error("process_call: no such call %s", call_id)
            return

        try:
            # Stage 1 — transcription (skipped if checkpoint exists).
            await _advance(session, call, CallStatus.TRANSCRIBING)
            if call.transcript is None:
                audio_url = await storage.presigned_url(call.storage_key)
                call.transcript = await transcribe(audio_url)
                await session.commit()  # checkpoint: transcript is durable

            # Stage 2 — analysis (skipped if checkpoint exists).
            await _advance(session, call, CallStatus.ANALYZING)
            if call.analysis is None:
                call.analysis = await analyze(call.transcript["text"])
                await session.commit()  # checkpoint: analysis is durable

            await _advance(session, call, CallStatus.COMPLETED)
            logger.info("call %s completed", call_id)

        except Exception:
            # Mark failure visibly, then let arq log the traceback.
            await session.rollback()
            await session.refresh(call)
            call.status = CallStatus.FAILED
            call.error_code = "internal_error"
            await session.commit()
            raise