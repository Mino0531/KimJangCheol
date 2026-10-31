import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { BargeGuard } from '../barge_guard.js';

test('interrupts for a different caller phrase and admits only its final segment', () => {
  const gate = new BargeGuard();
  gate.begin({ text: '어디가 불편하신가요?', protectedConsent: false, emergency: false });
  assert.equal(gate.partial('머리가 아파요', 1), 'interrupt');
  gate.end();
  assert.equal(gate.final('머리가 아파요.', 1), true);
});

test('rejects recognizable playback echo throughout its segment', () => {
  const gate = new BargeGuard();
  gate.begin({ text: '어디가 불편하신가요?', protectedConsent: false, emergency: false });
  assert.equal(gate.partial('어디가 불편', 2), 'ignore');
  gate.end();
  assert.equal(gate.final('어디가 불편하신가요?', 2), false);
});

test('does not expose recognized playback echo as caller speech', () => {
  const gate = new BargeGuard();
  gate.begin({ text: '지금 어지러워요?', protectedConsent: false, emergency: false });
  assert.equal(gate.partial('어지러워요', 88), 'ignore');
  assert.equal(gate.callerText('어지러워요', 88), '');
});

test('extracts caller speech only after an exact complete assistant prefix', () => {
  const gate = new BargeGuard();
  gate.begin({ text: '어디가 불편하신가요?', protectedConsent: false, emergency: false });
  assert.equal(gate.partial('어디가', 3), 'ignore');
  assert.equal(gate.partial('어디가 불편하신가요 머리가 아파요', 3), 'interrupt');
  gate.end();
  assert.equal(gate.final('어디가 불편하신가요 머리가 아파요', 3), true);
  assert.equal(gate.callerText('어디가 불편하신가요 머리가 아파요', 3), '머리가 아파요');
});

test('does not forward a mixed segment with only a partial playback prefix', () => {
  const gate = new BargeGuard();
  gate.begin({ text: '어디가 불편하신가요?', protectedConsent: false, emergency: false });
  assert.equal(gate.partial('불편하신가요 머리가 아파요', 34), 'ignore');
  gate.end();
  assert.equal(gate.final('불편하신가요 머리가 아파요', 34), false);
});

test('does not extract a short or repeated suffix from the playback itself', () => {
  const gate = new BargeGuard();
  gate.begin({ text: '말씀 잘 들었어요 잠시만요', protectedConsent: false, emergency: false });
  assert.equal(gate.partial('말씀 잘 들었어요 잠시만요 네', 35), 'ignore');
  assert.equal(gate.final('말씀 잘 들었어요 잠시만요 네', 35), false);
});

test('does not accept a consent mixed with the disclosure before it is complete', () => {
  const gate = new BargeGuard();
  gate.begin({ text: '정보 이용에 동의하시나요', protectedConsent: true, emergency: false });
  assert.equal(gate.partial('정보 이용에 동의하시나요 동의합니다', 36), 'ignore');
  gate.end();
  assert.equal(gate.final('정보 이용에 동의하시나요 동의합니다', 36), false);
});

test('extracts only caller danger text when it follows an exact playback prefix', () => {
  const gate = new BargeGuard();
  gate.begin({ text: '어디가 불편하신가요?', protectedConsent: false, emergency: false });
  const text = '어디가 불편하신가요 숨을 못 쉬어요';
  assert.equal(gate.partial(text, 37), 'urgent');
  assert.equal(gate.callerText(text, 37), '숨을 못 쉬어요');
});

test('raises an emergency but does not attribute unseparable assistant echo to the caller', () => {
  const gate = new BargeGuard();
  gate.begin({ text: '어디가 불편하신가요?', protectedConsent: false, emergency: false });
  const text = '불편하신가요 숨을 못 쉬어요';
  assert.equal(gate.partial(text, 38), 'urgent');
  assert.equal(gate.callerText(text, 38), '');
});

