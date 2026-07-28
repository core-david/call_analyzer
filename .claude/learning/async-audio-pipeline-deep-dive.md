# Async Audio Pipeline — A Deep Dive, Piece by Piece

This is teaching material, not a recipe. Each section starts with the smallest possible example of a concept, breaks it, fixes it, and only then assembles the "real" version you'd ship. The goal is that by the end, FastAPI, object storage, Redis queues, and arq are tools you can reach for in *any* project — the audio pipeline is just the excuse.

Suggested way to work through it: have a terminal open, and actually run the small snippets. Every one of them is runnable in isolation with at most `pip install fastapi uvicorn redis arq boto3 httpx` and `docker run` for Redis/MinIO.

---

## 1. The pattern we're building toward (short version)

Slow work (minutes of transcription) cannot live inside an HTTP request (seconds of patience). So we split the system in two:

```
        the fast half                          the slow half
┌──────────────────────────┐          ┌───────────────────────────┐
│ FastAPI: accept, record, │  Redis   │ arq workers: pull ticket, │
│ hand out ticket, answer  │ ──────►  │ do minutes of work, write │
│ "done yet?"              │  queue   │ the result down           │
└──────────────────────────┘          └───────────────────────────┘
              │                                     │
              └────────────► Postgres ◄─────────────┘
                        (the shared truth)
```

Three rules give the whole architecture its shape. Keep them in mind as you read; every design choice below traces back to one of them.

1. **The request and the work are decoupled.** The API returns `202 Accepted + job_id` in milliseconds; the work happens elsewhere, later.
2. **Redis holds only the ticket; Postgres holds the truth.** Anything you can't afford to lose lives in Postgres. Redis is fast, shared scratch space.
3. **The two halves never talk to each other directly.** The API writes a row and a ticket; the worker reads the ticket and updates the row. The database *is* the communication channel. No RPC, no service discovery, nothing to break.

Now let's earn each piece.

---

## 2. FastAPI — but really, `asyncio` first

You said you've used FastAPI lightly. The part people usually skip when learning it "lightly" is the part that makes this architecture work: the event loop. So we start below FastAPI.

### 2.1 The event loop in 15 lines

A coroutine (`async def`) is a function that can *pause itself* at an `await` and hand control back to a scheduler — the event loop — which runs something else in the meantime.

```python
import asyncio, time

async def task(name, seconds):
    print(f"{name} started")
    await asyncio.sleep(seconds)      # "I'm waiting on I/O — run someone else"
    print(f"{name} done")

async def main():
    t0 = time.perf_counter()
    await asyncio.gather(task("A", 2), task("B", 2), task("C", 2))
    print(f"total: {time.perf_counter() - t0:.1f}s")

asyncio.run(main())
```

Run it. Three 2-second tasks finish in **2.0 seconds**, not 6, on a single thread. Nothing ran "in parallel" in the CPU sense — the loop just interleaved three *waits*. This is the whole trick: **concurrency for I/O-bound work without threads.**

That matters here because everything our system does is waiting: waiting for an upload to arrive, waiting for S3, waiting for Postgres, waiting for Deepgram. Async lets one process wait on hundreds of things at once.

### 2.2 The one way to ruin it

Change `await asyncio.sleep(seconds)` to `time.sleep(seconds)` and rerun. Total: **6 seconds.** `time.sleep` never yields to the loop — it *blocks* it, and while the loop is blocked, *nothing else in the entire process runs*. Not other tasks, not other HTTP requests.

This is the cardinal sin of async programming, and it's why heavy work must not live in your API:

> **A blocked event loop = a frozen server.** Anything slow — CPU-heavy code, a synchronous library call, `time.sleep` — inside an `async def` freezes every concurrent request.

Transcribing audio for two minutes inside an endpoint wouldn't just make *that* request slow. It would make your API serve nobody for two minutes. Now you know, at the mechanical level, why the worker pool exists.

### 2.3 FastAPI = routing + validation on top of the loop

FastAPI's job is to map HTTP requests onto coroutines and validate the data crossing the boundary. Smallest possible app:

```python
# main.py
from fastapi import FastAPI

app = FastAPI()

@app.get("/health")
async def health():
    return {"ok": True}          # dict → JSON automatically
```

```bash
uvicorn main:app --reload
```

Two things to internalize:

- **Uvicorn is the server, FastAPI is the framework.** Uvicorn owns the event loop and speaks HTTP; FastAPI decides which coroutine handles which path. (Same relationship as gunicorn↔Flask, but async.)
- Open **http://localhost:8000/docs** — FastAPI generated interactive API docs from your function signatures. This falls out of the type system and it's genuinely how you'll test every endpoint below, no curl needed.

### 2.4 Path params, query params, and validation for free

FastAPI reads your function signature and does the parsing, coercion, and error responses for you:

```python
import uuid
from fastapi import HTTPException

@app.get("/jobs/{job_id}")
async def get_job(job_id: uuid.UUID, verbose: bool = False):
    ...
```

- `job_id` comes from the path. Because it's annotated `uuid.UUID`, the string `"3f2a..."` is parsed into a real UUID object — and `GET /jobs/banana` is rejected with a `422` *before your code runs*. Free input validation.
- `verbose` isn't in the path, so it becomes a query param (`?verbose=true`), with a default, coerced to `bool`.

