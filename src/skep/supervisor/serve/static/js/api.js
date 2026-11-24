/* Phase 10 10.1: the one fetch path. A 401 puts the re-auth state up; any
   other failure throws an ApiError that keeps the real HTTP status, so a view
   can tell "not found" (404) from "broken". */

const TOKEN_KEY = "skep-token";

export const REAUTH = "This browser's access token was rejected (401). "
  + "Paste the token from the server boot log (~/.skep/serve.log) to reconnect.";

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail);
    this.status = status;
  }
}

export function token() { return localStorage.getItem(TOKEN_KEY) || ""; }

export function setToken(value) {
  localStorage.setItem(TOKEN_KEY, value);
  // The cookie is what authenticates EventSource — it cannot set headers.
  document.cookie = `skep_token=${value}; path=/; SameSite=Strict`;
}

export function showLogin(reason = "") {
  document.getElementById("shell").classList.add("hidden");
  document.getElementById("login").classList.remove("hidden");
  document.getElementById("login-error").textContent = reason;
  document.getElementById("token-input").focus();
}

export async function api(method, path, body) {
  const options = { method, headers: { "X-Skep-Token": token() } };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  if (response.status === 401) { showLogin(REAUTH); throw new ApiError(401, "not authenticated"); }
  const text = await response.text();
  let data = text;
  try { data = JSON.parse(text); } catch { /* plain text (diff) stays text */ }
  if (!response.ok) throw new ApiError(response.status, (data && data.detail) || `${response.status}`);
  return data;
}