test('never accepts a consent segment that started before disclosure ended', () => {
  let clock = 5000;
  const gate = new BargeGuard(() => clock);
  gate.begin({ text: '정보를 이용합니다. 동의하시나요?', protectedConsent: true, emergency: false });
  assert.equal(gate.partial('동의합니다', 4), 'ignore');
  gate.end();
  clock += 2000;
  assert.equal(gate.final('동의합니다', 4), false);
  gate.partial('동의', 5);
  assert.equal(gate.final('동의합니다', 5), true);
});

test('blocks a final-only late consent echo after disclosure', () => {
  let clock = 1000;
  const gate = new BargeGuard(() => clock);
  gate.begin({ text: '동의하시면 동의라고 답해 주세요', protectedConsent: true, emergency: false });
  gate.end();
  clock += 100;
  assert.equal(gate.final('동의', 6), false);
  clock += 2000;
  assert.equal(gate.final('동의합니다', 7), false);
  gate.partial('동의합니다', 8);
  assert.equal(gate.final('동의합니다', 8), true);
});

test('offers bounded retries for separate early consent answers without looping on one segment', () => {
  let clock = 1000;
  const gate = new BargeGuard(() => clock);
  gate.begin({ text: '동의하시나요?', protectedConsent: true, emergency: false });
  gate.partial('동의합니다', 30);
  assert.equal(gate.final('동의합니다', 30), false);
  assert.equal(gate.takeConsentRetry(), true);
  assert.equal(gate.takeConsentRetry(), false);
  gate.end();
  clock += 100;
  gate.partial('네', 31);
  assert.equal(gate.final('네', 31), false);
  assert.equal(gate.takeConsentRetry(), true);
  assert.equal(gate.final('네', 31), false);
  assert.equal(gate.takeConsentRetry(), false);
  gate.partial('네', 32);
  assert.equal(gate.final('네', 32), false);
  assert.equal(gate.takeConsentRetry(), false);
});

test('reports repeated unambiguous early consent once after retry limit', () => {
  const gate = new BargeGuard();
  for (const segment of [60, 61]) {
    gate.begin({ text: '안내를 끝까지 들으신 뒤 동의 여부를 말씀해 주세요', protectedConsent: true, emergency: false });
    gate.partial('네 동의합니다', segment);
    assert.equal(gate.final('네 동의합니다', segment), false);
    assert.equal(gate.takeConsentRetry(), true);
    gate.end();
  }
  gate.begin({ text: '안내를 끝까지 들으신 뒤 동의 여부를 말씀해 주세요', protectedConsent: true, emergency: false });
  gate.partial('네 동의합니다', 62);
  assert.equal(gate.final('네 동의합니다', 62), false);
  assert.equal(gate.takeConsentRetry(), false);
  assert.equal(gate.takeConsentExhausted(), true);
  assert.equal(gate.takeConsentExhausted(), false);
  assert.equal(gate.final('네 동의합니다', 62), false);
  assert.equal(gate.takeConsentExhausted(), false);
});

test('does not end a call for exhausted retries when response could be guide echo', () => {
  const gate = new BargeGuard();
  for (const segment of [63, 64]) {
    gate.begin({ text: '동의하시면 동의라고 말씀하세요', protectedConsent: true, emergency: false });
    gate.partial('동의', segment);
    assert.equal(gate.final('동의', segment), false);
    assert.equal(gate.takeConsentRetry(), true);
    gate.end();
  }
  gate.begin({ text: '동의하시면 동의라고 말씀하세요', protectedConsent: true, emergency: false });
  gate.partial('동의', 65);
  assert.equal(gate.final('동의', 65), false);
  assert.equal(gate.takeConsentRetry(), false);
  assert.equal(gate.takeConsentExhausted(), false);
});

test('does not offer a retry for plain disclosure echo', () => {
  const gate = new BargeGuard();
  gate.begin({ text: '전화번호와 건강 상담 내용을 이용합니다', protectedConsent: true, emergency: false });
  gate.partial('건강 상담 내용을 이용합니다', 32);
  assert.equal(gate.final('건강 상담 내용을 이용합니다', 32), false);
  assert.equal(gate.takeConsentRetry(), false);
});