The general principle: **the function signature is the API contract.** This is 80% of day-to-day FastAPI.

### 2.5 `async def` vs plain `def` endpoints — a detail that bites people

FastAPI accepts both, and they behave differently:

```python
@app.get("/a")
async def a():
    time.sleep(5)        # ❌ blocks the loop — freezes the whole server
    return {}

@app.get("/b")
def b():
    time.sleep(5)        # ✓ FastAPI runs plain `def` in a thread pool —
    return {}            #   slow, but only for this request
```

Rule of thumb: use `async def` and `await`-friendly libraries (`asyncpg`, `httpx`, `aioboto3`) for everything. If you're stuck with a blocking library, either use a plain `def` endpoint or wrap the call in `await asyncio.to_thread(blocking_fn)`. Just never put blocking calls in `async def` bare.

### 2.6 Receiving a file upload

Browsers send files as `multipart/form-data`. FastAPI hands you an `UploadFile`:

```python
from fastapi import UploadFile

@app.post("/uploads")
async def create_upload(file: UploadFile):
    return {"filename": file.filename, "type": file.content_type}
```

Test it at `/docs` — you get a file picker. The important part is what `UploadFile` *is*: a **spooled temporary file**. Small uploads sit in RAM; past a threshold they spill to disk. You read it in chunks:

```python
@app.post("/uploads")
async def create_upload(file: UploadFile):
    size = 0
    while chunk := await file.read(1024 * 1024):   # 1 MB at a time
        size += len(chunk)
    return {"filename": file.filename, "bytes": size}
```

That loop is why a 500 MB recording doesn't need 500 MB of RAM — and it's exactly the shape of what "stream file → storage" will do in section 3: same loop, but each chunk goes to S3 instead of a counter.

One habit worth building immediately — reject garbage at the door:

```python
ALLOWED = {"audio/mpeg", "audio/wav", "audio/mp4", "audio/x-m4a", "audio/webm"}

if file.content_type not in ALLOWED:
    raise HTTPException(status_code=415, detail=f"Unsupported: {file.content_type}")
```

(`content_type` is client-supplied and spoofable — real validation means sniffing magic bytes — but it catches honest mistakes, which is most mistakes.)

### 2.7 Sharing expensive things: from global-variable sin to `lifespan` + `Depends`

Our endpoints need a Postgres pool, an S3 client, and an arq/Redis connection. Opening these per request would be slow and would exhaust the database. They must be created **once** and **shared**.

The naive approach is a module-level global:

```python
db = await asyncpg.create_pool(...)   # ❌ SyntaxError: can't await at module level
```

...which doesn't even parse, because connecting is itself async. FastAPI's answer is the **lifespan** hook — code that runs once at startup (before the first request) and once at shutdown:

```python
from contextlib import asynccontextmanager
import asyncpg
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = await asyncpg.create_pool(dsn=DATABASE_URL)   # startup
    yield                                                        # ← app serves requests
    await app.state.db.close()                                   # shutdown

app = FastAPI(lifespan=lifespan)
```

`app.state` is just an attribute bag. Endpoints *could* reach into it via `request.app.state.db`, but the idiomatic access path is **dependency injection**:

```python
from fastapi import Depends, Request

def get_db(request: Request):
    return request.app.state.db

@app.get("/jobs/{job_id}")
async def get_job(job_id: uuid.UUID, db = Depends(get_db)):
    row = await db.fetchrow("SELECT status FROM jobs WHERE id=$1", job_id)
    ...
```

Why bother with `Depends` instead of touching `app.state` directly? Two practical reasons: it makes dependencies **visible in the signature** (you can see what an endpoint needs at a glance), and it makes them **swappable in tests** (`app.dependency_overrides[get_db] = fake_db` — no monkeypatching). This pattern — resources created in lifespan, delivered by `Depends` — is the skeleton of essentially every production FastAPI app, and you'll see its mirror image in the arq worker's `ctx` later.

### 2.8 Now assemble the upload endpoint — in five versions

We build it the way you would in real life, one concern at a time.

**v1 — accept and acknowledge.** Prove the plumbing works.

```python
@app.post("/uploads", status_code=202)
async def create_upload(file: UploadFile):
    job_id = str(uuid.uuid4())
    return {"job_id": job_id, "status": "pending"}
```

Why `202 Accepted` and not `200`? HTTP codes are a vocabulary: `200` means "done", `201` means "created, here it is", **`202` means "received; processing will happen later."** It's the precise word for async jobs, and clients/tooling understand it.

**v2 — validate.** Add the content-type check from 2.6. Boring, essential.

**v3 — persist the bytes.** Stream to object storage (the `storage` object is built in section 3):

```python
    object_key = f"audio/{job_id}/{file.filename}"
    await storage.upload_stream(object_key, file)
```

Note the key embeds the `job_id` — every job's audio is addressable if you know the job.

**v4 — record that the job exists.** Postgres, the source of truth:

```python
    await db.execute(
        "INSERT INTO jobs (id, object_key, status) VALUES ($1, $2, 'pending')",
        job_id, object_key,
    )
```

**v5 — enqueue the ticket** (the `arq_pool` comes from section 5):

