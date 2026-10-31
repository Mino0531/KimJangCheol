import { ApiError } from "./api.js";

const $ = (id) => document.getElementById(id);
const status = $("location-status");
const deliveryStatus = $("location-delivery");
const results = $("hospital-results");
const panel = $("location-panel");
const consent = $("location-consent");
const consentStatus = $("location-consent-status");
const consentRetry = $("location-consent-retry");
const audioEnabled = $("location-audio-enabled");
const audioPlay = $("location-audio-play");
const searchNow = $("location-search-now");
const latitude = $("location-latitude");
const longitude = $("location-longitude");
const specialty = $("location-specialty");
const gps = $("location-gps");
const manual = $("location-manual");

function addText(parent, className, value) {
  const node = document.createElement("p");
  node.className = className;
  node.textContent = value;
  parent.append(node);
}

function displayTime(value) {
  const raw = String(value);
  return /^\d{4}$/.test(raw) ? `${raw.slice(0, 2)}:${raw.slice(2)}` : raw;
}

function showHospital(hospital, requestedSpecialty, specialtySource) {
  const card = document.createElement("li");
  const title = document.createElement("strong");
  title.textContent = hospital.name || "이름 미확인";
  card.append(title);
  const distance = Number.isFinite(hospital.distance_km) ? `직선거리 ${hospital.distance_km.toFixed(1)} km` : "거리 미확인";
  addText(card, "hospital-detail", `${distance} · ${hospital.address || "주소 미확인"}`);
  if (Number.isInteger(hospital.known_factors) && Number.isInteger(hospital.total_factors)) {
    addText(card, "hospital-detail", `확인된 항목 ${hospital.known_factors}/${hospital.total_factors}`);
  }
  addText(card, "hospital-detail", `전화 ${hospital.phone || "미확인"}`);
  const matches = { yes: "진료과 정보 일치", no: "진료과 정보 불일치", unknown: "진료과 미확인" };
  const count = Number.isInteger(hospital.specialist_count) ? `등록 전문의 ${hospital.specialist_count}명` : "전문의 수 미확인";
  let specialtyText = requestedSpecialty ? (matches[hospital.specialty_match] || "진료과 미확인") : "진료과 미선택";
  if (requestedSpecialty && specialtySource === "suggested") {
    specialtyText = `참고 진료과 ${requestedSpecialty} · ${specialtyText}`;
  }
  addText(card, "hospital-detail", `${specialtyText} · ${count}`);
  const hours = { open: "등록된 진료시간 내", closed: "등록된 진료시간 외", unknown: "진료시간 미확인" };
  addText(card, "hospital-detail", `${hours[hospital.open_status] || "진료시간 미확인"} · 현재 전문의 근무 여부 미확인`);
  const scheduled = { within_hours: "등록 요일 시간 기준 시간 내", outside_hours: "등록 요일 시간 기준 시간 외", unknown: "등록 요일 시간 미확인" };
  addText(card, "hospital-detail", `${scheduled[hospital.registered_schedule_status] || "등록 요일 시간 미확인"} · 공휴일·임시휴진·점심 미확인`);
  const days = { mon: "월", tue: "화", wed: "수", thu: "목", fri: "금", sat: "토", sun: "일", holiday: "공휴일" };
  const schedule = hospital.hours && typeof hospital.hours === "object"
    ? Object.entries(hospital.hours).filter(([, times]) => Array.isArray(times) && times.length === 2)
      .map(([day, times]) => `${days[day] || day} ${displayTime(times[0])}–${displayTime(times[1])}`).join(" · ") : "";
  addText(card, "hospital-detail", schedule ? `등록 진료시간 ${schedule}` : "등록 진료시간 미확인");
  addText(card, "hospital-detail", `출처 ${hospital.source || "미확인"} · 갱신 ${hospital.updated_at || "미확인"}`);
  if (Array.isArray(hospital.reasons) && hospital.reasons.length) {
    addText(card, "hospital-reasons", hospital.reasons.join(" · "));
  }
  results.append(card);
}

