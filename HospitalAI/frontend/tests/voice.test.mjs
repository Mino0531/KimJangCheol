import { strict as assert } from "node:assert";
import { test } from "node:test";
import { PcmEncoder, VoiceInput } from "../voice.js";

test("converts microphone samples to bounded little-endian PCM across chunks", () => {
  const encoder = new PcmEncoder(48000);
  const a = encoder.encode(new Float32Array([1, 1]));
  const b = encoder.encode(new Float32Array([1, -1, -1, -1]));
  assert.equal(a.byteLength, 0);
  const samples = new DataView(b);
  assert.equal(samples.byteLength, 4);
  assert.equal(samples.getInt16(0, true), 32767);
  assert.equal(samples.getInt16(2, true), -32768);
});

for (const failure of ["provider", "microphone"]) test(`starts after notice and cleans up on ${failure} failure`, async () => {
  const previous = { navigator: globalThis.navigator, window: globalThis.window, WebSocket: globalThis.WebSocket };
  let processor;
  let stopped = false;
  let socket;
  let trackEnded;
  const events = [];
  class Socket {
    static OPEN = 1;
    constructor(url) {
      this.url = String(url); this.listeners = {}; this.readyState = 1; this.bufferedAmount = 0;
      this.sent = []; socket = this;
      queueMicrotask(() => this.emit("open", {}));
    }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    emit(name, event) { this.listeners[name]?.(event); }
    send(frame) {
      this.sent.push(frame);
      if (typeof frame === "string") queueMicrotask(() => this.emit("message", { data: '{"type":"ready"}' }));
    }
    close() { this.readyState = 3; this.emit("close", {}); }
  }
  class Audio {
    constructor() { this.sampleRate = 48000; this.destination = {}; }
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    createScriptProcessor() { processor = { connect() {}, disconnect() {} }; return processor; }
    createGain() { return { gain: {}, connect() {}, disconnect() {} }; }
    async resume() {}
    async close() {}
  }
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { mediaDevices: {
    async getUserMedia() { return { getTracks: () => [{ stop() { stopped = true; } }],
      getAudioTracks: () => [{ getSettings: () => ({ echoCancellation: "all" }), addEventListener(name, fn) { if (name === "ended") trackEnded = fn; } }] }; },
  } } });
  globalThis.window = { location: { href: "http://localhost:8000/ui/" }, AudioContext: Audio };
  globalThis.WebSocket = Socket;
  try {
    const voice = new VoiceInput({ callId: "call", token: "capability", noticeAcknowledged: true,
      onText: (value) => events.push(value), onError: (value) => events.push(value) });
    await voice.start();
    assert.equal(voice.bargeSupported, true);
    assert.equal(socket.url, "ws://localhost:8000/calls/call/speech");
    assert.deepEqual(JSON.parse(socket.sent[0]), { type: "start", token: "capability", stt_notice_acknowledged: true });
    const input = { inputBuffer: { getChannelData: () => new Float32Array([1, 1, 1]) } };
    processor.onaudioprocess(input);
    assert.equal(socket.sent.length, 1);
    voice.setListening(true);
    processor.onaudioprocess(input);
    assert.equal(socket.sent.length, 2);
    socket.emit("message", { data: '{"type":"transcript","text":"안녕하세요"}' });
    if (failure === "provider") socket.emit("message", { data: '{"type":"error","code":"call_inactive"}' });
    else trackEnded();
    assert.deepEqual(events, ["안녕하세요", failure === "provider" ? "call_inactive" : "microphone_disconnected"]);
    assert.equal(stopped, true);
  } finally {
    Object.defineProperty(globalThis, "navigator", { configurable: true, value: previous.navigator });
    globalThis.window = previous.window;
    globalThis.WebSocket = previous.WebSocket;
  }
});

test("does not open the microphone without the separate Azure notice", async () => {
  const voice = new VoiceInput({ callId: "call", token: "capability", noticeAcknowledged: false,
    onText() {}, onError() {} });
  await assert.rejects(voice.start(), /notice_required/);
});

