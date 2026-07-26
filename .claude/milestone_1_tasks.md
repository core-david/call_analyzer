# Milestone 1 — Task Implementation Guide

> Companion to `milestone_1.md`. Each task: concepts explained first (this doc
> doubles as a learning reference), then the implementation with code, then a
> manual verify step. No pytest in M1 — tests are M5.
>
> **Decisions locked in before writing this doc:**
> - Redis gets a volume + AOF so queued jobs survive `docker compose down/up`
> - Upload cap: **500 MB**, enforced while streaming
> - All routes live under **`/api`** (`POST /api/calls`, `GET /api/calls`, …)

---

## Task 1.1 — State machine

### Explain first

**What a state machine is and why we want one.** A call moves through a fixed
set of statuses: `uploaded → transcribing → analyzing → completed`, with
`failed` reachable from the two working states and `failed → transcribing` as
the retry path. A *state machine* is just the formalization of that: a set of
states plus a table saying which state can move to which. The value is in what
it **forbids** — nothing can jump `uploaded → completed`, nothing can leave
`completed`. Without enforcement, every bug that writes a wrong status silently
corrupts data; with enforcement, the same bug raises an exception at the exact
line that attempted it. That's the difference between "rejected in code" and
"rejected by convention."

**Why a `StrEnum`.** Python's `StrEnum` (3.11+) makes each member *be* a string
(`CallStatus.UPLOADED == "uploaded"`). That means: SQLAlchemy can store it in a
`VARCHAR` column without converters, Pydantic can serialize it into JSON
responses without custom encoders, and comparisons against raw DB strings just
work. One type shared by all three layers, zero glue.

**Why a domain exception.** `InvalidTransition` is *our* exception, not
`ValueError`. The API layer can catch it and map it to HTTP 409; the worker can
catch it and abort a job without corrupting the row. Layers translate domain
errors into their own vocabulary — the domain layer never knows HTTP exists.

**The transition table is data, not code.** A dict of `state → allowed next
states` is the single source of truth. Adding a state later is a one-line
change, and the table itself is printable/testable.

### Steps

1. Create `backend/app/models/states.py`:

   ```python
   """Call lifecycle state machine.

   The TRANSITIONS table is the single source of truth for which status
   changes are legal. Everything that mutates `Call.status` must go through
   assert_transition() — illegal transitions raise instead of corrupting data.
   """

   from enum import StrEnum


   class CallStatus(StrEnum):
       UPLOADED = "uploaded"
       TRANSCRIBING = "transcribing"
       ANALYZING = "analyzing"
       COMPLETED = "completed"
       FAILED = "failed"


   TRANSITIONS: dict[CallStatus, frozenset[CallStatus]] = {
       CallStatus.UPLOADED: frozenset({CallStatus.TRANSCRIBING}),
       CallStatus.TRANSCRIBING: frozenset({CallStatus.ANALYZING, CallStatus.FAILED}),
       CallStatus.ANALYZING: frozenset({CallStatus.COMPLETED, CallStatus.FAILED}),
       CallStatus.COMPLETED: frozenset(),  # terminal
       CallStatus.FAILED: frozenset({CallStatus.TRANSCRIBING}),  # user-triggered retry (M2)
   }


   class InvalidTransition(Exception):
       """Raised when a status change violates the TRANSITIONS table."""

       def __init__(self, from_status: CallStatus, to_status: CallStatus) -> None:
           self.from_status = from_status
           self.to_status = to_status
           super().__init__(f"illegal transition: {from_status} -> {to_status}")


   def can_transition(from_status: CallStatus, to_status: CallStatus) -> bool:
       return to_status in TRANSITIONS[from_status]


   def assert_transition(from_status: CallStatus, to_status: CallStatus) -> None:
       if not can_transition(from_status, to_status):
           raise InvalidTransition(from_status, to_status)
   ```

2. Verify in a REPL:

   ```bash
   cd backend && uv run python -c "
   from app.models.states import *
   assert can_transition(CallStatus.UPLOADED, CallStatus.TRANSCRIBING)
   assert not can_transition(CallStatus.UPLOADED, CallStatus.COMPLETED)
   try:
       assert_transition(CallStatus.COMPLETED, CallStatus.FAILED)
   except InvalidTransition as e:
       print('OK:', e)
   "
   ```

