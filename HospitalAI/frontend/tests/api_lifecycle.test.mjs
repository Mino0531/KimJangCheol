import assert from "node:assert/strict";
import test from "node:test";
import { CallApi } from "../api.js";

test("page close sends a bounded authenticated keepalive end request", async () => {
  const previousFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (path, options) => { requests.push({ path, options }); return { ok: true }; };
  try {
    await new CallApi("call-capability").endCall("call/id", "hangup");
    assert.equal(requests.length, 1);
    assert.equal(requests[0].path, "/calls/call%2Fid/end");
    assert.equal(requests[0].options.keepalive, true);
    assert.equal(requests[0].options.headers.Authorization, "Bearer call-capability");
    assert.deepEqual(JSON.parse(requests[0].options.body), { reason: "hangup" });
  } finally { globalThis.fetch = previousFetch; }
});

test("a stalled request times out and a later request can succeed", async () => {
  const previousFetch = globalThis.fetch;
  let signal;
  globalThis.fetch = async (_path, options) => {
    signal = options.signal;
    return new Promise((_resolve, reject) => options.signal?.addEventListener("abort", () => reject(options.signal.reason)));
  };
  try {
    const api = new CallApi("call-capability", { requestTimeoutMs: 15 });
    const outcome = await Promise.race([
      api.get("/calls/example").then(() => "unexpected success", error => error.name),
      new Promise(resolve => setTimeout(() => resolve("still waiting"), 150)),
    ]);
    assert.equal(outcome, "TimeoutError");
    assert.equal(signal.aborted, true);
    globalThis.fetch = async () => ({ ok: true, json: async () => ({ active: true }) });
    assert.deepEqual(await api.get("/calls/example"), { active: true });
  } finally { globalThis.fetch = previousFetch; }
});

test("the deadline also covers a response body that stops arriving", async () => {
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async (_path, options) => ({
    ok: true,
    json: () => new Promise((_resolve, reject) => options.signal?.addEventListener("abort", () => reject(options.signal.reason))),
  });
  try {
    const api = new CallApi("call-capability", { requestTimeoutMs: 15 });
    const outcome = await Promise.race([
      api.get("/calls/example").then(() => "unexpected success", error => error.name),
      new Promise(resolve => setTimeout(() => resolve("still waiting"), 150)),
    ]);
    assert.equal(outcome, "TimeoutError");
  } finally { globalThis.fetch = previousFetch; }
});