test("reuses a prepared microphone and enables its audio track only after speech is ready", async () => {
  const previous = { navigator: globalThis.navigator, window: globalThis.window, WebSocket: globalThis.WebSocket };
  let requests = 0, stopped = false;
  const track = { enabled: false, stop() { stopped = true; }, getSettings: () => ({ echoCancellation: true }), addEventListener() {} };
  const stream = { getTracks: () => [track], getAudioTracks: () => [track] };
  class Socket {
    static OPEN = 1;
    constructor() { this.listeners = {}; this.readyState = 1; queueMicrotask(() => this.emit("open", {})); }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    emit(name, event) { this.listeners[name]?.(event); }
    send(frame) { if (typeof frame === "string") { assert.equal(track.enabled, false); queueMicrotask(() => this.emit("message", { data: '{"type":"ready"}' })); } }
    close() { this.readyState = 3; this.emit("close", {}); }
  }
  class Audio {
    constructor() { this.sampleRate = 48000; this.destination = {}; }
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    createScriptProcessor() { return { connect() {}, disconnect() {} }; }
    createGain() { return { gain: {}, connect() {}, disconnect() {} }; }
    async resume() {} async close() {}
  }
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { mediaDevices: { async getUserMedia() { requests++; throw new Error("unexpected microphone request"); } } } });
  globalThis.window = { location: { href: "http://localhost/ui/" }, AudioContext: Audio };
  globalThis.WebSocket = Socket;
  try {
    const voice = new VoiceInput({ callId: "call", token: "token", noticeAcknowledged: true, preAcquiredStream: stream, onText() {}, onError() {} });
    await voice.start();
    assert.equal(requests, 0);
    assert.equal(track.enabled, true);
    voice.stop();
    assert.equal(stopped, true);
  } finally {
    Object.defineProperty(globalThis, "navigator", { configurable: true, value: previous.navigator });
    globalThis.window = previous.window;
    globalThis.WebSocket = previous.WebSocket;
  }
});

test("releases a prepared microphone when the speech notice has not been accepted", async () => {
  let stopped = false;
  const stream = { getTracks: () => [{ stop() { stopped = true; } }] };
  const voice = new VoiceInput({ callId: "call", token: "token", noticeAcknowledged: false, preAcquiredStream: stream, onText() {}, onError() {} });
  await assert.rejects(voice.start(), /notice_required/);
  assert.equal(stopped, true);
});

