"""The pipeline task: walks a call through the state machine with
DB-checkpointed stages. Stage work is skipped when its output already
exists — idempotency that keeps a crash-redelivered job from re-paying for
transcription.

Failure handling (reduced M2 scope): provider failures are classified into a
machine-readable error_code and the call is marked `failed`. Automatic
retry-with-backoff and a user-triggered retry endpoint are deferred — the
error taxonomy (retryable vs permanent) is in place so both drop in later."""

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionFactory
from app.models.call import Call
from app.models.states import CallStatus, assert_transition
from app.services.analysis import analyze
from app.services.errors import ProviderError
from app.services.storage import storage
from app.services.transcription import transcribe

logger = logging.getLogger(__name__)


async def _advance(session: AsyncSession, call: Call, to_status: CallStatus) -> None:
    """Move the call to `to_status` — legality checked, then checkpointed."""
    assert_transition(CallStatus(call.status), to_status)
    call.status = to_status
    await session.commit()


async def _fail(session: AsyncSession, call: Call, error_code: str) -> None:
    """Land the row in `failed` with a diagnosable code — never silently."""
    await session.rollback()
    await session.refresh(call)
    call.status = CallStatus.FAILED
    call.error_code = error_code
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

        except ProviderError as e:
            # Classified external-service failure: persist its error_code.
            logger.warning("call %s failed: %s", call_id, e.error_code)
            await _fail(session, call, e.error_code)
            raise
        except Exception:
            # Unclassified bug — mark failed, then let arq log the traceback.
            await _fail(session, call, "internal_error")
            raise