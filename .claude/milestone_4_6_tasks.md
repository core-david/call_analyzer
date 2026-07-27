# Milestone 4 + 6 — Frontend & Deployment (combined task guide)

> One document: a **basic, read-only** React frontend, then **Render**
> deployment. Same format as the other guides — explain-first, then code, then
> verify. No pytest here — tests are M5 (still on the board, sequenced after).
>
> **Decisions locked in:**
> - Frontend: **React + Vite + TypeScript**, no UI library, plain CSS. Three
>   views (Upload, List, Detail), **read-only** — no tag editing, no JSON export
>   (deferred to future improvements).
> - The v2 analysis shape is rendered; the `reasoning` field is **hidden** in the
>   UI (kept in the payload for debugging).
> - Polling only while a visible call is in flight (uploaded/transcribing/analyzing).
> - Deployment: **Render** — web (FastAPI) · worker (arq) · Key Value (Redis) ·
>   Postgres · Static Site — as a `render.yaml` blueprint. Worker is a **separate
>   service**. R2 stays external.
> - Local dev: Vite dev server proxies `/api` → `localhost:8000`; prod uses
>   `VITE_API_BASE_URL` baked at build time.

---

# Part A — Frontend

The API the frontend consumes (already built):
- `POST /api/calls` (multipart `file`) → `202 {id, status}`
- `GET /api/calls?status=&cursor=&limit=` → `{items: [{id, filename, status, error_code, created_at}], next_cursor}`
- `GET /api/calls/{id}` → full record incl. `transcript` and `analysis` (v2 shape)

## Task F1 — Scaffold, API client, app shell

### Explain first

**Why Vite + TS, no framework beyond React.** The API already exists; the SPA
just fetches and renders. Vite gives instant dev reload and a static `dist/` for
Render's free static hosting. TypeScript types mirror the backend schemas so a
shape drift is a compile error, not a runtime surprise.

**One API base, two environments.** `import.meta.env.VITE_API_BASE_URL` is empty
in dev (so requests go to relative `/api`, which Vite proxies to `localhost:8000`)
and set to the API's public URL in prod (baked at build). The app code never
branches on environment.

### Steps

1. Scaffold (from repo root):

   ```bash
   npm create vite@latest frontend -- --template react-ts
   cd frontend && npm install && npm install react-router-dom
   ```

2. `frontend/vite.config.ts` — dev proxy so relative `/api` works locally:

   ```ts
   import { defineConfig } from "vite";
   import react from "@vitejs/plugin-react";

   export default defineConfig({
     plugins: [react()],
     server: {
       proxy: { "/api": "http://localhost:8000" },
     },
   });
   ```

3. `frontend/src/api.ts` — types (mirror the backend) + fetch helpers:

   ```ts
   const BASE = import.meta.env.VITE_API_BASE_URL ?? "";

   export type Status =
     | "uploaded" | "transcribing" | "analyzing" | "completed" | "failed";

   export const IN_FLIGHT: Status[] = ["uploaded", "transcribing", "analyzing"];

   export interface CallListItem {
     id: string;
     filename: string;
     status: Status;
     error_code: string | null;
     created_at: string;
   }

   export interface Utterance { speaker: number; start: number; end: number; text: string; }
   export interface Transcript { language: string | null; text: string; duration: number; utterances: Utterance[]; }

   export interface ObjectionItem { type: string; quote: string; }
   export interface SpeakerMood { label: string; note: string; }
   export interface Analysis {
     reasoning: string;
     summary: string;
     tags: { outcome: string; objections: ObjectionItem[]; lead_temperature: string };
     intent: string;
     mood: { agent: SpeakerMood; customer: SpeakerMood };
     next_step: string;
   }

   export interface CallDetail extends CallListItem {
     transcript: Transcript | null;
     analysis: Analysis | null;
   }

   export interface CallListPage { items: CallListItem[]; next_cursor: string | null; }

   export async function uploadCall(file: File): Promise<{ id: string; status: Status }> {
     const form = new FormData();
     form.append("file", file);
     const res = await fetch(`${BASE}/api/calls`, { method: "POST", body: form });
     if (!res.ok) throw new Error(`upload failed (${res.status})`);
     return res.json();
   }

   export async function listCalls(status?: Status): Promise<CallListPage> {
     const q = new URLSearchParams();
     if (status) q.set("status", status);
     const res = await fetch(`${BASE}/api/calls?${q}`);
     if (!res.ok) throw new Error(`list failed (${res.status})`);
     return res.json();
   }

   export async function getCall(id: string): Promise<CallDetail> {
     const res = await fetch(`${BASE}/api/calls/${id}`);
     if (!res.ok) throw new Error(`get failed (${res.status})`);
     return res.json();
   }
   ```

