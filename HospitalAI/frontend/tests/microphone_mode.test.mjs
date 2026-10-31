import { strict as assert } from "node:assert";
import { test } from "node:test";
import { acquireMicrophone } from "../voice.js";

test("requests system-output echo cancellation when the device supports it", async () => {
  const calls = [];
  const stream = { getAudioTracks: () => [{ getSettings: () => ({ echoCancellation: "all" }) }] };
  const devices = { getUserMedia: async (options) => { calls.push(options); return stream; } };
  const result = await acquireMicrophone(devices);
  assert.equal(result, stream);
  assert.deepEqual(calls[0].audio.echoCancellation, { exact: "all" });
  assert.equal(calls.length, 1);
});

test("falls back to standard cancellation only when all mode is unavailable", async () => {
  const calls = [];
  const stream = { getAudioTracks: () => [{ getSettings: () => ({ echoCancellation: true }) }] };
  const devices = { getUserMedia: async (options) => {
    calls.push(options);
    if (calls.length === 1) throw Object.assign(new Error("unsupported"), { name: "OverconstrainedError" });
    return stream;
  } };
  assert.equal(await acquireMicrophone(devices), stream);
  assert.deepEqual(calls.map((c) => c.audio.echoCancellation), [{ exact: "all" }, true]);
});

test("does not repeat the microphone request after permission is denied", async () => {
  let calls = 0;
  const error = Object.assign(new Error("denied"), { name: "NotAllowedError" });
  const devices = { getUserMedia: async () => { calls += 1; throw error; } };
  await assert.rejects(acquireMicrophone(devices), error);
  assert.equal(calls, 1);
});