3. Commit.

### Files created
- `backend/app/models/states.py`

---

## Task 1.2 — DB layer & first migration

### Explain first

**What an ORM is.** SQLAlchemy maps Python classes to tables and instances to
rows: you write `call.status = "failed"` and it emits `UPDATE calls SET
status=...`. We use the 2.0 style — `Mapped[type]` annotations on a class
inheriting a shared `Base`. The annotation drives the column type: `Mapped[uuid.UUID]`
becomes a native Postgres `uuid` column, `Mapped[str | None]` a nullable varchar.

**Engine vs. session.** The *engine* is the connection pool — one per process,
created at import, connects lazily on first use. A *session* is a short-lived
unit of work: open one per request (or per job), make changes, `commit()`,
close. `async_sessionmaker` is the factory that stamps them out.
`expire_on_commit=False` matters in async code: by default SQLAlchemy expires
all attributes after commit so the next access triggers a *refresh query* — in
async code that hidden I/O raises errors. Disabling it means objects keep their
values after commit.

**Why `asyncpg`.** Our URL is `postgresql+asyncpg://...` — asyncpg is the async
Postgres driver, so a slow query yields the event loop instead of blocking the
whole server. This is what makes one uvicorn process able to hold many
in-flight requests.

**What migrations are and why Alembic.** The database schema must evolve in
lockstep with the models, on every machine and in production. A migration is a
versioned script ("create table calls") applied in order; Alembic tracks which
have run in an `alembic_version` table. `--autogenerate` diffs models vs. the
live DB and writes the script for you — you review it, then `upgrade head`
applies it. Schema changes become code-reviewed, reproducible history instead
of hand-run SQL.

**JSONB, and which fields escape it.** JSONB is Postgres's binary JSON column —
schemaless, indexable, queryable. Transcript and analysis shapes will evolve
through M2/M3, so nailing them into columns now would mean a migration per
tweak; JSONB right-sizes it (plan.md §3). But fields that the app *filters or
sorts by* get real columns: `status`, `created_at`, `error_code`, `filename`,
`storage_key`. Rule of thumb: query-surface → column, payload → JSONB.

**Status: varchar, not native PG enum.** Postgres has a real enum type, but
altering it (adding a state) requires special migrations and has transactional
quirks. A `VARCHAR` + the 1.1 state machine in code gives the same practical
integrity with none of the pain — the app is the only writer.

**The two indexes.** Task 1.7 paginates ordered by `(created_at DESC, id DESC)`
— a composite index on `(created_at, id)` makes each page a cheap range scan
even at 1,000+ rows. The `status` index serves the list filter and the polling
query ("any call still in flight?").

### Steps

1. Create `backend/app/core/db.py`:

   ```python
   """Async engine, session factory, and declarative base.

   One engine per process (lazy — no connection until first use), one session
   per request/job via SessionFactory.
   """

   from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
   from sqlalchemy.orm import DeclarativeBase

   from app.core.config import settings


   class Base(DeclarativeBase):
       pass


   engine = create_async_engine(settings.database_url)
   SessionFactory = async_sessionmaker(engine, expire_on_commit=False)
   ```

2. Create `backend/app/models/call.py`:

   ```python
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
   ```

3. Initialize Alembic with its **async template** (from `backend/`):

   ```bash
   uv run alembic init -t async alembic
   ```

4. Edit `backend/alembic/env.py` — point it at our metadata and URL:

   ```python
   # add near the top, after existing imports
   from app.core.config import settings
   from app.core.db import Base
   import app.models.call  # noqa: F401 — registers the Call table on Base.metadata

   # replace the target_metadata = None line
   target_metadata = Base.metadata

   # after `config = context.config`, override the ini URL
   # (escape % for configparser — not needed with our simple local URL)
   config.set_main_option("sqlalchemy.url", settings.database_url)
   ```

