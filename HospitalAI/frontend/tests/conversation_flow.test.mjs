import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInContext, createContext } from "node:vm";
import test from "node:test";
import { urgentText as detectUrgent, pesticideUrgent as detectPesticideUrgent } from "../triage.js";

async function harness(initialState, responses = {}) {
  const source = (await readFile(new URL("../app.js", import.meta.url), "utf8")).replace(/^import .*;\s*$/gm, "");
  const elements = new Map();
  function element(id = "") {
    if (!elements.has(id)) elements.set(id, {
      value: "", textContent: "", disabled: false,
      classList: { add() {}, remove() {}, contains() { return true; } },
      children: [], append(...children) { this.children.push(...children); }, replaceChildren() {}, setAttribute() {}, addEventListener() {},
      removeAttribute() {}, load() {}, pause() {},
      play() { queueMicrotask(() => this.onended?.()); return Promise.resolve(); },
    });
    return elements.get(id);
  }
  const calls = [];
  const context = createContext({
    document: { getElementById: element, createElement: () => element(Symbol()), addEventListener() {}, createTextNode() {} },
    URL: { createObjectURL() { return "blob:test"; }, revokeObjectURL() {} },
    crypto: { randomUUID() { return "test-request"; } }, setTimeout, clearTimeout, queueMicrotask, console, Blob, atob,
    HTMLInputElement: class {}, ApiError: class extends Error {}, detectUrgent, detectPesticideUrgent,
  });
  runInContext(source, context);
  context.testApi = {
    post(path, body) {
      calls.push({ path, body });
      const result = responses[path.split("/").at(-1)] ?? {};
      if (path.endsWith("/consent") && result.state === "accepted" && result.location_generation === undefined) return Promise.resolve({...result, location_generation:1, location_revision:1});
      if (path.endsWith("/resolve") && result.location_generation === undefined) return Promise.resolve({...result, location_generation:1, location_revision:1});
      if (path.endsWith("/location-context/reset") && result.generation === undefined) return Promise.resolve({...result, generation:2, location_revision:2});
      if (path.endsWith("/location-consent") && result.location_revision === undefined) return Promise.resolve({...result, location_revision:2});
      if (path.endsWith("/interpret") && result.intent === "revoke_location" && result.applied === true && result.location_revision === undefined) return Promise.resolve({...result, location_revision:2});
      if (path.endsWith("/urgent-followup") && result.action === "revoke_location" && result.location_revision === undefined) return Promise.resolve({...result, location_revision:2});
      return Promise.resolve(result);
    },
    get(path) { calls.push({ path }); return Promise.resolve(responses.get ?? {}); },
    audio(path) { calls.push({ path }); return Promise.resolve(new Blob(["voice"])); },
  };
  context.testState = initialState;
  runInContext('api = testApi; callId = "call-test"; state = testState; preparedLocation = {latitude: 35, longitude: 126};', context);
  if (initialState === "accepted") runInContext('regionReference = {location_precision:"region_reference", region_label:"전라남도 화순군 백아면"};', context);
  return { context, calls, elements };
}

test("nearby facility without a known specialty is clearly identified", async () => {
  const { context, elements } = await harness("accepted");
  runInContext('hospitalBubble({name: "가까운병원", phone: "", specialty_match: "unknown"}, null)', context);
  const text = elements.get("conversation").children.at(-1).children.map(child => child?.textContent || "").join(" ");
  assert.match(text, /진료과.*확인하지 못/);
  assert.match(text, /가까운 의료기관/);
});

test("the preparation step requests microphone access before the call and disables capture until use", async () => {
  const { context, elements } = await harness("idle");
  const track = {enabled:true, stop() {}};
  context.navigator = {mediaDevices:{}, geolocation:null};
  context.acquireMicrophone = async () => ({getTracks: () => [track]});
  context.document.getElementById("caller-phone").value = "01000000000";
  context.document.getElementById("speech-notice").checked = true;
  await runInContext("prepareCall()", context);
  assert.equal(track.enabled, false);
  assert.ok(runInContext("preparedMicrophone", context));
});

test("microphone denial stays on preparation and does not start the call", async () => {
  const { context, calls, elements } = await harness("idle");
  context.navigator = {mediaDevices:{}, geolocation:null};
  context.acquireMicrophone = async () => { throw new Error("denied"); };
  context.document.getElementById("caller-phone").value = "01000000000";
  context.document.getElementById("speech-notice").checked = true;
  await runInContext("prepareCall()", context);
  assert.match(elements.get("setup-notice").textContent, /마이크/);
  assert.equal(calls.some(({path}) => path === "/calls"), false);
});

test("one accepted consent starts consultation without asking for location consent again", async () => {
  const { context, calls } = await harness("awaiting_consent", { consent: {state: "accepted", question_prompt: "어디가 불편하신가요?"} });
  await runInContext('receive("네, 동의해요")', context);
  assert.equal(runInContext("state", context), "accepted");
  assert.ok(calls.some(({ path }) => path.endsWith("/prompts/question/audio")));
  assert.equal(calls.some(({ path }) => path.includes("location-consent") || path.endsWith("/prompts/ready/audio")), false);
});

test("unconfirmed device coordinates never trigger a hospital search", async () => {
  const { context, calls } = await harness("accepted");
  await runInContext('location = {latitude:37.65, longitude:126.77}; regionReference = null; recommend(generation)', context);
  assert.equal(calls.some(({path}) => path.endsWith("/recommendations")), false);
});

test("skipping the consent audio records an explicit acceptance without waiting for synthesis", async () => {
  const { context, calls } = await harness("awaiting_consent", {consent: {state:"accepted"}});
  context.testApi.audio = (path) => path.endsWith("/consent/audio") ? new Promise(() => {}) : Promise.resolve(new Blob(["voice"]));
  const playback = runInContext('working = true; playConsentPrompt("consent", generation)', context);
  runInContext('skipConsent()', context);
  await playback;
  await runInContext('working = false; const answer = queuedSpeech.shift(); receive(answer.text, true, answer.requestId)', context);
  assert.equal(calls.find(({path}) => path.endsWith("/consent")).body.text, "동의합니다");
  assert.equal(runInContext("state", context), "accepted");
});

test("a manually entered region takes precedence over device location after consent", async () => {
  const match = {province:"전라남도", district:"화순군", locality:"백아면", latitude:35.165, longitude:127.134};
  const { context, calls, elements } = await harness("awaiting_consent", {
    consent:{state:"accepted"}, resolve:{status:"matched", match},
  });
  context.document.getElementById("caller-region").value = "화순군 백아면";
  await runInContext('receive("동의합니다")', context);
  assert.equal(calls.find(({path}) => path.endsWith("/regions/resolve")).body.text, "화순군 백아면");
  assert.equal(runInContext("location.latitude", context), match.latitude);
  assert.equal(runInContext("regionReference.location_precision", context), "region_reference");
});

