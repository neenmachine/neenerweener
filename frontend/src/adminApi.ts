// The one owner of browser admin auth: the session check, the in-memory CSRF token, requests and their
// error text, and the empty-text check. Used by /admin and by the composer on /timeline; lazy loaded.

import { fetchWithSignal } from "./request";
import type { Entry, Profile } from "./timelineTypes";

export type AdminEntry = Entry & { body_markdown: string };
export type Session = { authenticated: false } | { authenticated: true; csrf_token: string };
// message is "" for 401: the caller treats it as a signed-out session.
export type Result<T> = { ok: true; data: T } | { ok: false; status: number; message: string; retryAfter?: number };

export const LIMIT = 10_000;
// Unicode code points, the unit the server's limit counts (not UTF-16 units, like String.length).
export const length = (s: string) => [...s].length;
// A subset of what Python's str.strip() removes, so the client is never stricter than the server; JS trim() also strips U+FEFF.
const PYTHON_SPACE = /^[\t-\r\x1c-\x20\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$/;
// The only client gate on the submit buttons. Every other rule (NUL, length, links) is the server's: its 422 shows inline.
export const hasText = (s: string) => !PYTHON_SPACE.test(s);

// Tabs of this browser tell each other a saved profile, so open admin views (the shell, the profile form, the /timeline
// composer) show it without a reload. A channel never receives its own posts; close it on unmount. A message that is
// not a profile is ignored; without BroadcastChannel the channel does nothing.
export type ProfileChannel = Pick<BroadcastChannel, "postMessage" | "close">;
export function profileChannel(apply: (profile: Profile) => void): ProfileChannel {
  if (typeof BroadcastChannel === "undefined") return { postMessage() {}, close() {} };
  const c = new BroadcastChannel("profile");
  c.onmessage = (e: MessageEvent<unknown>) => isProfile(e.data) && apply(e.data);
  return c;
}
const isProfile = (d: unknown): d is Profile => {
  const p = d as Record<string, unknown> | null;
  const text = (v: unknown) => typeof v === "string";
  return (
    typeof p === "object" &&
    p !== null &&
    text(p.display_name) &&
    (p.handle === null || text(p.handle)) &&
    (p.avatar_id === null || text(p.avatar_id)) &&
    text(p.avatar_initial)
  );
};

// In memory only: never in storage, the URL, or the DOM.
let csrf: string | null = null;

// The hint only decides whether /timeline checks the session; the server sets it at login.
function clearHint() {
  document.cookie = `admin_hint=; Max-Age=0; Path=/; SameSite=Strict${location.protocol === "https:" ? "; Secure" : ""}`;
}

function failure(status: number, detail: unknown, retryAfter?: number): string {
  if (status === 401) return "";
  if (status === 403) return "request refused, reload the page";
  if (status === 404) return "entry not found";
  // The server's validation message is a short functional one; schema errors come as a list.
  if ((status === 413 || status === 422) && typeof detail === "string") return detail;
  if (status === 422) return "invalid text";
  if (status === 429)
    return retryAfter
      ? `too many attempts, try again in ${retryAfter < 60 ? `${retryAfter} s` : `${Math.ceil(retryAfter / 60)} min`}`
      : "too many attempts, try again later";
  if (status === 503) return "unavailable, try again";
  return "something went wrong";
}

// A Blob body (an image upload) goes raw; any other object goes as JSON. signal is the caller's useRequest run.
async function send<T>(method: string, url: string, body?: object, signal?: AbortSignal): Promise<Result<T>> {
  const raw = body instanceof Blob;
  let r: Response;
  try {
    r = await fetchWithSignal(url, signal, {
      method,
      cache: "no-store",
      headers: {
        ...(body && { "Content-Type": raw ? body.type || "application/octet-stream" : "application/json" }),
        ...(method !== "GET" && csrf && { "X-CSRF-Token": csrf }),
      },
      body: raw ? body : body && JSON.stringify(body),
    });
  } catch {
    return { ok: false, status: 0, message: "could not reach the server" };
  }
  const json = await r.json().catch(() => undefined);
  // Aborted (superseded or unmounted) after the headers came: never read as a success with no body.
  if (signal?.aborted) return { ok: false, status: 0, message: "" };
  if (r.ok) return { ok: true, data: json };
  const seconds = Number(r.headers.get("Retry-After"));
  const retryAfter = seconds > 0 ? Math.ceil(seconds) : undefined;
  return { ok: false, status: r.status, message: failure(r.status, json?.detail, retryAfter), retryAfter };
}

// An authenticated request; a 401 means the session ended, so the token and the hint go.
export async function request<T = undefined>(method: string, url: string, body?: object, signal?: AbortSignal): Promise<Result<T>> {
  const r = await send<T>(method, url, body, signal);
  if (!r.ok && r.status === 401) {
    csrf = null;
    clearHint();
  }
  return r;
}

// A 401 here is a wrong password, not an ended session.
export const login = (password: string, signal: AbortSignal) => send("POST", "/api/admin/login", { password }, signal);

export async function checkSession(signal?: AbortSignal): Promise<Result<Session>> {
  const r = await send<Session>("GET", "/api/admin/session", undefined, signal);
  // An aborted check (send gives !ok then) never writes: a newer check owns the token and hint. A 503 is not proof of sign-out: keep the hint.
  if (!r.ok) return r;
  csrf = r.data.authenticated ? r.data.csrf_token : null;
  if (!r.data.authenticated) clearHint();
  return r;
}

export async function logout(signal: AbortSignal) {
  const r = await request("POST", "/api/admin/logout", undefined, signal);
  if (r.ok) csrf = null;
  return r;
}

export type Uploaded = { id: string; width: number; height: number };
export type Attachment = { id: string; alt: string };
export const uploadMedia = (file: File, signal: AbortSignal) => request<Uploaded>("POST", "/api/admin/media", file, signal);
export const createTimeline = (body_markdown: string, attachments: Attachment[], signal: AbortSignal) =>
  request<AdminEntry>(
    "POST",
    "/api/admin/timeline",
    { body_markdown, ...(attachments.length > 0 && { attachments }) },
    signal,
  );
