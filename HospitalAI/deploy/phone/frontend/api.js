import { apiPath, callAuthorization } from "./api-path.js";

/** Same-origin API access; the service token remains in the current page only. */
export class ApiError extends Error {
  constructor(status) {
    super(`요청을 처리하지 못했습니다. (${status})`);
    this.status = status;
  }
}

export class CallApi {
  constructor(token) { this.token = token; }

  async request(path, options = {}) {
    const response = await fetch(apiPath(path), {
      ...options,
      headers: { ...callAuthorization(this.token), ...options.headers },
      cache: "no-store",
    });
    if (!response.ok) throw new ApiError(response.status);
    return response;
  }

  async get(path) { return (await this.request(path)).json(); }

  async post(path, body) {
    return (await this.request(path, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    })).json();
  }

  async audio(path) { return (await this.request(path)).blob(); }

  async audioWithText(path) {
    const response = await this.request(path);
    const encoded = response.headers.get("X-Spoken-Text-Base64");
    let text = "";
    if (encoded) {
      try { text = new TextDecoder("utf-8", { fatal: true }).decode(Uint8Array.from(atob(encoded), c => c.charCodeAt(0))); }
      catch { text = ""; }
    }
    return { blob: await response.blob(), text };
  }
}
