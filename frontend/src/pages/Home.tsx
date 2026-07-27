import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { listCalls, uploadCall, IN_FLIGHT, type CallListItem } from "../api";
import { runPool } from "../pool";

const UPLOAD_CONCURRENCY = 5;

export default function Home() {
  const [items, setItems] = useState<CallListItem[]>([]);
  const [uploading, setUploading] = useState<{ done: number; total: number } | null>(null);
  const timer = useRef<number>(undefined);

  const refresh = useCallback(async () => {
    try {
      const page = await listCalls();
      setItems(page.items);
    } catch (e) {
      console.error(e);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  // Poll only while at least one visible call is still in flight.
  useEffect(() => {
    const anyInFlight = items.some((c) => IN_FLIGHT.includes(c.status));
    if (anyInFlight && !timer.current) {
      timer.current = window.setInterval(refresh, 3000);
    } else if (!anyInFlight && timer.current) {
      clearInterval(timer.current);
      timer.current = undefined;
    }
    return () => {
      if (timer.current) { clearInterval(timer.current); timer.current = undefined; }
    };
  }, [items, refresh]);

  const onFiles = useCallback(async (files: FileList | null) => {
    if (!files) return;
    const accepted = [...files].filter((f) => /\.(wav|mp3)$/i.test(f.name));
    if (accepted.length === 0) return;
    let done = 0;
    setUploading({ done: 0, total: accepted.length });
    await runPool(accepted, UPLOAD_CONCURRENCY, async (f) => {
      await uploadCall(f);
      done += 1;
      setUploading({ done, total: accepted.length });
    });
    setUploading(null);
    refresh();
  }, [refresh]);

  return (
    <div className="container">
      <h1>Call Analyzer</h1>

      <label
        className="card dropzone"
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => { e.preventDefault(); onFiles(e.dataTransfer.files); }}
        style={{ display: "block", cursor: "pointer", padding: 32 }}
      >
        <input
          type="file"
          accept=".wav,.mp3"
          multiple
          style={{ display: "none" }}
          onChange={(e) => { onFiles(e.target.files); e.target.value = ""; }}
        />
        {uploading
          ? <>Uploading… {uploading.done}/{uploading.total}</>
          : <>Drop <b>.wav</b>/<b>.mp3</b> files here, or click to choose.</>}
      </label>

      {items.length === 0 && <p style={{ color: "#888" }}>No calls yet.</p>}
      {items.map((c) => (
        <Link to={`/calls/${c.id}`} key={c.id} style={{ textDecoration: "none" }}>
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
