import { ApiError, CallApi } from "./api.js";
import { VoiceInput } from "./voice.js";
import { BargeGuard } from "./barge_guard.js";
import { urgentText as detectUrgent, pesticideUrgent as detectPesticideUrgent } from "./triage.js";

const $ = (id) => document.getElementById(id);
const SERVICE_NUMBER = "07011223344";
const digitsDisplay = $("dial-number");
const log = $("conversation");
const player = $("audio-player");
let digits = "", api = null, callId = "", state = "idle", voice = null;
let generation = 0, working = false, location = null, preparedLocation = null, audioTail = Promise.resolve();
let audioEpoch = 0, playback = null, latestAssistantText = "", currentRequestId = null;
let interruptPending = Promise.resolve(true), protectedSpeechTail = Promise.resolve();
let pendingBargeSegment = null, pendingBargeTimer = null;
let shortRetryPending = false, shortRetryId = 0;
const bargeGuard = typeof BargeGuard === "undefined" ? null : new BargeGuard();
const completedSegments = new Set();
let audioDone = null, queuedSpeech = [], recentSpeech = [];
const priorityChecks = new Set();
const callerIntents = new Set();
let deferredVoiceFailure = false, deferredCallFailure = null, voiceFailureTimer = null;

for (const [number, letters] of [["1", ""], ["2", "ABC"], ["3", "DEF"], ["4", "GHI"], ["5", "JKL"], ["6", "MNO"], ["7", "PQRS"], ["8", "TUV"], ["9", "WXYZ"], ["*", ""], ["0", "+"], ["#", ""]]) {
  const key = document.createElement("button"); key.className = "key"; key.setAttribute("aria-label", number);
  const n = document.createElement("span"); n.className = "key-number"; n.textContent = number;
  const sub = document.createElement("span"); sub.className = "key-letters"; sub.textContent = letters;
  key.append(n, sub); key.addEventListener("click", () => addDigit(number)); $("keypad").append(key);
}
function addDigit(n) { if (digits.length < 20) digits += n; updateNumber(); }
function updateNumber() {
  digitsDisplay.textContent = digits.length === 11 && /^\d+$/.test(digits) ? `${digits.slice(0,3)}-${digits.slice(3,7)}-${digits.slice(7)}` : digits;
  $("start-call").disabled = !digits;
  $("delete-digit").disabled = !digits;
}
updateNumber();
$("prepare-call").addEventListener("click", () => {
  if (!$("caller-phone").value.trim()) { $("setup-notice").textContent = "내 전화번호를 입력해 주세요."; return; }
  if (!$("speech-notice").checked) { $("setup-notice").textContent = "음성 인식 안내에 동의해 주세요."; return; }
  $("setup-screen").classList.add("hidden");
  $("dial-screen").classList.remove("hidden");
  $("setup-notice").textContent = "";
});
function bubble(text, kind = "assistant") { if (kind !== "user") latestAssistantText = text; const el = document.createElement("div"); el.className = `message ${kind}`; el.textContent = text; log.append(el); log.scrollTop = log.scrollHeight; }
function status(text) { $("call-status").textContent = text; }
function hospitalBubble(hospital, specialty) {
  const item = document.createElement("div"); item.className = "message assistant";
  const name = document.createElement("div"); name.textContent = hospital.name;
  const advice = document.createElement("div"); advice.textContent = "먼저 전화해 보세요.";
  const label = document.createElement("div"); label.textContent = "전화번호는";
  const number = document.createElement("span"); number.className = "hospital-number"; number.textContent = hospital.phone || "확인되지 않았습니다";
  item.append(name);
  if (!specialty) {
    const detail = document.createElement("div");
    detail.textContent = "진료과를 확인하지 못해 가까운 의료기관을 안내해요.";
    item.append(detail);
  }
  if (specialty && hospital.specialty_match !== "yes") {
    const detail = document.createElement("div");
    detail.textContent = hospital.specialty_match === "no"
      ? "추천 진료과 등록 정보가 없어요."
      : "추천 진료과를 확인하지 못했어요.";
    item.append(detail);
  }
  if (specialty && hospital.specialist_count == null) {
    const detail = document.createElement("div"); detail.textContent = "전문의 정보는 확인되지 않았어요."; item.append(detail);
  }
  item.append(advice, label, number, document.createTextNode(hospital.phone ? "입니다." : ".")); log.append(item); log.scrollTop = log.scrollHeight;
}
function stopAudio() { player.pause(); player.removeAttribute("src"); player.load(); if (audioDone) audioDone(); audioDone = null; }
function playBlob(blob, run, epoch = audioEpoch, text = latestAssistantText, kind = "") {
  audioTail = audioTail.catch(() => {}).then(async () => {
    if (run !== generation || epoch !== audioEpoch) return;
    playback = { text, kind };
    bargeGuard?.begin({ text, protectedConsent: kind === "consent" || kind === "consent_retry", emergency: state === "urgent", supported: voice?.bargeSupported !== false });
    const url = URL.createObjectURL(blob);
    try {
      player.src = url;
      return await new Promise((resolve, reject) => {
        audioDone = () => resolve(false);
        player.onended = () => resolve(true);
        player.onerror = () => reject(new Error("audio"));
        player.play().catch(reject);
      });
    } finally {
      audioDone = null; player.onended = null; player.onerror = null; URL.revokeObjectURL(url);
      playback = null; bargeGuard?.end();
    }
  });
  return audioTail;
}
function playPrompt(kind, run) {
  const epoch = audioEpoch, text = latestAssistantText;
  const protectedConsent = kind === "consent" || kind === "consent_retry";
  // Protect before synthesis begins, not only after the audio blob arrives.
  if (protectedConsent) bargeGuard?.begin({ text, protectedConsent: true, emergency: false, supported: voice?.bargeSupported !== false });
  const task = (async () => {
    const path = `/calls/${callId}/prompts/${kind}/audio`;
    const audio = typeof api.audioWithText === "function" ? await api.audioWithText(path) : { blob: await api.audio(path), text: "" };
    await playBlob(audio.blob, run, epoch, audio.text, kind);
  })();
  if (protectedConsent) protectedSpeechTail = task;
  return task;
}
function errorMessage(error) {
  if (error instanceof ApiError && error.status === 401) return "상담 시간이 끝났습니다. 다시 시작해 주세요.";
  return "음성 상담을 이어가기 어렵습니다. 잠시 후 다시 시도해 주세요.";
}
function fail(error) { endCall(); $("dial-notice").textContent = errorMessage(error); }
function clearVoiceFailure() {
  deferredVoiceFailure = false;
  if (voiceFailureTimer !== null) clearTimeout(voiceFailureTimer);
  voiceFailureTimer = null;
}
function voiceFailure(code) {
  voice = null;
  if (callerIntents.size > 0 && state === "accepted") {
    deferredVoiceFailure = true;
    if (voiceFailureTimer !== null) clearTimeout(voiceFailureTimer);
    voiceFailureTimer = setTimeout(() => {
      if (deferredVoiceFailure) fail(new Error("speech"));
    }, 10000);
    return;
  }
  fail(new Error(code));
}
async function interpretCaller(text) {
  const request = Symbol();
  callerIntents.add(request);
  try { return await api.post(`/calls/${callId}/interpret`, { text, subject: "caller" }); }
  finally { callerIntents.delete(request); }
}
function finishVoiceFailure(decision) {
  if (!deferredVoiceFailure) return true;
  if (decision.intent === "revoke_consent" && decision.applied === true) {
    clearVoiceFailure();
    return true;
  }
  fail(new Error("speech"));
  return false;
}
function voicePartial(text, segment) {
  if (!bargeGuard) return;
  const action = bargeGuard.partial(text, segment);
  const callerText = bargeGuard.callerText(text, segment);
  const safetyText = [...recentSpeech.slice(-2), callerText].join(" ");
  if ((action === "urgent" || (recentSpeech.length > 0 && pesticideUrgent(safetyText, { partial: true }))) && state !== "idle" && state !== "urgent" && state !== "declined") {
    const kind = pesticideUrgent(safetyText) ? "pesticide_urgent" : "urgent";
    void urgent(kind, state === "accepted" ? safetyText : "");
    return;
  }
  if (action === "interrupt") {
    audioEpoch += 1;
    stopAudio();
    bubble("음성 안내가 중단됐습니다.", "warning");
    if (currentRequestId && api && callId) {
      const interrupted = currentRequestId;
      const request = api.post(`/calls/${callId}/messages/${encodeURIComponent(interrupted)}/interrupt`, {})
        .then(() => true, () => false);
      interruptPending = Promise.race([request, new Promise((resolve) => setTimeout(() => resolve(false), 5000))]);
    }
    pendingBargeSegment = segment;
  }
  if (pendingBargeSegment !== null && pendingBargeSegment === segment) {
    if (pendingBargeTimer !== null) clearTimeout(pendingBargeTimer);
    const run = generation;
    pendingBargeTimer = setTimeout(async () => {
      pendingBargeTimer = null; pendingBargeSegment = null;
      await retryUnrecognizedBarge(run);
    }, 10000);
  }
}
async function retryUnrecognizedBarge(run) {
  if (run !== generation || state !== "accepted") return;
  if (!await interruptPending) { if (run === generation) fail(new Error("interruption_unconfirmed")); return; }
  if (run !== generation) return;
  bubble("잘 못 들었어요. 다시 말씀해 주세요.");
  try { await playPrompt("intent_retry", run); } catch (error) { if (run === generation) fail(error); }
}
function voiceNoMatch(segment) {
  if (segment !== pendingBargeSegment) return;
  if (pendingBargeTimer !== null) clearTimeout(pendingBargeTimer);
  pendingBargeTimer = null; pendingBargeSegment = null;
  if (Number.isInteger(segment)) completedSegments.add(segment);
  void retryUnrecognizedBarge(generation);
}
async function voiceFinal(text, segment) {
  if (pendingBargeSegment === segment) {
    if (pendingBargeTimer !== null) clearTimeout(pendingBargeTimer);
    pendingBargeTimer = null; pendingBargeSegment = null;
  }
  if (Number.isInteger(segment)) {
    if (completedSegments.has(segment)) return;
    completedSegments.add(segment);
  }
  if (bargeGuard && !bargeGuard.final(text, segment)) {
    if (state === "awaiting_consent" && bargeGuard.takeConsentRetry()) {
      const run = generation, notice = protectedSpeechTail;
      void (async () => {
        try {
          await notice;
          await new Promise((resolve) => setTimeout(resolve, 1250));
          if (run !== generation || state !== "awaiting_consent") return;
          bubble("위치 이용을 포함한 안내 내용 전체에 동의하시나요? 동의 또는 아니라고 말씀해 주세요.");
          await playPrompt("consent_retry", run);
        } catch { /* The call's normal audio error path handles failures. */ }
      })();
    }
    if (state === "awaiting_consent" && bargeGuard.takeConsentExhausted()) {
      const run = generation, notice = protectedSpeechTail;
      void endAfterUnclearSpeech(run, notice);
    }
    if (state === "accepted" && bargeGuard.takeShortRetry()) {
      shortRetryPending = true;
      const run = generation, epoch = audioEpoch, retry = ++shortRetryId, notice = audioTail;
      void (async () => {
        try {
          await notice;
          if (run !== generation || epoch !== audioEpoch || retry !== shortRetryId || state !== "accepted") return;
          bubble("잘 못 들었어요. 다시 말씀해 주세요.");
          await playPrompt("intent_retry", run);
        } catch { /* The active call handles audio errors. */ }
        finally { if (retry === shortRetryId) shortRetryPending = false; }
      })();
    }
    if (state === "accepted" && bargeGuard.takeShortExhausted()) {
      shortRetryPending = true;
      const run = generation, notice = audioTail;
      void endAfterUnclearSpeech(run, notice);
    }
    return;
  }
  shortRetryId += 1; shortRetryPending = false;
  const callerText = bargeGuard ? bargeGuard.callerText(text, segment) : text;
  if (!callerText) {
    if ((!bargeGuard || !bargeGuard.pureEcho(text, segment)) && urgentText(text) && state !== "idle" && state !== "urgent" && state !== "declined") {
      void urgent(pesticideUrgent(text) ? "pesticide_urgent" : "urgent", "");
    }
    return;
  }
  const safetyText = [...recentSpeech.slice(-2), callerText].join(" ");
  if (state !== "idle" && state !== "urgent" && state !== "declined" && urgentText(safetyText)) {
    const kind = pesticideUrgent(safetyText) ? "pesticide_urgent" : "urgent";
    void urgent(kind, state === "accepted" ? safetyText : "");
    return;
  }
  const run = generation;
  if (!await interruptPending) { if (run === generation) fail(new Error("interruption_unconfirmed")); return; }
  if (run !== generation) return;
  void receive(callerText);
}
async function endAfterUnclearSpeech(run, notice) {
  try {
    await notice;
    if (run !== generation || (state !== "accepted" && state !== "awaiting_consent")) return;
    bubble("음성을 확인하지 못해 상담을 마칠게요. 다시 전화해 주세요.", "warning");
    await playPrompt("speech_unavailable", run);
  } catch { /* Stop even when the explanatory audio is unavailable. */ }
  if (run === generation) { endCall(); $("dial-notice").textContent = "음성을 확인하지 못해 상담을 마쳤습니다. 다시 전화해 주세요."; }
}
function endCall() {
  clearVoiceFailure();
  callerIntents.clear();
  priorityChecks.clear();
  deferredCallFailure = null;
  if (pendingBargeTimer !== null) clearTimeout(pendingBargeTimer);
  pendingBargeTimer = null; pendingBargeSegment = null; shortRetryPending = false; shortRetryId += 1;
  generation += 1; audioEpoch += 1; voice?.stop(); voice = null; stopAudio(); bargeGuard?.reset(); completedSegments.clear(); currentRequestId = null; interruptPending = Promise.resolve(true); location = null; preparedLocation = null; api = null; callId = ""; state = "idle"; working = false; queuedSpeech = []; recentSpeech = [];
  $("call-screen").classList.add("hidden"); $("dial-screen").classList.remove("hidden"); $("dial-notice").textContent = "상담을 마쳤습니다.";
}
async function startCall() {
  if (working) return;
  if (digits !== SERVICE_NUMBER) { $("dial-notice").textContent = "070-1122-3344를 입력해 주세요."; return; }
  const phone = $("caller-phone").value.trim();
  if (!phone) { $("dial-notice").textContent = "내 전화번호를 입력해 주세요."; return; }
  if (!$("speech-notice").checked) { $("dial-notice").textContent = "음성 인식 안내에 동의해 주세요."; return; }
  working = true; $("start-call").disabled = true;
  try {
    $("dial-notice").textContent = "기기 권한을 확인하고 있습니다.";
    preparedLocation = await getLocation();
    const result = await new CallApi("").post("/calls", { phone });
    if (!result.call_token) throw new Error("no call token");
    callId = result.id; api = new CallApi(result.call_token); state = result.state; const run = ++generation;
    status("음성 연결 중");
    voice = new VoiceInput({ callId, token: result.call_token, noticeAcknowledged: true, onText: voiceFinal, onPartial: voicePartial, onNoMatch: voiceNoMatch, onError: (code) => { if (run === generation) voiceFailure(code); } });
    await voice.start(); voice.setListening(true);
    if (run !== generation) return;
    if (!voice.bargeSupported) $("voice-help").textContent = "이 기기에서는 안내가 끝난 뒤 말씀해 주세요.";
    $("dial-screen").classList.add("hidden"); $("call-screen").classList.remove("hidden"); log.replaceChildren();
    status("동의 확인"); bubble(result.prompt);
    await playPrompt("consent", run);
  } catch (error) {
    fail(error);
  } finally { working = false; $("start-call").disabled = false; const next = queuedSpeech.shift(); if (next) void receive(next, true); }
}
function urgentText(text) { return detectUrgent(text); }
function pesticideUrgent(text, options) { return detectPesticideUrgent(text, options); }
async function urgent(kind = "urgent", text = "") {
  if (!api || state === "urgent") return;
  const body = (state === "accepted" || state === "location") && text ? { text } : {};
  const run = ++generation; state = "urgent"; location = null; queuedSpeech = []; stopAudio(); voice?.setListening(false); status("긴급 안내");
  void api.post(`/calls/${callId}/urgent`, body).catch(() => {});
  try { bubble(kind === "pesticide_urgent" ? "농약 때문에 위험할 수 있습니다. 지금 119에 전화해 주세요." : "위급할 수 있습니다. 지금 119나 가까운 응급실에 연락해 주세요.", "warning"); await playPrompt(kind, run); }
  catch (error) { bubble("위급하면 바로 119에 연락하세요.", "warning"); }
  endCall(); $("dial-notice").textContent = "위급할 수 있습니다. 지금 119나 가까운 응급실에 연락해 주세요.";
}
async function getLocation() {
  if (!navigator.geolocation) return null;
  return new Promise((resolve) => navigator.geolocation.getCurrentPosition(
    (p) => resolve({ latitude: p.coords.latitude, longitude: p.coords.longitude }), () => resolve(null),
    { enableHighAccuracy: false, timeout: 10000, maximumAge: 0 }));
}
async function revokeLocation(run, applied = false) {
  location = null; preparedLocation = null;
  voice?.setListening(false);
  if (!applied) await api.post(`/calls/${callId}/location-consent`, { accepted: false });
  if (run !== generation) return;
  voice?.setListening(true);
  bubble("위치 없이 상담을 계속할게요."); await playPrompt("location_declined", run);
  if (run === generation) { voice?.setListening(true); status("상담 중"); }
}
async function revokeConsent(run, applied = false) {
  location = null; preparedLocation = null;
  voice?.stop(); voice = null;
  const result = applied ? { state: "declined" } : await api.post(`/calls/${callId}/revoke`, {});
  if (run !== generation) return;
  if (result.state !== "declined") throw new Error("revocation_unconfirmed");
  bubble("동의를 철회하고 상담을 마칠게요. 이미 전달된 내용은 자동 회수되지 않아요.");
  try { await playPrompt("revoked", run); } catch { /* Do not continue after revocation. */ }
  endCall(); $("dial-notice").textContent = "동의를 철회했습니다. 이미 전달된 내용은 자동 회수되지 않습니다.";
}
async function checkPriorityIntent(text, run) {
  let decision;
  try { decision = await interpretCaller(text); }
  catch {
    if (deferredVoiceFailure && callerIntents.size === 0) fail(new Error("speech"));
    return; // Leave the speech in the queue for the ordinary retry path.
  }
  if (run !== generation || state !== "accepted") return;
  if (!finishVoiceFailure(decision)) return;
  if (decision.intent !== "revoke_location" && decision.intent !== "revoke_consent") return;
  generation += 1; const revokeRun = generation;
  queuedSpeech = []; recentSpeech = []; voice?.setListening(false); stopAudio(); status("동의 확인 중");
  try {
    if (decision.intent === "revoke_location") await revokeLocation(revokeRun, decision.applied === true);
    else await revokeConsent(revokeRun, decision.applied === true);
  } catch (error) { if (revokeRun === generation) fail(error); }
  finally { if (revokeRun === generation) working = false; }
}
async function receive(raw, alreadyShown = false) {
  const text = String(raw || "").trim(); if (!text || state === "idle" || state === "urgent" || state === "declined") return;
  if (!alreadyShown) {
    bubble(text, "user");
    recentSpeech.push(text); recentSpeech = recentSpeech.slice(-3);
    if (urgentText(recentSpeech.join(" "))) {
      generation += 1; voice?.setListening(false); stopAudio();
      const emergencyText = recentSpeech.join(" ").slice(-10000);
      const kind = pesticideUrgent(emergencyText) ? "pesticide_urgent" : "urgent";
      if (state === "accepted" || state === "location") {
        void api.post(`/calls/${callId}/messages`, { request_id: crypto.randomUUID(), text }).catch(() => {});
      }
      await urgent(kind, emergencyText); return;
    }
  }
  if (working) {
    queuedSpeech.push(text); status("답변 확인 중");
    if (state === "accepted") {
      const check = checkPriorityIntent(text, generation); priorityChecks.add(check);
      try { await check; } finally { priorityChecks.delete(check); }
    }
    return;
  }
  working = true; const run = generation;
  try {
    if (state === "awaiting_consent") {
      const result = await api.post(`/calls/${callId}/consent`, { text });
      if (run !== generation) return;
      if (result.urgent) { await urgent(); return; }
      if (result.state === "accepted") {
        state = "accepted"; recentSpeech = []; location = preparedLocation; preparedLocation = null;
        status("상담 중");
        if (!location) { bubble("위치를 확인하지 못했어요. 상담은 계속할게요.", "warning"); await playPrompt("location_unavailable", run); }
        if (run !== generation) return;
        bubble(result.question_prompt || "어디가 불편하신가요? 말씀해 주세요."); await playPrompt("question", run);
      }
      else if (result.state === "declined") { state = "declined"; preparedLocation = null; status("동의 거절"); bubble(result.prompt); await playPrompt("declined", run); endCall(); $("dial-notice").textContent = "동의하지 않으셔서 상담을 마쳤습니다."; }
      else { bubble("위치 이용을 포함한 안내 내용 전체에 동의하시나요? 동의 또는 아니라고 말씀해 주세요."); await playPrompt("consent_retry", run); }
    } else if (state === "accepted") {
      status("말씀 확인 중");
      const decision = await interpretCaller(text);
      if (run !== generation) return;
      if (!finishVoiceFailure(decision)) return;
      if (decision.intent === "end") { try { await playPrompt("goodbye", run); } catch { /* End even if audio fails. */ } endCall(); return; }
      if (decision.intent === "urgent") { await urgent("urgent", recentSpeech.join(" ").slice(-10000)); return; }
      if (decision.intent === "revoke_location") { await revokeLocation(run, decision.applied === true); return; }
      if (decision.intent === "revoke_consent") { await revokeConsent(run, decision.applied === true); return; }
      if (decision.intent === "recommend") {
        if (!location) { bubble("위치를 확인하지 못했어요. 상담은 계속할게요.", "warning"); await playPrompt("location_unavailable", run); if (run === generation) status("상담 중"); }
        else { status("병원 찾는 중"); await recommend(run, typeof decision.specialty === "string" ? decision.specialty : null); if (run === generation) status("상담 중"); }
        return;
      }
      if (decision.intent !== "consultation") { bubble("잘 못 들었어요. 다시 말씀해 주세요."); await playPrompt("intent_retry", run); if (run === generation) status("상담 중"); return; }
      status("내용 확인 중"); const requestId = crypto.randomUUID();
      currentRequestId = requestId;
      const submitted = await api.post(`/calls/${callId}/messages`, { request_id: requestId, text });
      if (run !== generation) return;
      if (submitted.urgent) { await urgent(typeof submitted.final_text === "string" && submitted.final_text.startsWith("농약 때문에 위험할 수 있습니다.") ? "pesticide_urgent" : "urgent", recentSpeech.join(" ").slice(-10000)); return; }
      await pollTurn(requestId, run);
      if (currentRequestId === requestId) currentRequestId = null;
    }
  } catch (error) {
    if (run === generation) {
      if (callerIntents.size > 0 && priorityChecks.size > 0) {
        deferredCallFailure = error;
        voice?.setListening(false);
        stopAudio();
      } else fail(error);
    }
  }
  finally {
    if (run === generation) {
      await Promise.allSettled([...priorityChecks]);
      if (run !== generation) return;
      if (deferredCallFailure) { fail(deferredCallFailure); return; }
      working = false;
      const next = queuedSpeech.shift();
      if (next) void receive(next, true);
    }
  }
}
async function pollTurn(requestId, run) {
  const epoch = audioEpoch, base = `/calls/${callId}/messages/${encodeURIComponent(requestId)}`; let initial = false, final = false, emergencySince = 0, finalPlayback = null; const fetched = new Set();
  while (run === generation && epoch === audioEpoch) {
    const turn = await api.get(base); if (run !== generation || epoch !== audioEpoch) return;
    if (turn.status === "emergency") { if (!emergencySince) emergencySince = Date.now(); state = "urgent"; location = null; stopAudio(); status("긴급 안내"); }
    if (turn.initial_text && !initial) { initial = true; bubble(turn.initial_text); }
    if (turn.final_text && !final) { final = true; bubble(turn.final_text, turn.status === "emergency" ? "warning" : "assistant"); }
    let tts = {}; try { tts = JSON.parse(turn.tts_json || "{}"); } catch { /* unavailable */ }
    for (const kind of ["low", "emergency", "final"]) if (tts[kind] === "synthesized" && !fetched.has(kind)) {
      fetched.add(kind); const blob = await api.audio(`${base}/audio/${kind}`); if (epoch !== audioEpoch) return;
      const playing = playBlob(blob, run, epoch, kind === "low" ? turn.initial_text : (typeof turn.speech_text === "string" ? turn.speech_text : ""));
      if (kind === "final") finalPlayback = playing;
      void playing.catch(() => {});
    }
    if (turn.status === "emergency" && (tts.emergency === "failed" || (emergencySince && Date.now() - emergencySince > 15000))) {
      try { await playPrompt("urgent", run); } catch { endCall(); $("dial-notice").textContent = "위급할 수 있습니다. 지금 119나 가까운 응급실에 연락해 주세요."; return; }
      endCall(); $("dial-notice").textContent = "위급할 수 있습니다. 지금 119나 가까운 응급실에 연락해 주세요."; return;
    }
    if (["completed", "degraded", "emergency"].includes(turn.status) && !(turn.status === "emergency" && !tts.emergency)) {
      await audioTail;
      if (run !== generation || epoch !== audioEpoch) return;
      if (turn.status === "emergency") { endCall(); $("dial-notice").textContent = "위급할 수 있습니다. 지금 119나 가까운 응급실에 연락해 주세요."; return; }
      if (tts.final !== "synthesized") { endCall(); $("dial-notice").textContent = "답변 음성을 준비하지 못해 상담을 마쳤습니다. 다시 시도해 주세요."; return; }
      if (!finalPlayback || !await finalPlayback) return;
      await api.post(`${base}/playback-complete`, {});
      if (run !== generation || epoch !== audioEpoch) return;
      if (currentRequestId === requestId) currentRequestId = null;
      status(turn.status === "degraded" ? "응답 확인 필요" : "상담 중");
      if (turn.status === "completed" && location && turn.final_text && !queuedSpeech.length && !shortRetryPending) {
        const decision = await api.post(`/calls/${callId}/interpret`, { text: turn.final_text, subject: "assistant" });
        if (run === generation && !queuedSpeech.length && !shortRetryPending && decision.intent === "answer") await recommend(run);
      }
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 900));
  }
}
async function recommend(run, specialty = null) {
  if (!location || run !== generation) return;
  let data;
  try { data = await api.post(`/calls/${callId}/recommendations`, { ...location, specialty, location_consent: true, urgent: false, include_audio: true }); }
  catch (error) { if (run === generation) { bubble("가까운 병원을 확인하지 못했어요. 병원이나 보건소에 문의해 주세요.", "warning"); await playPrompt("hospital_unavailable", run); } return; }
  if (run !== generation) return;
  if (data.status === "urgent") { await urgent(); return; }
  if (data.status === "ok" && data.hospitals?.length) {
    const hospital = data.hospitals[0]; hospitalBubble(hospital, data.specialty);
    if (data.audio_status === "synthesized" && data.audio_base64) { const raw = atob(data.audio_base64); await playBlob(new Blob([Uint8Array.from(raw, c => c.charCodeAt(0))], { type: "audio/mpeg" }), run, audioEpoch, typeof data.speech_text === "string" ? data.speech_text : ""); }
    else { endCall(); $("dial-notice").textContent = "병원 안내 음성을 준비하지 못해 상담을 마쳤습니다. 다시 시도해 주세요."; }
  } else { bubble("가까운 병원을 확인하지 못했어요. 병원이나 보건소에 문의해 주세요.", "warning"); await playPrompt("hospital_unavailable", run); }
}
$("start-call").addEventListener("click", () => void startCall());
$("delete-digit").addEventListener("click", () => { digits = digits.slice(0, -1); updateNumber(); });
document.addEventListener("keydown", (event) => {
  if (state !== "idle" || !$("setup-screen").classList.contains("hidden") || event.target instanceof HTMLInputElement) return;
  if (/^[0-9*#]$/.test(event.key)) addDigit(event.key);
  if (event.key === "Backspace") { digits = digits.slice(0, -1); updateNumber(); }
  if (event.key === "Enter") void startCall();
});
