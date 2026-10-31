import { apiPath } from "./api-path.js";

/** Build a same-origin speech URL without putting the call token in the URL. */
export function speechUrl(callId, originUrl = window.location.href) {
  const url = new URL(apiPath(`/calls/${encodeURIComponent(callId)}/speech`), originUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url;
}

/** Convert continuous mono microphone samples to the Azure 16 kHz PCM stream. */
export class PcmEncoder {
  constructor(sampleRate) {
    if (sampleRate < 16000) throw new Error("unsupported_sample_rate");
    this.sampleRate = sampleRate;
    this.phase = 0;
    this.sum = 0;
    this.count = 0;
  }

  encode(input) {
    const samples = [];
    for (const value of input) {
      this.sum += value;
      this.count += 1;
      this.phase += 16000;
      if (this.phase >= this.sampleRate) {
        const sample = Math.max(-1, Math.min(1, this.sum / this.count));
        samples.push(sample < 0 ? Math.round(sample * 32768) : Math.round(sample * 32767));
        this.phase -= this.sampleRate;
        this.sum = 0;
        this.count = 0;
      }
    }
    const buffer = new ArrayBuffer(samples.length * 2);
    const view = new DataView(buffer);
    samples.forEach((sample, index) => view.setInt16(index * 2, sample, true));
    return buffer;
  }
}

/** Prefer system-output echo cancellation; older browsers retain their existing mode. */
export async function acquireMicrophone(devices) {
  const audio = { channelCount: 1, noiseSuppression: true };
  try {
    return await devices.getUserMedia({ audio: { ...audio, echoCancellation: { exact: "all" } } });
  } catch (error) {
    if (error?.name !== "OverconstrainedError" && error?.name !== "TypeError") throw error;
    return devices.getUserMedia({ audio: { ...audio, echoCancellation: true } });
  }
}

/** A call-scoped stream. The capability is sent in the first frame, never in a URL. */
export class VoiceInput {
  constructor({ callId, token, noticeAcknowledged, onText, onPartial, onNoMatch, onError }) {
    this.callId = callId;
    this.token = token;
    this.noticeAcknowledged = noticeAcknowledged;
    this.onText = onText;
    this.onPartial = onPartial;
    this.onNoMatch = onNoMatch;
    this.onError = onError;
    this.listening = false;
    this.stopped = false;
    this.ready = false;
  }

  async start() {
    if (!this.noticeAcknowledged) throw new Error("notice_required");
    if (!navigator.mediaDevices?.getUserMedia) throw new Error("microphone_unavailable");
    const url = speechUrl(this.callId);
    const socket = new WebSocket(url);
    this.socket = socket;
    try {
      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error("speech_timeout")), 15000);
        const fail = (code) => { clearTimeout(timer); reject(new Error(code)); };
        socket.addEventListener("open", () => socket.send(JSON.stringify({
          type: "start", token: this.token, stt_notice_acknowledged: true,
        })));
        socket.addEventListener("message", (event) => {
          let message;
          try { message = JSON.parse(event.data); } catch { return; }
          if (message.type === "ready" && !this.ready) {
            this.ready = true;
            clearTimeout(timer);
            resolve();
          } else if (message.type === "partial" && this.listening && typeof message.text === "string") {
            this.onPartial?.(message.text, message.segment);
          } else if (message.type === "no_match" && this.listening) {
            this.onNoMatch?.(message.segment);
          } else if (message.type === "transcript" && this.listening && typeof message.text === "string") {
            this.onText(message.text, message.segment);
          } else if (message.type === "error") {
            const code = typeof message.code === "string" ? message.code : "speech_unavailable";
            if (this.ready) { this.stop(); this.onError(code); } else fail(code);
          }
        });
        socket.addEventListener("error", () => {
          if (!this.ready) fail("speech_unavailable");
        });
        socket.addEventListener("close", () => {
          if (!this.ready) fail("speech_unavailable");
          else if (!this.stopped) { this.stop(); this.onError("speech_disconnected"); }
        });
      });
      this.stream = await acquireMicrophone(navigator.mediaDevices);
      if (this.stopped) {
        this.stream.getTracks().forEach((track) => track.stop());
        throw new Error("speech_disconnected");
      }
      // The setting is necessary for playback overlap but is not proof of acoustic isolation.
      const cancellation = this.stream.getAudioTracks?.()[0]?.getSettings?.().echoCancellation;
      this.bargeSupported = cancellation === true || cancellation === "all";
      const AudioContextClass = window.AudioContext || window.webkitAudioContext;
      if (!AudioContextClass) throw new Error("microphone_unavailable");
      this.context = new AudioContextClass();
      this.encoder = new PcmEncoder(this.context.sampleRate);
      this.source = this.context.createMediaStreamSource(this.stream);
      this.processor = this.context.createScriptProcessor(4096, 1, 1);
      this.silent = this.context.createGain();
      this.silent.gain.value = 0;
      this.processor.onaudioprocess = (event) => {
        if (!this.listening || socket.readyState !== WebSocket.OPEN) return;
        if (socket.bufferedAmount > 512000) {
          this.stop();
          this.onError("speech_connection_slow");
          return;
        }
        const frame = this.encoder.encode(event.inputBuffer.getChannelData(0));
        if (frame.byteLength) socket.send(frame);
      };
      this.source.connect(this.processor);
      this.processor.connect(this.silent);
      this.silent.connect(this.context.destination);
      await this.context.resume();
    } catch (error) {
      this.stop();
      throw error;
    }
  }

  setListening(value) {
    this.listening = Boolean(value) && !this.stopped;
    if (!this.listening && this.encoder) this.encoder = new PcmEncoder(this.context.sampleRate);
  }

  stop() {
    if (this.stopped) return;
    this.stopped = true;
    this.listening = false;
    if (this.processor) this.processor.disconnect();
    if (this.source) this.source.disconnect();
    if (this.silent) this.silent.disconnect();
    if (this.stream) this.stream.getTracks().forEach((track) => track.stop());
    if (this.context) void this.context.close();
    if (this.socket) this.socket.close();
  }
}
