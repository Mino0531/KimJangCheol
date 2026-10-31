import assert from "node:assert/strict";
import { test } from "node:test";
import { VoiceInput } from "../voice.js";

test("voice input forwards no-match with its segment only while listening", async () => {
  const previous = { navigator: globalThis.navigator, window: globalThis.window, WebSocket: globalThis.WebSocket };
  let socket;
  const events = [];
  class Socket {
    static OPEN = 1;
    constructor() { this.listeners = {}; this.readyState = 1; this.bufferedAmount = 0; socket = this; queueMicrotask(() => this.emit("open", {})); }
    addEventListener(name, fn) { this.listeners[name] = fn; }
    emit(name, value) { this.listeners[name]?.(value); }
    send() { queueMicrotask(() => this.emit("message", { data: '{"type":"ready"}' })); }
    close() { this.readyState = 3; this.emit("close", {}); }
  }
  class Audio {
    constructor() { this.sampleRate = 48000; this.destination = {}; }
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    createScriptProcessor() { return { connect() {}, disconnect() {} }; }
    createGain() { return { gain: {}, connect() {}, disconnect() {} }; }
    async resume() {}
    async close() {}
  }
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { mediaDevices: {
    async getUserMedia() { return { getTracks: () => [{ stop() {} }] }; },
  } } });
  globalThis.window = { location: { href: "http://localhost/ui/" }, AudioContext: Audio };
  globalThis.WebSocket = Socket;
  try {
    const voice = new VoiceInput({ callId: "call", token: "token", noticeAcknowledged: true,
      onText() {}, onNoMatch: (segment) => events.push(segment), onError() {} });
    await voice.start();
    socket.emit("message", { data: '{"type":"no_match","segment":81}' });
    assert.deepEqual(events, []);
    voice.setListening(true);
    socket.emit("message", { data: '{"type":"no_match","segment":82}' });
    assert.deepEqual(events, [82]);
    voice.stop();
    socket.emit("message", { data: '{"type":"no_match","segment":83}' });
    assert.deepEqual(events, [82]);
  } finally {
    Object.defineProperty(globalThis, "navigator", { configurable: true, value: previous.navigator });
    globalThis.window = previous.window;
    globalThis.WebSocket = previous.WebSocket;
  }
});