test("reconnects an interrupted stream without losing the microphone or reusing segment identifiers", { timeout: 1500 }, async () => {
  const previous = { navigator: globalThis.navigator, window: globalThis.window, WebSocket: globalThis.WebSocket };
  const sockets = [], events = [];
  let processor, trackStopped = false, secondReady, thirdReady, fourthReady, fifthReady, gapCalls = 0;
  const reconnected = new Promise((resolve) => { secondReady = resolve; });
  const reconnectedAgain = new Promise((resolve) => { thirdReady = resolve; });
  const reconnectedAfterGap = new Promise((resolve) => { fourthReady = resolve; });
  const reconnectedAfterPartial = new Promise((resolve) => { fifthReady = resolve; });
  class Socket {
    static OPEN = 1;
    constructor() { this.listeners = {}; this.readyState = 1; this.bufferedAmount = 0; this.sent = []; sockets.push(this); queueMicrotask(() => this.emit("open", {})); }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    emit(name, event) { this.listeners[name]?.(event); }
    send(frame) {
      this.sent.push(frame);
      if (typeof frame === "string") queueMicrotask(() => { this.emit("message", { data: '{"type":"ready"}' }); if (sockets.length === 2) secondReady(); if (sockets.length === 3) thirdReady(); if (sockets.length === 4) fourthReady(); if (sockets.length === 5) fifthReady(); });
    }
    close() { this.readyState = 3; this.emit("close", {}); }
  }
  class Audio {
    constructor() { this.sampleRate = 48000; this.destination = {}; }
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    createScriptProcessor() { processor = { connect() {}, disconnect() {} }; return processor; }
    createGain() { return { gain: {}, connect() {}, disconnect() {} }; }
    async resume() {} async close() {}
  }
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { mediaDevices: {
    async getUserMedia() { return { getTracks: () => [{ stop() { trackStopped = true; } }], getAudioTracks: () => [] }; },
  } } });
  globalThis.window = { location: { href: "http://localhost/ui/" }, AudioContext: Audio };
  globalThis.WebSocket = Socket;
  try {
    const voice = new VoiceInput({ callId: "call", token: "token", noticeAcknowledged: true,
      onText: (text, segment) => events.push([text, segment]), onError: (code) => events.push(code), onReconnectGap: () => { assert.equal(voice.listening, false); gapCalls++; } });
    await voice.start();
    voice.setListening(true);
    voice.setReconnectBufferingAllowed(true);
    sockets[0].emit("message", { data: '{"type":"transcript","text":"첫 발화","segment":1}' });
    voice.token = "refreshed-token";
    sockets[0].emit("message", { data: '{"type":"error","code":"speech_provider_failed"}' });
    processor.onaudioprocess({ inputBuffer: { getChannelData: () => new Float32Array(480) } });
    await reconnected;
    sockets[0].emit("message", { data: '{"type":"transcript","text":"늦은 발화","segment":2}' });
    sockets[1].emit("message", { data: '{"type":"transcript","text":"다음 발화","segment":1}' });
    assert.equal(trackStopped, false);
    assert.equal(sockets[1].sent.length, 2);
    assert.equal(JSON.parse(sockets[1].sent[0]).token, "refreshed-token");
    assert.deepEqual(events.map((event) => event[0]), ["첫 발화", "다음 발화"]);
    assert.notEqual(events[0][1], events[1][1]);
    voice.setReconnectBufferingAllowed(false);
    sockets[1].close();
    processor.onaudioprocess({ inputBuffer: { getChannelData: () => new Float32Array(480) } });
    await reconnectedAgain;
    assert.equal(sockets[2].sent.length, 1);
    voice.setReconnectBufferingAllowed(true);
    sockets[2].close();
    for (let index = 0; index < 32; index++) processor.onaudioprocess({ inputBuffer: { getChannelData: () => new Float32Array(4800) } });
    assert.equal(voice.listening, false);
    voice.setListening(true);
    assert.equal(voice.listening, false);
    await reconnectedAfterGap;
    assert.equal(sockets[3].sent.length, 1);
    assert.equal(gapCalls, 1);
    assert.equal(voice.stopped, false);
    voice.setListening(true);
    assert.equal(voice.listening, true);
    sockets[3].emit("message", { data: '{"type":"partial","text":"잘린 발화","segment":2}' });
    sockets[3].close();
    processor.onaudioprocess({ inputBuffer: { getChannelData: () => new Float32Array(4800) } });
    await reconnectedAfterPartial;
    assert.equal(sockets[4].sent.length, 1);
    assert.equal(gapCalls, 2);
    assert.equal(voice.listening, false);
    voice.stop();
    assert.equal(trackStopped, true);
  } finally {
    Object.defineProperty(globalThis, "navigator", { configurable: true, value: previous.navigator });
    globalThis.window = previous.window;
    globalThis.WebSocket = previous.WebSocket;
  }
});

test("fails startup when the speech connection closes while microphone permission is pending", async () => {
  const previous = { navigator: globalThis.navigator, window: globalThis.window, WebSocket: globalThis.WebSocket };
  let socket, resolveMicrophone, signalRequest;
  const microphoneRequested = new Promise((resolve) => { signalRequest = resolve; });
  let trackStopped = false;
  class Socket {
    static OPEN = 1;
    constructor() { this.listeners = {}; this.readyState = 1; socket = this; queueMicrotask(() => this.emit("open", {})); }
    addEventListener(name, fn) { this.listeners[name] = fn; }
    emit(name, event) { this.listeners[name]?.(event); }
    send() { queueMicrotask(() => this.emit("message", { data: '{"type":"ready"}' })); }
    close() { this.readyState = 3; this.emit("close", {}); }
  }
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { mediaDevices: {
    getUserMedia() { signalRequest(); return new Promise((resolve) => { resolveMicrophone = resolve; }); },
  } } });
  globalThis.window = { location: { href: "http://localhost/ui/" } };
  globalThis.WebSocket = Socket;
  try {
    const voice = new VoiceInput({ callId: "call", token: "token", noticeAcknowledged: true, onText() {}, onError() {} });
    const starting = voice.start();
    await microphoneRequested;
    socket.close();
    resolveMicrophone({ getTracks: () => [{ stop() { trackStopped = true; } }] });
    await assert.rejects(starting, /speech_disconnected/);
    assert.equal(trackStopped, true);
  } finally {
    Object.defineProperty(globalThis, "navigator", { configurable: true, value: previous.navigator });
    globalThis.window = previous.window;
    globalThis.WebSocket = previous.WebSocket;
  }
});

