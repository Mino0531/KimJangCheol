/** Same-origin API access; the service token remains in the current page only. */
export class ApiError extends Error {
  constructor(status) {
    super(`요청을 처리하지 못했습니다. (${status})`);
    this.status = status;
  }
}

export class CallApi {
  constructor(token, timeouts = {}) {
    this.token = token;
    this.timeouts = { requestTimeoutMs: 15000, audioTimeoutMs: 45000, recommendationTimeoutMs: 60000, ...timeouts };
  }

  async request(path, options = {}, read = response => response) {
    const controller = new AbortController();
    const timeout = path.endsWith("/recommendations") ? this.timeouts.recommendationTimeoutMs
      : path.includes("/audio") ? this.timeouts.audioTimeoutMs : this.timeouts.requestTimeoutMs;
    const timer = setTimeout(() => controller.abort(new DOMException("응답을 기다리는 시간이 길어졌습니다.", "TimeoutError")), timeout);
    try {
      const response = await fetch(path, {
        ...options, signal: controller.signal,
        headers: { ...(this.token ? { Authorization: `Bearer ${this.token}` } : {}), ...options.headers },
        cache: "no-store",
      });
      if (!response.ok) throw new ApiError(response.status);
      return await read(response);
    } finally { clearTimeout(timer); }
  }

  async get(path) { return this.request(path, {}, response => response.json()); }

  async post(path, body) {
    return this.request(path, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }, response => response.json());
  }

  async audio(path) { return this.request(path, {}, response => response.blob()); }

  async endCall(callId, reason) {
    return this.request(`/calls/${encodeURIComponent(callId)}/end`, {
      method: "POST", keepalive: true,
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reason }),
    });
  }

  async audioWithText(path) {
    return this.request(path, {}, async response => {
      const encoded = response.headers.get("X-Spoken-Text-Base64");
      let text = "";
      if (encoded) {
        try { text = new TextDecoder("utf-8", { fatal: true }).decode(Uint8Array.from(atob(encoded), c => c.charCodeAt(0))); }
        catch { text = ""; }
      }
      return { blob: await response.blob(), text };
    });
  }
}
