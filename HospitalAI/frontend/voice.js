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
  constructor({ callId, token, noticeAcknowledged, onText, onPartial, onNoMatch, onError, onReconnectGap, preAcquiredStream }) {
    this.callId = callId;
    this.token = token;
    this.noticeAcknowledged = noticeAcknowledged;
    this.onText = onText;
    this.onPartial = onPartial;
    this.onNoMatch = onNoMatch;
    this.onError = onError;
    this.onReconnectGap = onReconnectGap;
    this.stream = preAcquiredStream || null;
    this.listening = false;
    this.stopped = false;
    this.ready = false;
    this.started = false;
    this.segmentSequence = 0;
    this.connectionCount = 0;
    this.connectionGeneration = 0;
    this.reconnectBufferingAllowed = false;
    this.pendingAudio = [];
    this.pendingBytes = 0;
    this.reconnectGap = false;
    this.pendingSegments = new Set();
  }

  async connect() {
    const url = new URL(`/calls/${encodeURIComponent(this.callId)}/speech`, window.location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(url);
    this.socket = socket;
    const initial = this.connectionCount++ === 0;
    const segments = new Map();
    const segmentId = (segment) => {
      if (!Number.isSafeInteger(segment) || segment < 0) return undefined;
      if (!segments.has(segment)) {
        const id = initial ? segment : ++this.segmentSequence;
        this.segmentSequence = Math.max(this.segmentSequence, id);
        segments.set(segment, id);
      }
      return segments.get(segment);
    };
    await new Promise((resolve, reject) => {
        let settled = false;
        const timer = setTimeout(() => fail("speech_timeout"), 15000);
        const fail = (code) => { if (!settled) { settled = true; clearTimeout(timer); if (this.socket === socket) this.cancelConnect = null; reject(new Error(code)); } };
        this.cancelConnect = () => fail("speech_stopped");
        socket.addEventListener("open", () => { if (!this.stopped && this.socket === socket) socket.send(JSON.stringify({
          type: "start", token: this.token, stt_notice_acknowledged: true,
        })); });
        socket.addEventListener("message", (event) => {
          if (this.stopped || this.socket !== socket) return;
          let message;
          try { message = JSON.parse(event.data); } catch { return; }
          if (message.type === "ready" && !settled) {
            this.ready = true;
            settled = true;
            clearTimeout(timer);
            this.cancelConnect = null;
            for (const frame of this.pendingAudio) socket.send(frame);
            this.pendingAudio = [];
            this.pendingBytes = 0;
            const gap = this.reconnectGap;
            this.reconnectGap = false;
            resolve();
            if (gap) this.onReconnectGap?.();
          } else if (message.type === "partial" && this.ready && this.listening && typeof message.text === "string") {
            const segment = segmentId(message.segment);
            if (segment !== undefined) this.pendingSegments.add(segment);
            this.onPartial?.(message.text, segment);
          } else if (message.type === "no_match" && this.ready && this.listening) {
            const segment = segmentId(message.segment);
            this.pendingSegments.delete(segment);
            const reason = ["silence", "unrecognized"].includes(message.reason) ? message.reason : undefined;
            this.onNoMatch?.(segment, reason);
          } else if (message.type === "transcript" && this.ready && this.listening && typeof message.text === "string") {
            const segment = segmentId(message.segment);
            this.pendingSegments.delete(segment);
            this.onText(message.text, segment);
          } else if (message.type === "error") {
            const code = typeof message.code === "string" ? message.code : "speech_unavailable";
            if (!settled) fail(code);
            else this.connectionFailure(socket, code);
          }
        });
        socket.addEventListener("error", () => {
          if (this.socket !== socket || this.stopped) return;
          if (!settled) fail("speech_unavailable");
          else this.connectionFailure(socket, "speech_disconnected");
        });
        socket.addEventListener("close", () => {
          if (this.socket !== socket || this.stopped) return;
          if (!settled) fail("speech_unavailable");
          else this.connectionFailure(socket, "speech_disconnected");
        });
      });
  }

  connectionFailure(socket, code) {
    if (this.stopped || this.socket !== socket) return;
    this.ready = false;
    this.socket = null;
    socket.close();
    if (!this.started || !["speech_disconnected", "speech_provider_failed", "speech_unavailable"].includes(code)) {
      this.stop();
      this.onError(code);
      return;
    }
    if (this.pendingSegments.size) {
      this.pendingSegments.clear();
      this.pendingAudio = []; this.pendingBytes = 0;
      this.reconnectGap = true;
      this.listening = false;
    }
    void this.reconnect(++this.connectionGeneration);
  }

  async reconnect(generation) {
    for (const delay of [250, 500, 1000]) {
      await new Promise((resolve) => setTimeout(resolve, delay));
      if (this.stopped || generation !== this.connectionGeneration) return;
      try {
        await this.connect();
        return;
      } catch (error) {
        if (this.stopped || generation !== this.connectionGeneration) return;
        const socket = this.socket;
        this.socket = null;
        socket?.close();
        if (this.stopped) return;
        if (["call_inactive", "invalid_start"].includes(error?.message)) {
          this.stop();
          this.onError(error.message);
          return;
        }
      }
    }
    if (!this.stopped && generation === this.connectionGeneration) { this.stop(); this.onError("speech_disconnected"); }
  }

  invalidateRecognitionBoundary() {
    if (this.stopped) return false;
    if (!this.started) {
      this.listening = false;
      this.pendingAudio = []; this.pendingBytes = 0;
      this.pendingSegments.clear();
      return false;
    }
    const generation = ++this.connectionGeneration;
    this.ready = false;
    this.listening = false;
    this.reconnectGap = true;
    this.pendingAudio = []; this.pendingBytes = 0;
    this.pendingSegments.clear();
    if (this.encoder) this.encoder = new PcmEncoder(this.context.sampleRate);
    this.cancelConnect?.();
    const socket = this.socket;
    this.socket = null;
    socket?.close();
    void this.reconnect(generation);
    return true;
  }

  setReconnectBufferingAllowed(value) {
    this.reconnectBufferingAllowed = Boolean(value) && !this.stopped;
    if (!this.reconnectBufferingAllowed) {
      if (this.pendingBytes) { this.reconnectGap = true; this.listening = false; }
      this.pendingAudio = []; this.pendingBytes = 0;
    }
  }

  async start() {
    if (!this.noticeAcknowledged) { this.stop(); throw new Error("notice_required"); }
    if (!this.stream && !navigator.mediaDevices?.getUserMedia) { this.stop(); throw new Error("microphone_unavailable"); }
    try {
      await this.connect();
      if (!this.stream) this.stream = await acquireMicrophone(navigator.mediaDevices);
      if (this.stopped) {
        this.stream.getTracks().forEach((track) => track.stop());
        throw new Error("speech_disconnected");
      }
      for (const track of this.stream.getAudioTracks?.() || []) {
        if (track.readyState === "ended") throw new Error("microphone_disconnected");
        track.enabled = true;
        track.addEventListener?.("ended", () => {
          if (!this.stopped) { this.stop(); this.onError("microphone_disconnected"); }
        });
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
        if (!this.listening) return;
        const socket = this.socket;
        if (!this.ready || !socket || socket.readyState !== WebSocket.OPEN) {
          if (!this.reconnectBufferingAllowed) return;
          const frame = this.encoder.encode(event.inputBuffer.getChannelData(0));
          if (this.pendingBytes + frame.byteLength > 64000) {
            this.pendingAudio = []; this.pendingBytes = 0;
            this.reconnectGap = true;
            this.listening = false;
            return;
          }
          if (frame.byteLength) { this.pendingAudio.push(frame); this.pendingBytes += frame.byteLength; }
          return;
        }
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
      this.started = true;
    } catch (error) {
      this.stop();
      throw error;
    }
  }

  setListening(value) {
    this.listening = Boolean(value) && !this.stopped && !this.reconnectGap;
    if (!this.listening) {
      this.pendingAudio = []; this.pendingBytes = 0;
      this.pendingSegments.clear();
      if (this.encoder) this.encoder = new PcmEncoder(this.context.sampleRate);
    }
  }

  stop() {
    if (this.stopped) return;
    this.stopped = true;
    this.connectionGeneration += 1;
    this.listening = false;
    this.cancelConnect?.();
    this.pendingAudio = []; this.pendingBytes = 0;
    this.pendingSegments.clear();
    if (this.processor) this.processor.disconnect();
    if (this.source) this.source.disconnect();
    if (this.silent) this.silent.disconnect();
    if (this.stream) this.stream.getTracks().forEach((track) => track.stop());
    if (this.context) void this.context.close();
    if (this.socket) this.socket.close();
  }
}
