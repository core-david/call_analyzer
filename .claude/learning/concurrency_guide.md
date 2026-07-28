# Concurrency, Parallelism, Async, Threading, Multiprocessing
### A mental model for Python (and beyond)

> **How to read this guide.** Part 0 gives you the map — the conceptual hierarchy that everything else hangs on. Parts 1–5 descend into each mechanism with increasing depth (OS foundations → threads → processes → asyncio → free-threaded CPython). Part 6 is the decision framework. Part 7 is a compendium of pitfalls and FAQs. Formal definitions appear in boxes marked **⊢ Definition**; you can skim them on a first pass and return later, but they're what makes the vocabulary precise.

---

## Part 0 — The Map: Getting the Hierarchy Right

The single most common source of confusion is treating these five words as if they lived at the same level of abstraction. They don't. Here is the correct stratification:

```
┌─────────────────────────────────────────────────────────────────┐
│  PROPERTIES of a program's execution        (what is happening) │
│                                                                 │
│      CONCURRENCY          PARALLELISM                           │
│      (structure)          (execution)                           │
├─────────────────────────────────────────────────────────────────┤
│  MECHANISMS you use to achieve them         (how you build it)  │
│                                                                 │
│      threading  ·  multiprocessing  ·  async/coroutines         │
├─────────────────────────────────────────────────────────────────┤
│  SUBSTRATE the mechanisms run on            (what the OS/HW do) │
│                                                                 │
│      OS processes · OS threads · CPU cores · syscalls · epoll   │
└─────────────────────────────────────────────────────────────────┘
```

**Concurrency and parallelism are properties. Threading, multiprocessing, and async are mechanisms.** Asking "should I use concurrency or threading?" is a category error, like asking "should I travel fast or by car?"

### 0.1 Concurrency vs. parallelism, precisely

> **⊢ Definition (Concurrency).** A program is *concurrent* if it is structured as multiple logical tasks whose executions may overlap in time — i.e., the lifetimes of two or more tasks intersect, and the program's correctness does not depend on a single fixed interleaving of their steps. Concurrency is a property of the program's *structure* and its *nondeterministic composition*.

> **⊢ Definition (Parallelism).** A program executes *in parallel* if at some instant *t*, two or more of its computations are literally executing simultaneously on distinct hardware execution units (cores, SMT threads, GPUs). Parallelism is a property of the *execution*, requiring hardware support.

The canonical formulation is Rob Pike's: concurrency is about *dealing with* many things at once (a way of structuring a program), parallelism is about *doing* many things at once (a way of executing it). The two are independent:

| | Not parallel | Parallel |
|---|---|---|
| **Not concurrent** | Ordinary sequential program | SIMD / vectorized code (one logical task, many lanes)* |
| **Concurrent** | asyncio on one core; threads on a single-core CPU; time-slicing | Multi-threaded code on multiple cores; multiprocessing |

\* Instruction-level and data-level parallelism (pipelining, SIMD, `numpy` vectorization) are parallelism *without* concurrency: one logical thread of control, many arithmetic units. This quadrant is why the two concepts must not be conflated.

A useful mental image — one core interleaving two tasks (concurrent, not parallel) versus two cores (concurrent *and* parallel):

```
Concurrency without parallelism (1 core, interleaved):
Core 0:  |--A--|--B--|--A--|--B--|--A--|
                 time ──────────────►

Concurrency with parallelism (2 cores):
Core 0:  |------A------|----A----|
Core 1:  |------B------|----B----|
                 time ──────────────►
```

**Key consequence:** concurrency can improve *latency and responsiveness* even on one core (do useful work while waiting on I/O); parallelism improves *throughput of computation* and requires multiple cores. Which one you need is determined by *what your program is waiting on* — which brings us to the second axis.

### 0.2 The second axis: CPU-bound vs. I/O-bound

> **⊢ Definition (Bound classification).** Let a task's wall-clock time decompose as *T = T_cpu + T_wait*, where *T_cpu* is time spent executing instructions and *T_wait* is time blocked on external events (disk, network, timers, IPC). The task is **CPU-bound** if *T_cpu ≫ T_wait* and **I/O-bound** if *T_wait ≫ T_cpu*.

This classification, not personal preference, is what should drive mechanism choice:

- **I/O-bound** → you need *concurrency* (overlap the waits). Parallelism is nearly useless: 8 cores waiting on the same network are no faster than 1 core waiting. Mechanisms: `asyncio` or threads.
- **CPU-bound** → you need *parallelism* (more silicon per unit time). Concurrency without parallelism is useless: interleaving two computations on one core takes *longer* than running them back-to-back (context-switch overhead). Mechanisms: `multiprocessing`, free-threaded Python, C extensions releasing the GIL, or vectorization.

### 0.3 The third axis: who decides when to switch?

The three Python mechanisms differ fundamentally in *scheduling authority*:

> **⊢ Definition (Preemptive scheduling).** The scheduler (OS kernel) may suspend a running task at essentially any instruction boundary, without the task's cooperation, typically driven by a timer interrupt.

> **⊢ Definition (Cooperative scheduling).** A task runs until it *voluntarily* yields control (in Python: at an `await` expression). The scheduler cannot interrupt it.

|                   | Scheduling   | Memory space | Parallel in CPython (with GIL)? | Switch cost |
|-------------------|-------------|--------------|-------------------------------|-------------|
| `multiprocessing` | Preemptive (OS) | Separate per process | **Yes** | ~ms to create; µs to context-switch |
| `threading`       | Preemptive (OS) | Shared | **No** (for pure-Python bytecode) | ~µs context switch |
| `asyncio`         | Cooperative (event loop) | Shared (single thread) | No | ~100ns task switch |

This table *is* the mental model. Everything else in this guide is an elaboration of its rows: why the "No" appears in the threading row (the GIL, Part 2), why "Yes" costs so much (Part 3), how the event loop achieves cooperative multitasking (Part 4), and what changes in free-threaded CPython (Part 5).

### 0.4 The hierarchy, one more time, as a taxonomy

```
Goal: make progress on multiple things
│
├── The things mostly WAIT (I/O-bound) ──► need CONCURRENCY
│   │
│   ├── Very many tasks (10³–10⁶ sockets), you control the code
│   │       └─► asyncio (cooperative, cheapest task switch)
│   │
│   └── Few/moderate tasks, or blocking libraries you can't change
│           └─► threading (preemptive, blocking calls tolerated)
│
└── The things mostly COMPUTE (CPU-bound) ──► need PARALLELISM
    │
    ├── Work expressible as array math ─► numpy/BLAS (parallel C, GIL released)
    ├── Independent Python tasks ────────► multiprocessing / ProcessPoolExecutor
    └── Shared-memory Python parallelism ► free-threaded CPython 3.13+/3.14
```