4. `frontend/src/main.tsx` — router (two routes):

   ```tsx
   import React from "react";
   import ReactDOM from "react-dom/client";
   import { createBrowserRouter, RouterProvider } from "react-router-dom";
   import Home from "./pages/Home";
   import Detail from "./pages/Detail";
   import "./index.css";

   const router = createBrowserRouter([
     { path: "/", element: <Home /> },
     { path: "/calls/:id", element: <Detail /> },
   ]);

   ReactDOM.createRoot(document.getElementById("root")!).render(
     <React.StrictMode>
       <RouterProvider router={router} />
     </React.StrictMode>,
   );
   ```

5. `frontend/src/index.css` — minimal, theme-neutral styling (keep it small):

   ```css
   :root { font-family: system-ui, sans-serif; color: #1a1a1a; }
   body { margin: 0; background: #f7f7f8; }
   .container { max-width: 900px; margin: 0 auto; padding: 24px; }
   .badge { padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; }
   .badge.completed { background: #dcfce7; color: #166534; }
   .badge.failed { background: #fee2e2; color: #991b1b; }
   .badge.uploaded, .badge.transcribing, .badge.analyzing { background: #fef9c3; color: #854d0e; }
   .row { display: flex; justify-content: space-between; padding: 12px; border-bottom: 1px solid #eee; align-items: center; }
   .card { background: #fff; border: 1px solid #eee; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
   .utt { margin: 6px 0; } .utt .spk { font-weight: 600; color: #555; }
   ```

6. Verify: `npm run dev`, open the printed URL — a blank shell renders with no
   console errors (pages come next).

### Files
- `frontend/` scaffold, `src/api.ts`, `src/main.tsx`, `src/index.css`, `vite.config.ts`

---

## Task F2 — Upload view (multi-file + concurrency pool)

### Explain first

**Batch upload, bounded concurrency.** A user may drop many files; firing 1,000
`fetch`es at once would swamp the browser and the API. A small pool (~5 in
flight) uploads a few at a time, each independent — one failure doesn't sink the
batch (the "1,000 recordings" constraint, client side). Each upload just needs a
`202 + id`; the pipeline runs server-side and the list view watches it.

### Steps

1. `frontend/src/pool.ts` — a tiny async pool:

   ```ts
   export async function runPool<T>(items: T[], limit: number, fn: (t: T) => Promise<void>) {
     const queue = [...items];
     const workers = Array.from({ length: Math.min(limit, queue.length) }, async () => {
       while (queue.length) {
         const item = queue.shift()!;
         try { await fn(item); } catch (e) { console.error(e); }
       }
     });
     await Promise.all(workers);
   }
   ```

2. Upload lives on the Home page (F3). The handler:

   ```tsx
   import { runPool } from "../pool";
   import { uploadCall } from "../api";

   async function onFiles(files: FileList, onDone: () => void) {
     const accepted = [...files].filter((f) => /\.(wav|mp3)$/i.test(f.name));
     await runPool(accepted, 5, async (f) => { await uploadCall(f); });
     onDone(); // refresh the list
   }
   ```

   Minimal control: `<input type="file" accept=".wav,.mp3" multiple>` plus a
   drop zone (`onDragOver`/`onDrop`). Show per-batch counts (queued / done).