test("does not replay buffered audio when the call becomes inactive during reconnect", { timeout: 1500 }, async () => {
  const previous = { window: globalThis.window, WebSocket: globalThis.WebSocket };
  const sockets = [];
  let reportError;
  const failed = new Promise((resolve) => { reportError = resolve; });
  class Socket {
    static OPEN = 1;
    constructor() { this.listeners = {}; this.readyState = 1; this.sent = []; sockets.push(this); queueMicrotask(() => this.emit("open", {})); }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    emit(name, event) { this.listeners[name]?.(event); }
    send(frame) {
      this.sent.push(frame);
      if (typeof frame === "string") queueMicrotask(() => this.emit("message", { data: sockets.length === 1 ? '{"type":"ready"}' : '{"type":"error","code":"call_inactive"}' }));
    }
    close() { this.readyState = 3; this.emit("close", {}); }
  }
  globalThis.window = { location: { href: "http://localhost/ui/" } };
  globalThis.WebSocket = Socket;
  try {
    const voice = new VoiceInput({ callId: "call", token: "token", noticeAcknowledged: true, onText() {}, onError: reportError });
    await voice.connect();
    voice.started = true;
    voice.setReconnectBufferingAllowed(true);
    voice.pendingAudio.push(new ArrayBuffer(640));
    voice.pendingBytes = 640;
    sockets[0].close();
    assert.equal(await failed, "call_inactive");
    assert.equal(sockets[1].sent.length, 1);
    assert.equal(voice.pendingBytes, 0);
    assert.equal(voice.stopped, true);
  } finally {
    globalThis.window = previous.window;
    globalThis.WebSocket = previous.WebSocket;
  }
});

test("stopping a call cancels scheduled reconnect attempts", { timeout: 1500 }, async () => {
  const previous = { window: globalThis.window, WebSocket: globalThis.WebSocket };
  const sockets = [];
  class Socket {
    static OPEN = 1;
    constructor() { this.listeners = {}; this.readyState = 1; sockets.push(this); queueMicrotask(() => this.emit("open", {})); }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    emit(name, event) { this.listeners[name]?.(event); }
    send(frame) { if (typeof frame === "string") queueMicrotask(() => this.emit("message", { data: '{"type":"ready"}' })); }
    close() { this.readyState = 3; this.emit("close", {}); }
  }
  globalThis.window = { location: { href: "http://localhost/ui/" } };
  globalThis.WebSocket = Socket;
  try {
    const voice = new VoiceInput({ callId: "call", token: "token", noticeAcknowledged: true, onText() {}, onError() { throw new Error("unexpected error"); } });
    await voice.connect();
    voice.started = true;
    sockets[0].close();
    voice.stop();
    await new Promise((resolve) => setTimeout(resolve, 350));
    assert.equal(sockets.length, 1);
  } finally {
    globalThis.window = previous.window;
    globalThis.WebSocket = previous.WebSocket;
  }
});

test("forwards silence and unrecognized reasons while accepting legacy no-match messages", async () => {
  const previous = { window: globalThis.window, WebSocket: globalThis.WebSocket };
  const events = [];
  let socket;
  class Socket {
    static OPEN = 1;
    constructor() { this.listeners = {}; this.readyState = 1; socket = this; queueMicrotask(() => this.emit("open", {})); }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    emit(name, event) { this.listeners[name]?.(event); }
    send(frame) { if (typeof frame === "string") queueMicrotask(() => this.emit("message", { data: '{"type":"ready"}' })); }
    close() { this.readyState = 3; this.emit("close", {}); }
  }
  globalThis.window = { location: { href: "http://localhost/ui/" } };
  globalThis.WebSocket = Socket;
  try {
    const voice = new VoiceInput({ callId: "call", token: "token", noticeAcknowledged: true, onText() {}, onNoMatch: (...args) => events.push(args), onError() {} });
    await voice.connect();
    voice.setListening(true);
    socket.emit("message", { data: '{"type":"no_match","segment":1,"reason":"silence"}' });
    socket.emit("message", { data: '{"type":"no_match","segment":2,"reason":"unrecognized"}' });
    socket.emit("message", { data: '{"type":"no_match","segment":3}' });
    assert.deepEqual(events, [[1, "silence"], [2, "unrecognized"], [3, undefined]]);
    voice.stop();
  } finally {
    globalThis.window = previous.window;
    globalThis.WebSocket = previous.WebSocket;
  }
});

