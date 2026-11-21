// Built from wherever the page was served, so the app works on any LAN address and port.
const ORIGIN = `${location.protocol}//${document.domain}${location.port ? `:${location.port}` : ""}`;

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status; // the HTTP status, 0 when the server could not be reached
  }
}

async function api(path, { method = "GET", body } = {}) {
  let response;
  try {
    response = await fetch(ORIGIN + path, {
      method,
      credentials: "same-origin",
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError("server unreachable", 0);
  }
  const data = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) throw new ApiError(data?.error ?? response.statusText, response.status);
  return data;
}
