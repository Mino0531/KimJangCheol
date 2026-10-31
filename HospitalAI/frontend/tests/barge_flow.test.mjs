import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createContext, runInContext } from "node:vm";
import test from "node:test";
import { urgentText as detectUrgent, pesticideUrgent as detectPesticideUrgent } from "../triage.js";

async function harness(initialState = "accepted") {
  const source = (await readFile(new URL("../app.js", import.meta.url), "utf8")).replace(/^import .*;\s*$/gm, "");
  const { BargeGuard } = await import("../barge_guard.js");
  const elements = new Map();
  let nextUrl = 0;
  const playerLog = [];
  function element(id = "") {
    if (!elements.has(id)) elements.set(id, {
      value: "", textContent: "", disabled: false, src: "", onended: null,
      classList: { add() {}, remove() {}, contains() { return true; } },
      append() {}, replaceChildren() {}, setAttribute() {}, addEventListener() {},
      removeAttribute() { this.src = ""; }, load() {},
      pause() { if (id === "audio-player") playerLog.push("pause"); },
      play() { if (id === "audio-player") playerLog.push(`play:${this.src}`); return Promise.resolve(); },
    });
    return elements.get(id);
  }
  const calls = [];
  const received = [];
  const context = createContext({
    document: { getElementById: element, createElement: () => element(Symbol()), addEventListener() {}, createTextNode() {} },
    URL: { createObjectURL() { return `blob:${++nextUrl}`; }, revokeObjectURL() {} },
    crypto: { randomUUID() { return "request"; } }, setTimeout, clearTimeout, queueMicrotask, console, Blob, atob,
    HTMLInputElement: class {}, ApiError: class extends Error {}, BargeGuard, detectUrgent, detectPesticideUrgent, testReceived: received,
  });
  runInContext(source, context);
  context.testApi = {
    post(path, body) { calls.push({ path, body }); return Promise.resolve({}); },
    audio(path) { calls.push({ path }); return Promise.resolve(new Blob(["audio"])); },
  };
  context.testVoice = { setListening() {}, stop() {} };
  context.testState = initialState;
  runInContext('api = testApi; callId = "test-call"; state = testState; voice = testVoice;', context);
  return { context, calls, received, playerLog, player: element("audio-player") };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

test("a caller partial interrupts current speech and stale queued speech never plays", async () => {
  const { context, playerLog } = await harness();
  const first = runInContext('playBlob(new Blob(["first"]), generation, audioEpoch, "네, 잠시만요.")', context);
  await tick();
  const second = runInContext('playBlob(new Blob(["second"]), generation, audioEpoch, "기다려 주세요.")', context);
  assert.equal(playerLog.filter((x) => x.startsWith("play:")).length, 1);
  runInContext('voicePartial("무릎이 아파요", 17)', context);
  await Promise.all([first, second]);
  assert.ok(playerLog.includes("pause"));
  assert.equal(playerLog.filter((x) => x.startsWith("play:")).length, 1);
});

test("a late audio fetch from the interrupted reply is not played", async () => {
  const { context, playerLog } = await harness();
  const epoch = runInContext("audioEpoch", context);
  const run = runInContext("generation", context);
  const first = runInContext('playBlob(new Blob(["first"]), generation, audioEpoch, "네, 잠시만요.")', context);
  await tick();
  runInContext('voicePartial("배가 아파요", 18)', context);
  await first;
  context.oldRun = run; context.oldEpoch = epoch;
  await runInContext('playBlob(new Blob(["late"]), oldRun, oldEpoch, "오래된 답변입니다.")', context);
  assert.equal(playerLog.filter((x) => x.startsWith("play:")).length, 1);
});

test("interim updates for one segment deliver its final transcript once", async () => {
  const { context, received } = await harness();
  runInContext('receive = async (text) => { testReceived.push(text); };', context);
  const first = runInContext('playBlob(new Blob(["first"]), generation, audioEpoch, "네, 확인할게요.")', context);
  await tick();
  runInContext('voicePartial("무릎이", 19); voicePartial("무릎이 아파요", 19);', context);
  await first;
  await runInContext('voiceFinal("무릎이 아파요", 19)', context);
  await runInContext('voiceFinal("무릎이 아파요", 19)', context);
  assert.deepEqual(received, ["무릎이 아파요"]);
});

test("an early affirmative cannot accept before the full consent notice has played", async () => {
  const { context, calls } = await harness("awaiting_consent");
  const prompt = runInContext('playPrompt("consent", generation)', context);
  await tick();
  runInContext('voicePartial("동의합니다", 20);', context);
  await runInContext('voiceFinal("동의합니다", 20)', context);
  await tick();
  assert.equal(calls.some(({ path }) => path.endsWith("/consent")), false);
  assert.equal(runInContext("state", context), "awaiting_consent");
  void prompt.catch(() => {});
});

test("urgent guidance is not silenced by incidental partial speech", async () => {
  const { context, playerLog, player } = await harness("urgent");
  const prompt = runInContext('playPrompt("urgent", generation)', context);
  await tick();
  const pauses = playerLog.filter((entry) => entry === "pause").length;
  runInContext('voicePartial("네", 21)', context);
  assert.equal(playerLog.filter((entry) => entry === "pause").length, pauses);
  player.onended?.();
  await prompt;
});

test("speech recognition of the playing assistant audio is not treated as a caller interruption", async () => {
  const { context, received, playerLog, player } = await harness();
  runInContext('receive = async (text) => { testReceived.push(text); };', context);
  const prompt = runInContext('playBlob(new Blob(["first"]), generation, audioEpoch, "말씀 잘 들었어요. 잠시만요.")', context);
  await tick();
  const pauses = playerLog.filter((entry) => entry === "pause").length;
  runInContext('voicePartial("말씀 잘 들었어요", 22); voicePartial("말씀 잘 들었어요. 잠시만요.", 22);', context);
  await runInContext('voiceFinal("말씀 잘 들었어요. 잠시만요.", 22)', context);
  assert.equal(playerLog.filter((entry) => entry === "pause").length, pauses);
  assert.deepEqual(received, []);
  player.onended?.();
  await prompt;
});

test("a chest-pain question echoed by the loudspeaker does not trigger emergency handling", async () => {
  const { context, calls, player } = await harness();
  const prompt = runInContext('playBlob(new Blob(["first"]), generation, audioEpoch, "가슴이 아프세요?")', context);
  await tick();
  runInContext('voicePartial("가슴이 아프세요?", 123)', context);
  await runInContext('voiceFinal("가슴이 아프세요?", 123)', context);
  assert.equal(runInContext('state', context), 'accepted');
  assert.equal(calls.some(({ path }) => path.endsWith('/urgent')), false);
  player.onended?.();
  await prompt;
});

test("a final danger phrase mixed with part of the playback still triggers emergency handling", async () => {
  const { context, calls } = await harness();
  const prompt = runInContext('playBlob(new Blob(["first"]), generation, audioEpoch, "어디가 불편하신가요?")', context);
  await tick();
  runInContext('voicePartial("불편하신가요", 124)', context);
  await runInContext('voiceFinal("불편하신가요 가슴이 아파요", 124)', context);
  await tick();
  assert.equal(runInContext('state', context), 'urgent');
  assert.equal(calls.some(({ path }) => path.includes('/prompts/urgent')), true);
  await prompt;
});

test("an emergency final is acted on while interruption confirmation is still pending", async () => {
  const { context, calls, playerLog, player } = await harness();
  const basePost = context.testApi.post;
  context.testApi.post = (path, body) => {
    if (path.endsWith("/interrupt")) { calls.push({ path, body }); return new Promise(() => {}); }
    return basePost(path, body);
  };
  runInContext('currentRequestId = "pending-turn";', context);
  const current = runInContext('playBlob(new Blob(["first"]), generation, audioEpoch, "네, 잠시 확인할게요.")', context);
  await tick();
  runInContext('voicePartial("무릎이 아파요", 30)', context);
  await current;
  assert.ok(calls.some(({ path }) => path.endsWith("/interrupt")));
  await runInContext('voiceFinal("숨을 못 쉬어요", 31)', context);
  await tick();
  assert.ok(calls.some(({ path }) => path.endsWith("/urgent")));
  assert.ok(calls.some(({ path }) => path.endsWith("/prompts/urgent/audio")));
  assert.equal(playerLog.filter((x) => x.startsWith("play:")).length, 2);
  player.onended?.();
});

test("early consent while the notice audio is still being fetched cannot be accepted", async () => {
  const { context, calls, player } = await harness("awaiting_consent");
  let resolveNotice;
  const pendingNotice = new Promise((resolve) => { resolveNotice = resolve; });
  const baseAudio = context.testApi.audio;
  context.testApi.audio = (path) => {
    if (path.endsWith("/prompts/consent/audio")) { calls.push({ path }); return pendingNotice; }
    return baseAudio(path);
  };
  context.setTimeout = (fn) => setTimeout(fn, 0);
  const prompt = runInContext('playPrompt("consent", generation)', context);
  await tick();
  runInContext('voicePartial("동의합니다", 32)', context);
  await runInContext('voiceFinal("동의합니다", 32)', context);
  assert.equal(calls.some(({ path }) => path.endsWith("/consent")), false);
  assert.equal(runInContext("state", context), "awaiting_consent");
  resolveNotice(new Blob(["notice"]));
  await tick();
  player.onended?.();
  await prompt;
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(calls.filter(({ path }) => path.endsWith("/prompts/consent_retry/audio")).length, 1);
  assert.equal(calls.some(({ path }) => path.endsWith("/consent")), false);
});

test("the assistant's echoed 119 instruction does not itself trigger an emergency call", async () => {
  const { context, calls, player } = await harness();
  const prompt = runInContext('playBlob(new Blob(["first"]), generation, audioEpoch, "심해지면 119에 연락해 주세요.")', context);
  await tick();
  runInContext('voicePartial("119에 연락해 주세요", 33)', context);
  await runInContext('voiceFinal("119에 연락해 주세요", 33)', context);
  assert.equal(calls.some(({ path }) => path.endsWith("/urgent")), false);
  assert.equal(runInContext("state", context), "accepted");
  player.onended?.();
  await prompt;
});

test("a delayed echo of the assistant's 119 instruction after playback is not an emergency", async () => {
  const { context, calls, player } = await harness();
  const prompt = runInContext('playBlob(new Blob(["first"]), generation, audioEpoch, "심해지면 119에 연락해 주세요.")', context);
  await tick();
  player.onended?.();
  await prompt;
  runInContext('voicePartial("119에 연락해 주세요", 34)', context);
  await tick();
  assert.equal(calls.some(({ path }) => path.endsWith("/urgent")), false);
  assert.equal(runInContext("state", context), "accepted");
});

test("split pesticide emergency bypasses a stalled interruption acknowledgement", async () => {
  const { context, calls, player } = await harness();
  const basePost = context.testApi.post;
  context.testApi.post = (path, body) => {
    if (path.endsWith("/interrupt")) { calls.push({ path, body }); return new Promise(() => {}); }
    return basePost(path, body);
  };
  runInContext('currentRequestId = "pending-turn"; recentSpeech = ["어제 농약을 뿌렸어요"];', context);
  const current = runInContext('playBlob(new Blob(["first"]), generation, audioEpoch, "말씀 잘 들었어요.")', context);
  await tick();
  runInContext('voicePartial("추가로 말씀드릴게요", 35)', context);
  await current;
  assert.ok(calls.some(({ path }) => path.endsWith("/interrupt")));
  runInContext('voicePartial("어지러워요", 36)', context);
  await tick();
  assert.ok(calls.some(({ path }) => path.endsWith("/urgent")));
  assert.ok(calls.some(({ path }) => path.endsWith("/prompts/pesticide_urgent/audio")));
  const urgent = calls.find(({ path }) => path.endsWith("/urgent"));
  assert.match(urgent.body.text, /농약/);
  assert.match(urgent.body.text, /어지러워요/);
  player.onended?.();
});

test("an echoed dizziness question is not attributed to a pesticide caller", async () => {
  const { context, calls, player } = await harness();
  runInContext('recentSpeech = ["어제 농약을 뿌렸어요"];', context);
  const current = runInContext('playBlob(new Blob(["first"]), generation, audioEpoch, "지금 어지러워요?")', context);
  await tick();
  runInContext('voicePartial("어지러워요", 86)', context);
  await tick();
  assert.equal(calls.some(({ path }) => path.endsWith("/urgent")), false);
  player.onended?.();
  await current;
});

function completedTurn() {
  return { status: "completed", final_text: "병원에서 확인해 보세요.", speech_text: "병원에서 확인해 보세요.",
    tts_json: '{"final":"synthesized"}' };
}

test("a final voice is acknowledged only after playback actually ends", async () => {
  const { context, calls, player } = await harness();
  context.testApi.get = async () => completedTurn();
  runInContext('currentRequestId = "turn";', context);
  const polling = runInContext('pollTurn("turn", generation)', context);
  await tick();
  assert.equal(calls.some(({ path }) => path.endsWith("/playback-complete")), false);
  player.onended?.();
  await polling;
  assert.equal(calls.filter(({ path }) => path.endsWith("/playback-complete")).length, 1);
  assert.equal(runInContext("currentRequestId", context), null);
});

test("interrupting a final voice does not acknowledge it as fully played", async () => {
  const { context, calls } = await harness();
  context.testApi.get = async () => completedTurn();
  runInContext('currentRequestId = "turn";', context);
  const polling = runInContext('pollTurn("turn", generation)', context);
  await tick();
  runInContext('voicePartial("배가 아파요", 37)', context);
  await polling;
  assert.equal(calls.some(({ path }) => path.endsWith("/playback-complete")), false);
  runInContext('if (pendingBargeTimer !== null) clearTimeout(pendingBargeTimer)', context);
});

test("an audio playback error does not acknowledge a final voice", async () => {
  const { context, calls, player } = await harness();
  context.testApi.get = async () => completedTurn();
  const polling = runInContext('pollTurn("turn", generation)', context);
  await tick();
  player.onerror?.();
  await assert.rejects(polling, /audio/);
  assert.equal(calls.some(({ path }) => path.endsWith("/playback-complete")), false);
});

test("a final audio fetched after its epoch became stale is never acknowledged", async () => {
  const { context, calls, playerLog } = await harness();
  let releaseAudio;
  context.testApi.get = async () => completedTurn();
  context.testApi.audio = () => new Promise((resolve) => { releaseAudio = resolve; });
  const polling = runInContext('pollTurn("turn", generation)', context);
  await tick();
  runInContext('audioEpoch += 1', context);
  releaseAudio(new Blob(["stale"]));
  await polling;
  assert.equal(playerLog.some((item) => item.startsWith("play:")), false);
  assert.equal(calls.some(({ path }) => path.endsWith("/playback-complete")), false);
});

test("interrupting hospital recommendation audio does not interrupt a completed consultation", async () => {
  const { context, calls, player } = await harness();
  context.testApi.get = async () => completedTurn();
  const basePost = context.testApi.post;
  context.testApi.post = (path, body) => {
    if (path.endsWith("/recommendations")) {
      calls.push({ path, body });
      return Promise.resolve({ status: "ok", hospitals: [{ name: "예시의원", phone: "000-0000-0000" }],
        audio_status: "synthesized", audio_base64: "AA==", speech_text: "예시의원에 먼저 전화해 보세요." });
    }
    return basePost(path, body);
  };
  runInContext('currentRequestId = "turn";', context);
  const polling = runInContext('pollTurn("turn", generation)', context);
  await tick(); player.onended?.(); await polling;
  runInContext('location = { latitude: 35, longitude: 126 };', context);
  const recommendation = runInContext('recommend(generation)', context);
  await tick();
  runInContext('voicePartial("다른 병원은요", 38)', context);
  await recommendation;
  assert.equal(calls.some(({ path }) => path.endsWith("/interrupt")), false);
  runInContext('if (pendingBargeTimer !== null) clearTimeout(pendingBargeTimer)', context);
});

test("a short answer during playback prompts a retry before any automatic hospital recommendation", async () => {
  const { context, calls, player } = await harness();
  context.testApi.get = async () => completedTurn();
  const basePost = context.testApi.post;
  context.testApi.post = (path, body) => {
    if (path.endsWith("/interpret")) { calls.push({ path, body }); return Promise.resolve({ intent: "answer" }); }
    return basePost(path, body);
  };
  runInContext('location = { latitude: 35, longitude: 126 }; currentRequestId = "turn";', context);
  const polling = runInContext('pollTurn("turn", generation)', context);
  await tick();
  runInContext('voicePartial("네", 39)', context);
  await runInContext('voiceFinal("네", 39)', context);
  assert.equal(calls.some(({ path }) => path.endsWith("/prompts/intent_retry/audio")), false);
  assert.equal(calls.some(({ path }) => path.endsWith("/recommendations")), false);
  player.onended?.();
  await tick();
  assert.equal(calls.filter(({ path }) => path.endsWith("/prompts/intent_retry/audio")).length, 1);
  assert.equal(calls.some(({ path }) => path.endsWith("/recommendations")), false);
  player.onended?.();
  await polling;
});

test("repeated early consent receives two retries and then a spoken failure instead of silent hanging", async () => {
  const { context, calls, player } = await harness("awaiting_consent");
  context.setTimeout = (fn) => setTimeout(fn, 0);
  context.testApi.audioWithText = (path) => {
    calls.push({ path });
    return Promise.resolve({ blob: new Blob(["notice"]), text: "안내 내용 전체에 동의하시나요?" });
  };
  const initial = runInContext('playPrompt("consent", generation)', context);
  await tick();
  for (let index = 0; index < 3; index += 1) {
    context.segment = 50 + index;
    runInContext('voicePartial("동의해요", segment)', context);
    await runInContext('voiceFinal("동의해요", segment)', context);
    assert.equal(calls.some(({ path }) => path.endsWith("/consent")), false);
    assert.equal(calls.some(({ path }) => path.endsWith("/prompts/speech_unavailable/audio")), false);
    player.onended?.();
    await new Promise((resolve) => setTimeout(resolve, 15));
    if (index < 2) assert.equal(calls.filter(({ path }) => path.endsWith("/prompts/consent_retry/audio")).length, index + 1);
  }
  await initial;
  assert.equal(calls.filter(({ path }) => path.endsWith("/prompts/speech_unavailable/audio")).length, 1);
  player.onended?.();
  await tick();
  assert.equal(runInContext("state", context), "idle");
});

test("repeated identical assistant echo does not end the consultation", async () => {
  const { context, calls, player } = await harness();
  const prompt = runInContext('playBlob(new Blob(["notice"]), generation, audioEpoch, "확인했습니다. 네.")', context);
  await tick();
  for (let index = 0; index < 4; index += 1) {
    context.segment = 60 + index;
    runInContext('voicePartial("네", segment)', context);
    await runInContext('voiceFinal("네", segment)', context);
  }
  player.onended?.();
  await prompt;
  await tick();
  assert.equal(calls.some(({ path }) => path.endsWith("/prompts/speech_unavailable/audio")), false);
  assert.equal(runInContext("state", context), "accepted");
});

test("repeated unclear short answers keep the consultation open with a bounded retry", async () => {
  const { context, calls, player } = await harness();
  context.testApi.audioWithText = (path) => {
    calls.push({ path });
    return Promise.resolve({ blob: new Blob(["notice"]), text: "어디가 불편하신가요?" });
  };
  const initial = runInContext('playPrompt("question", generation)', context);
  await tick();
  for (let index = 0; index < 3; index += 1) {
    context.segment = 70 + index;
    runInContext('voicePartial("네", segment)', context);
    await runInContext('voiceFinal("네", segment)', context);
    assert.equal(calls.some(({ path }) => path.endsWith("/prompts/speech_unavailable/audio")), false);
    player.onended?.();
    await tick();
    if (index < 2) assert.equal(calls.filter(({ path }) => path.endsWith("/prompts/intent_retry/audio")).length, index + 1);
  }
  await initial;
  assert.equal(calls.filter(({ path }) => path.endsWith("/prompts/listening_retry/audio")).length, 1);
  assert.equal(calls.some(({ path }) => path.endsWith("/recommendations")), false);
  player.onended?.();
  await tick();
  assert.equal(runInContext("state", context), "accepted");
});

test("no-match after barge-in retries promptly after cancellation confirmation and ignores a late final", async () => {
  const { context, calls, received, player } = await harness();
  let confirmInterrupt;
  const pendingInterrupt = new Promise((resolve) => { confirmInterrupt = resolve; });
  const basePost = context.testApi.post;
  context.testApi.post = (path, body) => {
    if (path.endsWith("/interrupt")) { calls.push({ path, body }); return pendingInterrupt; }
    return basePost(path, body);
  };
  runInContext('currentRequestId = "turn"; receive = async (text) => { testReceived.push(text); };', context);
  const original = runInContext('playBlob(new Blob(["first"]), generation, audioEpoch, "네, 잠시 확인할게요.")', context);
  await tick();
  runInContext('voicePartial("배가 아파요", 80)', context);
  await original;
  runInContext('voiceNoMatch(81)', context);
  assert.equal(calls.some(({ path }) => path.endsWith("/prompts/intent_retry/audio")), false);
  runInContext('voiceNoMatch(80)', context);
  await tick();
  assert.equal(calls.some(({ path }) => path.endsWith("/prompts/intent_retry/audio")), false);
  confirmInterrupt({});
  await tick();
  assert.equal(calls.filter(({ path }) => path.endsWith("/prompts/intent_retry/audio")).length, 1);
  await runInContext('voiceFinal("배가 아파요", 80)', context);
  assert.deepEqual(received, []);
  player.onended?.();
});

test("an urgent partial and its identical final do not start duplicate emergency guidance", async () => {
  const { context, calls, player } = await harness();
  runInContext('voicePartial("숨을 못 쉬어요", 90)', context);
  await runInContext('voiceFinal("숨을 못 쉬어요", 90)', context);
  await tick();
  assert.equal(calls.filter(({ path }) => path.endsWith('/urgent')).length, 1);
  assert.equal(calls.some(({ path }) => path.endsWith('/urgent-followup')), false);
  player.onended?.();
});

test("a richer final from the urgent triggering segment updates details without repeating guidance", async () => {
  const { context, calls, player } = await harness();
  runInContext('voicePartial("숨을 못 쉬어요", 91)', context);
  await runInContext('voiceFinal("숨을 못 쉬어요. 가슴도 아파요.", 91)', context);
  await tick();
  const updates = calls.filter(({ path }) => path.endsWith('/urgent'));
  assert.equal(updates.length, 2);
  assert.match(updates[1].body.text, /가슴도 아파요/);
  assert.equal(calls.some(({ path }) => path.endsWith('/urgent-followup')), false);
  player.onended?.();
});

for (const [text, action] of [['이제 끊을게요', 'end'], ['동의를 철회할게요', 'revoke']]) {
  test(`voice callbacks during emergency playback honor ${action} without waiting for the audio`, async () => {
    const { context, calls, playerLog } = await harness();
    const basePost = context.testApi.post;
    context.testApi.post = (path, body) => {
      if (path.endsWith('/urgent-followup')) {
        calls.push({ path, body });
        return Promise.resolve({ action, prompt_kind: 'urgent_goodbye', prompt: '안내' });
      }
      return basePost(path, body);
    };
    context.testApi.audioWithText = async () => ({ blob: new Blob(['audio']), text: '위급할 수 있습니다. 지금 119에 연락해 주세요.' });
    const initial = runInContext('urgent("urgent", "숨을 못 쉬어요")', context);
    await tick();
    context.callerText = text;
    runInContext('voicePartial(callerText, 92)', context);
    await runInContext('voiceFinal(callerText, 92)', context);
    await tick();
    assert.equal(runInContext('state', context), 'idle');
    assert.equal(calls.filter(({ path }) => path.endsWith('/urgent-followup')).length, 1);
    assert.equal(calls.some(({ path }) => path.includes('/audio/urgent_goodbye')), false);
    assert.ok(playerLog.includes('pause'));
    await initial;
  });
}

test("unrecognized speech while listening offers a bounded retry and keeps the call open", async () => {
  const { context, calls, player } = await harness();
  runInContext('voiceNoMatch(110)', context);
  await tick();
  assert.equal(calls.filter(({ path }) => path.endsWith('/prompts/listening_retry/audio')).length, 1);
  runInContext('voiceNoMatch(110); voiceNoMatch(111)', context);
  await tick();
  assert.equal(calls.filter(({ path }) => path.endsWith('/prompts/listening_retry/audio')).length, 1);
  player.onended?.();
  await tick();
  runInContext('voiceNoMatch(112)', context);
  await tick();
  assert.equal(calls.filter(({ path }) => path.endsWith('/prompts/listening_retry/audio')).length, 1);
  assert.equal(runInContext('state', context), 'accepted');
  assert.equal(calls.some(({ path }) => path.endsWith('/end')), false);
});

test("unrecognized playback audio does not produce an interrupting retry prompt", async () => {
  const { context, calls, player } = await harness();
  const playing = runInContext('playBlob(new Blob(["audio"]), generation, audioEpoch, "어디가 불편하신가요?")', context);
  await tick();
  runInContext('voiceNoMatch(113)', context);
  await tick();
  assert.equal(calls.some(({ path }) => path.endsWith('/prompts/listening_retry/audio')), false);
  player.onended?.();
  await playing;
});
