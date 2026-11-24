/* Phase 10 10.1: the two live-stream shapes.
   streamSse — POST, then read the reply as SSE (fetch carries the token header).
   liveSource — a GET EventSource that comes back after the browser gives up. */
import { ApiError, REAUTH, showLogin, token } from "./api.js";

export async function streamSse(path, body, handlers) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "X-Skep-Token": token(), "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (response.status === 401) { showLogin(REAUTH); throw new ApiError(401, "not authenticated"); }
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch { /* keep the status */ }
    throw new ApiError(response.status, detail);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let event = "message"; let data = null;
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data = JSON.parse(line.slice(6));
      }
      if (data !== null && handlers[event]) handlers[event](data);
    }
  }
}

// Phase 10 R1 D7: a run stream starts from the run's first event on every
// connection (the server keeps no cursor), so a reconnect — the browser's own
// retry or liveSource's reopen — replays what was drawn. Wrap a handler with
// this and each event_id is drawn once across connections.
export function onceByEventId(fn) {
  const seen = new Set();
  return (event) => {
    const id = event && event.event_id;
    if (id) {
      if (seen.has(id)) return;
      seen.add(id);
    }
    fn(event);
  };
}

// The browser retries a dropped EventSource by itself; it gives up for good
// (readyState CLOSED) only on an HTTP error or a wrong content type — a
// daemon restart mid-run. That is the case this reopens, backing off to 30 s,
// until the view closes it. handlers: { eventType: fn(MessageEvent) }.
export function liveSource(url, handlers) {
  let source = null;
  let closed = false;
  let timer = null;
  let delay = 1000;
  const open = () => {
    source = new EventSource(url);
    source.onopen = () => { delay = 1000; };
    for (const [type, fn] of Object.entries(handlers)) source.addEventListener(type, fn);
    source.onerror = () => {
      if (closed || source.readyState !== EventSource.CLOSED) return;
      timer = setTimeout(open, delay);
      delay = Math.min(delay * 2, 30000);
    };
  };
  open();
  return { close() { closed = true; clearTimeout(timer); source.close(); } };
}
