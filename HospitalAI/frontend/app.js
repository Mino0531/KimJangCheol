import { ApiError, CallApi } from "./api.js";
import { VoiceInput, acquireMicrophone } from "./voice.js";
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
let callerContext = "general", autoRecommendationAttempted = false;
let urgentInteractive = false;
let urgentResponseSequence = 0, urgentResponseTail = Promise.resolve();
let urgentOrigin = null;
let lastListeningRetryAt = null;
let locationPreparation = null, locationPreparationEpoch = 0, locationAllowed = false;
let pendingRegion = null, lastRepeatAudio = null, capabilityRefreshTimer = null;
let reconnectGapPending = false;
let speechRate = 1;
let regionReference = null, deviceLocation = null, proxyLocationBlocked = false, proxySubjectActive = false;
let silenceTimer = null, lastSpeechAt = 0, holdUntil = 0, silenceNoticeSent = false;
let preparedMicrophone = null, microphonePreparationEpoch = 0, preparingMicrophone = false;
let activeLocationGeneration = 1, activeLocationRevision = 1;
let locationSourceGeneration = 1, locationSourceRevision = 1;
let keypadAudio = null, consentSkipResolve = null;
const speechGenerations = new Map(), speechRevisions = new Map();
function currentLocationEpoch(sourceGeneration, sourceRevision) {
  return Number.isInteger(sourceGeneration) && Number.isInteger(sourceRevision) && sourceGeneration === activeLocationGeneration && sourceRevision === activeLocationRevision;
}
function speechGeneration(segment) {
  if (!Number.isInteger(segment)) return activeLocationGeneration;
  if (!speechGenerations.has(segment)) speechGenerations.set(segment, activeLocationGeneration);
  return speechGenerations.get(segment);
}
function speechRevision(segment) {
  if (!Number.isInteger(segment)) return activeLocationRevision;
  if (!speechRevisions.has(segment)) speechRevisions.set(segment, activeLocationRevision);
  return speechRevisions.get(segment);
}
function retryStaleSpeech() {
  if (state === "accepted") void retryUnrecognizedBarge(generation);
  else if (state === "urgent" && urgentInteractive) { bubble("말씀을 잘 못 들었어요. 다시 말씀해 주세요.", "warning"); voice?.setListening(true); }
}

