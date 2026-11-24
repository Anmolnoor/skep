/* Phase 10 10.1: client state that outlives one render but not a reload.
   The server stays the truth (I8); this holds only where the operator was —
   e.g. the Inbox row they last reviewed, so coming back lands on the next. */
const clientState = new Map();

export const getState = (key, fallback) => (clientState.has(key) ? clientState.get(key) : fallback);
export const setState = (key, value) => { clientState.set(key, value); };