Hold that skeleton in your head; we now build each bone.

---

## Part 1 — Foundations: What the OS Gives You

Python's mechanisms are thin wrappers over OS primitives. If the OS layer is fuzzy, everything above it stays fuzzy.

### 1.1 Processes and threads

> **⊢ Definition (Process).** An OS process is an instance of a running program: a private virtual address space, plus at least one thread of execution, plus kernel-managed resources (file descriptors, signal handlers, credentials). Isolation is the defining feature: process A cannot read process B's memory except through explicit IPC.

> **⊢ Definition (Thread).** A thread is a schedulable unit of execution *within* a process: its own program counter, register set, and stack — but *sharing* the process's address space (heap, globals, code) with its sibling threads.

```
        PROCESS A                      PROCESS B
┌──────────────────────────┐   ┌──────────────────────────┐
│  heap / globals / code   │   │  heap / globals / code   │
│  (shared by A's threads) │   │                          │
│ ┌───────┐   ┌───────┐    │   │ ┌───────┐                │
│ │Thread1│   │Thread2│    │   │ │Thread1│                │
│ │ stack │   │ stack │    │   │ │ stack │                │
│ │ regs  │   │ regs  │    │   │ │ regs  │                │
│ └───────┘   └───────┘    │   │ └───────┘                │
└──────────────────────────┘   └──────────────────────────┘
        ▲ shared memory: fast IPC,      ▲ isolated: safe,
          but data races possible         but IPC must copy/serialize
```

The trade is symmetric and fundamental — it survives all the way up to Python:

