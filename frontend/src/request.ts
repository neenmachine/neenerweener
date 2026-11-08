// The only raw fetch in frontend/src (no_raw_fetch.test.ts enforces it): every request carries its owner's AbortSignal,
// so a superseded or unmounted caller cannot write state late. Public code, so it may sit in the main chunk.
export function fetchWithSignal(url: string, signal: AbortSignal | undefined, init?: RequestInit): Promise<Response> {
  return fetch(url, { ...init, signal });
}