```python
    await arq_pool.enqueue_job("process_audio", job_id)
```

Full assembly:

```python
@app.post("/uploads", status_code=202)
async def create_upload(
    file: UploadFile,
    db = Depends(get_db),
    storage = Depends(get_storage),
    arq_pool = Depends(get_arq),
):
    if file.content_type not in ALLOWED:
        raise HTTPException(415, f"Unsupported: {file.content_type}")

    job_id = str(uuid.uuid4())
    object_key = f"audio/{job_id}/{file.filename}"

    await storage.upload_stream(object_key, file)          # 1. bytes are safe
    await db.execute(                                       # 2. job exists
        "INSERT INTO jobs (id, object_key, status) VALUES ($1,$2,'pending')",
        job_id, object_key,
    )
    await arq_pool.enqueue_job("process_audio", job_id)     # 3. ticket in queue

    return {"job_id": job_id, "status": "pending"}
```

**The order of 1→2→3 is not cosmetic.** Work through the failure at each gap:

- Crash after 1: an orphaned blob in storage. Wasteful, harmless. Clean up with a periodic sweep if you care.
- Crash after 2: a `pending` row that no worker will ever pick up. Recoverable — a small periodic task ("reaper") can re-enqueue any `pending` row older than N minutes. This is why the row goes in *before* the ticket.
- Now imagine the reversed order, ticket before row: a fast worker can pull the job and query Postgres **before the INSERT lands** — job not found, spurious failure. A real race condition, and it will happen precisely under load, when it's hardest to debug.

General principle, portable to any system you build: **create the durable record before announcing its existence.**

### 2.9 The polling endpoint

```python
@app.get("/jobs/{job_id}")
async def get_job(job_id: uuid.UUID, db = Depends(get_db)):
    row = await db.fetchrow(
        "SELECT status, transcript, summary, tags, mood, error FROM jobs WHERE id=$1",
        job_id,
    )
    if row is None:
        raise HTTPException(404, "Job not found")

    body = {"job_id": str(job_id), "status": row["status"]}
    if row["status"] == "done":
        body |= {"transcript": row["transcript"], "summary": row["summary"],
                 "tags": row["tags"], "mood": row["mood"]}
    elif row["status"] == "failed":
        body["error"] = row["error"]
    return body
```

Notice what it does *not* touch: Redis. Status lives in Postgres (rule 2 from section 1), so polling is a cheap indexed read that works even if Redis is down or has forgotten everything. The client polls with backoff:

```javascript
let delay = 1000;
while (true) {
  const job = await (await fetch(`/api/jobs/${jobId}`)).json();
  if (job.status === "done" || job.status === "failed") return job;
  await new Promise(r => setTimeout(r, delay));
  delay = Math.min(delay * 1.5, 10000);   // 1s, 1.5s, 2.25s ... cap 10s
}
```

Exponential backoff is a small courtesy with big aggregate effects: fast feedback when jobs are quick, gentle load when they're slow.

---

## 3. Object storage — S3, R2, and your local MinIO

### 3.1 The mental model: a dictionary, not a filesystem

Strip away the marketing and S3-style storage is this:

```python
storage = {}                          # bucket
storage["audio/3f2a/rec.m4a"] = b"..."   # put_object(key, bytes)
data = storage["audio/3f2a/rec.m4a"]     # get_object(key)
```

A **bucket** is a namespace; a **key** is a string; a **value** is an immutable blob. That's it. Three non-obvious consequences fall out of this model:

- **There are no folders.** `audio/3f2a/rec.m4a` is one flat string that happens to contain slashes. "Listing a folder" is really "list keys with this prefix." Consoles *render* prefixes as folders, which is a useful lie.
- **There are no edits.** You can't append to or modify byte 5000 of an object. You overwrite the whole key or you don't. (Perfect for audio files, which are write-once anyway.)
- **Access is HTTP.** Every operation is a signed HTTP request. This means *anything* that can make an HTTP request can read a blob — which becomes the crucial trick in 3.5.

Why three names for one thing: **S3** is AWS's service, whose API became the de facto protocol. **R2** is Cloudflare's implementation of that protocol (main pitch: zero egress fees — relevant to us, since Deepgram downloads every file *out* of storage). **MinIO** is an open-source implementation you self-host — you already run it in your homelab stack, so this section should feel familiar quickly. One protocol, three vendors, **identical client code**. Only two config values ever change: the endpoint URL and the credentials.

### 3.2 First contact, by hand

Start MinIO and talk to it with plain `boto3` (sync — simplest for learning; async comes in 3.4):

```bash
docker run -p 9000:9000 -p 9001:9001 \
  -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \
  minio/minio server /data --console-address ":9001"
```

```python
import boto3

s3 = boto3.client(
    "s3",
    endpoint_url="http://localhost:9000",     # ← the ONLY MinIO-specific line
    aws_access_key_id="minioadmin",
    aws_secret_access_key="minioadmin",
)

s3.create_bucket(Bucket="audio")
s3.put_object(Bucket="audio", Key="hello/test.txt", Body=b"hola mundo")

obj = s3.get_object(Bucket="audio", Key="hello/test.txt")
print(obj["Body"].read())                      # b'hola mundo'

for item in s3.list_objects_v2(Bucket="audio", Prefix="hello/")["Contents"]:
    print(item["Key"], item["Size"])
```