- **Threads**: cheap to create (~10–100 µs), cheap to communicate (write to shared memory), *dangerous* (data races, one thread's crash can corrupt all).
- **Processes**: expensive to create (~1–10 ms), expensive to communicate (serialize + copy through pipes/sockets/shared segments), *safe* (hardware-enforced isolation).

### 1.2 Context switches and why blocking wastes a core

The kernel scheduler multiplexes runnable threads onto cores. A **context switch** saves one thread's registers/PC/stack pointer and restores another's. Direct cost is ~1–5 µs, but the *indirect* cost dominates: cache lines, TLB entries, and branch-predictor state belonging to the old thread are evicted, so the new thread runs "cold" for a while.

When a thread issues a **blocking syscall** (e.g., `read()` on a socket with no data), the kernel marks it *not runnable* and schedules something else. This is the crucial fact behind all I/O concurrency:

**A blocked thread consumes zero CPU.** The inefficiency of the naive "one thread per connection" model is not CPU waste while blocked — it's (a) memory (each thread needs a stack, typically 1–8 MB of virtual address space) and (b) scheduler pressure and context-switch overhead when tens of thousands of threads wake frequently. This is the "C10K problem" that motivated event loops.

### 1.3 Readiness notification: the primitive under every event loop

The alternative to "one blocked thread per socket" is to ask the kernel: *"here are 10,000 file descriptors — wake me when any becomes readable/writable."* That's `select`/`poll` (portable, O(n) per call) and their scalable successors `epoll` (Linux), `kqueue` (BSD/macOS), IOCP (Windows) — O(ready) per call. One thread can then service thousands of connections by looping:

```
loop forever:
    events ← epoll_wait(...)          # block until ≥1 fd is ready
    for each ready fd:
        run the callback/coroutine waiting on that fd (must not block!)
```

Every async framework — asyncio, Node.js, nginx — is a disciplined way of writing programs against this loop. Part 4 shows exactly how asyncio wraps it.

### 1.4 Formal limits: Amdahl and Gustafson

Before spending effort on parallelism, know its ceiling.

> **⊢ Theorem (Amdahl's law).** If a fraction *p* ∈ [0,1] of a program's work is parallelizable and the rest is inherently serial, the speedup on *N* processors is
>
> S(N) = 1 / ((1 − p) + p/N),  with  lim_{N→∞} S(N) = 1/(1 − p).

If 90% of your runtime parallelizes (*p* = 0.9), infinite cores give you at most **10×**. The serial fraction — startup, I/O, coordination, the un-parallelized inner loop — is the asymptote. In Python, serialization/deserialization for `multiprocessing` IPC often *is* the serial fraction, which is why naive parallelization sometimes makes programs slower.

Gustafson's rejoinder: in practice we scale the *problem* with the machine (S(N) = N − (1−p)(N−1)), so parallelism pays off better for large workloads than Amdahl's fixed-size pessimism suggests. Both are true; they answer different questions ("speed up this task" vs. "solve bigger tasks in the same time").

### 1.5 Data races and the happens-before relation

The vocabulary you need to reason about shared-memory bugs:

> **⊢ Definition (Data race).** Two memory accesses form a *data race* if they (1) touch the same location, (2) at least one is a write, (3) they are performed by different threads, and (4) they are not ordered by any synchronization — formally, neither *happens-before* the other.

> **⊢ Definition (Happens-before).** The smallest partial order over a program's events such that (a) events within one thread are ordered by program order, and (b) a synchronization release (e.g., unlocking a mutex, putting on a queue) happens-before the corresponding acquire (locking that mutex, getting from that queue) in another thread.

A program whose executions contain no data races behaves as if its operations were interleaved in *some* sequential order (sequential consistency for data-race-free programs — the "DRF-SC" guarantee that C/C++/Java memory models are built around). A program *with* data races has, at the hardware/compiler level, essentially undefined behavior: reordered writes, torn reads, values out of thin air.

CPython with the GIL is unusually forgiving here — the GIL serializes bytecode execution, so you never see *torn* object state — but as Part 2 shows, it absolutely does **not** prevent logical races between bytecodes. And in free-threaded Python (Part 5) the classical discipline returns in full force. Learn the discipline now:

**Rule: every piece of mutable state shared between threads must be protected by a synchronization object (lock, queue, atomic), and every access must go through it.**

### 1.6 Two paradigms for communication

There are two grand traditions for structuring concurrent programs:

1. **Shared memory + locks** (Pthreads tradition): threads communicate by mutating common state, guarded by mutexes/condition variables. Maximum performance, maximum footguns.
2. **Message passing** (CSP — Hoare's *Communicating Sequential Processes*; the actor model): tasks own their state privately and communicate only by sending messages over channels/queues. Go's slogan — *"don't communicate by sharing memory; share memory by communicating"* — is CSP.

Python offers both: `threading.Lock` vs. `queue.Queue`; raw `multiprocessing.shared_memory` vs. `multiprocessing.Queue`; shared globals in asyncio vs. `asyncio.Queue`. **Prefer message passing by default.** A queue centralizes the synchronization into one battle-tested object; the happens-before edges come for free.

---
## Part 2 — Threading in Python: Preemptive Concurrency Under One Interpreter

### 2.1 The API in 60 seconds

`threading.Thread` wraps an OS thread (pthread on Unix, Win32 thread on Windows). These are *real* kernel threads — the kernel schedules them preemptively across cores. The limitation you've heard about (the GIL) lives above the OS, inside the interpreter.

```python
import threading, time, urllib.request

URLS = ["https://example.com"] * 8

def fetch(url: str) -> None:
    with urllib.request.urlopen(url, timeout=10) as r:
        r.read()

t0 = time.perf_counter()
threads = [threading.Thread(target=fetch, args=(u,)) for u in URLS]
for t in threads: t.start()
for t in threads: t.join()        # wait for completion
print(f"threaded:   {time.perf_counter() - t0:.2f}s")

t0 = time.perf_counter()
for u in URLS: fetch(u)
print(f"sequential: {time.perf_counter() - t0:.2f}s")
```

On a typical network, the threaded version is ~8× faster. Eight I/O waits overlap. This works *despite* the GIL — understanding why requires understanding what the GIL actually locks.

In modern code, prefer the pool abstraction over raw threads:

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

with ThreadPoolExecutor(max_workers=8) as pool:
    futures = {pool.submit(fetch, u): u for u in URLS}
    for fut in as_completed(futures):
        fut.result()   # re-raises any exception from the worker — always call this
```

`concurrent.futures` is the unified high-level API: swap `ThreadPoolExecutor` for `ProcessPoolExecutor` and the same code becomes multi-process. A `Future` is a handle to a result that doesn't exist yet — the same concept asyncio uses, which is why the two interoperate cleanly (§4.9).

### 2.2 The GIL: what it is, mechanically

> **⊢ Definition (GIL).** The Global Interpreter Lock is a process-wide mutex in CPython that a thread must hold to execute Python *bytecode* or touch any CPython C-API object state. At most one thread interprets bytecode at any instant, regardless of core count.

**Why it exists.** CPython's memory management is reference counting: every object holds an integer `ob_refcnt`, incremented/decremented constantly (`Py_INCREF`/`Py_DECREF`). Without a global lock, every refcount operation on every object would need to be atomic, and freeing an object whose count races to zero in two threads simultaneously would double-free. One big lock makes *all* interpreter internals — refcounts, the allocator (pymalloc), dict/list internals, the import system — trivially thread-safe, and makes single-threaded code fast (no per-object atomic traffic). The GIL is a *performance* choice for the single-threaded common case that sacrifices multi-core scaling of pure-Python code.

**How switching works (CPython 3.2+ "new GIL").** The GIL is not released "every N bytecodes" (that was Python 2). The modern mechanism:

1. A thread holding the GIL runs bytecode freely.
2. A waiting thread times out after the *switch interval* (default **5 ms**, tunable via `sys.setswitchinterval()`) and sets the interpreter's `gil_drop_request` flag.
3. The running thread checks pending flags at the **eval breaker** — a check embedded in the interpreter loop at safe points (notably backward jumps, i.e., loop edges, and calls). Seeing the flag, it releases the GIL, signals a condition variable, and waits for confirmation that another thread took it (this handoff protocol prevents the pathological case where the releasing thread instantly re-acquires).
4. The OS decides *which* waiting thread gets the mutex — CPython does no priority scheduling of its own.

Two subtle consequences:

- **A single bytecode instruction is never interrupted mid-flight by another Python thread.** Switches happen *between* bytecodes (at eval-breaker checks). This gives Python threads bytecode-granularity atomicity — the source of both real guarantees and dangerous folklore (§2.4).
- **The convoy effect.** An I/O-bound thread that releases the GIL around a syscall must, on return, *re-acquire* it — and if a CPU-bound thread is churning, the I/O thread waits up to a full switch interval each time. Mixing one CPU-hog thread with latency-sensitive I/O threads in the same process degrades I/O latency badly. (This asymmetry is a known weakness of the current GIL design.)

**When the GIL is released.** The GIL is dropped (allowing true parallelism) whenever a thread:
- performs a blocking syscall through the standard library (socket reads, file I/O, `time.sleep`, `subprocess` waits, lock acquisitions with timeouts) — the C implementation wraps the call in `Py_BEGIN_ALLOW_THREADS` / `Py_END_ALLOW_THREADS`;
- runs inside a C extension that explicitly releases it — `numpy` linear algebra, `hashlib`, `zlib`/`lzma` compression, `re` on large inputs (3.11+), database drivers, `Pillow` transforms, etc.

This yields the two honest use cases for threads in GIL-Python: **overlapping I/O waits**, and **parallel C-level compute**. The following demonstrates both faces:

```python
import threading, time

N = 20_000_000

def pure_python():            # holds the GIL while computing
    s = 0
    for i in range(N):
        s += i

def timed(fn, nthreads):
    ts = [threading.Thread(target=fn) for _ in range(nthreads)]
    t0 = time.perf_counter()
    for t in ts: t.start()
    for t in ts: t.join()
    return time.perf_counter() - t0

print(f"1 thread : {timed(pure_python, 1):.2f}s")
print(f"2 threads: {timed(pure_python, 2):.2f}s")   # ≈ 2× the 1-thread time!
```

Typical output: `1 thread: 0.62s`, `2 threads: 1.29s`. Two threads did *twice the work in twice the time* — zero speedup, plus switching overhead. Replace the body with `hashlib.pbkdf2_hmac` or a large `numpy` matmul (GIL released) and 2 threads approach 2× speedup.

### 2.3 Race conditions: the canonical bug

```python
import threading

counter = 0

def worker():
    global counter
    for _ in range(100_000):
        counter += 1        # NOT atomic!

threads = [threading.Thread(target=worker) for _ in range(8)]
for t in threads: t.start()
for t in threads: t.join()
print(counter)   # expected 800000; historically prints e.g. 522943
```

Why it's a race: `counter += 1` compiles to roughly *load* `counter` → *add 1* → *store* `counter` — three-plus bytecodes. If a thread switch lands between the load and the store, two threads read the same old value, both add 1, both store, and one increment is lost. **The GIL guarantees bytecode atomicity, not statement atomicity** — nothing in the language forbids that interleaving.

> **⚠ Honest reproducibility note.** If you run this on CPython ≥3.10, you will very likely get 800000 and conclude the race is a myth. It isn't. Since 3.10 the eval breaker fires essentially only at *backward jumps* (loop edges) and certain calls, so within one loop iteration the load-add-store triple is never split — the demo is "correct" **by implementation accident**, not by specification. The same code loses updates on Python ≤3.9, and races for real on free-threaded builds (§5.1) where switches aren't the only threat — genuine simultaneous execution is. The lesson generalizes: *"I ran it and it worked" is not evidence of thread-safety.* Races are existence claims about interleavings, and testing samples only a few. Reason from the memory model (§1.5), not from lucky runs.

The fix — establish happens-before edges with a lock:

```python
lock = threading.Lock()

def worker():
    global counter
    for _ in range(100_000):
        with lock:          # acquire … release
            counter += 1
```

A demonstration-friendly alternative: an easy way to make the race *observable* on any GIL build is to widen the window between read and write — `v = counter; time.sleep(0); counter = v + 1` reliably loses updates, because `sleep(0)` is a guaranteed switch point sitting exactly inside the critical section. Same bug, bigger target.

### 2.4 The synchronization toolbox

| Primitive | Semantics | Typical use |
|---|---|---|
| `Lock` | Mutual exclusion; non-reentrant | Guard one invariant |
| `RLock` | Reentrant: same thread may re-acquire | Methods that call each other under one lock |
| `Semaphore(n)` | Counter; at most *n* holders | Bound concurrency (e.g., ≤10 simultaneous downloads) |
| `Event` | One-shot broadcast flag | "Config loaded, everyone proceed" |
| `Condition` | Wait for a predicate under a lock | Bounded buffer, producer/consumer by hand |
| `Barrier(n)` | All *n* threads rendezvous | Phased algorithms |
| `queue.Queue` | Thread-safe FIFO with blocking put/get | **Default choice** — CSP-style pipelines |

The producer–consumer pattern with `queue.Queue` — the shape most threaded programs should take:

```python
import threading, queue

q: queue.Queue = queue.Queue(maxsize=64)   # bounded ⇒ backpressure
SENTINEL = object()

def producer():
    for item in range(1000):
        q.put(item)                        # blocks if full — flow control for free
    q.put(SENTINEL)

def consumer():
    while (item := q.get()) is not SENTINEL:
        process(item)

def process(item): pass

threads = [threading.Thread(target=producer), threading.Thread(target=consumer)]
for t in threads: t.start()
for t in threads: t.join()
```

Why a *bounded* queue matters: an unbounded queue lets a fast producer outrun a slow consumer until memory is exhausted. Bounding the queue converts a memory leak into **backpressure** — the producer blocks, pacing itself to the consumer. This principle (bound your buffers, propagate pressure upstream) recurs in asyncio, in Kafka, in TCP itself.

### 2.5 Deadlock: the formal conditions

> **⊢ Theorem (Coffman conditions).** Deadlock can occur iff all four hold simultaneously: (1) **mutual exclusion** — resources are held exclusively; (2) **hold-and-wait** — a task holds one resource while waiting for another; (3) **no preemption** — resources can't be forcibly taken; (4) **circular wait** — a cycle T₁→T₂→…→T₁ in the waits-for graph.

The classic instance:

```python
a, b = threading.Lock(), threading.Lock()

def t1():
    with a:
        with b: ...        # holds a, wants b

def t2():
    with b:
        with a: ...        # holds b, wants a   → cycle → deadlock (sometimes)
```

Break any one condition and deadlock is impossible. The standard, cheapest break is condition (4): **impose a global total order on locks and always acquire in that order.** Both threads acquire `a` then `b`; the waits-for graph is acyclic; deadlock cannot form. Alternatives: acquire with timeouts and back off (breaks 2/3, risks livelock), or hold at most one lock at a time (breaks 2).

### 2.6 Threading pitfalls checklist

- **Silent exceptions.** An exception in a `Thread` target kills that thread and by default just prints a traceback; the program continues, half-broken. Use `ThreadPoolExecutor` + `Future.result()` (re-raises in the caller), or `threading.excepthook`.
- **Daemon threads die mid-write.** `daemon=True` threads are killed abruptly at interpreter exit — no `finally`, no flushing. Never do I/O or hold resources in daemon threads; prefer non-daemon threads plus an explicit shutdown signal (Event or sentinel).
- **`fork()` + threads is poison** (§3.3): after `fork` in a multi-threaded process, the child contains copies of locks possibly held by threads that don't exist in the child → child deadlocks in `logging`, `malloc`, etc.
- **Timeouts everywhere.** `q.get(timeout=...)`, `lock.acquire(timeout=...)`, `t.join(timeout=...)` — unbounded waits turn small bugs into hung processes.
- **Don't guess thread counts for I/O.** For I/O-bound pools, workers ≈ concurrency you want, bounded by the remote end's tolerance — not by `os.cpu_count()`. CPU count is irrelevant when threads are mostly blocked.

---

## Part 3 — Multiprocessing: Parallelism by Paying the Isolation Tax

### 3.1 The core idea

If the GIL forbids two threads from running bytecode simultaneously *in one interpreter*, run **several interpreters**: separate OS processes, each with its own GIL, memory space, and Python runtime. The kernel schedules them onto different cores; you get genuine parallelism for pure-Python CPU work. The price is the isolation you learned in §1.1: nothing is shared, so every object crossing a process boundary must be **serialized** (pickled), copied through a pipe, and deserialized.

```python
# save as cpu_pool.py — must be importable! (§3.4)
import time
from concurrent.futures import ProcessPoolExecutor

def count(n: int) -> int:
    s = 0
    for i in range(n):
        s += i
    return s

if __name__ == "__main__":
    work = [20_000_000] * 4

    t0 = time.perf_counter()
    results = [count(n) for n in work]
    print(f"serial:   {time.perf_counter() - t0:.2f}s")

    t0 = time.perf_counter()
    with ProcessPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(count, work))
    print(f"4 procs:  {time.perf_counter() - t0:.2f}s")   # ≈ serial/4 on ≥4 cores
```

This is the same workload that showed *zero* speedup with threads in §2.2 — now it scales with cores, because each worker has a private GIL.

### 3.2 The cost model (read this before parallelizing anything)

Per task submitted to a process pool you pay: **pickle(args) + pipe write + pipe read + unpickle(args) + [compute] + pickle(result) + pipe + unpickle(result)**, plus one-time process startup (~10–100 ms each under `spawn`, which re-imports your modules). Hence the golden rule:

> **Parallelize coarse-grained work.** Each task should compute for ≫ the serialization+dispatch overhead (rule of thumb: ≥ tens of milliseconds of CPU per task, and small arguments/results). Sending 10 million tiny tasks of 1 µs each to a pool is slower than a plain loop — Amdahl's serial fraction (§1.4) becomes the IPC itself.

Corollaries: batch small items into chunks (`pool.map(..., chunksize=…)` does this for you); don't ship huge arrays as arguments — use shared memory (§3.5) or have workers load data themselves; return summaries, not raw data.

### 3.3 Start methods: fork, spawn, forkserver

How does the child process come to exist? Three answers, with materially different semantics:

| Method | Mechanism | Speed | Inherits parent state? | Default on |
|---|---|---|---|---|
| `fork` | `fork()`: copy-on-write clone of parent | Fast | Everything (globals, open fds, imported modules) | Linux ≤3.13* |
| `spawn` | Fresh interpreter, re-imports your module, pickles what's needed | Slow | Nothing implicit | Windows, macOS, **Linux 3.14+** |
| `forkserver` | Fork from a clean, single-threaded server process | Medium | Server's minimal state | opt-in |

\* **This changed recently and it matters:** `fork` in a process that has threads is fundamentally unsafe (the child gets a snapshot of memory including locks held by threads that were *not* copied — instant latent deadlocks, corrupted state). Since so many libraries spawn hidden threads, CPython 3.12 started warning about it, and **3.14 switched the Linux default from `fork` to `spawn`**, aligning all platforms. Consequences of `spawn` you must internalize:

1. Your worker function and its arguments must be **picklable** → no lambdas, no closures, no inner functions as targets (pickle stores functions by qualified name).
2. The child **re-imports your main module** → module-level code runs again → the `if __name__ == "__main__":` guard is mandatory, or you fork-bomb yourself (§3.4).
3. Globals mutated in the parent after import are **not** visible in children. Pass state explicitly (arguments, or an `initializer=` for the pool).

Be explicit rather than default-dependent: `multiprocessing.set_start_method("spawn")` or `ctx = multiprocessing.get_context("spawn")`.

### 3.4 The infamous guard, explained properly

```python
import multiprocessing as mp

def work(x): return x * x

if __name__ == "__main__":                       # ← without this, under spawn:
    with mp.Pool(4) as pool:                     #   child imports module →
        print(pool.map(work, range(10)))         #   module creates a Pool →
                                                 #   which spawns children → ∞
```

Under `spawn`, each child starts a fresh interpreter and imports your main module to get access to `work`. During that import, `__name__` is *not* `"__main__"` (it's the module's real name), so guarded code is skipped. Unguarded pool creation would recurse: every child creates its own pool of children. CPython detects and raises on this, but the mental model — *children re-execute your module top to bottom* — explains a whole family of "why does my print run 5 times" mysteries.

### 3.5 Sharing state across processes (when you must)

Message passing (default): `mp.Queue`, `mp.Pipe`, or just `pool.map` results — all pickle under the hood.

True shared memory (no copies):

```python
import numpy as np
from multiprocessing import shared_memory

# Parent: allocate a shared segment and view it as an array
shm = shared_memory.SharedMemory(create=True, size=8 * 1_000_000)
arr = np.ndarray((1_000_000,), dtype=np.float64, buffer=shm.buf)
arr[:] = 0.0

# Workers attach by *name* — only the string is pickled, not the data:
def worker(name: str, lo: int, hi: int):
    s = shared_memory.SharedMemory(name=name)
    view = np.ndarray((1_000_000,), dtype=np.float64, buffer=s.buf)
    view[lo:hi] += 1.0          # your responsibility: disjoint slices or locks!
    s.close()
```

Also in the toolbox: `mp.Value`/`mp.Array` (small ctypes scalars/arrays with an optional built-in lock) and `mp.Manager()` (a *proxy server* process exposing dict/list — convenient, but every access is an RPC: slow; last resort). Note that with shared memory you have reintroduced data races across processes — the isolation safety net is gone precisely where you opted out of it.

### 3.6 Multiprocessing pitfalls checklist

- **Unpicklable work** → `PicklingError` on lambdas/closures/local functions/open sockets. Use module-level functions; pass plain data.
- **Oversized IPC** → shipping a 2 GB DataFrame to 8 workers pickles it 8 times. Load in workers, or use `shared_memory`, or memory-map files.
- **Fork + threads** → deadlocks in children (see §3.3). If you must mix, use `spawn`/`forkserver`.
- **Copy-on-write illusions** → under `fork`, children *seem* to share parent data free of charge, but CPython's refcount updates touch every object's header page, forcing copies anyway; and the data silently diverges the moment anyone writes. Don't build designs on COW sharing of Python objects.
- **Zombie/leaked workers** → always use context managers (`with Pool(...)`) or `terminate/join` in `finally`.
- **`cpu_count()` naivety** → in containers, `os.cpu_count()` reports the host's cores, not your cgroup quota; use `os.process_cpu_count()` (3.13+) or `len(os.sched_getaffinity(0))`.

---
## Part 4 — asyncio: Cooperative Concurrency on One Thread

### 4.1 The inversion

Threads say: *write blocking code; the OS will interleave it for you (preemptively).* asyncio says: *never block; instead, tell the scheduler exactly where you're willing to pause (`await`), and one thread will interleave thousands of tasks at those points (cooperatively).*

> **⊢ Definition (Coroutine).** A coroutine is a generalization of a subroutine: a function whose execution can be **suspended** at designated points, preserving its full local state (locals, instruction pointer), and later **resumed** from that point. Subroutines have one entry and one exit; coroutines have many.

Python coroutines are, historically and mechanically, enhanced generators. `yield` was always suspension-with-state; PEP 342 made generators resumable *with a value* (`gen.send(x)`), PEP 380 added delegation (`yield from`), and PEP 492 gave the pattern dedicated syntax (`async def` / `await`, where `await` is essentially `yield from` restricted to awaitables). Knowing this lineage demystifies everything: **an `async def` function call creates a paused state machine; `await` is a structured `yield` that propagates suspension up to the scheduler.**

```python
async def f():
    return 42

coro = f()          # nothing ran! just created the coroutine object
print(coro)         # <coroutine object f at 0x...>
try:
    coro.send(None) # drive it manually, like a generator
except StopIteration as e:
    print(e.value)  # 42 — return value travels in StopIteration
```

That `send/StopIteration` protocol is *all* the event loop does, industrially.

### 4.2 The event loop, deconstructed

The asyncio event loop is §1.3's readiness loop plus a task scheduler. Its state: a **ready queue** of callbacks to run now, a **timer heap** of delayed callbacks, and a **selector** (`epoll`/`kqueue`/IOCP) mapping file descriptors to callbacks. One iteration:

```
1. timeout ← time until nearest timer (0 if ready queue non-empty)
2. events ← selector.select(timeout)          # the ONLY place the loop blocks
3. move callbacks for ready fds → ready queue
4. move expired timers → ready queue
5. run every callback currently in the ready queue, each to completion
   (a "callback" is usually: resume some coroutine at its await point)
6. goto 1
```

And a **Task** is the object that adapts a coroutine to this callback world. Pseudocode of the real thing:

```python
class Task:
    def __init__(self, coro, loop):
        self.coro = coro
        loop.call_soon(self._step)            # schedule first step

    def _step(self, value=None):
        try:
            fut = self.coro.send(value)       # run until next await
        except StopIteration:
            return                            # coroutine finished
        # coroutine awaited some Future `fut`:
        fut.add_done_callback(
            lambda f: loop.call_soon(self._step, f.result()))
```

Read `_step` twice; it's the whole engine. `send()` resumes the coroutine, which runs **uninterrupted** until its next `await` on something not yet ready — at which point a `Future` bubbles up the `await`/`yield from` chain to `_step`, which parks a callback on it and returns, freeing the thread for other tasks. When the selector reports the awaited I/O complete, the Future resolves, the callback fires, `send()` resumes the coroutine exactly where it left off. Concurrency from pure sequential machinery — no OS threads switched, no locks taken; a task switch is a function call (~100 ns, vs ~µs for threads), and a Task costs a few KB (vs MBs of thread stack). That is why 100,000 concurrent asyncio tasks are routine and 100,000 threads are not.

> **⊢ Definition (Future).** A `Future` is a mutable cell with states *pending → (done | cancelled)*, holding a result or exception once done, plus a list of callbacks to invoke on completion. `await future` means: "suspend me; add my resumption to your callbacks." A `Task` is a `Future` subclass whose result is produced by driving a coroutine.

### 4.3 The golden rule and its violation

Because scheduling is cooperative, the contract is absolute:

> **Between two `await` points, a coroutine has exclusive ownership of the thread. Therefore no coroutine may block or compute for long between awaits — it starves every other task in the loop.**

```python
import asyncio, time

async def heartbeat():
    while True:
        print("tick", time.strftime("%X"))
        await asyncio.sleep(1)

async def bad():
    time.sleep(5)          # BLOCKING call in a coroutine: freezes the WHOLE loop
                           # (no ticks for 5 s — every task in the program stalls)

async def good():
    await asyncio.sleep(5) # suspends this task only; loop keeps spinning

async def main():
    hb = asyncio.create_task(heartbeat())
    await bad()            # observe: heartbeat stops
    await good()           # observe: heartbeat continues
    hb.cancel()

asyncio.run(main())
```

`time.sleep`, `requests.get`, `psycopg2` queries, heavy `pandas` transforms — one such call anywhere in any coroutine and your "async server" is a sequential server with extra syntax. Escapes for unavoidable blocking/CPU work:

```python
result = await asyncio.to_thread(blocking_fn, arg)        # → thread pool (I/O-blocking libs)
result = await loop.run_in_executor(process_pool, cpu_fn, arg)  # → processes (CPU-bound)
```

Diagnostics: run with `asyncio.run(main(), debug=True)` (or `PYTHONASYNCIODEBUG=1`) — the loop logs any callback/step exceeding 100 ms.

### 4.4 Concurrency combinators

`await coro()` alone gives you *no* concurrency — it's a sequential call with pause semantics. Concurrency begins when multiple tasks are *in flight* simultaneously:

```python
import asyncio

async def fetch(i: int) -> int:
    await asyncio.sleep(1)          # stand-in for network I/O
    return i * i

async def main():
    # Sequential: 3 seconds
    r = [await fetch(i) for i in range(3)]

    # Concurrent: 1 second — all sleeps overlap
    r = await asyncio.gather(*(fetch(i) for i in range(3)))

    # Structured concurrency (3.11+), the modern default:
    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(fetch(i)) for i in range(3)]
    r = [t.result() for t in tasks]

asyncio.run(main())
```

Why prefer `TaskGroup` over `gather`: it is **structured concurrency** — child tasks cannot outlive the `async with` block; if one child fails, the others are *cancelled* (not leaked, not silently continued) and errors are aggregated in an `ExceptionGroup`. `gather`'s failure semantics are famously subtle (by default the first exception propagates while sibling tasks keep running detached). Structured concurrency restores to concurrent code the property that made structured programming work: **control flow that enters a block also exits through it** — the task tree matches the code's block tree, so reasoning is local.

Other combinators: `asyncio.wait_for(aw, timeout)` / `async with asyncio.timeout(3):` (3.11+) for deadlines; `asyncio.as_completed` to consume results in completion order; `asyncio.Semaphore(n)` to bound in-flight concurrency:

```python
sem = asyncio.Semaphore(10)               # ≤10 simultaneous requests: be a good citizen

async def polite_fetch(url):
    async with sem:
        return await fetch_url(url)
```

### 4.5 Cancellation, precisely

Cancellation is asyncio's sharpest edge and its most distinctive semantic. `task.cancel()` does **not** kill the task; it arranges for `asyncio.CancelledError` to be raised *inside the coroutine at its next suspension point*. Consequences:

- A coroutine that never awaits can never be cancelled (cooperative to the end).
- Cancellation is deliverable *at every `await`* — so every `await` is a point where your function may abruptly exit. Cleanup must therefore live in `finally`/`async with` blocks.
- You may `except CancelledError` to clean up, but you must re-raise (or at least not swallow it), or you break cancellation for everyone above you (and `TaskGroup`/`timeout` internally *depend* on it propagating).

```python
async def worker():
    conn = await open_connection()
    try:
        while True:
            await handle(conn)
    finally:
        await conn.close()     # runs even when cancelled at any await above
```

### 4.6 async iteration, context managers, generators

The `async` variants exist because their protagonists must be able to *await inside their protocol methods*: `async for` calls `__anext__` (which may await I/O to produce the next item — e.g., rows streaming from a DB cursor); `async with` calls `__aenter__`/`__aexit__` (which may await — e.g., releasing a connection back to a pool). `async def` + `yield` gives asynchronous generators, ideal for streaming pipelines:

```python
async def read_lines(reader):
    while line := await reader.readline():
        yield line.decode()

async def consume(reader):
    async for line in read_lines(reader):
        process(line)
```

Note carefully: `async for` does **not** parallelize the loop body — iterations remain strictly sequential. It only means "obtaining each item may suspend."

### 4.7 Sync/async interop and the "colored functions" problem

`async` is viral: only coroutines can `await`, so an async callee forces async callers up the whole stack ("what color is your function?"). The sanctioned crossings:

| From → To | Tool |
|---|---|
| sync → async (top level, once) | `asyncio.run(main())` |
| async → blocking sync | `await asyncio.to_thread(fn, ...)` |
| async → CPU-bound sync | `await loop.run_in_executor(ProcessPoolExecutor(), fn, ...)` |
| other thread → running loop | `asyncio.run_coroutine_threadsafe(coro, loop)`; `loop.call_soon_threadsafe(cb)` |

Everything inside one event loop is single-threaded, so **asyncio objects are not thread-safe** — never call `task.cancel()`, `future.set_result()`, or `queue.put_nowait()` from another thread directly; always cross via the two threadsafe functions above.

Conversely, because it *is* single-threaded, data races between bytecode interleavings vanish: shared state is safe to mutate **between awaits** with no locks. But logical races across awaits remain — if you read state, `await`, then write, the world may have changed in between. `asyncio.Lock` exists precisely for critical sections that *contain* awaits.

### 4.8 asyncio pitfalls checklist

- **Blocking the loop** (§4.3) — the number-one failure. Audit every third-party call inside coroutines.
- **Forgetting `await`** — `f()` without await creates a coroutine object and discards it; nothing runs. Watch for `RuntimeWarning: coroutine 'f' was never awaited`.
- **Fire-and-forget tasks garbage-collected.** The loop holds only *weak* references to tasks. `asyncio.create_task(bg())` without keeping the returned handle can be GC'd mid-flight and its exceptions lost. Keep a reference (or use a TaskGroup — it holds them for you).
- **Swallowing `CancelledError`** (§4.5) — breaks timeouts and TaskGroups above you.
- **Unbounded fan-out** — `gather(*(fetch(u) for u in million_urls))` opens a million sockets; bound with a Semaphore or a worker-pool-plus-`asyncio.Queue` pattern.
- **One loop per thread; don't nest.** `asyncio.run()` inside a running loop raises. (Jupyter runs a loop already — hence `await` works directly in cells, and `nest_asyncio` hacks exist.)
- **CPU work in the loop** — even non-blocking pure computation starves peers; offload past ~10 ms (§4.3 escapes).

---

## Part 5 — The Frontier: Free-Threaded CPython and Subinterpreters

### 5.1 Free-threaded CPython (PEP 703)

Since 3.13 (experimental) and 3.14 (officially supported, PEP 779), CPython ships a second build — `python3.14t`, the **free-threaded** build — in which *the GIL is gone*: threads execute bytecode truly in parallel. §2.2's `pure_python` benchmark scales with cores, in plain threads, sharing memory. The default build keeps the GIL for now; free-threading remains opt-in (install the `t` build), with eventual default status planned but not yet scheduled.

What replaced one big lock, conceptually:

- **Biased reference counting** — each object keeps two refcounts: an *owner-thread* count updated without atomics (fast, the common case: most objects are only ever touched by their creating thread) and a *shared* count updated atomically by other threads. Plus **deferred refcounting** for hot immortal-ish objects (functions, modules) and **immortal objects** (`None`, `True`, small ints: refcount never changes at all — also in the default build since 3.12).
- **Per-object locking with optimistic avoidance** — `dict`/`list` mutations take a lightweight per-object mutex, but reads use a lock-free fast path validated by versioning; the critical-section machinery avoids the deadlocks a naive "lock per object" scheme would create.
- **mimalloc + quiescent-state memory reclamation** — so one thread can't free memory another thread is concurrently reading.

Two consequences to internalize:

1. **Single-threaded cost.** All this machinery taxes the sequential case — a few percent in 3.14 (down from ~35% in the 3.13 experiment, with specialization/JIT work still landing). This overhead *is* the historical reason the GIL survived every removal attempt since the 1990s (Greg Stein's 1999 free-threading patch was rejected for a ~2× single-thread slowdown); PEP 703's technical achievement is shrinking that tax to acceptable.
2. **Your bugs are now real.** The GIL's accidental bytecode-atomicity guarantees are gone. `counter += 1` races exactly as in C. Builtin containers remain *internally* consistent (per-object locks: no corrupted dicts), but every *compound* operation (check-then-act, read-modify-write) needs your locks. The discipline of §1.5–§2.5 was always the correct model; free-threading merely stops forgiving its absence.

Also arrived (3.14): `concurrent.futures.InterpreterPoolExecutor` — the pool API over **subinterpreters** (PEP 684/734): multiple interpreters *in one process*, each with its own GIL. Cheaper than processes (shared address space, no pickling through the kernel — though objects still cross by sharing-protocols/serialization), stronger isolation than threads. A middle point on the isolation-vs-cost spectrum.

### 5.2 Does asyncio become obsolete without a GIL?

No — this is the map from Part 0 paying rent. Free-threading changes one cell: pure-Python CPU work can now parallelize via threads. But asyncio's advantages were never about the GIL: cooperative task switches are still ~10× cheaper than thread context switches, tasks are still ~1000× lighter than thread stacks, single-threaded loops still need no locks between awaits, and structured concurrency is still a better programming model for 100k sockets than 100k threads. **I/O-bound at scale → asyncio remains the answer. CPU-bound → your options just got better.**

---

## Part 6 — The Decision Framework

### 6.1 The flowchart

```
                       ┌────────────────────────────┐
                       │ What dominates task time?  │
                       └─────────────┬──────────────┘
              waiting (I/O) ◄────────┴────────► computing (CPU)
                    │                                │
     ┌──────────────┴──────────────┐    ┌────────────┴────────────────┐
     │ How many concurrent tasks?  │    │ Is the hot loop already C?  │
     └───┬───────────────────┬─────┘    │ (numpy/BLAS/hashlib/...)    │
   ~10²  │                   │ 10³–10⁶  └───┬────────────────────┬────┘
         ▼                   ▼            yes                    no
  ┌─────────────┐   ┌────────────────┐     ▼                     ▼
  │ threading / │   │    asyncio     │ ┌──────────────┐ ┌──────────────────┐
  │ ThreadPool- │   │ (TaskGroup +   │ │ threads are  │ │ ProcessPool-     │
  │ Executor    │   │  Semaphore)    │ │ fine: GIL is │ │ Executor; or     │
  │             │   │ *if libraries  │ │ released in  │ │ free-threaded    │
  │ works with  │   │  are async-    │ │ the C code   │ │ 3.13t/3.14t; or  │
  │ blocking    │   │  capable; else │ └──────────────┘ │ rewrite hot loop │
  │ libraries   │   │  threads       │                  │ in C/Rust/numba  │
  └─────────────┘   └────────────────┘                  └──────────────────┘
```

Plus the null option, chosen too rarely: **sequential code**. If total runtime is fine, concurrency is complexity without benefit. Profile first (`cProfile`, `py-spy`); confirm *what* you're bound on before choosing machinery.

### 6.2 Composition: the mechanisms stack

Real systems combine layers. The canonical high-performance shape — an async web server doing CPU work:

```python
import asyncio
from concurrent.futures import ProcessPoolExecutor

pool = ProcessPoolExecutor()             # parallel lane for CPU-bound work

def score_model(features: list[float]) -> float:
    ...                                  # heavy pure-Python / sklearn compute

async def handle_request(features):
    loop = asyncio.get_running_loop()
    # loop stays free to juggle 10k connections while 8 cores crunch:
    return await loop.run_in_executor(pool, score_model, features)
```

asyncio handles the 10,000 waiting sockets (concurrency); the process pool handles the arithmetic (parallelism). Each mechanism deployed *for the axis it wins on*. Similarly, `gunicorn -w 8 -k uvicorn.workers.UvicornWorker` = 8 processes × 1 event loop each: parallelism across cores, concurrency within each.

### 6.3 Worked micro-decisions

| Scenario | Choice | Why |
|---|---|---|
| Scrape 200 pages with `requests` | `ThreadPoolExecutor(20)` | I/O-bound, blocking lib, moderate scale |
| Scrape 50,000 pages | asyncio + `aiohttp` + `Semaphore(100)` | Thread-per-request too heavy at this scale |
| Resize 5,000 images | `ProcessPoolExecutor` — or threads! | Pillow releases GIL in C transforms; benchmark both |
| Train/score sklearn per-customer | `ProcessPoolExecutor(chunked)` | Pure-Python glue + heavy CPU; coarse tasks amortize pickling |
| Web API awaiting DB + cache + service | asyncio (`asyncpg`, etc.) | Many concurrent waits, async drivers exist |
| Tail a log file while serving a UI | one extra thread | Trivial concurrency; async machinery unwarranted |
| Monte Carlo simulation, pure Python | processes; consider 3.14t or numba | Embarrassingly parallel CPU |
| Same, but vectorizable | `numpy` first | Data parallelism beats task parallelism when applicable |

---

## Part 7 — Common Questions (the ones everyone eventually asks)

**Q1. "Is asyncio faster than threads?"**
Wrong axis. Per-task overhead: async wins (ns vs µs switches, KB vs MB stacks) — decisive at 10⁴⁺ tasks. At 20 tasks, both spend 99.9% of time waiting on the same network; throughput is identical, and threads are simpler with blocking libraries. asyncio buys *scalability and explicit control of interleaving*, not speed per se.

**Q2. "If threads can't run Python in parallel, why does `ThreadPoolExecutor` exist at all?"**
Because blocked threads release the GIL. Threads deliver concurrency for I/O and parallelism for GIL-releasing C code — that covers an enormous share of real workloads. The GIL forbids exactly one thing: simultaneous *bytecode* execution.

**Q3. "Why didn't they just remove the GIL years ago?"**
Every removal attempt until PEP 703 slowed single-threaded code unacceptably (fine-grained locking + atomic refcounts are expensive) and threatened the C-extension ecosystem, whose ABI assumed the GIL. It took biased/deferred refcounting, immortal objects, mimalloc, and a multi-year compatibility campaign to change the trade-off. Engineering, not stubbornness.

**Q4. "Does `async` make my code use multiple cores?"**
No. One event loop = one thread = one core. asyncio is concurrency without parallelism (Part 0's top-left quadrant... precisely the point). Cores require processes, GIL-releasing C, or free-threaded builds.

**Q5. "Can I mix asyncio and threads/processes?"**
Yes, and production systems should: `asyncio.to_thread` for blocking libraries, `run_in_executor(ProcessPoolExecutor)` for CPU, `run_coroutine_threadsafe` for outside threads poking the loop. See §6.2.

**Q6. "Do I need locks in asyncio?"**
Not for state mutated atomically *between* awaits (single thread). Yes (`asyncio.Lock`) for invariants spanning an `await` — the world changes while you're suspended. And `asyncio` primitives ≠ `threading` primitives: never mix them.

**Q7. "How many workers should my pool have?"**
CPU-bound processes: ≈ physical cores (`os.process_cpu_count()`). I/O-bound threads: as many as the latency/throughput math and the remote service tolerate — often 10–100; CPU count is irrelevant. Async: bound with a Semaphore chosen by the same reasoning. Always: measure.

**Q8. "Why does my multiprocessing code crash only on Windows/macOS (or after upgrading to 3.14)?"**
`spawn` semantics: missing `__main__` guard, unpicklable targets, or reliance on inherited globals — all silently tolerated by `fork` on older-Linux defaults (§3.3–3.4).

**Q9. "Is `await` a context switch?"**
Only potentially. `await` on an already-completed future can resume immediately without visiting the loop (fast path). `await` marks *permission* to suspend, not a mandate. Corollary: a `while True: pass` inside `async def` never yields — cooperative means cooperative.

**Q10. "Threads vs. greenlets/gevent?"**
gevent achieves asyncio-like cooperative concurrency by monkey-patching blocking calls into implicit yields — no `async` syntax, but suspension points become invisible, which reintroduces exactly the reasoning difficulties `await` was designed to make explicit. Modern Python has largely converged on explicit async.

---

## Coda — The Whole Model on One Page

1. **Concurrency** = structure for overlapping tasks; **parallelism** = simultaneous execution. Independent properties; mechanisms below, properties above.
2. Classify the workload: **I/O-bound → overlap the waits** (asyncio, threads); **CPU-bound → more silicon** (processes, free-threading, C, vectorize).
3. **Threads**: preemptive, shared memory, µs switches; GIL blocks parallel *bytecode* but not I/O or C. Guard every shared invariant; order your locks; prefer queues.
4. **Processes**: true parallelism, isolation tax (pickle + IPC + startup). Coarse tasks; `spawn` semantics; `__main__` guard; shared memory only deliberately.
5. **asyncio**: cooperative, single-threaded, ~ns switches, 10⁵ tasks feasible. Never block between awaits; structure with TaskGroups; bound with Semaphores; respect cancellation.
6. **Free-threaded CPython** removes the GIL, making thread discipline (happens-before, locks) load-bearing rather than optional — and leaving asyncio's raison d'être intact.
7. Mechanisms **compose**: event loop for the sockets, process pool for the math, one lock order for the state, bounded queues for the flow.

*Profile first. Choose the mechanism the bottleneck demands, not the one that's fashionable. And keep the map.*