test('offers one retry for final-only early consent during disclosure', () => {
  const gate = new BargeGuard();
  gate.begin({ text: '동의하시나요?', protectedConsent: true, emergency: false });
  assert.equal(gate.final('동의합니다', 33), false);
  assert.equal(gate.takeConsentRetry(), true);
});

test('offers a retry for a natural affirmative phrase during disclosure', () => {
  const gate = new BargeGuard();
  gate.begin({ text: '동의하시나요?', protectedConsent: true, emergency: false });
  gate.partial('네 동의합니다', 39);
  assert.equal(gate.final('네 동의합니다', 39), false);
  assert.equal(gate.takeConsentRetry(), true);
});

test('prioritizes a clear emergency during consent but not echoed speech', () => {
  const gate = new BargeGuard();
  gate.begin({ text: '정보를 이용합니다. 동의하시나요?', protectedConsent: true, emergency: false });
  assert.equal(gate.partial('숨을 못 쉬어요', 8), 'urgent');
});

test('prioritizes emergency speech even when recognition includes the whole assistant prompt', () => {
  const gate = new BargeGuard();
  gate.begin({ text: '어디가 불편하신가요?', protectedConsent: false, emergency: false });
  assert.equal(gate.partial('어디가 불편하신가요', 20), 'ignore');
  assert.equal(gate.partial('어디가 불편하신가요 숨을 못 쉬어요', 20), 'urgent');
});

test('waits for a complete chest symptom when a partial could still be negated', () => {
  const gate = new BargeGuard();
  gate.begin({ text: '가슴이 아프세요?', protectedConsent: false, emergency: false });
  assert.equal(gate.partial('가슴이 아프', 22), 'ignore');
  assert.equal(gate.final('가슴이 아프지 않아요', 22), false);
});

test('prioritizes a final emergency after an echo-like partial', () => {
  const gate = new BargeGuard();
  gate.begin({ text: '가슴이 아프세요?', protectedConsent: false, emergency: false });
  assert.equal(gate.partial('가슴이', 23), 'ignore');
  assert.equal(gate.final('가슴이 아파요', 23), true);
});

test('detects urgent partial speech even when no prompt is playing', () => {
  const gate = new BargeGuard();
  assert.equal(gate.partial('숨을 못 쉬어요', 40), 'urgent');
  assert.equal(gate.final('숨을 못 쉬어요', 40), true);
});

test('suppresses a recently echoed 119 after playback, including a delayed final', () => {
  let clock = 3000;
  const gate = new BargeGuard(() => clock);
  gate.begin({ text: '위급하면 119에 연락해 주세요', protectedConsent: false, emergency: false });
  gate.end();
  clock += 100;
  assert.equal(gate.partial('119', 41), 'ignore');
  clock += 2000;
  assert.equal(gate.final('119', 41), false);
});

test('prioritizes danger even immediately after similar playback', () => {
  let clock = 3000;
  const gate = new BargeGuard(() => clock);
  gate.begin({ text: '가슴이 아프세요?', protectedConsent: false, emergency: false });
  gate.end();
  clock += 100;
  assert.equal(gate.partial('가슴이 아파요', 42), 'urgent');
});

test('does not treat echoed emergency wording from an assistant prompt as new distress', () => {
  const gate = new BargeGuard();
  gate.begin({ text: '위급하면 119에 연락해 주세요', protectedConsent: false, emergency: false });
  assert.equal(gate.partial('위급하면 119에 연락해 주세요', 21), 'ignore');
  assert.equal(gate.final('위급하면 119에 연락해 주세요', 21), false);
});

test('does not interrupt or forward speech during emergency playback', () => {
  const gate = new BargeGuard();
  gate.begin({ text: '지금 119에 전화해 주세요', protectedConsent: false, emergency: true });
  assert.equal(gate.partial('살려주세요', 9), 'ignore');
  gate.end();
  assert.equal(gate.final('살려주세요', 9), false);
});