Open http://localhost:9001 (minioadmin/minioadmin) and see your object in the console. Point the same script at R2 by changing `endpoint_url` and the keys — nothing else. *That* is the portability claim, verified.

### 3.3 Streaming instead of buffering

`put_object(Body=b"...")` requires the whole payload in memory. For big files, use `upload_fileobj`, which reads from any file-like object in chunks and automatically switches to **multipart upload** (the file is split into parts, uploaded in parallel, and stitched server-side — also giving you resume-on-retry per part):

```python
with open("recording.m4a", "rb") as f:
    s3.upload_fileobj(f, "audio", "audio/3f2a/recording.m4a")
```

Connect this to section 2.6: FastAPI's `UploadFile` wraps a file-like object at `file.file`. So "stream the upload to storage" is literally:

```python
s3.upload_fileobj(file.file, bucket, key)
```

The chunk-reading loop you wrote earlier is what `upload_fileobj` does internally. Client → FastAPI → MinIO, constant memory, any file size.

### 3.4 Going async

Sync `boto3` inside `async def` would block the loop (section 2.2's sin). Two clean options:

```python
# Option A: wrap sync boto3 in a thread — fine, pragmatic
await asyncio.to_thread(s3.upload_fileobj, file.file, bucket, key)

# Option B: aioboto3 — native async, same API surface behind a context manager
import aioboto3
session = aioboto3.Session()
async with session.client("s3", endpoint_url=..., aws_access_key_id=..., aws_secret_access_key=...) as s3:
    await s3.upload_fileobj(file.file, bucket, key)
```

We'll use B. The `async with` is because aioboto3 clients hold aiohttp connections that must be released.

### 3.5 Presigned URLs — the load-bearing trick

Here's the problem the diagram's "presigned URL" arrow solves. Your bucket is private (it must be — it holds users' voice recordings). But Deepgram, an outside company with none of your credentials, needs to read one specific file. Your options:

1. Make the bucket public. Absolutely not.
2. Download the file in your worker and upload it to Deepgram. Works, but now every byte flows through your worker — memory, bandwidth, time, twice.
3. **Mint a presigned URL:** a URL carrying a signature that grants exactly one operation on exactly one object for a limited time.

```python
url = s3.generate_presigned_url(
    "get_object",
    Params={"Bucket": "audio", "Key": "audio/3f2a/recording.m4a"},
    ExpiresIn=3600,
)
print(url)
```

Look at what comes out:

```
http://localhost:9000/audio/audio/3f2a/recording.m4a
   ?X-Amz-Algorithm=AWS4-HMAC-SHA256
   &X-Amz-Credential=minioadmin%2F20260724%2F...
   &X-Amz-Date=20260724T120000Z
   &X-Amz-Expires=3600
   &X-Amz-Signature=9c1d4e8a...
```

What actually happened: your client computed an **HMAC signature** over (operation + bucket + key + expiry + timestamp) using your *secret* key, and put the signature — not the secret — in the query string. When anyone GETs this URL, the server recomputes the same HMAC with its copy of the secret and compares. Match + not expired → serve the object. No accounts, no tokens, no server-side state; the URL *is* the credential. Change one character of the key or the expiry and the signature no longer matches.

Two properties worth noticing because you'll reuse this pattern:

- **Generating a presigned URL involves no network call.** It's pure local crypto. Mint thousands, it's free.
- **It works in both directions.** `"put_object"` presigns an *upload*. That's the standard evolution of this architecture: the React client asks FastAPI for a presigned PUT, then uploads the audio **directly to storage**, and your API never touches the heavy bytes at all. File in your mental drawer for later.

### 3.6 The MinIO/localhost gotcha (this one will bite you, so pre-bite it)

A presigned URL embeds the endpoint hostname it was generated with. In docker-compose, your API reaches MinIO at `http://minio:9000` — a hostname that only resolves *inside* the compose network. So:

- Worker (inside compose) presigns → URL says `http://minio:9000/...` → hand it to a *local* consumer inside compose: works.
- Hand that same URL to **the real Deepgram cloud**: their servers try to resolve `minio` and obviously can't. Dead on arrival.

Local-dev escape hatches, in order of effort: (a) in dev, skip the URL flow — download the file in the worker and use Deepgram's direct-upload mode (`Content-Type: audio/*`, raw bytes in the body; their API supports both); (b) expose your MinIO through a tunnel (you already run Tailscale — `tailscale funnel` does exactly this); (c) just point local dev at a real R2 bucket with test credentials. In production with R2 the problem evaporates: the endpoint is a public hostname.

### 3.7 Assemble the `Storage` class

Everything above, folded into the object the endpoint and worker both use:

```python
# app/storage.py
import aioboto3

class Storage:
    def __init__(self, endpoint: str, key: str, secret: str, bucket: str):
        self._session = aioboto3.Session()
        self._cfg = dict(endpoint_url=endpoint,
                         aws_access_key_id=key,
                         aws_secret_access_key=secret)
        self.bucket = bucket

    async def upload_stream(self, key: str, upload_file) -> None:
        async with self._session.client("s3", **self._cfg) as s3:
            await s3.upload_fileobj(upload_file.file, self.bucket, key)   # 3.3

    async def presigned_get(self, key: str, expires: int = 3600) -> str:
        async with self._session.client("s3", **self._cfg) as s3:
            return await s3.generate_presigned_url(                       # 3.5
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=expires,
            )
```

Local `.env` vs production `.env` differ in three lines (`S3_ENDPOINT`, keys); the class never changes. This config-not-code separation is the entire "MinIO locally, R2 in prod" story.

---

## 4. Redis — and building a job queue by hand before using one

The fastest way to understand what arq does for you is to build a queue *without* it, watch it fail, and let each failure motivate a feature.

### 4.1 What Redis is

An in-memory data-structure server. Not "a cache" — a server that holds live data structures (strings, lists, hashes, sets, sorted sets) that many processes can manipulate over TCP with sub-millisecond latency and **atomic operations**. Persistence to disk exists but is best-effort; the honest posture is to treat Redis contents as losable. (Hence rule 2: tickets in Redis, truth in Postgres.)

Start one and poke it:

```bash
docker run -p 6379:6379 redis:7-alpine
docker exec -it <container> redis-cli
```

```
> SET greeting "hola"
> GET greeting            → "hola"
> LPUSH myqueue "job-1"   → push onto the left of a list
> LPUSH myqueue "job-2"
> RPOP myqueue            → "job-1"   (pop from the right: FIFO)
```

`LPUSH` + `RPOP` on a list — that's a queue, already. One more command completes the picture:

```
> BRPOP myqueue 0
```

**B**locking RPOP: if the list is empty, the connection *waits* (timeout 0 = forever) until something arrives, then pops it. No polling loop, no sleep — the consumer is woken the instant work exists.

### 4.2 A working queue in 20 lines

Producer:

```python
# producer.py
import json, uuid, redis
r = redis.Redis()

for i in range(5):
    job = {"id": str(uuid.uuid4()), "task": f"transcribe file {i}"}
    r.lpush("jobs", json.dumps(job))
    print("enqueued", job["id"])
```

Consumer — run **two or three copies in separate terminals**:

```python
# consumer.py
import json, time, redis
r = redis.Redis()

while True:
    _, raw = r.brpop("jobs")          # blocks until a job exists
    job = json.loads(raw)
    print("working on", job["id"])
    time.sleep(2)                     # pretend to transcribe
    print("done      ", job["id"])
```

Run the producer and watch the consumers split the work. You have just built distributed work distribution: because `BRPOP` is **atomic**, Redis guarantees each job is handed to exactly one consumer, no locks, no coordination code. This 20-line pair *is* the essence of Celery, arq, RQ, Sidekiq, and every queue system you'll ever meet.

### 4.3 Now break it — four failures, four features

**Failure 1: kill a consumer mid-job** (Ctrl-C during the `sleep`). The job was already popped from Redis, the consumer died before finishing → **the job is gone forever**, and nothing anywhere records that it was lost. Real queues solve this with an in-progress ledger (pop the job *and* record who took it and when, so a reaper can reclaim jobs from dead workers).

**Failure 2: make the job raise an exception.** Our consumer crashes; even if we `try/except`, the job just... evaporates. There's no retry. Real queues re-enqueue failed jobs with a delay and a max-attempts cap.

**Failure 3: try to schedule** "run this in 5 minutes." A list can't express that; you'd have to sleep in the consumer (blocking a worker slot for 5 idle minutes). Real queues use a **sorted set** scored by timestamp: `ZADD queue <run_at_unix_time> <job_id>`, and workers only pop entries whose score ≤ now. Delayed jobs are just entries with future scores — elegant.

**Failure 4: ask "what happened to job X?"** Nothing stores results or status. Real queues keep a small result record per job (and in *our* architecture, Postgres does this job anyway).

These four gaps — acknowledgment/reclaim, retries, scheduling, results — are precisely the feature list of arq. You now know not just *what* arq does but *why each feature exists*, which means when something misbehaves you'll know where to look.

### 4.4 Why not FastAPI's `BackgroundTasks`?

FastAPI has a built-in "run this after the response" mechanism, and it's the first thing everyone reaches for:

```python
from fastapi import BackgroundTasks

@app.post("/uploads")
async def create_upload(file: UploadFile, bg: BackgroundTasks):
    bg.add_task(process_audio, job_id)      # tempting…
    return {"job_id": job_id}
```

Measure it against the failures above: the task lives **inside the web process's memory**. Deploy, crash, or scale-down → task vanishes (failure 1, unfixable). No retries (2), no scheduling (3), no status (4). Plus the work now competes with request handling for the same event loop and CPU, and you can't scale workers independently of API replicas.

`BackgroundTasks` is right for fire-and-forget trivia: send a log line, invalidate a cache. The moment a task is *valuable* — a user is waiting for this transcription — it needs to survive the process, and that means a queue.

---

## 5. arq — the worker pool

### 5.1 What it is and why this one

[arq](https://arq-docs.helpmanual.io/) is a minimal async job queue over Redis, by Samuel Colvin (Pydantic's author). It's the four fixes from 4.3 wrapped around the BRPOP pattern from 4.2, for `async def` jobs.

Why arq over Celery here: our jobs are ~95% *waiting* — on Deepgram, on the LLM, on Postgres. arq runs jobs as **coroutines on one event loop**, so a single worker process interleaves 10+ jobs (section 2.1's trick, applied server-side). Celery's default prefork model would burn one OS process per concurrent job. For I/O-bound pipelines, async workers are simply the right shape. (If your jobs were CPU-bound — video encoding, model inference — the calculus flips and you'd want processes. Match the concurrency model to where time is spent; this heuristic transfers everywhere.)

### 5.2 The smallest possible arq system

Three tiny files. First, a job and the worker's configuration:

```python
# worker.py
async def say_hello(ctx, name: str):
    print(f"hello {name}")
    return f"greeted {name}"

class WorkerSettings:
    functions = [say_hello]
```

That's a complete worker. `WorkerSettings` is just a class arq inspects for configuration (Redis defaults to localhost). Run it:

```bash
arq worker.WorkerSettings
```

Second, enqueue from anywhere:

```python
# enqueue.py
import asyncio
from arq import create_pool

async def main():
    pool = await create_pool()                       # connects to Redis
    job = await pool.enqueue_job("say_hello", "David")
    print("enqueued:", job.job_id)

asyncio.run(main())
```

Run it; watch the worker terminal print `hello David`. Note the function is referenced **by string name** — enqueuer and worker share only Redis and a naming convention, not code imports. That's what lets them be separate processes, separate containers, even separate codebases.

Two mechanical details that answer questions you'd hit later:

- **Arguments travel through Redis**, serialized (pickle by default). So pass small, plain values — a `job_id` string, not a 50 MB audio blob or a DB connection. The worker re-fetches heavy things itself. (Under the hood it's what section 4.3 predicted: a Redis hash holding the serialized call + a sorted set as the queue.)
- `enqueue_job` returns immediately with a `Job` handle. You *can* `await job.result()` — but we never will, because our results go to Postgres.

### 5.3 `ctx` — the worker's version of lifespan

Every arq job's first parameter is `ctx`, a plain dict shared by all jobs in a worker process. Combined with startup/shutdown hooks, it plays exactly the role `lifespan` + `app.state` played in FastAPI (section 2.7) — create expensive clients once, share them:

```python
# worker.py
import asyncpg, httpx

async def startup(ctx):
    ctx["db"] = await asyncpg.create_pool(dsn=DATABASE_URL)
    ctx["http"] = httpx.AsyncClient(timeout=300)

async def shutdown(ctx):
    await ctx["db"].close()
    await ctx["http"].aclose()

async def say_hello(ctx, name: str):
    version = await ctx["db"].fetchval("SELECT version()")
    print(f"hello {name}, pg says {version[:15]}")

class WorkerSettings:
    functions = [say_hello]
    on_startup = startup
    on_shutdown = shutdown
```

Same pattern, both halves of the system: **resources are process-scoped, jobs/requests borrow them.** Once you see it twice you'll reach for it in every long-running Python service.

### 5.4 The knobs that matter

```python
class WorkerSettings:
    functions = [process_audio]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn("redis://redis:6379")
    max_jobs = 10          # coroutines running concurrently in THIS process
    job_timeout = 900      # seconds before a job is cancelled
    max_tries = 3          # total attempts per job
    keep_result = 3600     # how long arq's own result record lives in Redis
```

- `max_jobs` is per-process concurrency. Total system concurrency = `max_jobs × number of worker containers`. "Scaling the pool" is just running the same command more times — every worker BRPOPs the same Redis, and atomicity (4.2) guarantees no job runs twice concurrently.
- `job_timeout` is your defense against a hung HTTP call to Deepgram eating a worker slot forever.
- Enqueue-side knobs worth knowing:

```python
await pool.enqueue_job("process_audio", job_id,
                       _defer_by=timedelta(minutes=5))   # scheduling (failure 3, solved)

await pool.enqueue_job("process_audio", job_id,
                       _job_id=f"process:{job_id}")      # dedup: same _job_id while one
                                                          # is queued/running → no-op
```

That `_job_id` trick is a quietly great tool: make the arq job id deterministic from your domain id, and double-submissions (user double-click, reaper re-enqueue racing a live worker) collapse into one execution.

### 5.5 Retries — and the discipline they force on you

Deepgram will sometimes 500. Networks flap. arq's contract: **if a job raises, and attempts < `max_tries`, it goes back in the queue.** You can also request a delayed retry explicitly:

```python
from arq import Retry

async def process_audio(ctx, job_id):
    try:
        ...call deepgram...
    except httpx.HTTPStatusError as e:
        if e.response.status_code >= 500:          # transient → retry with backoff
            raise Retry(defer=ctx["job_try"] * 30)  # 30s, 60s, 90s…
        raise                                       # 4xx = our bug → fail for real
```

(`ctx["job_try"]` is the current attempt number — arq puts bookkeeping like this in `ctx` alongside your own entries.)

Retries buy resilience and charge for it in a specific currency: **your job may run more than once**, possibly even overlapping in pathological cases. So jobs must be **idempotent** — re-running with the same input converges to the same state, without duplicated side effects. Audit our job through that lens:

- `UPDATE jobs SET status='running'…` — overwrite, same result twice. ✓
- Deepgram + LLM calls — stateless reads (they cost money twice, but corrupt nothing). ✓
- Final `UPDATE … SET status='done', transcript=…` — overwrite. ✓

Now the counterexample that makes the lesson stick: suppose the job ended by emailing the user "your transcript is ready." Retry after a crash-between-email-and-DB-write → **two emails**. The standard fix is to make the side effect conditional on a state check (`UPDATE jobs SET email_sent=true WHERE id=$1 AND email_sent=false` — and only send if that touched a row). Designing side effects to be repeat-safe is arguably *the* core skill of distributed background processing; it will follow you to every queue system, and to stream processing, and to payment systems.

### 5.6 Building `process_audio`, step by step

Now the real job, assembled the way we built the upload endpoint. Each step is small; the composition is the system.

**Step 1 — claim the job and load its inputs.**

```python
async def process_audio(ctx, job_id: str):
    db = ctx["db"]
    row = await db.fetchrow("SELECT object_key FROM jobs WHERE id=$1", job_id)
    await db.execute(
        "UPDATE jobs SET status='running', started_at=now() WHERE id=$1", job_id
    )
```

The worker receives only `job_id` and looks everything else up — keeping queue payloads minimal (5.2) and Postgres authoritative.

**Step 2 — mint the presigned URL** (section 3.5 pays off):

```python
    audio_url = await ctx["storage"].presigned_get(row["object_key"])
```

**Step 3 — Deepgram: speech-to-text + diarization.** Deepgram's hosted-file mode takes a URL and fetches the audio itself — our bytes never route through the worker:

```python
    resp = await ctx["http"].post(
        "https://api.deepgram.com/v1/listen",
        params={"model": "nova-3", "diarize": "true", "smart_format": "true"},
        headers={"Authorization": f"Token {DEEPGRAM_KEY}"},
        json={"url": audio_url},
    )
    resp.raise_for_status()
    dg = resp.json()
    transcript = dg["results"]["channels"][0]["alternatives"][0]["transcript"]
```

(`diarize=true` labels speakers; the word-level speaker tags live deeper in the response under `words[*].speaker` — for a "Speaker 0: … / Speaker 1: …" transcript you'd fold those, a pure-Python exercise.)

**Step 4 — LLM enrichment.** Transcript in, structured JSON out. The two practical points: instruct *JSON only*, and cap the input size:

```python
    resp = await ctx["http"].post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"},
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content":
                "Return ONLY a JSON object: 'summary' (2-3 sentences), "
                "'tags' (3-6 strings), 'mood' (one word). Transcript:\n\n"
                + transcript[:50_000]}],
        },
    )
    resp.raise_for_status()
    enriched = json.loads(resp.json()["content"][0]["text"])
```

**Step 5 — write the truth down.**

```python
    await db.execute(
        """UPDATE jobs SET status='done', transcript=$2, summary=$3,
                          tags=$4, mood=$5, finished_at=now() WHERE id=$1""",
        job_id, transcript, enriched["summary"], enriched["tags"], enriched["mood"],
    )
```

**Step 6 — wrap it in failure handling.** Transient errors retry (5.5); permanent ones must land somewhere the *user* can see — the Postgres row:

```python
    except Retry:
        raise                              # let arq's scheduler have it back
    except Exception as e:
        await db.execute(
            "UPDATE jobs SET status='failed', error=$2 WHERE id=$1",
            job_id, str(e)[:2000],
        )
        raise                              # arq still counts/records the failure
```

The full worker file is these six steps inside one `try`, plus the `WorkerSettings` from 5.4 — roughly 80 lines total. You've now read every line of it with its reason attached, which is the difference between copying an architecture and owning one.

### 5.7 Running and scaling

```bash
arq worker.WorkerSettings            # one worker, max_jobs coroutines
```

Scale = run it again (another terminal, another container, another machine — anything that reaches the same Redis). The diagram's "Worker pool" is not a special component; it's *this command, N times*.

---

## 6. Assembly: the whole system on your machine

### 6.1 One repo, two entrypoints

The API and the worker share a codebase (models, `Storage`, config) and differ only in what runs. The compose file makes this literal — same `build`, different `command`:

```yaml
# docker-compose.yml
services:
  api:
    build: .
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000
    ports: ["8000:8000"]
    env_file: .env
    depends_on: [postgres, redis, minio]

  worker:
    build: .                              # ← same image as api
    command: arq worker.WorkerSettings
    env_file: .env
    depends_on: [postgres, redis, minio]
    deploy: {replicas: 2}                 # ← the "pool"

  postgres:
    image: postgres:16-alpine
    environment: {POSTGRES_PASSWORD: dev, POSTGRES_DB: audio}
    ports: ["5432:5432"]
    volumes: [pg-data:/var/lib/postgresql/data]

  redis:
    image: redis:7-alpine

  minio:
    image: minio/minio
    command: server /data --console-address ":9001"
    ports: ["9000:9000", "9001:9001"]
    environment: {MINIO_ROOT_USER: minioadmin, MINIO_ROOT_PASSWORD: minioadmin}
    volumes: [minio-data:/data]

volumes: {pg-data: {}, minio-data: {}}
```

```
# .env (local)
DATABASE_URL=postgresql://postgres:dev@postgres:5432/audio
REDIS_URL=redis://redis:6379
S3_ENDPOINT=http://minio:9000
S3_ACCESS_KEY=minioadmin
S3_SECRET_KEY=minioadmin
S3_BUCKET=audio
DEEPGRAM_KEY=...
ANTHROPIC_KEY=...
```

Production `.env` swaps `S3_ENDPOINT` for the R2 URL, the Redis/Postgres URLs for managed instances, and nothing else. Environment is the seam; code is invariant.

### 6.2 The jobs table

```sql
CREATE TABLE jobs (
    id           UUID PRIMARY KEY,
    object_key   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','running','done','failed')),
    transcript   TEXT,
    summary      TEXT,
    tags         TEXT[],
    mood         TEXT,
    error        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ
);
CREATE INDEX ON jobs (status, created_at);
```

The `CHECK` constraint encodes the state machine (`pending → running → done|failed`) at the database level, and the index serves both the reaper ("old pending rows") and any dashboard ("jobs by status over time"). The timestamps cost nothing and give you latency metrics for free — `finished_at - started_at` is your processing-time distribution, `started_at - created_at` is your queue-wait distribution. When (not if) someone asks "why are jobs slow today," those two columns are the answer's raw material.

### 6.3 Trace one request through everything you've built

1. React POSTs the file → **2.6** receives it as a spooled `UploadFile`.
2. **2.8 v5** runs: stream to MinIO (**3.3**), INSERT `pending` (**6.2**), `enqueue_job` (**5.2**) — in that order, for the reasons in **2.8**. Returns `202 + job_id` in milliseconds.
3. React polls with backoff (**2.9**).
4. A worker's BRPOP-equivalent (**4.2**, wrapped by arq) atomically claims the ticket; `process_audio` flips the row to `running` (**5.6 step 1**).
5. Presigned URL minted locally, no network (**3.5**); Deepgram pulls the audio straight from storage (**5.6 step 3**).
6. Transcript → LLM → structured JSON (**5.6 step 4**).
7. Results overwrite the row, status `done` (**5.6 step 5**) — idempotently (**5.5**).
8. Next poll returns everything; React renders.

If any step now feels fuzzy, the section number tells you exactly where to reread.

### 6.4 Hardening ideas, in the order you'd actually need them

**A reaper for stuck jobs** — the missing piece acknowledged in 2.8 and 4.3-failure-1. arq has cron jobs built in, so the reaper is just another function in the same worker:

```python
from arq import cron

async def reap_stuck_jobs(ctx):
    rows = await ctx["db"].fetch(
        """SELECT id FROM jobs
           WHERE (status='pending' AND created_at < now() - interval '10 minutes')
              OR (status='running' AND started_at < now() - interval '30 minutes')"""
    )
    for r in rows:
        await ctx["arq"].enqueue_job("process_audio", str(r["id"]),
                                     _job_id=f"process:{r['id']}")   # dedup (5.4)

class WorkerSettings:
    functions = [process_audio]
    cron_jobs = [cron(reap_stuck_jobs, minute=set(range(0, 60, 5)))]  # every 5 min
    ...
```

Note how the `_job_id` dedup from 5.4 makes the reaper safe even if it races a slow-but-alive worker.

**Push instead of poll** — Server-Sent Events are the 90%-of-the-benefit option (one-way server→client updates, plain HTTP, ~20 lines in FastAPI with `StreamingResponse`); WebSockets only if you later need bidirectional.

**Direct-to-storage uploads** — the presigned **PUT** from 3.5: FastAPI's upload endpoint shrinks to "validate, presign, INSERT, enqueue-on-confirm," and gigabyte files never touch your API's bandwidth.

**Split the pipeline** — one job per stage (`transcribe` → enqueues `enrich`), each writing its output to the row. Then an LLM outage doesn't force re-paying for transcription on retry, and each stage gets its own timeout/retry policy. This is the first step on the road that ends at DAG orchestrators (Airflow/Prefect); most systems should stop well before the end of that road.

---

## 7. What you can now take elsewhere

Worth naming explicitly, because these transfer far beyond this project:

1. **The event loop model** (2.1–2.2): I/O concurrency on one thread, and why one blocking call freezes everything. This is Node, this is asyncio, this is every async runtime.
2. **Lifespan-scoped resources + injection** (2.7, 5.3): create once, share safely, swap in tests. Every long-running service, any language.
3. **202 + durable record + ticket, in that order** (2.8): the async job pattern and the race you avoid by creating truth before announcing it.
4. **Object storage as a signed-HTTP dictionary** (3.1, 3.5): and presigned URLs as capability tokens — letting third parties touch exactly one object without holding your credentials.
5. **Queues from first principles** (4.2–4.3): LPUSH/BRPOP, and the four failures (loss, no-retry, no-scheduling, no-status) that every mature queue system exists to fix.
6. **Idempotency** (5.5): retries mean at-least-once execution; design side effects to be repeat-safe. The single most valuable habit in distributed systems.
7. **Config-not-code portability** (3.2, 6.1): MinIO→R2, laptop→prod, by changing environment variables only.

The diagram you started with is a specific instance of a very general machine. Swap Deepgram for a PDF parser and the LLM for your BERTopic pipeline, and you'd have an async document-intelligence service with the same skeleton — which is exactly the point of learning it this way.
