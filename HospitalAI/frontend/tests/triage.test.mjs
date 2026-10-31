import assert from 'node:assert/strict';
import test from 'node:test';
import { urgentText, pesticideUrgent } from '../triage.js';

test('does not treat clearly absent or past complaints as emergencies', () => {
  for (const text of ['가슴이 아프지 않아요', '가슴이 아프진 않아요', '호흡곤란은 없어요 기침만 나요', '어머니가 작년에 쓰러졌어요', '작년에 어머니가 쓰러졌는데 지금 기침해요', '아버지가 쓰러졌다는 말은 아닙니다', '피를 조금 흘렸는데 멈췄어요', '농약 때문에 어지럽지는 않아요', '농약 때문에 어지럽진 않아요', '농약을 안 뿌렸는데 어지러워요', '어제 농약 뿌렸지만 안 어지러워요', '농약을 마시지 않았어요', '농약을 먹은 적 없어요', '작년에 농약을 마셨고 지금은 괜찮아요', '농약 뿌렸지만 토하고 있지는 않아요']) {
    assert.equal(urgentText(text), false, text);
  }
});

test('preserves urgent present danger including another person and mixed symptoms', () => {
  for (const text of ['아버지가 지금 숨을 못 쉬어요', '숨 못 쉬어요', '숨을 쉴 수가 없어요', '숨이 안 쉬어져요', '아버지가 작년에 쓰러졌고 지금 또 쓰러졌어요', '작년에 쓰러졌고 지금도 쓰러져 있어요', '작년에 어머니가 쓰러졌지만 지금 숨을 못 쉬어요', '작년에 무릎 수술했는데 가슴이 아파요', '가슴이 아파요', '가슴 아파요', '가슴이 아프지 않은 건 아니에요', '가슴은 아프지 않지만 숨을 못 쉬어요', '호흡곤란이 없지는 않아요', '심한 출혈이에요', '어제 농약 뿌렸고 지금 어지러워요', '농약을 마셨어요', '농약 뿌리고 토하고 있어요', '농약을 마셨는지 몰라도 토해요', '농약을 뿌렸어요 안 어지러운 게 아니에요', '농약을 뿌렸는데 안 어지럽지 않아요', '농약은 뿌렸지만 안 어지러운데 숨을 못 쉬어요']) {
    assert.equal(urgentText(text), true, text);
  }
});

test('only treats pesticide dizziness as urgent when dizziness is not negated', () => {
  assert.equal(pesticideUrgent('농약 뿌렸지만 어지럽지 않아요'), false);
  assert.equal(pesticideUrgent('농약 뿌렸고 어지러워요'), true);
});

test('recognizes pesticide in the mouth and associated symptoms without disregarding negation', () => {
  for (const text of ['농약이 입에 들어갔어요', '농약 뿌리고 구역질 나요', '농약 뿌린 후 몸이 떨려요']) {
    assert.equal(pesticideUrgent(text), true, text);
  }
  for (const text of ['농약이 입에 들어가지 않았어요', '농약을 뿌렸지만 구역질은 없어요', '농약 뿌렸지만 몸이 떨리지 않아요']) {
    assert.equal(pesticideUrgent(text), false, text);
  }
});

test('recognizes definite pesticide inhalation while preserving uncertainty and negation', () => {
  for (const text of ['농약을 흡입했어요', '농약을 들이마셨어요', '농약을 마셨는지 몰라도 토해요']) {
    assert.equal(pesticideUrgent(text), true, text);
  }
  for (const text of ['농약을 흡입하지 않았어요', '농약을 들이마시지 않았어요', '농약을 마셨는지 몰라요']) {
    assert.equal(pesticideUrgent(text), false, text);
  }
  assert.equal(pesticideUrgent('농약을 들이마셨어요', { partial: true }), true);
});

test('waits for final transcript before acting on ambiguous partial chest symptoms', () => {
  assert.equal(urgentText('가슴이 아프', { partial: true }), false);
  assert.equal(urgentText('가슴이 아파요', { partial: true }), true);
  assert.equal(urgentText('숨을 못 쉬어요', { partial: true }), true);
  assert.equal(pesticideUrgent('어제 농약 뿌렸어요 어지러워요', { partial: true }), true);
  assert.equal(pesticideUrgent('농약을 마셨어요', { partial: true }), true);
  assert.equal(pesticideUrgent('농약 뿌리고 토하고 있어요', { partial: true }), true);
  assert.equal(pesticideUrgent('농약을 마셨는지 모르겠어요'), false);
});