test("stopping during a stalled reconnect handshake releases its timeout", { timeout: 1500 }, async () => {
  const previous = { window: globalThis.window, WebSocket: globalThis.WebSocket };
  const sockets = [];
  let secondStarted;
  const secondConnection = new Promise((resolve) => { secondStarted = resolve; });
  class Socket {
    static OPEN = 1;
    constructor() { this.listeners = {}; this.readyState = 1; sockets.push(this); queueMicrotask(() => this.emit("open", {})); }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    emit(name, event) { this.listeners[name]?.(event); }
    send(frame) {
      if (typeof frame !== "string") return;
      if (sockets.length === 1) queueMicrotask(() => this.emit("message", { data: '{"type":"ready"}' }));
      else secondStarted();
    }
    close() { this.readyState = 3; this.emit("close", {}); }
  }
  globalThis.window = { location: { href: "http://localhost/ui/" } };
  globalThis.WebSocket = Socket;
  try {
    const voice = new VoiceInput({ callId: "call", token: "token", noticeAcknowledged: true, onText() {}, onError() { throw new Error("unexpected error"); } });
    await voice.connect();
    voice.started = true;
    sockets[0].close();
    await secondConnection;
    voice.stop();
    assert.equal(voice.cancelConnect, null);
  } finally {
    globalThis.window = previous.window;
    globalThis.WebSocket = previous.WebSocket;
  }
});

test("a recognition boundary discards old speech and replaces even a pending reconnect", { timeout: 2000 }, async () => {
  const previous = { window: globalThis.window, WebSocket: globalThis.WebSocket };
  const sockets = [], events = [];
  let secondStarted, thirdReady, gapCount = 0;
  const secondPending = new Promise((resolve) => { secondStarted = resolve; });
  const thirdConnected = new Promise((resolve) => { thirdReady = resolve; });
  class Socket {
    static OPEN = 1;
    constructor() { this.listeners = {}; this.readyState = 1; this.sent = []; this.index = sockets.length; sockets.push(this); queueMicrotask(() => this.emit("open", {})); }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    emit(name, event) { this.listeners[name]?.(event); }
    send(frame) {
      this.sent.push(frame);
      if (typeof frame !== "string") return;
      if (this.index === 1) secondStarted();
      else queueMicrotask(() => { this.emit("message", { data: '{"type":"ready"}' }); if (this.index === 2) thirdReady(); });
    }
    close() { this.readyState = 3; this.emit("close", {}); }
  }
  globalThis.window = { location: { href: "http://localhost/ui/" } };
  globalThis.WebSocket = Socket;
  try {
    const voice = new VoiceInput({ callId: "call", token: "token", noticeAcknowledged: true,
      onText: (text) => events.push(text), onError: (error) => events.push(error), onReconnectGap: () => gapCount++ });
    assert.equal(voice.invalidateRecognitionBoundary(), false);
    await voice.connect();
    voice.started = true;
    voice.setListening(true);
    sockets[0].emit("message", { data: '{"type":"partial","text":"이전 위치","segment":1}' });
    voice.pendingAudio.push(new ArrayBuffer(100)); voice.pendingBytes = 100;
    assert.equal(voice.invalidateRecognitionBoundary(), true);
    assert.equal(voice.listening, false);
    assert.equal(voice.pendingBytes, 0);
    sockets[0].emit("message", { data: '{"type":"transcript","text":"이전 최종","segment":1}' });
    await secondPending;
    assert.equal(voice.invalidateRecognitionBoundary(), true);
    sockets[1].emit("message", { data: '{"type":"ready"}' });
    sockets[1].emit("message", { data: '{"type":"transcript","text":"오래된 결과","segment":1}' });
    await thirdConnected;
    assert.equal(gapCount, 1);
    assert.equal(voice.listening, false);
    voice.setListening(true);
    sockets[2].emit("message", { data: '{"type":"transcript","text":"새 위치","segment":1}' });
    assert.deepEqual(events, ["새 위치"]);
    voice.stop();
    assert.equal(voice.invalidateRecognitionBoundary(), false);
  } finally {
    globalThis.window = previous.window;
    globalThis.WebSocket = previous.WebSocket;
  }
});