### Files
- `frontend/src/pool.ts` (upload UI folded into F3's Home page)

---

## Task F3 — List view + polling

### Explain first

**Disciplined polling.** Re-fetch the list only while at least one visible call
is still in flight (`uploaded/transcribing/analyzing`). When everything is
`completed`/`failed`, stop the timer — no idle network churn. This is the
"polling only while in-flight" rule, the right-sized alternative to SSE.

### Steps

1. `frontend/src/pages/Home.tsx`:

   ```tsx
   import { useCallback, useEffect, useRef, useState } from "react";
   import { Link } from "react-router-dom";
   import { listCalls, CallListItem, IN_FLIGHT } from "../api";
   import { runPool } from "../pool";
   import { uploadCall } from "../api";

   export default function Home() {
     const [items, setItems] = useState<CallListItem[]>([]);
     const timer = useRef<number>();

     const refresh = useCallback(async () => {
       const page = await listCalls();
       setItems(page.items);
     }, []);

     useEffect(() => { refresh(); }, [refresh]);

     // Poll only while something is in flight.
     useEffect(() => {
       const anyInFlight = items.some((c) => IN_FLIGHT.includes(c.status));
       if (anyInFlight && !timer.current) {
         timer.current = window.setInterval(refresh, 3000);
       } else if (!anyInFlight && timer.current) {
         clearInterval(timer.current); timer.current = undefined;
       }
       return () => { if (timer.current) { clearInterval(timer.current); timer.current = undefined; } };
     }, [items, refresh]);

     async function onFiles(files: FileList | null) {
       if (!files) return;
       const accepted = [...files].filter((f) => /\.(wav|mp3)$/i.test(f.name));
       await runPool(accepted, 5, async (f) => { await uploadCall(f); });
       refresh();
     }

     return (
       <div className="container">
         <h1>Call Analyzer</h1>
         <div className="card"
           onDragOver={(e) => e.preventDefault()}
           onDrop={(e) => { e.preventDefault(); onFiles(e.dataTransfer.files); }}>
           <input type="file" accept=".wav,.mp3" multiple
             onChange={(e) => onFiles(e.target.files)} />
           <p>Drop .wav/.mp3 files here or choose above.</p>
         </div>
         {items.map((c) => (
           <Link to={`/calls/${c.id}`} key={c.id} style={{ textDecoration: "none", color: "inherit" }}>
             <div className="row">
               <span>{c.filename}</span>
               <span className={`badge ${c.status}`}>
                 {c.status}{c.error_code ? ` · ${c.error_code}` : ""}
               </span>
             </div>
           </Link>
         ))}
       </div>
     );
   }
   ```

2. Verify (backend up via `docker compose up -d`): `npm run dev`, drop a real
   recording, watch the badge walk `uploaded → transcribing → analyzing →
   completed`, then polling stops (check the Network tab goes quiet).

### Files
- `frontend/src/pages/Home.tsx`

---

## Task F4 — Detail view

### Explain first

**Render the v2 analysis, hide the plumbing.** Summary, tags (outcome badge,
objections *with their quotes*, lead temperature), intent, per-speaker mood
(label + arc note), and next step; then the diarized transcript as
speaker-labeled lines with timestamps. `reasoning` is intentionally not shown —
it's a model-internal justification, useful in the payload, noise in the UI.
`failed` calls show the `error_code` instead of an analysis.

### Steps

1. `frontend/src/pages/Detail.tsx`:

   ```tsx
   import { useEffect, useState } from "react";
   import { Link, useParams } from "react-router-dom";
   import { getCall, CallDetail } from "../api";

   const fmt = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

   export default function Detail() {
     const { id } = useParams();
     const [call, setCall] = useState<CallDetail | null>(null);

     useEffect(() => { if (id) getCall(id).then(setCall); }, [id]);
     if (!call) return <div className="container">Loading…</div>;

     const a = call.analysis;
     return (
       <div className="container">
         <Link to="/">← back</Link>
         <h1>{call.filename}</h1>
         <span className={`badge ${call.status}`}>{call.status}</span>

         {call.status === "failed" && (
           <div className="card">Failed: <code>{call.error_code}</code></div>
         )}

         {a && (
           <>
             <div className="card">
               <h3>Summary</h3><p>{a.summary}</p>
               <p><b>Next step:</b> {a.next_step}</p>
             </div>
             <div className="card">
               <h3>Tags</h3>
               <p><b>Outcome:</b> {a.tags.outcome} &nbsp; <b>Lead:</b> {a.tags.lead_temperature} &nbsp; <b>Intent:</b> {a.intent}</p>
               <b>Objections:</b>
               <ul>{a.tags.objections.map((o, i) => (
                 <li key={i}>{o.type} — <i>"{o.quote}"</i></li>
               ))}{a.tags.objections.length === 0 && <li>none</li>}</ul>
             </div>
             <div className="card">
               <h3>Mood</h3>
               <p><b>Agent:</b> {a.mood.agent.label} — {a.mood.agent.note}</p>
               <p><b>Customer:</b> {a.mood.customer.label} — {a.mood.customer.note}</p>
             </div>
           </>
         )}

         {call.transcript && (
           <div className="card">
             <h3>Transcript <small>({call.transcript.language}, {fmt(call.transcript.duration)})</small></h3>
             {call.transcript.utterances.map((u, i) => (
               <div className="utt" key={i}>
                 <span className="spk">[{fmt(u.start)}] Speaker {u.speaker}:</span> {u.text}
               </div>
             ))}
           </div>
         )}
       </div>
     );
   }
   ```

2. Verify: open a completed call — summary, tags with quotes, mood arcs, and the
   speaker-labeled transcript render; open a `failed` call — the error code shows.

### Files
- `frontend/src/pages/Detail.tsx`

---

# Part B — Deployment (Render)

## Task D1 — Backend prod-readiness

### Explain first

**Three prod gaps to close before deploying:** (1) CORS — the static site is a
different origin than the API, so the browser needs the API to allow it;
(2) the DB URL scheme — Render's managed Postgres hands out a `postgresql://`
URL, but our async stack needs `postgresql+asyncpg://`; (3) making both
configurable from env so nothing is hard-coded.

### Steps

1. `backend/app/core/config.py` — add CORS origins + normalize the DB scheme:

   ```python
   from pydantic import field_validator

   # inside Settings:
       cors_origins: str = "http://localhost:5173"  # comma-separated

       @field_validator("database_url")
       @classmethod
       def _asyncpg_scheme(cls, v: str) -> str:
           # Render Postgres gives postgresql:// (or postgres://); asyncpg needs the +asyncpg driver.
           for prefix in ("postgresql://", "postgres://"):
               if v.startswith(prefix):
                   return "postgresql+asyncpg://" + v[len(prefix):]
           return v

       @property
       def cors_origin_list(self) -> list[str]:
           return [o.strip() for o in self.cors_origins.split(",") if o.strip()]
   ```

2. `backend/app/main.py` — add CORS middleware:

   ```python
   from fastapi.middleware.cors import CORSMiddleware

   app.add_middleware(
       CORSMiddleware,
       allow_origins=settings.cors_origin_list,
       allow_methods=["*"],
       allow_headers=["*"],
   )
   ```

3. Verify locally: `docker compose up -d`, then from the Vite dev server the app
   still works (localhost:5173 is in the default `cors_origins`).

### Files
- edits: `backend/app/core/config.py`, `backend/app/main.py`

---

## Task D2 — `render.yaml` blueprint

### Explain first

**Infra as one reviewed file.** The blueprint declares all five pieces and wires
them together: the worker and web share one Docker image (different commands);
`DATABASE_URL`/`REDIS_URL` are injected from the managed services; secrets are
`sync: false` (entered in the dashboard, never in git). **`maxmemoryPolicy:
noeviction`** on Key Value matters — the job queue must never have entries
evicted. Migrations run in the web service's `preDeployCommand`.

### Steps

1. Create `render.yaml` at the repo root:

   ```yaml
   services:
     - name: call-analyzer-api
       type: web
       runtime: docker
       dockerfilePath: ./backend/Dockerfile
       dockerContext: ./backend
       dockerCommand: uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
       plan: starter
       region: oregon
       healthCheckPath: /health
       preDeployCommand: uv run alembic upgrade head
       envVars: &backend_env
         - key: DATABASE_URL
           fromDatabase: { name: call-analyzer-db, property: connectionString }
         - key: REDIS_URL
           fromService: { name: call-analyzer-redis, type: keyvalue, property: connectionString }
         - key: STORAGE_REGION
           value: auto
         - key: DEEPGRAM_API_KEY
           sync: false
         - key: GOOGLE_API_KEY
           sync: false
         - key: STORAGE_ENDPOINT_URL
           sync: false
         - key: STORAGE_ACCESS_KEY
           sync: false
         - key: STORAGE_SECRET_KEY
           sync: false
         - key: STORAGE_BUCKET_NAME
           sync: false
         - key: CORS_ORIGINS
           sync: false     # set to the static site URL after first deploy

     - name: call-analyzer-worker
       type: worker
       runtime: docker
       dockerfilePath: ./backend/Dockerfile
       dockerContext: ./backend
       dockerCommand: uv run arq app.worker.settings.WorkerSettings
       plan: starter
       region: oregon
       envVars: *backend_env   # same env as the API (minus CORS, harmless)

     - name: call-analyzer-redis
       type: keyvalue
       plan: starter
       region: oregon
       ipAllowList: []            # internal-only; no public access
       maxmemoryPolicy: noeviction  # never evict queued jobs

     - name: call-analyzer-frontend
       type: web
       runtime: static
       rootDir: frontend
       buildCommand: npm ci && npm run build
       staticPublishPath: dist
       envVars:
         - key: VITE_API_BASE_URL
           sync: false     # set to the API URL, then redeploy (Vite bakes at build)
       routes:
         - type: rewrite
           source: /*
           destination: /index.html   # SPA fallback for react-router

   databases:
     - name: call-analyzer-db
       plan: basic-256mb
       region: oregon
       postgresMajorVersion: "16"
   ```

2. Notes to confirm at deploy time:
   - Plans (`starter`, `basic-256mb`) are adjustable; `free` exists but web/worker
     spin down on idle (cold starts) and free Postgres expires — note it in the
     README either way.
   - The `&backend_env` / `*backend_env` YAML anchor shares the env list between
     API and worker so secrets are entered once per service in the dashboard.

### Files
- `render.yaml`

---

## Task D3 — Provision, configure, smoke test

### Explain first

**The chicken-and-egg**: the API needs the frontend's URL (CORS) and the
frontend needs the API's URL (`VITE_API_BASE_URL`) — neither exists until
deployed. So: deploy the blueprint, then set the two cross-referencing vars and
redeploy the two affected services.

### Steps

1. Push the branch; in Render, **New → Blueprint**, point at the repo. It creates
   all five resources. Enter the `sync: false` secrets on the API and worker:
   `DEEPGRAM_API_KEY`, `GOOGLE_API_KEY`, and the four `STORAGE_*` R2 values.
2. First deploy runs `preDeployCommand` → `alembic upgrade head` against managed
   Postgres (the URL is normalized to `+asyncpg` by D1). Confirm the `calls`
   table exists (Render shell: `uv run alembic current`).
3. Wire the two cross-references:
   - Set the API's `CORS_ORIGINS` to the static site URL (e.g.
     `https://call-analyzer-frontend.onrender.com`).
   - Set the frontend's `VITE_API_BASE_URL` to the API URL, and **redeploy the
     static site** (Vite bakes env at build time — a var change needs a rebuild).
4. Smoke test the live URL end to end: upload a real recording, watch it reach
   `completed`, open the detail, read transcript + analysis.
5. README note: free-tier services cold-start (first request after idle is slow) —
   so a reviewer isn't confused by a slow first load.

### Files
- none (Render dashboard + the pushed blueprint)

---

## Milestone exit check

Local: `docker compose up -d` + `cd frontend && npm run dev` → drop recordings,
watch statuses progress live, open a call to read transcript + analysis.
Live: a public Render URL does the same end to end, reproducible from `render.yaml`
plus the documented secrets.

## Execution order

```
F1 scaffold → F2 upload → F3 list+polling → F4 detail →
D1 prod-readiness → D2 render.yaml → D3 provision & smoke test
```

Each task ends with a commit. (M5 tests and M7 docs remain, sequenced after.)
