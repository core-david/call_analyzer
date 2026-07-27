import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { getCall, type CallDetail } from "../api";

const fmt = (s: number) =>
  `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

export default function Detail() {
  const { id } = useParams();
  const [call, setCall] = useState<CallDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    getCall(id).then(setCall).catch((e) => setError(String(e)));
  }, [id]);

  if (error) return <div className="container"><Link to="/">← back</Link><p>{error}</p></div>;
  if (!call) return <div className="container">Loading…</div>;

  const a = call.analysis;
  return (
    <div className="container">
      <Link to="/">← back</Link>
      <h1 style={{ marginBottom: 8 }}>{call.filename}</h1>
      <span className={`badge ${call.status}`}>{call.status}</span>

      {call.status === "failed" && (
        <div className="card" style={{ marginTop: 16 }}>
          Processing failed: <code>{call.error_code}</code>
        </div>
      )}

      {a && (
        <>
          <div className="card" style={{ marginTop: 16 }}>
            <h3>Summary</h3>
            <p>{a.summary}</p>
            <p><b>Next step:</b> {a.next_step}</p>
          </div>

          <div className="card">
            <h3>Tags</h3>
            <p>
              <b>Outcome:</b> {a.tags.outcome} &nbsp;·&nbsp;
              <b> Lead:</b> {a.tags.lead_temperature} &nbsp;·&nbsp;
              <b> Intent:</b> {a.intent}
            </p>
            <b>Objections</b>
            <ul>
              {a.tags.objections.map((o, i) => (
                <li key={i}>{o.type} — <i>"{o.quote}"</i></li>
              ))}
              {a.tags.objections.length === 0 && <li style={{ color: "#888" }}>none</li>}
            </ul>
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
          <h3>
            Transcript{" "}
            <small style={{ color: "#888", fontWeight: 400 }}>
              ({call.transcript.language}, {fmt(call.transcript.duration)})
            </small>
          </h3>
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
