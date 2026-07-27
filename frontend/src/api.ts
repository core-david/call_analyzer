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