test("dialing a number produces a short dual-tone sound", async () => {
  const { context } = await harness("idle");
  const frequencies = [];
  context.AudioContext = class {
    state = "running"; currentTime = 0; destination = {};
    createGain() { return {gain:{setValueAtTime() {}, exponentialRampToValueAtTime() {}}, connect() {}, disconnect() {}}; }
    createOscillator() { const frequency = {}; return {frequency, connect() {}, start() { frequencies.push(frequency.value); }, stop() {}, disconnect() {}}; }
  };
  runInContext('addDigit("5")', context);
  assert.deepEqual(frequencies, [770, 1336]);
});

test("consent mixed with a symptom continues into consultation using the follow-up text", async () => {
  const { context, calls } = await harness("awaiting_consent", {
    consent: {state:"accepted", followup_text:"어제부터 무릎이 아파요."},
    interpret: {intent:"consultation"},
    get: {status:"degraded", final_text:"방문 준비를 도와드릴게요.", tts_json:'{"final":"failed"}'},
  });
  await runInContext('receive("동의합니다. 어제부터 무릎이 아파요.")', context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.find(({path}) => path.endsWith("/interpret")).body.text, "어제부터 무릎이 아파요.");
  assert.equal(calls.find(({path}) => path.endsWith("/messages")).body.text, "어제부터 무릎이 아파요.");
  assert.equal(calls.some(({path}) => path.endsWith("/prompts/question/audio")), false);
});

test("an unaccepted consent never forwards follow-up text into consultation", async () => {
  const { context, calls } = await harness("awaiting_consent", {
    consent: {state:"awaiting_consent", followup_text:"무릎이 아파요."},
  });
  await runInContext('receive("동의하는지 모르겠어요")', context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.some(({path}) => path.endsWith("/interpret") || path.endsWith("/messages")), false);
});