for (const [number, letters] of [["1", ""], ["2", "ABC"], ["3", "DEF"], ["4", "GHI"], ["5", "JKL"], ["6", "MNO"], ["7", "PQRS"], ["8", "TUV"], ["9", "WXYZ"], ["*", ""], ["0", "+"], ["#", ""]]) {
  const key = document.createElement("button"); key.className = "key"; key.setAttribute("aria-label", number);
  const n = document.createElement("span"); n.className = "key-number"; n.textContent = number;
  const sub = document.createElement("span"); sub.className = "key-letters"; sub.textContent = letters;
  key.append(n, sub); key.addEventListener("click", () => addDigit(number)); $("keypad").append(key);
}
function playKeyTone(digit) {
  const tones = {"1":[697,1209], "2":[697,1336], "3":[697,1477], "4":[770,1209], "5":[770,1336], "6":[770,1477], "7":[852,1209], "8":[852,1336], "9":[852,1477], "*":[941,1209], "0":[941,1336], "#":[941,1477]};
  const AudioContextClass = globalThis.AudioContext || globalThis.webkitAudioContext;
  if (!AudioContextClass || !tones[digit]) return;
  try {
    keypadAudio ||= new AudioContextClass();
    if (keypadAudio.state === "suspended") void keypadAudio.resume().catch(() => {});
    const gain = keypadAudio.createGain(), now = keypadAudio.currentTime;
    gain.gain.setValueAtTime(.07, now); gain.gain.exponentialRampToValueAtTime(.001, now + .09);
    gain.connect(keypadAudio.destination);
    for (const frequency of tones[digit]) {
      const oscillator = keypadAudio.createOscillator(); oscillator.frequency.value = frequency;
      oscillator.connect(gain); oscillator.start(now); oscillator.stop(now + .09);
      oscillator.onended = () => oscillator.disconnect();
    }
    setTimeout(() => gain.disconnect(), 150);
  } catch { /* A muted or unsupported browser must still accept the number. */ }
}
function addDigit(n) { if (digits.length < 20) { digits += n; playKeyTone(n); } updateNumber(); }
function updateNumber() {
  digitsDisplay.textContent = digits.length === 11 && /^\d+$/.test(digits) ? `${digits.slice(0,3)}-${digits.slice(3,7)}-${digits.slice(7)}` : digits;
  $("start-call").disabled = !digits;
  $("delete-digit").disabled = !digits;
}
updateNumber();
async function prepareCall() {
  if (!$("caller-phone").value.trim()) { $("setup-notice").textContent = "내 전화번호를 입력해 주세요."; return; }
  if (!$("speech-notice").checked) { $("setup-notice").textContent = "음성 인식 안내에 동의해 주세요."; return; }
  if (preparingMicrophone) return;
  preparingMicrophone = true; $("prepare-call").disabled = true;
  const epoch = ++microphonePreparationEpoch;
  try {
    $("setup-notice").textContent = "마이크 권한을 확인하고 있어요.";
    const stream = await acquireMicrophone(navigator.mediaDevices);
    if (epoch !== microphonePreparationEpoch) { stream.getTracks().forEach(track => track.stop()); return; }
    stream.getTracks().forEach(track => { track.enabled = false; });
    preparedMicrophone = stream;
    locationPreparation = Promise.resolve(null); preparedLocation = null; deviceLocation = null;
    $("dial-location-status").textContent = $("caller-region").value.trim()
      ? `${$("caller-region").value.trim()} 대표 위치를 기준으로 찾아요.`
      : "병원을 찾을 때 계신 읍·면·동을 확인할게요.";
    $("setup-screen").classList.add("hidden");
    $("dial-screen").classList.remove("hidden");
    $("setup-notice").textContent = "";
  } catch {
    if (epoch === microphonePreparationEpoch) $("setup-notice").textContent = "마이크를 사용할 수 없어요. 브라우저의 마이크 권한을 확인해 주세요.";
  } finally { preparingMicrophone = false; $("prepare-call").disabled = false; }
}
$("prepare-call").addEventListener("click", () => void prepareCall());
$("use-demo-region").addEventListener("click", () => { $("caller-region").value = "화순군 백아면"; });
$("caller-phone").addEventListener("beforeinput", (event) => { if (/^[0-9*#]$/.test(event.data || "")) playKeyTone(event.data); });
function bubble(text, kind = "assistant") {
  if (kind !== "user") latestAssistantText = text;
  const el = document.createElement("div"); el.className = `message ${kind}`;
  el.textContent = typeof Intl.Segmenter === "function"
    ? [...new Intl.Segmenter("ko", { granularity: "sentence" }).segment(text)].map(part => part.segment.trim()).join("\n")
    : text;
  log.append(el); log.scrollTop = log.scrollHeight;
}
function status(text) {
  $("call-status").textContent = text;
  let help;
  if (state === "accepted") help = [voice?.bargeSupported === false ? "안내가 끝나면 말씀해 주세요." : "안내 중에도 말씀하실 수 있어요.", "다시 듣고 싶으면 말씀해 주세요."];
  else if (state === "urgent") help = ["도움이 필요한 점을 말씀해 주세요.", "통화를 마치려면 끊겠다고 말씀해 주세요."];
  else help = ["안내를 읽고 동의하시면 버튼을 누르거나 말씀해 주세요."];
  $("voice-help").replaceChildren(...help.map((line) => { const span = document.createElement("span"); span.textContent = line; return span; }));
}
function hospitalBubble(hospital, specialty, facilityType = "all") {
  const item = document.createElement("div"); item.className = "message assistant";
  const name = document.createElement("div"); name.textContent = hospital.name;
  const advice = document.createElement("div"); advice.textContent = "먼저 전화해 보세요.";
  const label = document.createElement("div"); label.textContent = "전화번호는";
  const number = document.createElement("span"); number.className = "hospital-number"; number.textContent = hospital.phone || "확인되지 않았습니다";
  item.append(name);
  if (hospital.address) { const address = document.createElement("div"); address.textContent = hospital.address; item.append(address); }
  if (Number.isFinite(hospital.distance_km)) {
    const distance = document.createElement("div");
    const value = hospital.distance_km < .1 ? "100m 미만" : `${hospital.distance_km.toFixed(1)}km`;
    distance.textContent = `${regionReference?.region_label || "확인한 지역"} 기준 직선거리 ${value}`; item.append(distance);
  }
  if (facilityType === "general_hospital") {
    const detail = document.createElement("div"); detail.textContent = "종합병원으로 등록된 기관이에요."; item.append(detail);
  } else if (!specialty) {
    const detail = document.createElement("div");
    detail.textContent = "진료과를 확인하지 못해 가까운 의료기관을 안내해요.";
    item.append(detail);
  }
  if (facilityType !== "general_hospital" && specialty && hospital.specialty_match !== "yes") {
    const detail = document.createElement("div");
    detail.textContent = hospital.specialty_match === "no"
      ? "추천 진료과 등록 정보가 없어요."
      : "추천 진료과를 확인하지 못했어요.";
    item.append(detail);
  }
  if (facilityType !== "general_hospital" && specialty && hospital.specialist_count == null) {
    const detail = document.createElement("div"); detail.textContent = "전문의 정보는 확인되지 않았어요."; item.append(detail);
  }
  item.append(advice, label, number, document.createTextNode(hospital.phone ? "입니다." : ".")); log.append(item); log.scrollTop = log.scrollHeight;
}
function stopAudio() { player.pause(); player.removeAttribute("src"); player.load(); if (audioDone) audioDone(); audioDone = null; }
function playBlob(blob, run, epoch = audioEpoch, text = latestAssistantText, kind = "", rate = 1) {
  audioTail = audioTail.catch(() => {}).then(async () => {
    if (run !== generation || epoch !== audioEpoch) return;
    playback = { text, kind };
    voice?.setReconnectBufferingAllowed?.(false);
    bargeGuard?.begin({ text, protectedConsent: kind === "consent" || kind === "consent_retry", emergency: state === "urgent", supported: voice?.bargeSupported !== false });
    const url = URL.createObjectURL(blob);
    try {
      player.src = url;
      player.playbackRate = rate;
      const played = await new Promise((resolve, reject) => {
        audioDone = () => resolve(false);
        player.onended = () => resolve(true);
        player.onerror = () => reject(new Error("audio"));
        player.play().catch(reject);
      });
      if (played && run === generation && epoch === audioEpoch && (kind === "final" || kind === "hospital")) {
        lastRepeatAudio = { blob, text, kind, speed: speechRate };
      }
      return played;
    } finally {
      audioDone = null; player.onended = null; player.onerror = null; URL.revokeObjectURL(url);
      playback = null; bargeGuard?.end();
      if (run === generation && state === "accepted") {
        voice?.setReconnectBufferingAllowed?.(true);
        lastSpeechAt = Date.now(); scheduleSilenceCheck();
      }
    }
  });
  return audioTail;
}
function playPrompt(kind, run) {
  if (state === "accepted" && ["hospital_followup", "hospital_unavailable"].includes(kind)) callerContext = "visit_preparation_offer";
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
async function playConsentPrompt(kind, run) {
  let resolveSkip;
  const skipped = new Promise((resolve) => { resolveSkip = resolve; });
  consentSkipResolve = resolveSkip;
  try { await Promise.race([playPrompt(kind, run), skipped]); }
  finally { if (consentSkipResolve === resolveSkip) consentSkipResolve = null; }
}
function skipConsent() {
  if (state !== "awaiting_consent" || $("skip-consent").disabled) return;
  $("skip-consent").disabled = true; $("skip-consent").classList.add("hidden");
  audioEpoch += 1; stopAudio(); bargeGuard?.end(); consentSkipResolve?.();
  bubble("동의합니다", "user");
  const answer = {text: "동의합니다", requestId: crypto.randomUUID()};
  if (working) queuedSpeech.unshift(answer);
  else void receive(answer.text, true, answer.requestId);
}
function errorMessage(error) {
  if (error instanceof ApiError && error.status === 401) return "상담 시간이 끝났습니다. 다시 시작해 주세요.";
  return "음성 상담을 이어가기 어렵습니다. 잠시 후 다시 시도해 주세요.";
}
function fail(error) { endCall("connection_lost"); $("dial-notice").textContent = errorMessage(error); }
class LocationEpochSyncError extends Error {}
async function endAfterLocationSyncFailure(run) {
  if (run !== generation || state !== "accepted") return;
  bubble("연결을 확인하지 못했어요. 다시 전화해 주세요.", "warning");
  try { await playPrompt("context_unavailable", run); } catch { /* The dial screen also reports the failure. */ }
  if (run === generation) { endCall("connection_lost"); $("dial-notice").textContent = "연결을 확인하지 못했어요. 다시 전화해 주세요."; }
}
async function resetLocationContext(run, requestId, sourceGeneration, sourceRevision) {
  const resetApi = api, resetCallId = callId;
  const body = {request_id: requestId, source_location_generation: sourceGeneration, source_location_revision: sourceRevision};
  voice?.invalidateRecognitionBoundary?.(); activeLocationGeneration = null; activeLocationRevision = null;
  let result = null;
  for (let attempt = 0; attempt < 2 && !result; attempt += 1) {
    try { result = await resetApi.post(`/calls/${resetCallId}/location-context/reset`, body); }
    catch (error) { if (error instanceof ApiError && [401, 403].includes(error.status)) break; }
    if (run !== generation || api !== resetApi || callId !== resetCallId) return;
  }
  // A later epoch might have been created by another request; only the same
  // idempotent reset request can prove that this patient boundary was applied.
  if (run !== generation || api !== resetApi || callId !== resetCallId) return;
  const nextGeneration = Number.isInteger(result?.location_generation) ? result.location_generation : result?.generation;
  if (!Number.isInteger(nextGeneration) || !Number.isInteger(result?.location_revision) || result.location_revision <= sourceRevision) throw new LocationEpochSyncError("location_epoch_unavailable");
  activeLocationGeneration = nextGeneration; activeLocationRevision = result.location_revision;
  locationAllowed = nextGeneration > 0;
}
async function recoverCall(error, run) {
  if (run !== generation || state !== "accepted") return;
  if (error instanceof LocationEpochSyncError) { await endAfterLocationSyncFailure(run); return; }
  if (error instanceof ApiError && [401, 403].includes(error.status)) { fail(error); return; }
  if (currentRequestId) {
    const request = currentRequestId;
    try { await api.post(`/calls/${callId}/messages/${encodeURIComponent(request)}/interrupt`, { reason: "service_error" }); }
    catch { /* A later utterance can retry; unconfirmed output is never acknowledged. */ }
    if (run !== generation || state !== "accepted") return;
    if (currentRequestId === request) currentRequestId = null;
  }
  interruptPending = Promise.resolve(true);
  bubble("잠시 연결이 고르지 않았어요. 궁금한 점을 다시 말씀해 주세요.", "warning");
  try { await playPrompt("service_retry", run); }
  catch { if (run === generation) status("음성 안내를 확인하지 못했어요. 말씀해 주세요."); }
  if (run === generation && state === "accepted") { voice?.setListening(true); status("상담 중"); }
}
function clearVoiceFailure() {
  deferredVoiceFailure = false;
  if (voiceFailureTimer !== null) clearTimeout(voiceFailureTimer);
  voiceFailureTimer = null;
}
function voiceFailure(code) {
  voice = null;
  if (callerIntents.size > 0 && (state === "accepted" || (state === "urgent" && urgentInteractive))) {
    deferredVoiceFailure = true;
    if (voiceFailureTimer !== null) clearTimeout(voiceFailureTimer);
    voiceFailureTimer = setTimeout(() => {
      if (deferredVoiceFailure) fail(new Error("speech"));
    }, 10000);
    return;
  }
  fail(new Error(code));
}
async function interpretCaller(text, requestId = crypto.randomUUID(), sourceGeneration = activeLocationGeneration, sourceRevision = activeLocationRevision) {
  const request = Symbol();
  callerIntents.add(request);
  try { return await api.post(`/calls/${callId}/interpret`, { text, request_id: requestId, subject: "caller", caller_context: callerContext, source_location_generation: sourceGeneration, source_location_revision: sourceRevision }); }
  finally { callerIntents.delete(request); }
}
function finishVoiceFailure(decision) {
  if (!deferredVoiceFailure) return true;
  if (decision.intent === "end" || (decision.intent === "revoke_consent" && decision.applied === true)) {
    clearVoiceFailure();
    return true;
  }
  fail(new Error("speech"));
  return false;
}
function voicePartial(text, segment) {
  const sourceGeneration = speechGeneration(segment);
  const sourceRevision = speechRevision(segment);
  if (state !== "awaiting_consent" && !currentLocationEpoch(sourceGeneration, sourceRevision)) {
    retryStaleSpeech();
    return;
  }
  if (!bargeGuard) return;
  const action = bargeGuard.partial(text, segment);
  const callerText = bargeGuard.callerText(text, segment);
  const safetyText = [...recentSpeech.slice(-2), callerText].join(" ");
  if ((action === "urgent" || (recentSpeech.length > 0 && pesticideUrgent(safetyText, { partial: true }))) && state !== "idle" && state !== "urgent" && state !== "declined") {
    const kind = pesticideUrgent(safetyText) ? "pesticide_urgent" : "urgent";
    urgentOrigin = Number.isInteger(segment) ? { segment, text: callerText, history: recentSpeech.slice(-2), sourceGeneration, sourceRevision } : null;
    void urgent(kind, state === "accepted" ? safetyText : "", sourceGeneration, sourceRevision);
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
  if (!await interruptPending) { if (run === generation) await recoverCall(new Error("interruption_unconfirmed"), run); return; }
  if (run !== generation) return;
  bubble("잘 못 들었어요. 다시 말씀해 주세요.");
  try { await playPrompt("intent_retry", run); } catch (error) { if (run === generation) await recoverCall(error, run); }
}
function voiceNoMatch(segment, reason) {
  speechGenerations.delete(segment);
  speechRevisions.delete(segment);
  if (reason === "silence") { scheduleSilenceCheck(); return; }
  if (segment !== pendingBargeSegment) {
    if (state !== "accepted" || working || playback || shortRetryPending || completedSegments.has(segment)) return;
    if (Number.isInteger(segment)) completedSegments.add(segment);
    const now = Date.now();
    if (lastListeningRetryAt !== null && now - lastListeningRetryAt < 8000) return;
    lastListeningRetryAt = now;
    shortRetryPending = true;
    void endAfterUnclearSpeech(generation, Promise.resolve());
    return;
  }
  if (pendingBargeTimer !== null) clearTimeout(pendingBargeTimer);
  pendingBargeTimer = null; pendingBargeSegment = null;
  if (Number.isInteger(segment)) completedSegments.add(segment);
  void retryUnrecognizedBarge(generation);
}
async function voiceFinal(text, segment) {
  const sourceGeneration = speechGeneration(segment);
  const sourceRevision = speechRevision(segment);
  speechGenerations.delete(segment);
  speechRevisions.delete(segment);
  if (state !== "awaiting_consent" && !currentLocationEpoch(sourceGeneration, sourceRevision)) {
    retryStaleSpeech();
    return;
  }
  if (pendingBargeSegment === segment) {
    if (pendingBargeTimer !== null) clearTimeout(pendingBargeTimer);
    pendingBargeTimer = null; pendingBargeSegment = null;
  }
  if (Number.isInteger(segment)) {
    if (completedSegments.has(segment)) return;
    completedSegments.add(segment);
  }
  if (urgentOrigin?.segment === segment) {
    const origin = urgentOrigin;
    urgentOrigin = null;
    const callerText = bargeGuard ? bargeGuard.callerText(text, segment) : text;
    bargeGuard?.final(text, segment);
    const normalize = (value) => String(value || '').replace(/[\s\p{P}\p{S}]/gu, '');
    if (state === "urgent" && urgentInteractive && callerText && normalize(callerText) !== normalize(origin.text)) {
      void api.post(`/calls/${callId}/urgent`, { text: [...origin.history, callerText].join(' ').slice(-10000), source_location_generation: origin.sourceGeneration, source_location_revision: origin.sourceRevision }).catch(() => {});
    }
    return;
  }
  if (bargeGuard && !bargeGuard.final(text, segment)) {
    if (state === "urgent" && urgentInteractive) {
      const callerText = bargeGuard.emergencyCallerText(text, segment);
      if (callerText) void receive(callerText, false, crypto.randomUUID(), false, sourceGeneration, sourceRevision);
      return;
    }
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
    void urgent(kind, state === "accepted" ? safetyText : "", sourceGeneration, sourceRevision);
    return;
  }
  if (state === "urgent" && urgentInteractive) { void receive(callerText, false, crypto.randomUUID(), false, sourceGeneration, sourceRevision); return; }
  const run = generation;
  if (!await interruptPending) {
    if (run === generation && state === "accepted") {
      await checkPriorityIntent(callerText, run, crypto.randomUUID(), sourceGeneration, sourceRevision);
      if (run === generation) await recoverCall(new Error("interruption_unconfirmed"), run);
    }
    return;
  }
  if (run !== generation) return;
  void receive(callerText, false, crypto.randomUUID(), false, sourceGeneration, sourceRevision);
}
async function endAfterUnclearSpeech(run, notice) {
  if (state === "accepted") {
    try {
      await notice;
      if (run !== generation || state !== "accepted") return;
      bubble("말씀을 잘 듣지 못했어요. 다시 말씀해 주세요.");
      await playPrompt("listening_retry", run);
    } catch { /* Keep the microphone available when only guidance audio fails. */ }
    finally { if (run === generation) { shortRetryPending = false; status("상담 중"); } }
    return;
  }
  try {
    await notice;
    if (run !== generation || (state !== "accepted" && state !== "awaiting_consent")) return;
    bubble("음성을 확인하지 못해 상담을 마칠게요. 다시 전화해 주세요.", "warning");
    await playPrompt("speech_unavailable", run);
  } catch { /* Stop even when the explanatory audio is unavailable. */ }
  if (run === generation) { endCall("safety_stop"); $("dial-notice").textContent = "음성을 확인하지 못해 상담을 마쳤습니다. 다시 전화해 주세요."; }
}
function endCall(reason = "hangup") {
  if (api && callId && reason) {
    const ending = typeof api.endCall === "function" ? api.endCall(callId, reason) : api.post(`/calls/${callId}/end`, { reason });
    void ending.catch(() => {});
  }
  clearVoiceFailure(); consentSkipResolve?.(); consentSkipResolve = null;
  $("skip-consent").classList.add("hidden"); $("skip-consent").disabled = false;
  microphonePreparationEpoch += 1;
  preparedMicrophone?.getTracks().forEach(track => track.stop()); preparedMicrophone = null;
  locationPreparationEpoch += 1; locationPreparation = null; locationAllowed = false; pendingRegion = null; lastRepeatAudio = null;
  activeLocationGeneration = null; activeLocationRevision = null; locationSourceGeneration = null; locationSourceRevision = null; speechGenerations.clear(); speechRevisions.clear();
  reconnectGapPending = false;
  speechRate = 1;
  regionReference = null; deviceLocation = null; proxyLocationBlocked = false; proxySubjectActive = false;
  if (silenceTimer !== null) clearTimeout(silenceTimer);
  silenceTimer = null; lastSpeechAt = 0; holdUntil = 0; silenceNoticeSent = false;
  if (capabilityRefreshTimer !== null) clearTimeout(capabilityRefreshTimer);
  capabilityRefreshTimer = null;
  callerIntents.clear();
  priorityChecks.clear();
  deferredCallFailure = null;
  if (pendingBargeTimer !== null) clearTimeout(pendingBargeTimer);
  pendingBargeTimer = null; pendingBargeSegment = null; shortRetryPending = false; shortRetryId += 1;
  generation += 1; audioEpoch += 1; voice?.stop(); voice = null; stopAudio(); bargeGuard?.reset(); completedSegments.clear(); currentRequestId = null; interruptPending = Promise.resolve(true); location = null; preparedLocation = null; api = null; callId = ""; state = "idle"; working = false; queuedSpeech = []; recentSpeech = []; callerContext = "general"; autoRecommendationAttempted = false; urgentInteractive = false; urgentResponseSequence += 1; urgentResponseTail = Promise.resolve(); urgentOrigin = null; lastListeningRetryAt = null;
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
    if (!locationPreparation) locationPreparation = Promise.resolve(null);
    const result = await new CallApi("").post("/calls", { phone });
    if (!result.call_token) throw new Error("no call token");
    activeLocationGeneration = null; activeLocationRevision = null; locationSourceGeneration = null; locationSourceRevision = null;
    callId = result.id; api = new CallApi(result.call_token); state = result.state; const run = ++generation;
    scheduleCapabilityRefresh();
    status("음성 연결 중");
    const input = new VoiceInput({ callId, token: result.call_token, noticeAcknowledged: true, preAcquiredStream: preparedMicrophone,
      onText: (...args) => { if (voice === input) void voiceFinal(...args); },
      onPartial: (...args) => { if (voice === input) voicePartial(...args); },
      onNoMatch: (...args) => { if (voice === input) voiceNoMatch(...args); },
      onReconnectGap: () => { if (voice !== input) return; if (state === "urgent" && urgentInteractive) input.setListening(true); else if (state === "accepted") { reconnectGapPending = true; void announceReconnectGap(); } },
      onError: (code) => { if (voice === input) voiceFailure(code); } });
    preparedMicrophone = null; voice = input;
    await input.start();
    if (voice !== input) return;
    input.setListening(true);
    if (run !== generation) return;
    $("dial-screen").classList.add("hidden"); $("call-screen").classList.remove("hidden"); log.replaceChildren();
    status("동의 확인"); bubble(result.prompt);
    $("skip-consent").disabled = false; $("skip-consent").classList.remove("hidden");
    await playConsentPrompt("consent", run);
  } catch (error) {
    fail(error);
  } finally { working = false; $("start-call").disabled = false; const next = queuedSpeech.shift(); if (next) void receive(next.text ?? next, true, next.requestId, next.safetyCheck === true, next.sourceGeneration, next.sourceRevision); }
}
function urgentText(text) { return detectUrgent(text); }
function pesticideUrgent(text, options) { return detectPesticideUrgent(text, options); }
async function urgent(kind = "urgent", text = "", sourceGeneration = activeLocationGeneration, sourceRevision = activeLocationRevision) {
  if (!api || state === "urgent") return;
  urgentInteractive = state === "accepted";
  const body = (state === "accepted" || state === "location") && text && currentLocationEpoch(sourceGeneration, sourceRevision) ? { text, source_location_generation: sourceGeneration, source_location_revision: sourceRevision } : {};
  const run = ++generation; state = "urgent"; working = false; location = null; queuedSpeech = []; stopAudio(); voice?.setListening(urgentInteractive); status("긴급 안내");
  const urgentApi = api, urgentCallId = callId;
  void urgentApi.post(`/calls/${urgentCallId}/urgent`, body).catch(async () => {
    if (api !== urgentApi || callId !== urgentCallId || state !== "urgent") return;
    try {
      const latest = await urgentApi.get(`/calls/${urgentCallId}`);
      if (api !== urgentApi || callId !== urgentCallId || state !== "urgent" || latest.urgent) return;
      if (!Number.isInteger(latest.location_generation) || !Number.isInteger(latest.location_revision)) throw new Error("location_revision_missing");
      if (!currentLocationEpoch(sourceGeneration, sourceRevision) || latest.location_generation !== sourceGeneration || latest.location_revision !== sourceRevision) {
        await resumeAfterUrgentEpochChange(urgentApi, urgentCallId, latest);
      } else {
        try { await urgentApi.post(`/calls/${urgentCallId}/urgent`, {source_location_generation:sourceGeneration, source_location_revision:sourceRevision}); }
        catch (error) {
          const afterRetry = await urgentApi.get(`/calls/${urgentCallId}`);
          if (afterRetry.urgent) return;
          if (!currentLocationEpoch(sourceGeneration, sourceRevision) || afterRetry.location_generation !== sourceGeneration || afterRetry.location_revision !== sourceRevision) await resumeAfterUrgentEpochChange(urgentApi, urgentCallId, afterRetry);
          else throw error;
        }
      }
    } catch { if (api === urgentApi && state === "urgent") bubble("긴급 내용을 전달했는지 확인하지 못했어요.", "warning"); }
  });
  try { bubble(kind === "pesticide_urgent" ? "농약 때문에 위험할 수 있습니다. 지금 119에 전화해 주세요." : "위급할 수 있습니다. 지금 119나 가까운 응급실에 연락해 주세요.", "warning"); await playPrompt(kind, run); }
  catch (error) { bubble("위급하면 바로 119에 연락하세요.", "warning"); }
  finishUrgent(run);
}
async function resumeAfterUrgentEpochChange(urgentApi, urgentCallId, latest) {
  if (!Number.isInteger(latest.location_generation) || !Number.isInteger(latest.location_revision)) throw new Error("location_revision_missing");
  voice?.setListening(false);
  await audioTail.catch(() => {});
  if (api !== urgentApi || callId !== urgentCallId || state !== "urgent") return;
  if (!Number.isInteger(activeLocationRevision) || latest.location_revision >= activeLocationRevision) {
    activeLocationGeneration = latest.location_generation; activeLocationRevision = latest.location_revision;
  }
  location = null; deviceLocation = null; preparedLocation = null; regionReference = null;
  locationSourceGeneration = null; locationSourceRevision = null; lastRepeatAudio = null;
  recentSpeech = []; urgentOrigin = null; queuedSpeech = []; locationPreparationEpoch += 1;
  locationAllowed = activeLocationGeneration > 0; state = "accepted"; urgentInteractive = false;
  voice?.invalidateRecognitionBoundary?.(); status("상담 중");
  bubble("말씀을 잘 못 들었어요. 다시 말씀해 주세요.", "warning");
  await playPrompt("intent_retry", generation); voice?.setListening(true);
}
function finishUrgent(run) {
  if (run !== generation || state !== "urgent") return;
  currentRequestId = null;
  if (urgentInteractive) { voice?.setListening(true); status("긴급 도움 안내 중"); }
  else { endCall("safety_stop"); $("dial-notice").textContent = "위급할 수 있습니다. 지금 119나 가까운 응급실에 연락해 주세요."; }
}
async function receiveUrgent(text, sourceGeneration = activeLocationGeneration, sourceRevision = activeLocationRevision) {
  if (!urgentInteractive) return;
  if (!currentLocationEpoch(sourceGeneration, sourceRevision)) return;
  const run = generation, urgentApi = api, urgentCallId = callId, sequence = ++urgentResponseSequence;
  const request = Symbol(); callerIntents.add(request);
  try {
    const result = await urgentApi.post(`/calls/${urgentCallId}/urgent-followup`, { text, request_id: crypto.randomUUID(), source_location_generation: sourceGeneration, source_location_revision: sourceRevision });
    if (run !== generation || state !== "urgent") return;
    if (deferredVoiceFailure) {
      if (result.action === "revoke" || result.action === "end") clearVoiceFailure();
      else { if (callerIntents.size === 1) fail(new Error("speech")); return; }
    }
    if (result.action === "end" || result.action === "revoke") {
      endCall(result.action === "revoke" ? null : "caller");
      if (result.action === "revoke") $("dial-notice").textContent = "동의를 철회했습니다. 이미 전달된 내용은 자동 회수되지 않습니다.";
      return;
    }
    if (result.action === "revoke_location") {
      voice?.invalidateRecognitionBoundary?.(); activeLocationGeneration = 0; activeLocationRevision = Number.isInteger(result.location_revision) ? result.location_revision : null;
      location = null; preparedLocation = null; deviceLocation = null; regionReference = null;
      locationAllowed = false; pendingRegion = null; lastRepeatAudio = null; locationSourceGeneration = null; locationSourceRevision = null; locationPreparationEpoch += 1;
      recentSpeech = []; urgentOrigin = null; queuedSpeech = [];
    }
    urgentResponseTail = urgentResponseTail.catch(() => {}).then(async () => {
      if (run !== generation || sequence !== urgentResponseSequence) return;
      bubble(result.prompt, "warning");
      const path = `/calls/${urgentCallId}/urgent-followup/audio/${encodeURIComponent(result.prompt_kind)}`;
      try {
        const audio = typeof urgentApi.audioWithText === "function" ? await urgentApi.audioWithText(path) : { blob: await urgentApi.audio(path), text: result.prompt };
        if (run === generation && sequence === urgentResponseSequence) await playBlob(audio.blob, run, audioEpoch, audio.text || result.prompt);
      } catch { /* The fixed emergency message remains visible if audio fails. */ }
      if (run === generation) status("긴급 도움 안내 중");
    });
    await urgentResponseTail;
  } catch (error) {
    if (run !== generation) return;
    if (error instanceof ApiError && [401, 403].includes(error.status)) fail(error);
    else { bubble("지금 119에 전화하거나 곁에 있는 분에게 도움을 요청해 주세요.", "warning"); status("긴급 도움 안내 중"); }
  } finally {
    callerIntents.delete(request);
    if (run === generation && deferredVoiceFailure && callerIntents.size === 0) fail(new Error("speech"));
  }
}
async function getLocation() {
  if (!navigator.geolocation) return null;
  return new Promise((resolve) => navigator.geolocation.getCurrentPosition(
    (p) => {
      const accuracy = p.coords.accuracy;
      $("dial-location-status").textContent = Number.isFinite(accuracy) ? `기기 위치 오차 약 ${Math.round(accuracy)}m (환경에 따라 달라질 수 있어요).` : "기기 위치를 확인했어요.";
      resolve({ latitude: p.coords.latitude, longitude: p.coords.longitude });
    }, () => { $("dial-location-status").textContent = "기기 위치를 확인하지 못했어요."; resolve(null); },
    { enableHighAccuracy: true, timeout: 10000, maximumAge: 0 }));
}
function prepareDeviceLocation() {
  const epoch = ++locationPreparationEpoch;
  $("dial-location-status").textContent = "기기 위치 확인 중이에요.";
  preparedLocation = null;
  locationPreparation = getLocation().then((value) => {
    if (epoch !== locationPreparationEpoch) return null;
    deviceLocation = value;
    preparedLocation = value;
    return value;
  });
}
function scheduleSilenceCheck() {
  if (state !== "accepted" || silenceNoticeSent || silenceTimer !== null) return;
  const due = Math.max(lastSpeechAt + 20000, holdUntil);
  silenceTimer = setTimeout(async () => {
    silenceTimer = null;
    if (state !== "accepted" || silenceNoticeSent) return;
    if (working || playback || Date.now() < Math.max(lastSpeechAt + 20000, holdUntil)) { scheduleSilenceCheck(); return; }
    silenceNoticeSent = true;
    const run = generation;
    bubble("듣고 계신가요? 천천히 말씀해 주세요.");
    try { await playPrompt("silence_check", run); } catch { /* Keep listening if speech is unavailable. */ }
  }, Math.max(1000, due - Date.now()));
  silenceTimer?.unref?.();
}
async function announceReconnectGap() {
  if (!reconnectGapPending || state !== "accepted" || working || playback) return;
  reconnectGapPending = false;
  const run = generation;
  bubble("잠시 못 들었어요. 다시 말씀해 주세요.");
  try { await playPrompt("listening_retry", run); } catch { /* The display remains available. */ }
  if (run === generation && state === "accepted") voice?.setListening(true);
}
function scheduleCapabilityRefresh(delay = 45 * 60 * 1000) {
  if (capabilityRefreshTimer !== null) clearTimeout(capabilityRefreshTimer);
  const refreshApi = api, refreshCallId = callId;
  capabilityRefreshTimer = setTimeout(async () => {
    if (!refreshApi || api !== refreshApi || callId !== refreshCallId || state === "idle") return;
    try {
      const result = await refreshApi.post(`/calls/${refreshCallId}/capability/refresh`, {});
      if (api !== refreshApi || callId !== refreshCallId || !result.call_token) return;
      api.token = result.call_token;
      if (voice) voice.token = result.call_token;
      scheduleCapabilityRefresh();
    } catch (error) {
      if (api !== refreshApi || callId !== refreshCallId) return;
      if (error instanceof ApiError && [401, 403, 409].includes(error.status)) { fail(error); return; }
      scheduleCapabilityRefresh(60000);
    }
  }, delay);
  capabilityRefreshTimer?.unref?.();
}
async function revokeLocation(run, applied = false, appliedRevision = null) {
  const previousGeneration = activeLocationGeneration, previousRevision = activeLocationRevision, revokeApi = api, revokeCallId = callId;
  voice?.invalidateRecognitionBoundary?.(); activeLocationGeneration = null; activeLocationRevision = null;
  location = null; preparedLocation = null; deviceLocation = null; regionReference = null; locationAllowed = false; pendingRegion = null; lastRepeatAudio = null; locationSourceGeneration = null; locationSourceRevision = null; locationPreparationEpoch += 1;
  recentSpeech = []; urgentOrigin = null; queuedSpeech = [];
  voice?.setListening(false);
  let result = applied ? {location_revision: appliedRevision} : null;
  for (let attempt = 0; !applied && attempt < 2 && !result; attempt += 1) {
    try { result = await revokeApi.post(`/calls/${revokeCallId}/location-consent`, { accepted: false }); }
    catch (error) { if (error instanceof ApiError && [401, 403].includes(error.status)) break; }
    if (run !== generation || api !== revokeApi || callId !== revokeCallId) return;
  }
  if (!Number.isInteger(result?.location_revision)) {
    try {
      const current = await revokeApi.get(`/calls/${revokeCallId}`);
      if (current.state === "accepted" && !current.urgent && !current.ended_at && current.location_generation === 0 && Number.isInteger(current.location_revision) && (current.location_revision > previousRevision || (previousGeneration === 0 && current.location_revision === previousRevision))) result = current;
    } catch { /* Do not resume with an unconfirmed revocation. */ }
  }
  if (run !== generation || api !== revokeApi || callId !== revokeCallId) return;
  if (!Number.isInteger(result?.location_revision) || (result.location_revision <= previousRevision && !(previousGeneration === 0 && result.location_revision === previousRevision))) throw new LocationEpochSyncError("location_epoch_unavailable");
  activeLocationGeneration = 0;
  activeLocationRevision = result.location_revision;
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
  endCall(null); $("dial-notice").textContent = "동의를 철회했습니다. 이미 전달된 내용은 자동 회수되지 않습니다.";
}
async function checkPriorityIntent(text, run, requestId = crypto.randomUUID(), sourceGeneration = activeLocationGeneration, sourceRevision = activeLocationRevision) {
  if (!currentLocationEpoch(sourceGeneration, sourceRevision)) return;
  let decision;
  try { decision = await interpretCaller(text, requestId, sourceGeneration, sourceRevision); }
  catch {
    if (deferredVoiceFailure && callerIntents.size === 0) fail(new Error("speech"));
    return; // Leave the speech in the queue for the ordinary retry path.
  }
  if (run !== generation || state !== "accepted" || !currentLocationEpoch(sourceGeneration, sourceRevision)) return;
  if (!finishVoiceFailure(decision)) return;
  if (decision.intent === "end") {
    endCall("caller");
    return;
  }
  if (decision.intent === "urgent") { await urgent("urgent", text, sourceGeneration, sourceRevision); return; }
  if (decision.intent !== "revoke_location" && decision.intent !== "revoke_consent") return;
  generation += 1; const revokeRun = generation;
  queuedSpeech = []; recentSpeech = []; voice?.setListening(false); stopAudio(); status("동의 확인 중");
  try {
    if (decision.intent === "revoke_location") await revokeLocation(revokeRun, decision.applied === true, decision.location_revision);
    else await revokeConsent(revokeRun, decision.applied === true);
  } catch (error) { if (revokeRun === generation) { if (error instanceof LocationEpochSyncError) await endAfterLocationSyncFailure(revokeRun); else fail(error); } }
  finally { if (revokeRun === generation) working = false; }
}
async function receive(raw, alreadyShown = false, requestId = crypto.randomUUID(), safetyCheck = false, sourceGeneration = activeLocationGeneration, sourceRevision = activeLocationRevision) {
  const text = String(raw || "").trim(); if (!text || state === "idle" || state === "declined") return;
  if (state !== "awaiting_consent" && !currentLocationEpoch(sourceGeneration, sourceRevision)) {
    retryStaleSpeech();
    return;
  }
  if (state === "urgent") { if (!alreadyShown) bubble(text, "user"); await receiveUrgent(text, sourceGeneration, sourceRevision); return; }
  if (!alreadyShown || safetyCheck) {
    lastSpeechAt = Date.now(); silenceNoticeSent = false; holdUntil = 0;
    if (!alreadyShown) bubble(text, "user");
    recentSpeech.push(text); recentSpeech = recentSpeech.slice(-3);
    if (urgentText(recentSpeech.join(" "))) {
      generation += 1; voice?.setListening(false); stopAudio();
      const emergencyText = recentSpeech.join(" ").slice(-10000);
      const kind = pesticideUrgent(emergencyText) ? "pesticide_urgent" : "urgent";
      if (state === "accepted" || state === "location") {
        void api.post(`/calls/${callId}/messages`, { request_id: requestId, text, source_location_generation: sourceGeneration, source_location_revision: sourceRevision }).catch(() => {});
      }
      await urgent(kind, emergencyText, sourceGeneration, sourceRevision); return;
    }
  }
  if (working) {
    queuedSpeech.push({ text, requestId, sourceGeneration, sourceRevision }); status("답변 확인 중");
    if (state === "accepted") {
      const check = checkPriorityIntent(text, generation, requestId, sourceGeneration, sourceRevision); priorityChecks.add(check);
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
        if (!Number.isInteger(result.location_generation) || !Number.isInteger(result.location_revision)) { endCall("safety_stop"); $("dial-notice").textContent = "상담 연결을 확인할 수 없어요. 최신 연결로 다시 시작해 주세요."; return; }
        activeLocationGeneration = result.location_generation;
        activeLocationRevision = result.location_revision;
        state = "accepted"; $("skip-consent").classList.add("hidden"); recentSpeech = []; locationAllowed = true; location = null; locationSourceGeneration = null; locationSourceRevision = null; preparedLocation = null;
        const manualRegion = $("caller-region").value.trim();
        if (manualRegion) {
          location = null; locationSourceGeneration = null; locationSourceRevision = null;
          const resolved = await api.post(`/calls/${callId}/regions/resolve`, {text: manualRegion, source_location_generation: activeLocationGeneration, source_location_revision: activeLocationRevision});
          if (run !== generation || state !== "accepted" || resolved.location_generation !== activeLocationGeneration || resolved.location_revision !== activeLocationRevision) return;
          if (resolved.status === "matched" && resolved.match) {
            location = {latitude: resolved.match.latitude, longitude: resolved.match.longitude};
            locationSourceGeneration = activeLocationGeneration; locationSourceRevision = activeLocationRevision;
            regionReference = {location_precision: "region_reference", region_label: [resolved.match.province, resolved.match.district, resolved.match.locality].filter(Boolean).join(" ")};
          }
        }
        lastSpeechAt = Date.now(); scheduleSilenceCheck();
        status("상담 중");
        if (typeof result.followup_text === "string" && result.followup_text.trim()) {
          queuedSpeech.unshift({text: result.followup_text.trim(), requestId: crypto.randomUUID(), safetyCheck: true, sourceGeneration: activeLocationGeneration, sourceRevision: activeLocationRevision});
          return;
        }
        if (run !== generation) return;
        bubble(result.question_prompt || "어디가 불편하신가요? 말씀해 주세요."); await playPrompt("question", run);
      }
      else if (result.state === "declined") { state = "declined"; preparedLocation = null; status("동의 거절"); bubble(result.prompt); await playPrompt("declined", run); endCall("consent_declined"); $("dial-notice").textContent = "동의하지 않으셔서 상담을 마쳤습니다."; }
      else { bubble("위치 이용을 포함한 안내 내용 전체에 동의하시나요? 동의 또는 아니라고 말씀해 주세요."); $("skip-consent").disabled = false; $("skip-consent").classList.remove("hidden"); await playConsentPrompt("consent_retry", run); }
    } else if (state === "accepted") {
      status("말씀 확인 중");
      const decision = await interpretCaller(text, requestId, sourceGeneration, sourceRevision);
      if (run !== generation || !currentLocationEpoch(sourceGeneration, sourceRevision)) return;
      if (!finishVoiceFailure(decision)) return;
      if (decision.intent === "end") { endCall("caller"); return; }
      if (decision.intent === "urgent") { await urgent("urgent", recentSpeech.join(" ").slice(-10000), sourceGeneration, sourceRevision); return; }
      if (decision.intent === "revoke_location") { await revokeLocation(run, decision.applied === true, decision.location_revision); return; }
      if (decision.intent === "revoke_consent") { await revokeConsent(run, decision.applied === true); return; }
      if (decision.intent === "accept_location") {
        if (decision.applied !== true || !Number.isInteger(decision.location_generation) || decision.location_generation <= 0 || !Number.isInteger(decision.location_revision)) throw new Error("location_generation_missing");
        voice?.invalidateRecognitionBoundary?.();
        activeLocationGeneration = decision.location_generation; activeLocationRevision = decision.location_revision; locationAllowed = true;
        location = null; preparedLocation = null; deviceLocation = null; regionReference = null; locationSourceGeneration = null; locationSourceRevision = null;
        lastRepeatAudio = null; locationPreparationEpoch += 1; recentSpeech = []; urgentOrigin = null; queuedSpeech = [];
        pendingRegion = {query:"", specialty:null, mode:"new"};
        bubble("계신 읍이나 면, 동을 말씀해 주세요."); await playPrompt("region_request", run); return;
      }
      if ((decision.location_subject === "other" && !proxySubjectActive) || decision.patient_changed === true) {
        proxySubjectActive = decision.location_subject !== "caller"; proxyLocationBlocked = true; location = null; deviceLocation = null; regionReference = null; locationSourceGeneration = null; locationSourceRevision = null; lastRepeatAudio = null; autoRecommendationAttempted = false; locationPreparationEpoch += 1;
        recentSpeech = []; urgentOrigin = null; queuedSpeech = [];
        if (locationAllowed) await resetLocationContext(run, requestId, sourceGeneration, sourceRevision);
        if (run !== generation) return;
      } else if (decision.location_subject === "same_place" && proxyLocationBlocked) {
        proxyLocationBlocked = false; regionReference = null;
        const current = activeLocationGeneration, currentRevision = activeLocationRevision;
        if (run !== generation || !currentLocationEpoch(current, currentRevision) || !locationAllowed) return;
        location = null; deviceLocation = null; locationSourceGeneration = null; locationSourceRevision = null;
      }
      const controls = decision.voice_controls;
      if (controls?.speed === "slow" || controls?.speed === "normal") {
        await api.post(`/calls/${callId}/speech-speed`, { speed: controls.speed });
        if (run !== generation) return;
        speechRate = controls.speed === "slow" ? 0.85 : 1;
      }
      let resolvedRegionSearch = null, deferredRegionPrompt = decision.patient_changed === true && locationAllowed && decision.region_text ? "patient_reconfirm" : null;
      if (locationAllowed && decision.patient_changed !== true && typeof decision.region_text === "string" && decision.region_text.trim()) {
        const query = [pendingRegion?.query || "", decision.region_text.trim()].filter(Boolean).join(" ");
        const result = await api.post(`/calls/${callId}/regions/resolve`, { text: query, source_location_generation: sourceGeneration, source_location_revision: sourceRevision });
        if (run !== generation || state !== "accepted" || !locationAllowed || !currentLocationEpoch(sourceGeneration, sourceRevision) || result.location_generation !== sourceGeneration || result.location_revision !== sourceRevision) return;
        if (result.status === "matched" && result.match) {
          const nextLabel = [result.match.province, result.match.district, result.match.locality].filter(Boolean).join(" ");
          if (location && (!regionReference || regionReference.region_label !== nextLabel)) {
            location = null; deviceLocation = null; regionReference = null; locationSourceGeneration = null; locationSourceRevision = null; lastRepeatAudio = null; autoRecommendationAttempted = false; locationPreparationEpoch += 1;
            recentSpeech = []; urgentOrigin = null; queuedSpeech = [];
            await resetLocationContext(run, requestId, sourceGeneration, sourceRevision);
            if (run !== generation || state !== "accepted" || !locationAllowed) return;
            pendingRegion = {query:"", specialty:null, mode:"new"};
            bubble("계신 읍이나 면, 동을 다시 말씀해 주세요."); await playPrompt("region_request", run); return;
          }
          location = { latitude: result.match.latitude, longitude: result.match.longitude };
          locationSourceGeneration = sourceGeneration;
          locationSourceRevision = sourceRevision;
          regionReference = { location_precision: "region_reference", region_label: nextLabel };
          const search = pendingRegion; pendingRegion = null;
          if (decision.region_only === true) {
            if (search) await recommend(run, search.specialty, search.mode);
            else { bubble("네, 그 지역을 확인했어요. 병원을 찾아드릴까요?"); await playPrompt("region_confirmed", run); }
            return;
          }
          if (search) { resolvedRegionSearch = search; autoRecommendationAttempted = true; }
        } else {
          pendingRegion = { ...(pendingRegion || { specialty: null, mode: "new" }), query: result.status === "ambiguous" ? query : "" };
          deferredRegionPrompt = result.status === "ambiguous" ? "region_ambiguous" : "region_unavailable";
          if (decision.region_only === true || decision.intent === "recommend") {
            bubble(deferredRegionPrompt === "region_ambiguous" ? "어느 시나 군인지 말씀해 주세요." : "그 지역을 찾지 못했어요. 방문 준비를 먼저 도와드릴게요.");
            await playPrompt(deferredRegionPrompt, run); return;
          }
        }
      }
      if (decision.intent === "consultation" && controls?.control_only === true) {
        if (controls.repeat_target === "last" && lastRepeatAudio) {
          const repeat = lastRepeatAudio; bubble(repeat.text);
          await playBlob(repeat.blob, run, audioEpoch, repeat.text, "repeat", speechRate / (repeat.speed || 1));
          return;
        }
        if (controls.hold === true) { holdUntil = Date.now() + 60000; bubble("네, 천천히 말씀해 주세요. 듣고 있을게요."); await playPrompt("hold", run); return; }
        if (controls.speed === "slow" || controls.speed === "normal") {
          bubble(controls.speed === "slow" ? "네, 천천히 말씀드릴게요." : "네, 보통 속도로 말씀드릴게요.");
          await playPrompt(controls.speed === "slow" ? "speed_slow" : "speed_normal", run); return;
        }
      }
      if (decision.intent === "recommend") {
        if (!location && locationAllowed) { pendingRegion = {query: "", specialty: typeof decision.specialty === "string" ? decision.specialty : null, mode: decision.recommendation_mode || "new"}; bubble("계신 읍이나 면, 동을 말씀해 주세요."); await playPrompt("region_request", run); if (run === generation) status("상담 중"); }
        else if (!location) { bubble("위치를 확인하지 못했어요. 상담은 계속할게요.", "warning"); await playPrompt("location_unavailable", run); if (run === generation) status("상담 중"); }
        else { status("병원 찾는 중"); await recommend(run, typeof decision.specialty === "string" ? decision.specialty : null, decision.recommendation_mode); if (run === generation) status("상담 중"); }
        return;
      }
      if (decision.intent !== "consultation") { bubble("잘 못 들었어요. 다시 말씀해 주세요."); await playPrompt("intent_retry", run); if (run === generation) status("상담 중"); return; }
      if (!currentLocationEpoch(sourceGeneration, sourceRevision)) {
        bubble("확인할게요. 환자분이 어디가 불편하신가요?"); await playPrompt("patient_reconfirm", run); return;
      }
      status("내용 확인 중");
      currentRequestId = requestId;
      const submitted = await api.post(`/calls/${callId}/messages`, { request_id: requestId, text, caller_context: callerContext, source_location_generation: sourceGeneration, source_location_revision: sourceRevision });
      callerContext = "general";
      if (run !== generation) return;
      if (submitted.urgent) { await urgent(typeof submitted.final_text === "string" && submitted.final_text.startsWith("농약 때문에 위험할 수 있습니다.") ? "pesticide_urgent" : "urgent", recentSpeech.join(" ").slice(-10000)); return; }
      const answerDelivered = await pollTurn(requestId, run, sourceGeneration, sourceRevision);
      if (currentRequestId === requestId) currentRequestId = null;
      if (run === generation && answerDelivered && resolvedRegionSearch && !queuedSpeech.length) await recommend(run, resolvedRegionSearch.specialty, resolvedRegionSearch.mode);
      else if (run === generation && answerDelivered && deferredRegionPrompt && !queuedSpeech.length) {
        if (deferredRegionPrompt === "region_request") pendingRegion = {query:"", specialty:null, mode:"new"};
        bubble(deferredRegionPrompt === "patient_reconfirm" ? "확인할게요. 환자분이 어디가 불편하신가요?" : deferredRegionPrompt === "region_request" ? "계신 읍이나 면, 동을 말씀해 주세요." : deferredRegionPrompt === "region_ambiguous" ? "어느 시나 군인지 말씀해 주세요." : "그 지역을 찾지 못했어요. 방문 준비를 먼저 도와드릴게요.");
        await playPrompt(deferredRegionPrompt, run);
      }
    }
  } catch (error) {
    if (run === generation) {
      if (callerIntents.size > 0 && priorityChecks.size > 0) {
        deferredCallFailure = error;
        voice?.setListening(false);
        stopAudio();
      } else await recoverCall(error, run);
    }
  }
  finally {
    if (run === generation) {
      await Promise.allSettled([...priorityChecks]);
      if (run !== generation) return;
      if (deferredCallFailure) { const error = deferredCallFailure; deferredCallFailure = null; await recoverCall(error, run); }
      working = false;
      const next = queuedSpeech.shift();
      if (next) void receive(next.text ?? next, true, next.requestId, next.safetyCheck === true, next.sourceGeneration, next.sourceRevision);
      else void announceReconnectGap();
    }
  }
}
async function pollTurn(requestId, run, sourceGeneration = activeLocationGeneration, sourceRevision = activeLocationRevision) {
  const epoch = audioEpoch, base = `/calls/${callId}/messages/${encodeURIComponent(requestId)}`, startedAt = Date.now(); let initial = false, final = false, emergencySince = 0, finalPlayback = null; const fetched = new Set();
  while (run === generation && epoch === audioEpoch && currentLocationEpoch(sourceGeneration, sourceRevision)) {
    // Exceed the longest server reasoning (130s) plus speech (30s) budget,
    // while recovering if a restart leaves this turn marked processing.
    if (Date.now() - startedAt > 180000) throw new Error("answer_timeout");
    const turn = await api.get(base); if (run !== generation || epoch !== audioEpoch || !currentLocationEpoch(sourceGeneration, sourceRevision)) return;
    if (turn.status === "cancelled" || turn.status === "interrupted") throw new Error(`answer_${turn.status}`);
    if (turn.status === "emergency" && !emergencySince) { emergencySince = Date.now(); urgentInteractive = state === "accepted"; state = "urgent"; location = null; stopAudio(); status("긴급 안내"); }
    if (turn.initial_text && !initial) { initial = true; bubble(turn.initial_text); }
    if (turn.final_text && !final) { final = true; bubble(turn.final_text, turn.status === "emergency" ? "warning" : "assistant"); }
    let tts = {}; try { tts = JSON.parse(turn.tts_json || "{}"); } catch { /* unavailable */ }
    for (const kind of ["low", "emergency", "final"]) if (tts[kind] === "synthesized" && !fetched.has(kind)) {
      fetched.add(kind); const blob = await api.audio(`${base}/audio/${kind}`); if (epoch !== audioEpoch) return;
      const playing = playBlob(blob, run, epoch, kind === "low" ? turn.initial_text : (typeof turn.speech_text === "string" ? turn.speech_text : ""), kind);
      if (kind === "final") finalPlayback = playing;
      void playing.catch(() => {});
    }
    if (turn.status === "emergency" && (tts.emergency === "failed" || (emergencySince && Date.now() - emergencySince > 15000))) {
      try { await playPrompt("urgent", run); } catch { /* Keep fixed safety guidance visible. */ }
      finishUrgent(run); return;
    }
    if (["completed", "degraded", "emergency"].includes(turn.status) && !(turn.status === "emergency" && !tts.emergency)) {
      await audioTail;
      if (run !== generation || epoch !== audioEpoch) return;
      if (turn.status === "emergency") { finishUrgent(run); return; }
      if (tts.final !== "synthesized") {
        if (currentRequestId === requestId) currentRequestId = null;
        await recoverCall(new Error("answer_audio_unavailable"), run); return;
      }
      if (!finalPlayback || !await finalPlayback) return;
      if (currentRequestId === requestId) currentRequestId = null;
      try { await api.post(`${base}/playback-complete`, {}); }
      catch (error) {
        if (error instanceof ApiError && [401, 403].includes(error.status)) { if (run === generation) fail(error); return; }
        if (run === generation) bubble("안내 재생 기록을 확인하지 못했어요. 상담은 계속할게요.", "warning");
      }
      if (run !== generation || epoch !== audioEpoch) return;
      if (currentRequestId === requestId) currentRequestId = null;
      status(turn.status === "degraded" ? "응답 확인 필요" : "상담 중");
      if (turn.guidance?.action === "hospital_search_offer") callerContext = "hospital_search_offer";
      if (!turn.guidance && turn.status === "completed" && location && !autoRecommendationAttempted && turn.final_text && !queuedSpeech.length && !shortRetryPending) {
        const decision = await api.post(`/calls/${callId}/interpret`, { text: turn.final_text, subject: "assistant", source_location_generation: sourceGeneration, source_location_revision: sourceRevision });
        if (run === generation && currentLocationEpoch(sourceGeneration, sourceRevision) && !queuedSpeech.length && !shortRetryPending && decision.intent === "answer") await recommend(run);
      }
      return true;
    }
    await new Promise((resolve) => setTimeout(resolve, 900));
  }
}
async function recommend(run, specialty = null, mode = "new") {
  const sourceGeneration = locationSourceGeneration, sourceRevision = locationSourceRevision;
  if (!location || !regionReference || run !== generation || !currentLocationEpoch(sourceGeneration, sourceRevision)) return;
  autoRecommendationAttempted = true;
  let data;
  try { data = await api.post(`/calls/${callId}/recommendations`, { ...location, ...(regionReference || {}), specialty, mode: mode || "new", defer_confirmation: true, location_consent: true, urgent: false, include_audio: true, source_location_generation: sourceGeneration, source_location_revision: sourceRevision }); }
  catch (error) {
    if (error instanceof ApiError && [401, 403].includes(error.status)) throw error;
    if (run === generation) await hospitalUnavailable(run);
    return;
  }
  if (run !== generation || !currentLocationEpoch(sourceGeneration, sourceRevision)) return;
  if (data.status === "urgent") { await urgent(); return; }
  if (data.status === "ok" && data.hospitals?.length) {
    const hospital = data.hospitals[0]; hospitalBubble(hospital, data.specialty, data.facility_type);
    const epoch = audioEpoch;
    if (data.audio_status === "synthesized" && data.audio_base64) {
      const raw = atob(data.audio_base64);
      const heard = await playBlob(new Blob([Uint8Array.from(raw, c => c.charCodeAt(0))], { type: "audio/mpeg" }), run, epoch, typeof data.speech_text === "string" ? data.speech_text : "", "hospital");
      if (!heard || run !== generation || epoch !== audioEpoch) return;
      if (typeof data.navigation_token === "string") await api.post(`/calls/${callId}/recommendations/confirm`, { navigation_token: data.navigation_token });
    } else { await recoverCall(new Error("hospital_audio_unavailable"), run); return; }
    if (run === generation && !queuedSpeech.length && !shortRetryPending) {
      bubble("병원 갈 때 준비물이나 다른 궁금한 점을 말씀해 주세요.");
      await playPrompt("hospital_followup", run);
    }
  } else await hospitalUnavailable(run);
}
async function hospitalUnavailable(run) {
  bubble("지금은 가까운 종합병원을 찾지 못했어요. 방문 준비부터 도와드릴까요?", "warning");
  await playPrompt("hospital_unavailable", run);
}
$("start-call").addEventListener("click", () => void startCall());
$("skip-consent").addEventListener("click", skipConsent);
$("delete-digit").addEventListener("click", () => { digits = digits.slice(0, -1); updateNumber(); });
document.addEventListener("keydown", (event) => {
  if (state !== "idle" || !$("setup-screen").classList.contains("hidden") || event.target instanceof HTMLInputElement) return;
  if (/^[0-9*#]$/.test(event.key)) addDigit(event.key);
  if (event.key === "Backspace") { digits = digits.slice(0, -1); updateNumber(); }
  if (event.key === "Enter") void startCall();
});
globalThis.addEventListener?.("pagehide", () => {
  if (state !== "idle") endCall("hangup");
  else { microphonePreparationEpoch += 1; preparedMicrophone?.getTracks().forEach(track => track.stop()); preparedMicrophone = null; }
});