test("stopping after a recognition boundary prevents reconnect and gap callbacks", { timeout: 1500 }, async () => {
  const previous = { window: globalThis.window, WebSocket: globalThis.WebSocket };
  const sockets = [], errors = [];
  let gaps = 0;
  class Socket {
    static OPEN = 1;
    constructor() { this.listeners = {}; this.readyState = 1; sockets.push(this); queueMicrotask(() => this.emit("open", {})); }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    emit(name, event) { this.listeners[name]?.(event); }
    send(frame) { if (typeof frame === "string") queueMicrotask(() => this.emit("message", { data: '{"type":"ready"}' })); }
    close() { this.readyState = 3; this.emit("close", {}); }
  }
  globalThis.window = { location: { href: "http://localhost/ui/" } };
  globalThis.WebSocket = Socket;
  try {
    const voice = new VoiceInput({ callId: "call", token: "token", noticeAcknowledged: true, onText() {}, onError: (error) => errors.push(error), onReconnectGap: () => gaps++ });
    await voice.connect();
    voice.started = true;
    assert.equal(voice.invalidateRecognitionBoundary(), true);
    voice.stop();
    await new Promise((resolve) => setTimeout(resolve, 350));
    assert.equal(sockets.length, 1);
    assert.equal(gaps, 0);
    assert.deepEqual(errors, []);
  } finally {
    globalThis.window = previous.window;
    globalThis.WebSocket = previous.WebSocket;
  }
});

test("a boundary retries when the server is still releasing the previous speech socket", { timeout: 2000 }, async () => {
  const previous = { window: globalThis.window, WebSocket: globalThis.WebSocket };
  const sockets = [], errors = [];
  let readyThird, gaps = 0;
  const ready = new Promise((resolve) => { readyThird = resolve; });
  class Socket {
    static OPEN = 1;
    constructor() { this.listeners = {}; this.readyState = 1; this.index = sockets.length; sockets.push(this); queueMicrotask(() => this.emit("open", {})); }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    emit(name, event) { this.listeners[name]?.(event); }
    send(frame) {
      if (typeof frame !== "string") return;
      queueMicrotask(() => {
        this.emit("message", { data: this.index === 1 ? '{"type":"error","code":"speech_already_active"}' : '{"type":"ready"}' });
        if (this.index === 2) readyThird();
      });
    }
    close() { this.readyState = 3; this.emit("close", {}); }
  }
  globalThis.window = { location: { href: "http://localhost/ui/" } };
  globalThis.WebSocket = Socket;
  try {
    const voice = new VoiceInput({ callId: "call", token: "token", noticeAcknowledged: true, onText() {}, onError: (error) => errors.push(error), onReconnectGap: () => gaps++ });
    await voice.connect();
    voice.started = true;
    assert.equal(voice.invalidateRecognitionBoundary(), true);
    await ready;
    assert.equal(sockets.length, 3);
    assert.equal(gaps, 1);
    assert.equal(voice.stopped, false);
    assert.deepEqual(errors, []);
    voice.stop();
  } finally {
    globalThis.window = previous.window;
    globalThis.WebSocket = previous.WebSocket;
  }
});

test("invalidating during the initial handshake stays silent and does not create another socket", async () => {
  const previous = { window: globalThis.window, WebSocket: globalThis.WebSocket };
  const sockets = [], results = [];
  let started;
  const pending = new Promise((resolve) => { started = resolve; });
  class Socket {
    static OPEN = 1;
    constructor() { this.listeners = {}; this.readyState = 1; sockets.push(this); queueMicrotask(() => this.emit("open", {})); }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    emit(name, event) { this.listeners[name]?.(event); }
    send(frame) { if (typeof frame === "string") started(); }
    close() { this.readyState = 3; this.emit("close", {}); }
  }
  globalThis.window = { location: { href: "http://localhost/ui/" } };
  globalThis.WebSocket = Socket;
  try {
    const voice = new VoiceInput({ callId: "call", token: "token", noticeAcknowledged: true, onText: (text) => results.push(text), onError: (error) => results.push(error) });
    const connecting = voice.connect();
    await pending;
    voice.setListening(true);
    assert.equal(voice.invalidateRecognitionBoundary(), false);
    assert.equal(voice.listening, false);
    sockets[0].emit("message", { data: '{"type":"transcript","text":"오래된 발화","segment":1}' });
    sockets[0].emit("message", { data: '{"type":"ready"}' });
    await connecting;
    assert.equal(sockets.length, 1);
    assert.deepEqual(results, []);
    voice.stop();
  } finally {
    globalThis.window = previous.window;
    globalThis.WebSocket = previous.WebSocket;
  }
});