/** Location and results live only for the currently active call view. */
export function createLocationPanel(getContext, enqueueAudio, onLocationReady) {
  let sequence = 0;
  let urgent = false;
  let recommendationAudio = null;
  let consentConfirmed = false;
  let consentOperation = 0;
  let capturedLocation = null;

  function clearAudio() {
    recommendationAudio = null;
    audioPlay.classList.add("hidden");
  }

  function showDelivery(state) {
    const labels = {
      pending: "병원 대시보드로 전송 대기 중입니다.",
      sent: "병원 대시보드에 전달됐습니다.",
      disabled: "병원 대시보드 연결이 설정되지 않아 전송 대기 중입니다.",
      failed: "병원 대시보드 전송을 준비하지 못했습니다. 다시 시도해 주세요.",
      cancelled: "위치 동의 철회로 이 대기 전송이 취소됐습니다. 이미 전송됐거나 전송 중인 결과는 자동 삭제되지 않을 수 있습니다.",
      not_applicable: "전달할 병원 추천 결과가 없습니다.",
    };
    deliveryStatus.textContent = labels[state] || "병원 대시보드 전달 여부를 확인할 수 없습니다.";
  }

  async function watchDelivery(api, callId, eventId, run) {
    for (let attempt = 0; attempt < 15; attempt += 1) {
      if (run !== sequence || urgent || !getContext().active) return;
      try {
        const result = await api.get(`/calls/${encodeURIComponent(callId)}/recommendations/${encodeURIComponent(eventId)}/delivery`);
        if (run !== sequence || urgent || !getContext().active) return;
        showDelivery(result.state);
        if (result.state === "sent") return;
        if (result.state !== "pending") return;
      } catch (error) {
        if (run === sequence && !urgent && getContext().active) {
          deliveryStatus.textContent = "전달 상태를 확인하지 못했습니다. 병원 대시보드에서 확인해 주세요.";
        }
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
  }

  function clear() {
    sequence += 1;
    consentOperation += 1;
    consentConfirmed = false;
    urgent = false;
    capturedLocation = null;
    panel.open = false;
    panel.classList.add("hidden");
    searchNow.classList.add("hidden");
    consent.checked = false;
    consent.disabled = false;
    consentRetry.classList.add("hidden");
    consentStatus.textContent = "";
    audioEnabled.checked = true;
    latitude.value = "";
    longitude.value = "";
    specialty.value = "";
    results.replaceChildren();
    clearAudio();
    status.textContent = "";
    deliveryStatus.textContent = "";
    gps.disabled = false;
    manual.disabled = false;
  }

  function activate() {
    if (!urgent) {
      panel.classList.remove("hidden");
      panel.open = true;
    }
  }

  function setUrgent() {
    if (urgent) return;
    sequence += 1;
    consentOperation += 1;
    consentConfirmed = false;
    urgent = true;
    capturedLocation = null;
    consent.checked = false;
    consent.disabled = false;
    consentRetry.classList.add("hidden");
    consentStatus.textContent = "";
    audioEnabled.checked = false;
    latitude.value = "";
    longitude.value = "";
    specialty.value = "";
    results.replaceChildren();
    clearAudio();
    deliveryStatus.textContent = "";
    panel.open = false;
    panel.classList.add("hidden");
    searchNow.classList.add("hidden");
  }

  async function search(lat, lon, run) {
    const { api, callId, active } = getContext();
    if (!active || urgent || !api || !callId || run !== sequence) return;
    if (!Number.isFinite(lat) || lat < -90 || lat > 90 || !Number.isFinite(lon) || lon < -180 || lon > 180) {
      status.textContent = "올바른 위도와 경도를 입력해 주세요.";
      return;
    }
    gps.disabled = true;
    manual.disabled = true;
    results.replaceChildren();
    clearAudio();
    deliveryStatus.textContent = "";
    status.textContent = "병원 정보를 확인하고 있습니다.";
    const requestedSpecialty = specialty.value.trim();
    const includeAudio = audioEnabled.checked;
    try {
      const data = await api.post(`/calls/${encodeURIComponent(callId)}/recommendations`, {
        latitude: lat, longitude: lon, specialty: requestedSpecialty || null,
        location_consent: true, urgent: false, include_audio: includeAudio,
      });
      if (run !== sequence || urgent || !getContext().active) return;
      if (data.status === "urgent") {
        sequence += 1;
        consentOperation += 1;
        consentConfirmed = false;
        urgent = true;
        consent.disabled = true;
        audioEnabled.checked = false;
        latitude.value = "";
        longitude.value = "";
        specialty.value = "";
        results.replaceChildren();
        deliveryStatus.textContent = "";
        status.textContent = data.warning || "응급 가능성이 있으면 즉시 119 또는 응급의료기관에 연락하세요.";
        return;
      }
      if (data.status !== "ok" || !Array.isArray(data.hospitals)) {
        status.textContent = data.warning || "병원 정보를 확인할 수 없습니다. 직접 의료기관에 문의해 주세요.";
        return;
      }
      const effectiveSpecialty = typeof data.specialty === "string" && data.specialty.trim()
        ? data.specialty.trim() : requestedSpecialty;
      const specialtySource = typeof data.specialty_source === "string" ? data.specialty_source : "unknown";
      for (const hospital of data.hospitals) showHospital(hospital, effectiveSpecialty, specialtySource);
      showDelivery(data.dashboard_delivery);
      if (data.dashboard_delivery === "pending" && typeof data.dashboard_event_id === "string" && data.dashboard_event_id) {
        void watchDelivery(api, callId, data.dashboard_event_id, run);
      }
      status.textContent = data.hospitals.length
        ? (data.warning || "직선거리와 공개 정보를 기준으로 표시합니다. 진료 가능 여부는 병원에 확인해 주세요.")
        : "조회된 병원이 없습니다. 위치나 진료과를 다시 확인해 주세요.";
      if (typeof data.specialty_notice === "string" && data.specialty_notice.trim()) {
        status.textContent = `${data.specialty_notice.trim()} ${status.textContent}`;
      }
      if (includeAudio && data.hospitals.length) {
        try {
          if (data.audio_status !== "synthesized" || data.audio_mime_type !== "audio/mpeg" || typeof data.audio_base64 !== "string") {
            throw new Error("음성 없음");
          }
          const raw = atob(data.audio_base64);
          if (!raw.length) throw new Error("빈 음성");
          const bytes = Uint8Array.from(raw, (char) => char.charCodeAt(0));
          recommendationAudio = new Blob([bytes], { type: "audio/mpeg" });
          audioPlay.classList.remove("hidden");
          enqueueAudio(recommendationAudio);
        } catch (error) {
          if (!(error instanceof Error)) throw error;
          status.textContent += " 음성 안내를 준비하지 못했습니다.";
        }
      }
    } catch (error) {
      if (run !== sequence || urgent || !getContext().active) return;
      if (error instanceof ApiError && error.status === 403) {
        status.textContent = "상담 및 위치 제공 동의 상태를 확인해 주세요.";
      } else if (error instanceof ApiError && error.status === 409) {
        status.textContent = "현재 상담 내용을 확인 중입니다. 안내가 끝난 뒤 다시 시도해 주세요.";
      } else {
        status.textContent = "병원 정보를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.";
      }
    } finally {
      if (run === sequence) { gps.disabled = false; manual.disabled = false; }
    }
  }

  function checkConsent() {
    if (consent.checked && consentConfirmed) return true;
    status.textContent = consent.checked
      ? "서버의 위치 동의 확인을 완료해 주세요."
      : "위치 좌표 전송에 먼저 동의해 주세요.";
    (consent.checked && !consentRetry.classList.contains("hidden") ? consentRetry : consent).focus();
    return false;
  }

  function capture(lat, lon) {
    if (!Number.isFinite(lat) || lat < -90 || lat > 90 || !Number.isFinite(lon) || lon < -180 || lon > 180) {
      status.textContent = "올바른 위치를 입력해 주세요.";
      return;
    }
    capturedLocation = { latitude: lat, longitude: lon };
    gps.disabled = false;
    manual.disabled = false;
    status.textContent = "위치를 확인했습니다. 불편한 곳을 말씀해 주세요.";
    onLocationReady();
  }

  function beginTurn() {
    sequence += 1;
    searchNow.classList.add("hidden");
    results.replaceChildren();
    clearAudio();
    deliveryStatus.textContent = "";
    status.textContent = "";
    gps.disabled = false;
    manual.disabled = false;
  }

  function recommendAfterTurn() {
    if (!capturedLocation || !consent.checked || !consentConfirmed || urgent || !getContext().active) return false;
    panel.classList.remove("hidden");
    panel.open = true;
    searchNow.classList.add("hidden");
    void search(capturedLocation.latitude, capturedLocation.longitude, ++sequence);
    return true;
  }

  function offerSearch() {
    if (!capturedLocation || !consent.checked || !consentConfirmed || urgent || !getContext().active) return false;
    panel.classList.remove("hidden");
    panel.open = true;
    searchNow.classList.remove("hidden");
    status.textContent = "답해 주시거나, 지금 병원을 찾을 수 있습니다.";
    return true;
  }

  gps.addEventListener("click", () => {
    if (!checkConsent()) return;
    const run = ++sequence;
    if (!navigator.geolocation) {
      status.textContent = "이 환경에서 현재 위치를 사용할 수 없습니다. 좌표를 직접 입력해 주세요.";
      return;
    }
    gps.disabled = true;
    manual.disabled = true;
    status.textContent = "기기 위치를 확인하고 있습니다.";
    navigator.geolocation.getCurrentPosition((position) => {
      if (run !== sequence || urgent || !getContext().active) return;
      latitude.value = String(position.coords.latitude);
      longitude.value = String(position.coords.longitude);
      capture(position.coords.latitude, position.coords.longitude);
    }, (error) => {
      if (run !== sequence || urgent || !getContext().active) return;
      gps.disabled = false;
      manual.disabled = false;
      status.textContent = error.code === 1
        ? "위치 사용이 허용되지 않았습니다. 위치 없이 계속할 수 있습니다."
        : "위치를 확인하지 못했습니다. 위치 없이 계속할 수 있습니다.";
    }, { enableHighAccuracy: false, timeout: 10000, maximumAge: 0 });
  });

  manual.addEventListener("click", () => {
    if (!checkConsent()) return;
    if (!latitude.value.trim() || !longitude.value.trim()) {
      status.textContent = "위도와 경도를 모두 입력해 주세요.";
      return;
    }
    sequence += 1;
    capture(Number(latitude.value), Number(longitude.value));
  });

  async function applyConsent() {
    const { api, callId, active } = getContext();
    const accepted = consent.checked;
    if (!active || !api || !callId || urgent) return;
    const operation = ++consentOperation;
    sequence += 1;
    consentConfirmed = false;
    consent.disabled = true;
    gps.disabled = true;
    manual.disabled = true;
    consentRetry.classList.add("hidden");
    consentStatus.textContent = accepted ? "위치 동의를 확인하고 있습니다." : "위치 동의 철회를 요청하고 있습니다.";
    if (!accepted) {
      capturedLocation = null;
      searchNow.classList.add("hidden");
      results.replaceChildren();
      clearAudio();
      deliveryStatus.textContent = "";
      latitude.value = "";
      longitude.value = "";
      status.textContent = "";
    }
    try {
      const response = await api.post(`/calls/${encodeURIComponent(callId)}/location-consent`, { accepted });
      if (operation !== consentOperation || urgent || !getContext().active || getContext().callId !== callId) return;
      if (response.state !== (accepted ? "accepted" : "revoked")) throw new Error("동의 상태를 확인할 수 없습니다.");
      consentConfirmed = accepted;
      if (accepted) {
        consentStatus.textContent = "위치 동의가 확인되었습니다.";
      } else {
        const cancelled = Number.isInteger(response.cancelled)
          ? `전송 대기 중인 ${response.cancelled}건을 취소했습니다.` : "전송 대기 건의 취소 결과를 확인해 주세요.";
        consentStatus.textContent = `위치 동의를 철회했습니다. ${cancelled} 이미 전송됐거나 전송 중인 추천 결과는 자동 삭제되지 않을 수 있습니다.`;
      }
    } catch (error) {
      if (operation !== consentOperation || urgent || !getContext().active) return;
      consentStatus.textContent = accepted
        ? "위치 동의를 확인하지 못해 검색할 수 없습니다. 다시 시도해 주세요."
        : "동의 철회를 확인하지 못했습니다. 대기 중인 전송이 계속될 수 있으니 다시 시도해 주세요.";
      consentRetry.classList.remove("hidden");
    } finally {
      if (operation === consentOperation && !urgent && getContext().active) {
        consent.disabled = false;
        gps.disabled = false;
        manual.disabled = false;
      }
    }
  }

  consent.addEventListener("change", () => { void applyConsent(); });
  consentRetry.addEventListener("click", () => { void applyConsent(); });

  searchNow.addEventListener("click", () => { recommendAfterTurn(); });

  audioPlay.addEventListener("click", () => {
    if (recommendationAudio && !urgent && consent.checked && getContext().active) {
      enqueueAudio(recommendationAudio);
    }
  });

  return { activate, clear, setUrgent, beginTurn, recommendAfterTurn, offerSearch };
}