5. Generate and apply the first migration (postgres must be running):

   ```bash
   docker compose up -d postgres
   uv run alembic revision --autogenerate -m "create calls table"
   # review the generated file in alembic/versions/ — it should create
   # exactly the calls table and two indexes, nothing else
   uv run alembic upgrade head
   ```

6. Verify:

   ```bash
   docker compose exec postgres psql -U postgres -d call_analyzer -c "\d calls"
   docker compose exec postgres psql -U postgres -d call_analyzer -c "select * from alembic_version"
   ```

   Expect the full column list, both indexes, and one version row.

7. Commit.

### Files created
- `backend/app/core/db.py`, `backend/app/models/call.py`
- `backend/alembic.ini`, `backend/alembic/env.py`, `backend/alembic/versions/<hash>_create_calls_table.py`

---

## Task 1.3 — Storage layer

### Explain first

**Object storage vs. filesystem.** Render services don't share a disk, so audio
can't live in local files. Object storage (S3, R2, MinIO) is a key → blob store
over HTTP: no directories (keys just look pathy), no partial writes, and any
service can reach it. MinIO speaks the same S3 wire protocol as R2, which is
the whole trick: **local dev and production differ only by endpoint URL and
credentials in config** — the code is identical.

**What a `Protocol` is.** Python's `typing.Protocol` is a *structural*
interface: any class with matching method signatures satisfies it, no
inheritance needed. Callers depend on `Storage` (the shape), not `S3Storage`
(the implementation) — this is the "storage behind a protocol" seam from
plan.md §4, and it's what lets M5 swap in a fake for tests.

**Presigned URLs.** The bucket is private. A presigned URL is a time-limited
link the server *signs* with its credentials; anyone holding it can fetch that
one object until it expires. This is how the worker will hand Deepgram the
audio in M2 without ever proxying bytes itself. It enters the protocol now
because it's core to the design and trivial to include.

**Client lifecycle.** One `aioboto3.Session` per process (it's just config +
credentials), and a fresh client per operation via `async with` (clients hold
connections; the context manager guarantees cleanup). At M1 traffic this is
simple and correct; pooling clients is a later optimization.

**Key scheme.** `{call_id}.{ext}` — the API generates the UUID *before* the DB
insert, so the row id and object key are born together and either can derive
the other. The original filename is display metadata in the DB, never a storage
key (user filenames collide and can contain hostile characters).

### Steps

1. Create `backend/app/services/storage.py`:

   ```python
   """Storage seam: Storage protocol + S3 implementation (MinIO local, R2 prod).

   The rest of the app depends on the Storage protocol, never on aioboto3.
   """

   from typing import BinaryIO, Protocol

   import aioboto3

   from app.core.config import settings


   class Storage(Protocol):
       async def save(self, fileobj: BinaryIO, key: str) -> None: ...
       async def presigned_url(self, key: str, expires_in: int = 3600) -> str: ...
       async def delete(self, key: str) -> None: ...


   class S3Storage:
       def __init__(self) -> None:
           self._session = aioboto3.Session()

       def _client(self):
           return self._session.client(
               "s3",
               endpoint_url=settings.storage_endpoint_url,
               aws_access_key_id=settings.storage_access_key,
               aws_secret_access_key=settings.storage_secret_key,
           )

       async def save(self, fileobj: BinaryIO, key: str) -> None:
           # upload_fileobj streams in chunks (multipart under the hood) —
           # the whole file is never held in memory.
           async with self._client() as s3:
               await s3.upload_fileobj(fileobj, settings.storage_bucket_name, key)

       async def presigned_url(self, key: str, expires_in: int = 3600) -> str:
           async with self._client() as s3:
               return await s3.generate_presigned_url(
                   "get_object",
                   Params={"Bucket": settings.storage_bucket_name, "Key": key},
                   ExpiresIn=expires_in,
               )

       async def delete(self, key: str) -> None:
           async with self._client() as s3:
               await s3.delete_object(Bucket=settings.storage_bucket_name, Key=key)


   storage: Storage = S3Storage()
   ```

