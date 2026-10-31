import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInContext, createContext } from "node:vm";
import test from "node:test";
import { urgentText as detectUrgent, pesticideUrgent as detectPesticideUrgent } from "../triage.js";

async function harness(callState, urgentText) {
  const source = (await readFile(new URL("../app.js", import.meta.url), "utf8"))
    .replace(/^import .*;\s*$/gm, "");
  const elements = new Map();
  function element(id = "") {
    if (!elements.has(id)) elements.set(id, {
      value: "", textContent: "", disabled: false,
      classList: { add() {}, remove() {}, contains() { return true; } },
      append() {}, replaceChildren() {}, setAttribute() {}, addEventListener() {},
      removeAttribute() {}, load() {}, pause() {},
      play() { queueMicrotask(() => this.onended?.()); return Promise.resolve(); },
    });
    return elements.get(id);
  }
  const context = createContext({
    document: { getElementById: element, createElement: () => element(Symbol()), addEventListener() {}, createTextNode() {} },
    URL: { createObjectURL() { return "blob:test"; }, revokeObjectURL() {} },
    crypto: { randomUUID() { return "test-request"; } },
    setTimeout, clearTimeout, queueMicrotask, console, Blob,
    HTMLInputElement: class {}, ApiError: class extends Error {}, detectUrgent, detectPesticideUrgent,
  });
  runInContext(source, context);
  const calls = [];
  context.testApi = {
    post(path, body) { calls.push({ path, body }); return new Promise(() => {}); },
    audio() { return Promise.resolve(new Blob(["voice"])); },
  };
  context.testState = callState;
  context.testText = urgentText;
  runInContext('api = testApi; callId = "call-test"; state = testState;', context);
  return { calls, context };
}

test("urgent speech is sent after consent while audio starts without waiting for the network", async () => {
  const { calls, context } = await harness("accepted", "농약을 뿌렸고 어지러워요");
  const result = runInContext('urgent("urgent", testText)', context);
  await Promise.race([result, new Promise((_, reject) => setTimeout(() => reject(new Error("urgent audio blocked")), 100))]);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].body.text, "농약을 뿌렸고 어지러워요");
});

test("urgent speech is not sent before consent", async () => {
  const { calls, context } = await harness("awaiting_consent", "숨을 못 쉬겠어요");
  const result = runInContext('urgent("urgent", testText)', context);
  await Promise.race([result, new Promise((_, reject) => setTimeout(() => reject(new Error("urgent audio blocked")), 100))]);
  assert.equal(calls.filter(({ path }) => path.endsWith("/urgent")).length, 1);
  assert.deepEqual(Object.keys(calls[0].body), []);
  assert.equal(calls.find(({ path }) => path.endsWith("/end")).body.reason, "safety_stop");
});

test("split urgent speech preserves context and starts audio even when the consultation request stalls", async () => {
  const { calls, context } = await harness("accepted", "어지러워요");
  runInContext('recentSpeech = ["어제 농약을 뿌렸어요"];', context);
  const result = runInContext('receive(testText)', context);
  await Promise.race([result, new Promise((_, reject) => setTimeout(() => reject(new Error("urgent audio blocked")), 100))]);
  const urgentCall = calls.find((call) => call.path.endsWith("/urgent"));
  assert.equal(urgentCall.body.text, "어제 농약을 뿌렸어요 어지러워요");
  assert.ok(calls.some((call) => call.path.endsWith("/messages")));
});

test("pre-consent urgent recognition sends neither the utterance nor a consultation message", async () => {
  const { calls, context } = await harness("awaiting_consent", "숨을 못 쉬겠어요");
  await runInContext('receive(testText)', context);
  assert.equal(calls.filter(({ path }) => path.endsWith("/urgent")).length, 1);
  assert.ok(calls[0].path.endsWith("/urgent"));
  assert.deepEqual(Object.keys(calls[0].body), []);
  assert.equal(calls.some(({ path }) => path.endsWith("/messages") || path.includes("urgent-followup")), false);
});