test('requires echo cancellation before ordinary interruption, but still flags emergency', () => {
  const gate = new BargeGuard();
  gate.begin({ text: '어디가 불편하신가요?', protectedConsent: false, emergency: false, supported: false });
  assert.equal(gate.partial('머리가 아파요', 11), 'ignore');
  assert.equal(gate.final('머리가 아파요', 11), false);
  assert.equal(gate.partial('숨을 못 쉬어요', 12), 'urgent');
});

test('does not interrupt when playback text is unknown', () => {
  const gate = new BargeGuard();
  gate.begin({ text: '', protectedConsent: false, emergency: false });
  assert.equal(gate.partial('머리가 아파요', 13), 'ignore');
  gate.end();
  assert.equal(gate.final('머리가 아파요', 13), false);
});

test('suppresses echoed final speech arriving shortly after playback', () => {
  let clock = 2000;
  const gate = new BargeGuard(() => clock);
  gate.begin({ text: '병원에 전화해 보세요', protectedConsent: false, emergency: false });
  gate.end();
  clock += 200;
  assert.equal(gate.final('병원에 전화해 보세요', 14), false);
  assert.equal(gate.final('어지러워요', 15), true);
});

test('ignores too-short partials but admits later non-echo context', () => {
  const gate = new BargeGuard();
  gate.begin({ text: '말씀해 주세요', protectedConsent: false, emergency: false });
  assert.equal(gate.partial('네', 10), 'ignore');
  assert.equal(gate.partial('네 머리가 아파요', 10), 'interrupt');
  assert.equal(gate.final('네 머리가 아파요', 10), true);
});

test('requests a bounded retry for a one-syllable caller answer during speech', () => {
  const gate = new BargeGuard();
  gate.begin({ text: '숨이 차세요?', protectedConsent: false, emergency: false });
  assert.equal(gate.partial('네', 50), 'ignore');
  assert.equal(gate.final('네', 50), false);
  assert.equal(gate.takeShortRetry(), true);
  assert.equal(gate.takeShortRetry(), false);
});

test('does not request short-answer retries from recognizable assistant echo', () => {
  const gate = new BargeGuard();
  gate.begin({ text: '네, 확인해 볼게요', protectedConsent: false, emergency: false });
  assert.equal(gate.partial('네', 51), 'ignore');
  assert.equal(gate.final('네', 51), false);
  assert.equal(gate.takeShortRetry(), false);
});

test('bounds short-answer retries from distinct segments to avoid an echo loop', () => {
  const gate = new BargeGuard();
  gate.begin({ text: '숨이 차세요?', protectedConsent: false, emergency: false });
  for (const segment of [52, 53]) {
    gate.partial('네', segment);
    assert.equal(gate.final('네', segment), false);
    assert.equal(gate.takeShortRetry(), true);
  }
  gate.partial('네', 54);
  assert.equal(gate.final('네', 54), false);
  assert.equal(gate.takeShortRetry(), false);
  assert.equal(gate.takeShortExhausted(), true);
  assert.equal(gate.takeShortExhausted(), false);
  assert.equal(gate.final('네', 54), false);
  assert.equal(gate.takeShortExhausted(), false);
});

test('does not exhaust short retries when the answer is indistinguishable from playback echo', () => {
  const gate = new BargeGuard();
  gate.begin({ text: '숨이 차세요?', protectedConsent: false, emergency: false });
  for (const segment of [55, 56]) {
    gate.partial('네', segment);
    assert.equal(gate.final('네', segment), false);
    assert.equal(gate.takeShortRetry(), true);
  }
  gate.end();
  gate.begin({ text: '네, 확인해 볼게요', protectedConsent: false, emergency: false });
  gate.partial('네', 57);
  assert.equal(gate.final('네', 57), false);
  assert.equal(gate.takeShortExhausted(), false);
});

test('can reset rejected segments when a different call begins', () => {
  const gate = new BargeGuard();
  gate.begin({ text: '어디가 불편하세요', protectedConsent: false, emergency: false });
  gate.partial('어디가', 1);
  gate.reset();
  assert.equal(gate.final('동의합니다', 1), true);
});