test("an urgent follow-up after consent passes through the safety gate", async () => {
  const { context, calls } = await harness("awaiting_consent", {
    consent: {state:"accepted", followup_text:"숨을 못 쉬어요"},
  });
  await runInContext('receive("동의합니다")', context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(runInContext("state", context), "urgent");
  assert.ok(calls.some(({path}) => path.endsWith("/urgent")));
  assert.equal(calls.some(({path}) => path.endsWith("/interpret")), false);
});

test("a late consent result cannot queue a consultation after the caller ends", async () => {
  const { context, calls } = await harness("awaiting_consent");
  let completeConsent;
  const pending = new Promise(resolve => { completeConsent = resolve; });
  const basePost = context.testApi.post;
  context.testApi.post = (path, body) => path.endsWith("/consent") ? pending : basePost(path, body);
  const accepting = runInContext('receive("동의하고 무릎이 아파요")', context);
  runInContext('endCall("caller")', context);
  completeConsent({state:"accepted", followup_text:"무릎이 아파요"});
  await accepting;
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(runInContext("state", context), "idle");
  assert.equal(calls.some(({path}) => path.endsWith("/messages") || path.endsWith("/interpret")), false);
});

test("location revocation during urgent support clears local location without ending support", async () => {
  const { context } = await harness("urgent", {
    "urgent-followup": {action:"revoke_location", prompt:"위치는 사용하지 않고 도와드릴게요.", prompt_kind:"urgent_help"},
  });
  runInContext('urgentInteractive = true; locationAllowed = true; location = preparedLocation; deviceLocation = preparedLocation; regionReference = {region_label:"동면"};', context);
  await runInContext('receiveUrgent("위치는 쓰지 마세요")', context);
  assert.equal(runInContext("state", context), "urgent");
  assert.equal(runInContext("location", context), null);
  assert.equal(runInContext("deviceLocation", context), null);
  assert.equal(runInContext("locationAllowed", context), false);
});

test("contextual intent can end the call without matching a keyword", async () => {
  const { context, calls } = await harness("accepted", { interpret: {intent: "end"} });
  await runInContext('receive("오늘은 여기까지만 할게요")', context);
  assert.equal(runInContext("state", context), "idle");
  assert.equal(calls.find(({ path }) => path.endsWith("/end")).body.reason, "caller");
  assert.equal(calls.some(({ path }) => path.endsWith("/messages")), false);
});

test("an uncertain intent asks again without creating a consultation or recommendation", async () => {
  const { context, calls } = await harness("accepted", { interpret: {intent: "uncertain"} });
  await runInContext('receive("그건 좀")', context);
  assert.ok(calls.some(({ path }) => path.endsWith("/prompts/intent_retry/audio")));
  assert.equal(calls.some(({ path }) => path.endsWith("/messages") || path.endsWith("/recommendations")), false);
});

test("a caller's explicit department is passed to the hospital search", async () => {
  const { context, calls } = await harness("accepted", {
    interpret: {intent: "recommend", specialty: "정형외과"},
    recommendations: {status: "unavailable"},
  });
  runInContext('location = preparedLocation;', context);
  await runInContext('receive("정형외과요")', context);
  const request = calls.find(({ path }) => path.endsWith("/recommendations"));
  assert.equal(request.body.specialty, "정형외과");
});

test("a request for another hospital forwards the contextual recommendation mode", async () => {
  const { context, calls } = await harness("accepted", {
    interpret: {intent: "recommend", specialty: null, recommendation_mode: "next"},
    recommendations: {status: "unavailable"},
  });
  runInContext('location = preparedLocation;', context);
  await runInContext('receive("다른 병원 알려주세요")', context);
  assert.equal(calls.find(({ path }) => path.endsWith("/recommendations")).body.mode, "next");
});

test("a pure hold request keeps the call open without making a consultation", async () => {
  const { context, calls } = await harness("accepted", {
    interpret: {intent: "consultation", voice_controls: {hold: true, control_only: true}},
  });
  await runInContext('receive("잠깐만 기다려 주세요")', context);
  assert.equal(runInContext("state", context), "accepted");
  assert.equal(calls.some(({path}) => path.endsWith("/messages")), false);
  assert.ok(calls.some(({path}) => path.endsWith("/prompts/hold/audio")));
});

test("a request to repeat more slowly actually slows the cached audio", async () => {
  const { context, calls, elements } = await harness("accepted", {
    interpret: {intent: "consultation", voice_controls: {repeat_target: "last", speed: "slow", control_only: true}},
  });
  runInContext('lastRepeatAudio = {blob: new Blob(["voice"]), text: "안내 문장입니다.", kind: "final", speed: 1};', context);
  await runInContext('receive("천천히 다시 말해 주세요")', context);
  assert.equal(calls.find(({path}) => path.endsWith("/speech-speed")).body.speed, "slow");
  assert.equal(elements.get("audio-player").playbackRate, 0.85);
  assert.equal(calls.some(({path}) => path.endsWith("/messages")), false);
});

test("a spoken locality triggers a search using an approximate region reference", async () => {
  const { context, calls } = await harness("accepted", {
    interpret: {intent: "consultation", region_text: "화순군 동면", region_only: true},
    resolve: {status: "matched", match: {province: "전라남도", district: "화순군", locality: "동면", latitude: 35.1, longitude: 127.1}},
    recommendations: {status: "unavailable"},
  });
  runInContext('locationAllowed = true; location = null; pendingRegion = {query:"", specialty:"내과", mode:"new"};', context);
  await runInContext('receive("화순군 동면이요")', context);
  const request = calls.find(({path}) => path.endsWith("/recommendations"));
  assert.equal(request.body.location_precision, "region_reference");
  assert.equal(request.body.region_label, "전라남도 화순군 동면");
  assert.equal(request.body.latitude, 35.1);
});

test("changing location invalidates previous recommendations before using the new region", async () => {
  const { context, calls } = await harness("accepted", {
    interpret: {intent:"consultation", region_text:"화순군 동면", region_only:true},
    resolve: {status:"matched", match:{province:"전라남도", district:"화순군", locality:"동면", latitude:35.1, longitude:127.1}},
  });
  runInContext('locationAllowed = true; location = preparedLocation; lastRepeatAudio = {blob:new Blob(["old"]), text:"예전 병원"};', context);
  await runInContext('receive("화순군 동면이요")', context);
  assert.ok(calls.some(({path}) => path.endsWith("/location-context/reset")));
  assert.equal(runInContext("lastRepeatAudio", context), null);
});

test("a proxy caller's GPS is not used to recommend hospitals for another person", async () => {
  const { context, calls } = await harness("accepted", {
    interpret: {intent: "recommend", location_subject: "other", specialty: "내과"},
  });
  runInContext('locationAllowed = true; location = preparedLocation;', context);
  await runInContext('receive("다른 동네에 계신 어머니 병원 찾아주세요")', context);
  assert.equal(calls.some(({path}) => path.endsWith("/recommendations")), false);
  assert.ok(calls.some(({path}) => path.endsWith("/prompts/region_request/audio")));
  assert.ok(calls.some(({path}) => path.endsWith("/location-context/reset")));
});

test("a follow-up about the same proxy patient preserves the confirmed location", async () => {
  const { context, calls } = await harness("accepted", {
    interpret: {intent:"consultation", location_subject:"other", voice_controls:{hold:true, control_only:true}},
  });
  runInContext('locationAllowed = true; proxySubjectActive = true; proxyLocationBlocked = true; location = {latitude:35.1, longitude:127.1};', context);
  await runInContext('receive("어머니가 잠깐 기다려 달래요")', context);
  assert.equal(calls.some(({path}) => path.endsWith("/location-context/reset")), false);
  assert.equal(runInContext("location.latitude", context), 35.1);
});

test("a failed location reset retries with the same idempotency key", async () => {
  const { context, calls } = await harness("accepted", {interpret:{intent:"consultation", location_subject:"other"}});
  const basePost = context.testApi.post;
  let attempts = 0;
  context.testApi.post = (path, body) => {
    if (!path.endsWith("/location-context/reset")) return basePost(path, body);
    attempts += 1;
    if (attempts === 1) { calls.push({path, body}); return Promise.reject(new Error("network")); }
    return basePost(path, body);
  };
  runInContext('locationAllowed = true; location = preparedLocation;', context);
  await runInContext('receive("어머니가 아프세요")', context);
  const resets = calls.filter(({path}) => path.endsWith("/location-context/reset"));
  assert.equal(resets.length, 2);
  assert.equal(resets[0].body.request_id, resets[1].body.request_id);
  assert.equal(runInContext("activeLocationRevision", context), 2);
  assert.equal(calls.some(({path}) => path.endsWith("/messages")), false);
});

test("a newer server epoch alone never proves the requested patient reset succeeded", async () => {
  const { context, calls } = await harness("accepted", {interpret:{intent:"consultation", location_subject:"other"}});
  const basePost = context.testApi.post;
  context.testApi.post = (path, body) => path.endsWith("/location-context/reset") ? Promise.reject(new Error("lost")) : basePost(path, body);
  context.testApi.get = async () => ({state:"accepted", urgent:false, location_generation:2, location_revision:2});
  runInContext('locationAllowed = true; location = preparedLocation;', context);
  await runInContext('receive("어머니가 아프세요")', context);
  assert.equal(runInContext("state", context), "idle");
  assert.equal(calls.some(({path}) => path.endsWith("/messages")), false);
  assert.ok(calls.some(({path}) => path.endsWith("/prompts/context_unavailable/audio")));
});

test("unconfirmed location reset ends with an audible failure instead of a silent call", async () => {
  const { context, calls } = await harness("accepted", {interpret:{intent:"consultation", location_subject:"other"}});
  const basePost = context.testApi.post;
  context.testApi.post = (path, body) => path.endsWith("/location-context/reset") ? Promise.reject(new Error("lost")) : basePost(path, body);
  context.testApi.get = async () => ({state:"accepted", urgent:false, location_generation:1, location_revision:1});
  runInContext('locationAllowed = true; location = preparedLocation;', context);
  await runInContext('receive("어머니가 아프세요")', context);
  assert.equal(runInContext("state", context), "idle");
  assert.ok(calls.some(({path}) => path.endsWith("/prompts/context_unavailable/audio")));
  assert.equal(calls.some(({path}) => path.endsWith("/messages")), false);
});

test("an already applied location revocation recovers its lost revision from the server", async () => {
  const { context } = await harness("accepted");
  const basePost = context.testApi.post;
  context.testApi.post = (path, body) => path.endsWith("/interpret") ? Promise.resolve({intent:"revoke_location", applied:true}) : basePost(path, body);
  context.testApi.get = async () => ({state:"accepted", urgent:false, location_generation:0, location_revision:2});
  await runInContext('receive("위치는 쓰지 마세요")', context);
  assert.equal(runInContext("activeLocationGeneration", context), 0);
  assert.equal(runInContext("activeLocationRevision", context), 2);
  assert.equal(runInContext("state", context), "accepted");
});

test("repeating an already applied location revocation keeps the call active", async () => {
  const { context } = await harness("accepted", {interpret:{intent:"revoke_location", applied:true, location_revision:2}});
  runInContext('activeLocationGeneration = 0; activeLocationRevision = 2; locationAllowed = false;', context);
  await runInContext('receive("위치는 쓰지 마세요")', context);
  assert.equal(runInContext("state", context), "accepted");
  assert.equal(runInContext("activeLocationRevision", context), 2);
});

test("an explicit change to a different patient invalidates the earlier location", async () => {
  const { context, calls } = await harness("accepted", {
    interpret: {intent:"recommend", location_subject:"other", patient_changed:true},
  });
  runInContext('locationAllowed = true; proxySubjectActive = true; proxyLocationBlocked = true; location = {latitude:35.1, longitude:127.1};', context);
  await runInContext('receive("이번에는 아버지 병원 찾아주세요")', context);
  assert.ok(calls.some(({path}) => path.endsWith("/location-context/reset")));
  assert.equal(calls.some(({path}) => path.endsWith("/recommendations")), false);
});

test("a mixed patient-change utterance asks for the new patient's region again", async () => {
  const { context, calls } = await harness("accepted", {
    interpret: {intent:"recommend", location_subject:"other", patient_changed:true, region_text:"동면", region_only:false},
  });
  runInContext('locationAllowed = true; proxySubjectActive = true; proxyLocationBlocked = true; location = {latitude:35.1, longitude:127.1};', context);
  await runInContext('receive("어머니 말고 동면에 계신 아버지 병원을 알려줘요")', context);
  assert.equal(calls.some(({path}) => path.endsWith("/regions/resolve")), false);
  assert.ok(calls.some(({path}) => path.endsWith("/prompts/region_request/audio")));
  assert.equal(calls.some(({path}) => path.endsWith("/recommendations")), false);
});

test("a new patient's mixed symptom and location is reconfirmed after the generation changes", async () => {
  const { context, calls } = await harness("accepted", {
    interpret: {intent:"consultation", location_subject:"other", patient_changed:true, region_text:"동면", region_only:false},
    get: {status:"completed", final_text:"말씀을 확인할게요.", speech_text:"말씀을 확인할게요.", tts_json:'{"final":"synthesized"}'},
  });
  runInContext('locationAllowed = true; proxySubjectActive = true; proxyLocationBlocked = true; location = {latitude:35.1, longitude:127.1};', context);
  await runInContext('receive("이번에는 동면의 아버지가 무릎이 아프세요")', context);
  assert.equal(calls.some(({path}) => path.endsWith("/messages")), false);
  assert.equal(calls.some(({path}) => path.endsWith("/regions/resolve")), false);
  assert.ok(calls.some(({path}) => path.endsWith("/prompts/patient_reconfirm/audio")));
});

test("the recommendation is acknowledged only after its audio ends", async () => {
  const { context, calls } = await harness("accepted", {
    recommendations: {status:"ok", specialty:"내과", hospitals:[{name:"한 병원", phone:"010", specialty_match:"yes"}], audio_status:"synthesized", audio_base64:"dm9pY2U=", speech_text:"병원 안내입니다.", navigation_token:"token-1"},
  });
  runInContext('location = preparedLocation;', context);
  await runInContext('recommend(generation)', context);
  assert.equal(calls.find(({path}) => path.endsWith("/recommendations/confirm")).body.navigation_token, "token-1");
});

test("caller interpretation and saved consultation share a request identifier", async () => {
  const { context, calls } = await harness("accepted", {
    interpret: {intent:"consultation"},
    get: {status:"degraded", final_text:"준비를 도와드릴게요.", tts_json:'{"final":"failed"}'},
  });
  await runInContext('receive("무릎이 불편해요")', context);
  assert.equal(calls.find(({path}) => path.endsWith("/interpret")).body.request_id,
    calls.find(({path}) => path.endsWith("/messages")).body.request_id);
});

test("a delayed old-generation interpretation cannot promote the utterance", async () => {
  const { context, calls } = await harness("accepted");
  let complete;
  const pending = new Promise(resolve => { complete = resolve; });
  const basePost = context.testApi.post;
  context.testApi.post = (path, body) => path.endsWith("/interpret") ? (calls.push({path, body}), pending) : basePost(path, body);
  const receiving = runInContext('receive("동면에서 무릎이 아파요")', context);
  runInContext('activeLocationGeneration = 2;', context);
  complete({intent:"consultation"});
  await receiving;
  assert.equal(calls.find(({path}) => path.endsWith("/interpret")).body.source_location_generation, 1);
  assert.equal(calls.some(({path}) => path.endsWith("/messages")), false);
});

test("an old-generation final segment is discarded after location changes", async () => {
  const { context, calls } = await harness("accepted");
  runInContext('speechGenerations.set(17, 1); activeLocationGeneration = 2;', context);
  await runInContext('voiceFinal("동면에 있어요", 17)', context);
  assert.equal(calls.some(({path}) => path.endsWith("/interpret") || path.endsWith("/messages")), false);
});

test("an old-generation urgent utterance is discarded without sending or latching it", async () => {
  const { context, calls } = await harness("accepted");
  runInContext('activeLocationGeneration = 2;', context);
  await runInContext('receive("숨을 못 쉬어요", false, "old-urgent", false, 1)', context);
  assert.equal(calls.some(({path}) => path.endsWith("/urgent")), false);
  assert.equal(calls.some(({path}) => path.endsWith("/messages")), false);
});

test("a stale revision is rejected even when both location generations are zero", async () => {
  const { context, calls } = await harness("accepted");
  runInContext('activeLocationGeneration = 0; activeLocationRevision = 4;', context);
  await runInContext('receive("동면에서 숨을 못 쉬어요", false, "old-urgent", false, 0, 2)', context);
  assert.equal(calls.some(({path}) => path.endsWith("/urgent") || path.endsWith("/interpret") || path.endsWith("/messages")), false);
  assert.equal(runInContext("state", context), "accepted");
});

test("a delayed first-revocation utterance cannot survive revoke-reaccept-revoke", async () => {
  const { context, calls } = await harness("accepted");
  let complete;
  const pending = new Promise(resolve => { complete = resolve; });
  const basePost = context.testApi.post;
  context.testApi.post = (path, body) => path.endsWith("/interpret") ? (calls.push({path, body}), pending) : basePost(path, body);
  runInContext('activeLocationGeneration = 0; activeLocationRevision = 2;', context);
  const receiving = runInContext('receive("예전 주소에 살아요")', context);
  runInContext('activeLocationGeneration = 0; activeLocationRevision = 4;', context);
  complete({intent:"consultation"});
  await receiving;
  const request = calls.find(({path}) => path.endsWith("/interpret"));
  assert.equal(request.body.source_location_generation, 0);
  assert.equal(request.body.source_location_revision, 2);
  assert.equal(calls.some(({path}) => path.endsWith("/messages")), false);
});

test("a rejected urgent request retries without text only when the server epoch is unchanged", async () => {
  const { context, calls } = await harness("accepted");
  let attempts = 0;
  const basePost = context.testApi.post;
  context.testApi.get = async () => ({state:"accepted", urgent:false, location_generation:1, location_revision:1});
  context.testApi.post = (path, body) => {
    if (!path.endsWith("/urgent")) return basePost(path, body);
    calls.push({path, body}); attempts += 1;
    return attempts === 1 ? Promise.reject(new Error("failed")) : Promise.resolve({});
  };
  await runInContext('urgent("urgent", "숨을 못 쉬어요", 1, 1)', context);
  await new Promise(resolve => setImmediate(resolve));
  const requests = calls.filter(({path}) => path.endsWith("/urgent"));
  assert.equal(requests.length, 2);
  assert.equal(requests[0].body.text, "숨을 못 쉬어요");
  assert.equal(requests[1].body.text, undefined);
  assert.equal(requests[1].body.source_location_revision, 1);
});

test("a rejected urgent request does not relatch when the server epoch changed", async () => {
  const { context, calls } = await harness("accepted");
  const basePost = context.testApi.post;
  context.testApi.get = async () => ({state:"accepted", urgent:false, location_generation:2, location_revision:3});
  context.testApi.post = (path, body) => {
    if (!path.endsWith("/urgent")) return basePost(path, body);
    calls.push({path, body}); return Promise.reject(new Error("stale"));
  };
  await runInContext('urgent("urgent", "숨을 못 쉬어요", 1, 1)', context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.filter(({path}) => path.endsWith("/urgent")).length, 1);
  assert.equal(runInContext("state", context), "accepted");
  assert.equal(runInContext("activeLocationRevision", context), 3);
  assert.ok(calls.some(({path}) => path.endsWith("/prompts/intent_retry/audio")));
});

test("a delayed old-generation recommendation is never played or confirmed", async () => {
  const { context, calls } = await harness("accepted");
  let complete;
  const pending = new Promise(resolve => { complete = resolve; });
  const basePost = context.testApi.post;
  context.testApi.post = (path, body) => path.endsWith("/recommendations") ? (calls.push({path, body}), pending) : basePost(path, body);
  runInContext('location = preparedLocation;', context);
  const searching = runInContext('recommend(generation)', context);
  runInContext('activeLocationGeneration = 2; location = null;', context);
  complete({status:"ok", hospitals:[{name:"과거 병원"}], audio_status:"synthesized", audio_base64:"dm9pY2U=", navigation_token:"old"});
  await searching;
  assert.equal(calls.find(({path}) => path.endsWith("/recommendations")).body.source_location_generation, 1);
  assert.equal(calls.some(({path}) => path.endsWith("/recommendations/confirm")), false);
});

test("a delayed old-generation region match does not trigger a hospital search", async () => {
  const { context, calls } = await harness("accepted", {interpret:{intent:"consultation", region_text:"동면", region_only:true}});
  let complete;
  const pending = new Promise(resolve => { complete = resolve; });
  const basePost = context.testApi.post;
  context.testApi.post = (path, body) => path.endsWith("/resolve") ? (calls.push({path, body}), pending) : basePost(path, body);
  runInContext('locationAllowed = true; location = null; pendingRegion = {query:"", specialty:"내과", mode:"new"};', context);
  const receiving = runInContext('receive("동면이에요")', context);
  await new Promise(resolve => setImmediate(resolve));
  runInContext('activeLocationGeneration = 2;', context);
  complete({status:"matched", location_generation:1, match:{province:"전라남도", district:"화순군", locality:"동면", latitude:35.1, longitude:127.1}});
  await receiving;
  assert.equal(calls.find(({path}) => path.endsWith("/resolve")).body.source_location_generation, 1);
  assert.equal(calls.some(({path}) => path.endsWith("/recommendations")), false);
});

test("a server missing the generation contract cannot start a clinical consultation", async () => {
  const { context, calls } = await harness("awaiting_consent");
  const basePost = context.testApi.post;
  context.testApi.post = (path, body) => path.endsWith("/consent") ? Promise.resolve({state:"accepted", followup_text:"무릎이 아파요"}) : basePost(path, body);
  await runInContext('receive("동의합니다. 무릎이 아파요")', context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(runInContext("state", context), "idle");
  assert.equal(calls.some(({path}) => path.endsWith("/interpret") || path.endsWith("/messages")), false);
});

test("renewing location consent starts a fresh generation and asks for location", async () => {
  const { context, calls } = await harness("accepted", {
    interpret: {intent:"accept_location", applied:true, location_generation:2, location_revision:3},
  });
  runInContext('activeLocationGeneration = 0; locationAllowed = false; location = null;', context);
  await runInContext('receive("위치 이용 다시 동의할게요")', context);
  assert.equal(runInContext("activeLocationGeneration", context), 2);
  assert.equal(runInContext("activeLocationRevision", context), 3);
  assert.equal(runInContext("locationAllowed", context), true);
  assert.ok(calls.some(({path}) => path.endsWith("/prompts/region_request/audio")));
  assert.equal(calls.find(({path}) => path.endsWith("/interpret")).body.source_location_generation, 0);
});

test("urgent support resumes listening after an STT generation boundary reconnect", async () => {
  const { context } = await harness("idle");
  let input;
  context.navigator = {geolocation:null};
  context.VoiceInput = class {
    constructor(options) { this.options = options; input = this; }
    async start() {}
    setListening(value) { this.listening = value; }
    stop() {}
  };
  context.CallApi = class {
    async post() { return {id:"call-test", call_token:"token", state:"awaiting_consent", prompt:"동의하시나요?"}; }
    async audio() { return new Blob(["voice"]); }
    async endCall() {}
  };
  context.document.getElementById("caller-phone").value = "01000000000";
  context.document.getElementById("speech-notice").checked = true;
  runInContext('digits = "07011223344";', context);
  await runInContext('startCall()', context);
  runInContext('state = "urgent"; urgentInteractive = true;', context);
  input.setListening(false);
  input.options.onReconnectGap();
  assert.equal(input.listening, true);
  runInContext('endCall()', context);
});

test("silence is checked without relying on a speech-provider no-match event", async () => {
  const { context, calls } = await harness("accepted");
  let scheduled;
  context.setTimeout = (fn) => { scheduled = fn; return 1; };
  runInContext('lastSpeechAt = Date.now() - 21000; scheduleSilenceCheck();', context);
  await scheduled();
  assert.ok(calls.some(({path}) => path.endsWith("/prompts/silence_check/audio")));
  assert.equal(runInContext("state", context), "accepted");
});

test("a hold request delays the silence check for a minute", async () => {
  const { context } = await harness("accepted");
  let delay;
  context.setTimeout = (_fn, ms) => { delay = ms; return 1; };
  runInContext('lastSpeechAt = Date.now(); holdUntil = Date.now() + 60000; scheduleSilenceCheck();', context);
  assert.ok(delay >= 59000 && delay <= 60000);
});

test("a hold request mixed with a symptom still records the consultation", async () => {
  const { context, calls } = await harness("accepted", {
    interpret: {intent: "consultation", voice_controls: {hold: true, control_only: false}},
    get: {status: "degraded", final_text: "방문 준비를 도와드릴게요.", tts_json: '{"final":"failed"}'},
  });
  await runInContext('receive("잠깐만요, 무릎도 아파요")', context);
  assert.equal(calls.find(({path}) => path.endsWith("/messages")).body.text, "잠깐만요, 무릎도 아파요");
});

test("a non-affirmative response does not activate the prepared location", async () => {
  const { context, calls } = await harness("awaiting_consent", { consent: {state: "awaiting_consent"} });
  await runInContext('receive("무슨 이야기예요?")', context);
  assert.equal(runInContext("state", context), "awaiting_consent");
  assert.equal(runInContext("location", context), null);
  assert.ok(calls.some(({ path }) => path.endsWith("/prompts/consent_retry/audio")));
  assert.equal(calls.some(({ path }) => path.endsWith("/recommendations") || path.endsWith("/interpret")), false);
});

test("a request to stop using location revokes the server consent and clears the coordinates", async () => {
  const { context, calls } = await harness("accepted", { interpret: {intent: "revoke_location"}, "location-consent": {state: "declined"} });
  runInContext('location = preparedLocation; preparedLocation = null;', context);
  await runInContext('receive("위치는 쓰지 마세요")', context);
  assert.equal(runInContext("location", context), null);
  const request = calls.find(({ path }) => path.endsWith("/location-consent"));
  assert.equal(request.body.accepted, false);
  assert.equal(calls.some(({ path }) => path.endsWith("/recommendations")), false);
});

test("a request to revoke consent is recorded before the call ends", async () => {
  const { context, calls } = await harness("accepted", { interpret: {intent: "revoke_consent"}, revoke: {state: "declined", delivered_records_recallable: false} });
  await runInContext('receive("동의를 취소할게요")', context);
  assert.equal(runInContext("state", context), "idle");
  assert.ok(calls.some(({ path }) => path.endsWith("/revoke")));
  assert.ok(calls.some(({ path }) => path.endsWith("/prompts/revoked/audio")));
  assert.equal(calls.some(({ path }) => path.endsWith("/messages")), false);
});

test("queued caller speech is processed before any automatic recommendation", async () => {
  const { context, calls } = await harness("accepted", {
    get: {status: "completed", final_text: "확인해 보세요.", tts_json: '{"final":"synthesized"}'},
    interpret: {intent: "answer"},
  });
  runInContext('location = preparedLocation; queuedSpeech = ["위치는 쓰지 마세요"];', context);
  await runInContext('pollTurn("test-request", generation)', context);
  assert.equal(calls.some(({ path }) => path.endsWith("/recommendations")), false);
});

test("a symptom follow-up never triggers automatic hospital search", async () => {
  const { context, calls } = await harness("accepted", {
    get: {status: "completed", final_text: "질문입니다.", speech_text: "질문입니다.",
      tts_json: '{"final":"synthesized"}', guidance: {action: "follow_up", question_id: "fall_walking"}},
    interpret: {intent: "answer"},
  });
  runInContext('location = preparedLocation;', context);
  await runInContext('pollTurn("test-request", generation)', context);
  assert.ok(calls.some(({path}) => path.endsWith("/playback-complete")));
  assert.equal(calls.some(({path}) => path.endsWith("/interpret") || path.endsWith("/recommendations")), false);
});

test("a hospital offer waits for the caller and supplies the next-turn context", async () => {
  const { context, calls } = await harness("accepted", {
    get: {status: "completed", final_text: "병원을 찾아드릴까요?", speech_text: "병원을 찾아드릴까요?",
      tts_json: '{"final":"synthesized"}', guidance: {action: "hospital_search_offer", specialty: "정형외과"}},
    interpret: {intent: "answer"},
  });
  runInContext('location = preparedLocation;', context);
  await runInContext('pollTurn("test-request", generation)', context);
  assert.equal(runInContext("callerContext", context), "hospital_search_offer");
  assert.equal(calls.some(({path}) => path.endsWith("/interpret") || path.endsWith("/recommendations")), false);
});

test("an explicit revocation is processed while an earlier answer is still pending", async () => {
  const { context, calls } = await harness("accepted", {
    interpret: {intent: "revoke_consent"}, revoke: {state: "declined", delivered_records_recallable: false},
  });
  runInContext("working = true", context);
  await runInContext('receive("동의를 취소할게요")', context);
  assert.ok(calls.some(({ path }) => path.endsWith("/revoke")));
  assert.equal(runInContext("state", context), "idle");
});

test("server-applied revocation remains a successful end when speech closes before the intent response", async () => {
  const { context, calls } = await harness("accepted");
  let completeIntent;
  const pending = new Promise((resolve) => { completeIntent = resolve; });
  const basePost = context.testApi.post;
  context.testApi.post = (path, body) => path.endsWith("/interpret") ? pending : basePost(path, body);
  const receiving = runInContext('receive("동의를 취소할게요")', context);
  runInContext('voiceFailure("call_inactive")', context);
  completeIntent({intent: "revoke_consent", applied: true});
  await receiving;
  assert.equal(runInContext("state", context), "idle");
  assert.equal(calls.some(({ path }) => path.endsWith("/revoke")), false);
  assert.ok(calls.some(({ path }) => path.endsWith("/prompts/revoked/audio")));
});

test("an interrupted speech connection does not continue an ordinary consultation", async () => {
  const { context, calls } = await harness("accepted");
  let completeIntent;
  const pending = new Promise((resolve) => { completeIntent = resolve; });
  const basePost = context.testApi.post;
  context.testApi.post = (path, body) => path.endsWith("/interpret") ? pending : basePost(path, body);
  const receiving = runInContext('receive("기침이 나요")', context);
  runInContext('voiceFailure("speech_disconnected")', context);
  completeIntent({intent: "consultation"});
  await receiving;
  assert.equal(runInContext("state", context), "idle");
  assert.equal(calls.some(({ path }) => path.endsWith("/messages")), false);
});

test("a previous call's pending interpretation does not delay a later connection failure", async () => {
  const { context } = await harness("accepted");
  let completeIntent;
  const pending = new Promise((resolve) => { completeIntent = resolve; });
  context.testApi.post = () => pending;
  const receiving = runInContext('receive("기침이 나요")', context);
  runInContext('endCall(); state = "accepted"; api = testApi; callId = "new-call";', context);
  runInContext('voiceFailure("speech_disconnected")', context);
  assert.equal(runInContext("state", context), "idle");
  completeIntent({intent: "consultation"});
  await receiving;
});

test("a revoked turn failing before its pending intent response still plays the revocation notice", async () => {
  const { context, calls } = await harness("accepted");
  let completeRevocation, failTurn, reportTurnRequested;
  const turnRequested = new Promise((resolve) => { reportTurnRequested = resolve; });
  const pendingTurn = new Promise((_, reject) => { failTurn = reject; });
  const pendingRevocation = new Promise((resolve) => { completeRevocation = resolve; });
  const basePost = context.testApi.post;
  let interpretations = 0;
  context.testApi.post = (path, body) => {
    if (!path.endsWith("/interpret")) return basePost(path, body);
    interpretations += 1;
    return interpretations === 1 ? Promise.resolve({intent: "consultation"}) : pendingRevocation;
  };
  context.testApi.get = () => { reportTurnRequested(); return pendingTurn; };
  const consultation = runInContext('receive("기침이 나요")', context);
  await turnRequested;
  const revocation = runInContext('receive("동의를 취소할게요")', context);
  failTurn(new Error("call was revoked"));
  await new Promise((resolve) => setImmediate(resolve));
  completeRevocation({intent: "revoke_consent", applied: true});
  await Promise.all([consultation, revocation]);
  assert.equal(runInContext("state", context), "idle");
  assert.ok(calls.some(({ path }) => path.endsWith("/prompts/revoked/audio")));
});

test("missing final speech keeps the microphone flow open without claiming playback", async () => {
  const { context, calls } = await harness("accepted", { get: { status: "degraded", final_text: "방문 준비를 도와드릴게요.", tts_json: '{"final":"failed"}' } });
  runInContext('currentRequestId = "turn";', context);
  await runInContext('pollTurn("turn", generation)', context);
  assert.equal(runInContext("state", context), "accepted");
  assert.ok(calls.some(({ path }) => path.endsWith("/prompts/service_retry/audio")));
  assert.equal(calls.some(({ path }) => /playback-complete|interrupt|\/end$/.test(path)), false);
});

test("a playback receipt failure preserves an already played answer and continues", async () => {
  const { context, calls } = await harness("accepted", { get: { status: "completed", final_text: "방문 준비 안내", tts_json: '{"final":"synthesized"}' } });
  const basePost = context.testApi.post;
  context.testApi.post = (path, body) => path.endsWith("/playback-complete") ? Promise.reject(new Error("receipt failed")) : basePost(path, body);
  runInContext('currentRequestId = "turn";', context);
  await runInContext('pollTurn("turn", generation)', context);
  assert.equal(runInContext("state", context), "accepted");
  assert.equal(runInContext("currentRequestId", context), null);
  assert.equal(calls.some(({ path }) => /interrupt|\/end$/.test(path)), false);
});

test("hospital audio failure keeps consultation open and does not auto-search again", async () => {
  const { context, calls } = await harness("accepted", { recommendations: { status: "ok", specialty: "내과", hospitals: [{ name: "동네병원", phone: "061-000-0000", specialty_match: "yes", specialist_count: 1 }], audio_status: "unavailable" } });
  runInContext('location = { latitude: 35, longitude: 126 };', context);
  await runInContext('recommend(generation)', context);
  assert.equal(runInContext("state", context), "accepted");
  assert.equal(runInContext("autoRecommendationAttempted", context), true);
  assert.ok(calls.some(({ path }) => path.endsWith("/prompts/service_retry/audio")));
  assert.equal(calls.some(({ path }) => path.endsWith("/end")), false);
});

test("recommendation invites preparation and its context survives a recognition retry", async () => {
  const { context, calls } = await harness("accepted", {
    recommendations: { status: "ok", hospitals: [{ name: "동네병원", phone: "061-000-0000" }], audio_status: "synthesized", audio_base64: "dm9pY2U=", speech_text: "동네병원을 안내합니다." },
    interpret: { intent: "uncertain" },
  });
  runInContext('location = { latitude: 35, longitude: 126 };', context);
  await runInContext('recommend(generation)', context);
  assert.ok(calls.some(({ path }) => path.endsWith("/prompts/hospital_followup/audio")));
  await runInContext('receive("네")', context);
  await runInContext('receive("준비물 알려주세요")', context);
  const intents = calls.filter(({ path }) => path.endsWith("/interpret"));
  assert.equal(intents.length, 2);
  assert.ok(intents.every(({ body }) => body.caller_context === "visit_preparation_offer"));
});

test("later preparation answers do not automatically repeat a completed hospital search", async () => {
  const { context, calls } = await harness("accepted", { get: { status: "completed", final_text: "준비물 안내", tts_json: '{"final":"synthesized"}' }, interpret: { intent: "answer" } });
  runInContext('location = {latitude: 35, longitude: 126}; autoRecommendationAttempted = true;', context);
  await runInContext('pollTurn("next-question", generation)', context);
  assert.equal(calls.some(({ path }) => path.endsWith("/recommendations")), false);
});

test("a transient interpretation failure permits another spoken turn", async () => {
  const { context, calls } = await harness("accepted");
  const basePost = context.testApi.post;
  let attempts = 0;
  context.testApi.post = (path, body) => {
    if (path.endsWith("/interpret")) { attempts += 1; return attempts === 1 ? Promise.reject(new Error("temporary")) : Promise.resolve({ intent: "uncertain" }); }
    return basePost(path, body);
  };
  await runInContext('receive("병원에 뭘 챙겨가요")', context);
  assert.equal(runInContext("state", context), "accepted");
  await runInContext('receive("다시 말씀드릴게요")', context);
  assert.equal(attempts, 2);
  assert.equal(calls.some(({ path }) => path.endsWith("/end")), false);
});

test("an explicit end while an answer is pending cancels the active call once", async () => {
  const { context, calls } = await harness("accepted", { interpret: { intent: "end" } });
  runInContext('working = true; currentRequestId = "pending-answer";', context);
  await runInContext('receive("이제 끊을게요")', context);
  assert.equal(runInContext("state", context), "idle");
  const endings = calls.filter(({ path }) => path.endsWith("/end"));
  assert.equal(endings.length, 1);
  assert.equal(endings[0].body.reason, "caller");
});

test("urgent guidance stays open for bounded support and only ends on caller intent", async () => {
  const { context, calls } = await harness("accepted", {
    "urgent-followup": { action: "continue", prompt_kind: "urgent_support", prompt: "곁에 계신 분에게 도움을 요청해 주세요." },
  });
  await runInContext('urgent("urgent", "숨을 못 쉬겠어요")', context);
  assert.equal(runInContext("state", context), "urgent");
  await runInContext('receive("혼자 있어요")', context);
  assert.ok(calls.some(({ path }) => path.endsWith("/urgent-followup")));
  assert.equal(calls.some(({ path }) => /interpret|recommendations|\/messages$|\/end$/.test(path)), false);
  const basePost = context.testApi.post;
  context.testApi.post = (path, body) => path.endsWith("/urgent-followup")
    ? Promise.resolve({ action: "end", prompt_kind: "urgent_goodbye", prompt: "119에 전화해 주세요." }) : basePost(path, body);
  await runInContext('receive("지금 끊고 연락할게요")', context);
  assert.equal(runInContext("state", context), "idle");
  assert.equal(calls.find(({ path }) => path.endsWith("/end")).body.reason, "caller");
});

test("urgent revocation finishes immediately when the speech socket closes first", async () => {
  const { context, calls } = await harness("accepted");
  await runInContext('urgent("urgent", "숨을 못 쉬겠어요")', context);
  const basePost = context.testApi.post;
  context.testApi.post = (path, body) => {
    if (!path.endsWith("/urgent-followup")) return basePost(path, body);
    runInContext('voiceFailure("speech_disconnected")', context);
    return Promise.resolve({ action: "revoke", prompt_kind: "urgent_revoked", prompt: "동의를 철회했어요." });
  };
  await runInContext('receive("동의 철회할게요")', context);
  assert.equal(runInContext("state", context), "idle");
  assert.equal(calls.some(({ path }) => path.endsWith("/urgent-followup/audio/urgent_revoked")), false);
  assert.equal(calls.some(({ path }) => path.endsWith("/end")), false);
});

test("a late answer cannot resume speech or acknowledgement after hangup", async () => {
  const { context, calls } = await harness("accepted");
  let finish;
  context.testApi.get = () => new Promise((resolve) => { finish = resolve; });
  const polling = runInContext('pollTurn("late", generation)', context);
  runInContext('endCall("hangup")', context);
  finish({ status: "completed", final_text: "늦은 답변", tts_json: '{"final":"synthesized"}' });
  await polling;
  assert.equal(runInContext("state", context), "idle");
  assert.equal(calls.some(({ path }) => /audio\/final|playback-complete/.test(path)), false);
});

test("a server turn left processing cannot keep the caller waiting forever", async () => {
  const { context, calls } = await harness("accepted");
  let now = 0;
  context.Date = { now: () => now };
  context.setTimeout = (callback) => setTimeout(callback, 0);
  context.testApi.get = async () => { now += 150001; return { status: 'processing' }; };
  await assert.rejects(runInContext('pollTurn("abandoned", generation)', context), /answer_timeout/);
  assert.equal(calls.some(({ path }) => path.endsWith('/playback-complete')), false);
});

test("a cancelled server turn stops polling so the recovery path can listen again", async () => {
  const { context } = await harness("accepted", { get: { status: 'cancelled' } });
  await assert.rejects(runInContext('pollTurn("cancelled", generation)', context), /answer_cancelled/);
});

test("unconfirmed barge cancellation offers another spoken turn without ending the call", async () => {
  const { context, calls } = await harness("accepted");
  runInContext('interruptPending = Promise.resolve(false); currentRequestId = "turn";', context);
  await runInContext('retryUnrecognizedBarge(generation)', context);
  assert.equal(runInContext('state', context), 'accepted');
  assert.equal(calls.some(({ path }) => path.endsWith('/end')), false);
  assert.ok(calls.some(({ path }) => path.endsWith('/prompts/service_retry/audio')));
});

test("a final after failed interruption confirmation recovers instead of hanging up", async () => {
  const { context, calls } = await harness("accepted");
  runInContext('interruptPending = Promise.resolve(false); currentRequestId = "turn";', context);
  await runInContext('voiceFinal("무릎이 불편해요", 104)', context);
  assert.equal(runInContext('state', context), 'accepted');
  assert.equal(calls.some(({ path }) => path.endsWith('/end')), false);
  assert.ok(calls.some(({ path }) => path.endsWith('/prompts/service_retry/audio')));
  context.heardAfterRecovery = [];
  runInContext('receive = async (text) => { heardAfterRecovery.push(text); };', context);
  await runInContext('voiceFinal("무릎이 불편해요", 105)', context);
  assert.deepEqual([...context.heardAfterRecovery], ['무릎이 불편해요']);
});

test("an explicit end survives failed interruption acknowledgement", async () => {
  const { context, calls } = await harness("accepted", { interpret: { intent: 'end' } });
  runInContext('interruptPending = Promise.resolve(false); currentRequestId = "turn";', context);
  await runInContext('voiceFinal("이제 끊을게요", 106)', context);
  assert.equal(runInContext('state', context), 'idle');
  assert.equal(calls.find(({ path }) => path.endsWith('/end')).body.reason, 'caller');
});

test("an already heard end is honored when the speech connection closes during intent analysis", async () => {
  const { context, calls } = await harness("accepted");
  let finishIntent;
  const basePost = context.testApi.post;
  context.testApi.post = (path, body) => path.endsWith('/interpret')
    ? new Promise((resolve) => { finishIntent = resolve; }) : basePost(path, body);
  const received = runInContext('receive("이제 끊을게요")', context);
  runInContext('voiceFailure("speech_disconnected")', context);
  finishIntent({ intent: 'end' });
  await received;
  assert.equal(runInContext('state', context), 'idle');
  assert.equal(calls.find(({ path }) => path.endsWith('/end')).body.reason, 'caller');
});

test("an urgent caller's already heard end survives a closing speech connection", async () => {
  const { context, calls } = await harness("urgent");
  runInContext('urgentInteractive = true;', context);
  let finishIntent;
  const basePost = context.testApi.post;
  context.testApi.post = (path, body) => path.endsWith('/urgent-followup')
    ? new Promise((resolve) => { finishIntent = resolve; }) : basePost(path, body);
  const received = runInContext('receive("이제 끊을게요")', context);
  runInContext('voiceFailure("speech_disconnected")', context);
  finishIntent({ action: 'end', prompt_kind: 'urgent_goodbye', prompt: '통화를 마칠게요.' });
  await received;
  assert.equal(runInContext('state', context), 'idle');
  assert.equal(calls.find(({ path }) => path.endsWith('/end')).body.reason, 'caller');
});