2. Verify against MinIO (stack up):

   ```bash
   cd backend && uv run python -c "
   import asyncio, io
   from app.services.storage import storage

   async def main():
       await storage.save(io.BytesIO(b'hello storage'), 'smoke-test.txt')
       url = await storage.presigned_url('smoke-test.txt')
       print('presigned:', url[:80], '...')
       import httpx
       r = httpx.get(url)
       assert r.content == b'hello storage', r.content
       await storage.delete('smoke-test.txt')
       print('save/presign/fetch/delete OK')

   asyncio.run(main())
   "
   ```

3. Commit.

### Files created
- `backend/app/services/storage.py`

---

## Task 1.4 — Stub services

### Explain first

**Why stubs at all.** M1's purpose is proving the *architecture* — queue, state
machine, checkpoints, polling — with zero external variables. If we integrated
Deepgram now and something broke, we couldn't tell an architecture bug from an
API-integration bug. Stubs make the categories impossible to confuse: M2/M3
then swap implementations *behind unchanged signatures*.

**The stub's shape is a contract.** Whatever these functions return is what the
worker persists, the detail endpoint serves, and the frontend renders. So the
field names are decided *now*, once: utterances as `{speaker, start, end,
text}` (Deepgram's natural output shape), analysis as `{summary, tags{outcome,
objections, lead_temperature}, intent, mood}` (the closed tagging vocabulary
from plan.md §3). M2/M3 must map real provider responses *into* these shapes —
not the reverse.

**Production-shaped signatures.** `transcribe(audio_url)` takes a URL — because
that's what the real Deepgram call takes (the presigned URL). `analyze(
transcript_text)` takes text — because that's what the LLM prompt consumes.
Neither takes a `Call` row: services stay ignorant of the database
(plan.md §4, "orchestration ↔ external services").

**Why they sleep.** `asyncio.sleep(2)` per stage makes intermediate states
*observable*: polling actually catches `transcribing` and `analyzing` instead
of the row teleporting to `completed`. It also rehearses reality — these calls
take seconds to minutes in production.

### Steps

1. Create `backend/app/services/transcription.py`:

   ```python
   """Transcription service. M1: canned stub. M2 replaces the body with a real
   Deepgram call behind the exact same signature."""

   import asyncio

   STUB_DELAY_SECONDS = 2


   async def transcribe(audio_url: str) -> dict:
       """Transcribe the audio at `audio_url` with speaker diarization.

       Returns: {language, text, utterances: [{speaker, start, end, text}]}
       """
       await asyncio.sleep(STUB_DELAY_SECONDS)  # simulate provider latency
       utterances = [
           {"speaker": 0, "start": 0.0, "end": 3.5,
            "text": "Hi, this is Ana calling from Altur, do you have a minute?"},
           {"speaker": 1, "start": 3.9, "end": 6.1,
            "text": "Sure, what is this about?"},
           {"speaker": 0, "start": 6.4, "end": 11.0,
            "text": "We help teams analyze their sales calls automatically."},
           {"speaker": 1, "start": 11.4, "end": 14.2,
            "text": "Interesting — send me the details, price matters though."},
       ]
       return {
           "language": "en",
           "text": " ".join(u["text"] for u in utterances),
           "utterances": utterances,
       }
   ```

2. Create `backend/app/services/analysis.py`:

   ```python
   """LLM analysis service. M1: canned stub. M3 replaces the body with a real
   Gemini call behind the exact same signature."""

   import asyncio

   STUB_DELAY_SECONDS = 2


   async def analyze(transcript_text: str) -> dict:
       """Analyze a call transcript into the closed tagging schema.

       Returns: {summary, tags: {outcome, objections, lead_temperature},
                 intent, mood: {agent, customer}}
       """
       await asyncio.sleep(STUB_DELAY_SECONDS)  # simulate provider latency
       return {
           "summary": "Cold outreach call. Prospect showed interest but "
                      "flagged price sensitivity; asked for details by email.",
           "tags": {
               "outcome": "follow_up",
               "objections": ["price"],
               "lead_temperature": "warm",
           },
           "intent": "evaluate_product",
           "mood": {"agent": "friendly", "customer": "neutral"},
       }
   ```

3. Verify:

   ```bash
   cd backend && uv run python -c "
   import asyncio
   from app.services.transcription import transcribe
   from app.services.analysis import analyze

   async def main():
       t = await transcribe('http://fake-url')
       assert {'language', 'text', 'utterances'} <= t.keys()
       a = await analyze(t['text'])
       assert {'summary', 'tags', 'intent', 'mood'} <= a.keys()
       print('shapes OK')

   asyncio.run(main())
   "
   ```

4. Commit.

### Files created
- `backend/app/services/transcription.py`, `backend/app/services/analysis.py`

---

## Task 1.5 — Upload endpoint

### Explain first

**Why `202 Accepted`, not `200`/`201`.** HTTP 202 means "received, processing
will happen later" — the honest description of an async pipeline. The response
carries the id; the client learns the outcome by polling, never by waiting on
this request.

**Streaming, not buffering.** A 30-minute WAV can be ~300 MB. Reading it into
memory (`await file.read()`) would let a handful of concurrent uploads exhaust
the API's RAM. Instead FastAPI's `UploadFile` wraps a `SpooledTemporaryFile`,
and `upload_fileobj` pulls it in chunks — memory use is one chunk, regardless
of file size. The 500 MB cap is enforced *inside the stream* by a counting
wrapper: the limit can't be dodged by lying about Content-Length.

**Dependency injection & lifespan.** FastAPI's `Depends()` hands request
handlers what they need (a DB session, the queue pool) instead of them reaching
for globals — this is what makes handlers testable with fakes in M5. The arq
pool is created once at app startup in the *lifespan* context manager (runs
before the first request, cleans up on shutdown) and lives on `app.state`.

**The write order is deliberate: storage → row → enqueue.** Each step only
promises things that already exist. If storage fails: nothing anywhere, clean
422/500. If the insert fails: an orphan object in storage, but no row promising
it — invisible, cleanable later. If enqueue fails: file and row both exist, so
mark the row `failed / enqueue_failed` — the failure is *visible in the UI*
rather than silently lost. Worst-case leftovers are always inert, never a row
whose audio is missing.

**Validation depth (M1).** Extension must be `.wav`/`.mp3`; declared
Content-Type is checked only if the client sent a meaningful one (curl sends
`application/octet-stream` by default — rejecting that would break every curl
test). Sniffing real magic bytes is M2's error-classification work; a corrupt
file uploaded today simply fails at the (stubbed) transcription stage, which is
the correct path anyway.

### Steps

1. Create `backend/app/api/deps.py`:

   ```python
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
   ```

2. Create `backend/app/api/calls.py` (upload only; 1.7 adds the reads):

   ```python
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
   ```

3. Update `backend/app/main.py` — lifespan + router:

   ```python
   from contextlib import asynccontextmanager

   from arq import create_pool
   from arq.connections import RedisSettings
   from fastapi import FastAPI

   from app.api import calls
   from app.core.config import settings


   @asynccontextmanager
   async def lifespan(app: FastAPI):
       # One arq pool per process, shared by all requests via app.state.
       app.state.arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
       yield
       await app.state.arq_pool.aclose()


   app = FastAPI(title="Call Analyzer", lifespan=lifespan)
   app.include_router(calls.router, prefix="/api")


   @app.get("/health")
   async def health():
       return {"status": "ok"}
   ```

4. Add Redis durability in `docker-compose.yml` (decision: queued jobs must
   survive `down`/`up`):

   ```yaml
   redis:
     image: redis:7-alpine
     command: redis-server --appendonly yes
     volumes:
       - redis-data:/data
     # ports/healthcheck/networks unchanged
   ```

   and register the volume:

   ```yaml
   volumes:
     postgres-data:
     minio-data:
     redis-data:
   ```

5. Verify (stack up; worker will log a missing-function error when the job is
   picked up — expected until 1.6):

   ```bash
   curl -s -X POST localhost:8000/api/calls \
     -F "file=@tests/fixtures/audio/stubs/stub_0001.wav" | jq
   # → {"id": "...", "status": "uploaded"}  with HTTP 202

   curl -s -X POST localhost:8000/api/calls -F "file=@README.md" -w "%{http_code}\n"
   # → 422

   docker compose exec postgres psql -U postgres -d call_analyzer \
     -c "select id, filename, status from calls"
   # → one row, status 'uploaded'
   # MinIO console (localhost:9001): bucket contains <id>.wav
   ```

6. Commit.

### Files created / changed
- `backend/app/api/deps.py`, `backend/app/api/calls.py`
- edits: `backend/app/main.py`, `docker-compose.yml`

---

## Task 1.6 — Worker pipeline task

### Explain first

**How arq delivers work.** `enqueue_job("process_call", id)` serializes the
call into Redis. A worker process — completely separate from the API — pulls
it and invokes the function named `process_call` from its `functions` list with
the same arguments. The API and worker never talk directly; Redis is the seam.
This is why the API survives worker crashes and why workers scale horizontally.

**Checkpointing = crash-safe progress.** The task walks the state machine and
**commits after every stage**: status update → commit, transcript persisted →
commit, and so on. Each commit is a checkpoint: whatever happens later, that
progress is durable. The payoff is the M2 retry contract — on a retried job the
task *re-reads the row* and skips any stage whose output already exists.
Transcript present? Don't transcribe again (that's the expensive Deepgram call
we never want to double-pay). The status still walks `transcribing →
analyzing` so the machine's rules hold; only the *work* is skipped. This is
**idempotency**: running the task twice produces the same result as once —
essential because queues deliver *at least* once, not exactly once.

**Every transition goes through `assert_transition`.** If a job is somehow
delivered for a call in the wrong state (double enqueue, race), the task raises
instead of trampling the row. `InvalidTransition` aborts without writes — the
row is left exactly as found.

**Failure handling (M1 scope).** Any exception in a stage: mark the row
`failed` (a legal transition from both working states) with
`error_code="internal_error"`, then re-raise so arq logs it. `max_tries = 1` —
automatic retries only make sense once M2 classifies which errors *deserve*
retry. A worker killed mid-stage (SIGKILL) leaves a stuck `transcribing` row:
accepted for M1, documented; the guard comes with M2's error classification.

**DB access in the worker.** The engine in `core/db.py` is lazy — importing it
creates no connections, so API and worker share the same module and each
process gets its own pool on first use. The task opens one session per job.

### Steps

1. Create `backend/app/worker/tasks.py`:

   ```python
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
   ```

2. Update `backend/app/worker/settings.py` — real task replaces the `ping`
   placeholder:

   ```python
   """arq worker settings."""

   from arq.connections import RedisSettings

   from app.core.config import settings
   from app.worker.tasks import process_call


   class WorkerSettings:
       redis_settings = RedisSettings.from_dsn(settings.redis_url)
       functions = [process_call]
       max_tries = 1        # retries become meaningful with M2 error classification
       job_timeout = 600    # generous ceiling; a 30-min call is minutes of work in M2
   ```

3. Verify the full pipeline (this is the milestone's core moment):

   ```bash
   docker compose up -d --build
   ID=$(curl -s -X POST localhost:8000/api/calls \
     -F "file=@tests/fixtures/audio/stubs/stub_0002.wav" | jq -r .id)

   # watch the state machine walk (~4s total thanks to stub delays)
   for i in 1 2 3 4 5; do
     docker compose exec postgres psql -U postgres -d call_analyzer -t \
       -c "select status from calls where id = '$ID'"
     sleep 1.5
   done
   # expect to see: transcribing → analyzing → completed

   docker compose logs worker | tail -5   # "call ... completed", no errors
   ```

4. Verify durability (the exit-check rehearsal): enqueue one, immediately
   `docker compose down`, then `up -d` — the job must complete after restart.

5. Commit.

### Files created / changed
- `backend/app/worker/tasks.py`
- edits: `backend/app/worker/settings.py`

---

## Task 1.7 — List & detail endpoints

### Explain first

**Why cursor (keyset) pagination, not offset.** `OFFSET 900 LIMIT 50` makes
Postgres *walk and discard* 900 rows — cost grows with page depth, and rows
inserted mid-scroll shift every page (the polling UI would see duplicates and
gaps). Keyset pagination instead says "give me rows *after this exact
position*": `WHERE (created_at, id) < (cursor values) ORDER BY created_at DESC,
id DESC LIMIT n`. With the composite index from 1.2 every page is an equally
cheap range scan, stable under concurrent inserts — exactly what a list being
polled during a 1,000-file burst needs. `id` is in the key because `created_at`
alone has ties (1,000 rows in seconds); the pair is unique, so the ordering is
total.

**Opaque cursors.** The cursor is base64 of `created_at|id` — the client treats
it as a token, echoes it back, and never parses it. That keeps the pagination
scheme swappable without breaking clients.

**The `limit + 1` trick.** Fetch one row more than requested: if it arrives,
there's a next page (build `next_cursor` from the *last returned* row); if not,
`next_cursor` is null. One query, no `COUNT(*)`.

**Two response models.** `CallListItem` is the cheap summary the UI polls every
few seconds (id, filename, status, error_code, created_at). `CallDetail` adds
transcript/analysis/overrides — potentially hundreds of KB for a 30-minute
call, fetched only when a user opens one. Splitting them keeps polling cost
flat. `model_config = ConfigDict(from_attributes=True)` lets Pydantic read
straight from ORM objects.

**404 semantics.** Unknown-but-valid UUID → 404. Malformed id → FastAPI's
automatic 422 (it fails UUID parsing before the handler runs). Both are fine;
they mean different things ("no such call" vs. "not even a valid id").

### Steps

1. Create `backend/app/models/schemas.py`:

   ```python
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
   ```

2. Add to `backend/app/api/calls.py`:

   ```python
   # --- add to imports ---
   import base64
   from datetime import datetime

   from fastapi import Query
   from sqlalchemy import select, tuple_

   from app.models.schemas import CallDetail, CallListItem, CallListPage


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
   ```

3. Verify:

   ```bash
   # seed a handful
   for f in tests/fixtures/audio/stubs/stub_000{1..5}.wav; do
     curl -s -X POST localhost:8000/api/calls -F "file=@$f" > /dev/null
   done

   curl -s "localhost:8000/api/calls?limit=2" | jq          # 2 items + next_cursor
   CUR=$(curl -s "localhost:8000/api/calls?limit=2" | jq -r .next_cursor)
   curl -s "localhost:8000/api/calls?limit=2&cursor=$CUR" | jq   # next 2, no overlap
   curl -s "localhost:8000/api/calls?status=completed" | jq      # filter works
   ID=$(curl -s localhost:8000/api/calls | jq -r '.items[0].id')
   curl -s "localhost:8000/api/calls/$ID" | jq '.transcript.utterances | length'  # 4
   curl -s -w "%{http_code}\n" -o /dev/null \
     "localhost:8000/api/calls/00000000-0000-0000-0000-000000000000"  # 404
   ```

4. Commit.

### Files created / changed
- `backend/app/models/schemas.py`
- edits: `backend/app/api/calls.py`

---

## Milestone exit check

```bash
docker compose down && docker compose up -d --build
ID=$(curl -s -X POST localhost:8000/api/calls \
  -F "file=@tests/fixtures/audio/stubs/stub_0001.wav" | jq -r .id)
watch -n 1 "curl -s localhost:8000/api/calls | jq '.items[0].status'"
# uploaded → transcribing → analyzing → completed

curl -s localhost:8000/api/calls/$ID | jq '{status, transcript, analysis}'
# canned transcript + analysis present

# durability: enqueue, kill mid-flight, restart — job must finish
curl -s -X POST localhost:8000/api/calls \
  -F "file=@tests/fixtures/audio/stubs/stub_0002.wav" > /dev/null \
  && docker compose down && docker compose up -d
```

## Execution order

```
1.1 states → 1.2 db+migration → 1.3 storage → 1.4 stubs → 1.5 upload → 1.6 worker → 1.7 list/detail
```

Each task ends with a commit.
