import { strict as assert } from "node:assert";
import { test } from "node:test";
import { needsFollowUp } from "../followup.js";

test("defers the recommendation when the reply asks a direct question", () => {
  assert.equal(needsFollowUp("언제부터 아프셨나요?"), true);
});

test("defers the recommendation when the reply requests more information without punctuation", () => {
  assert.equal(needsFollowUp("증상이 언제부터 있었는지 알려 주세요"), true);
  assert.equal(needsFollowUp("함께 나타나는 증상을 말씀해 주세요."), true);
  assert.equal(needsFollowUp("언제 시작됐는지 말씀해 주실 수 있나요"), true);
});

test("does not defer when the patient is advised to speak to a clinician", () => {
  assert.equal(needsFollowUp("병원에 복용 중인 약을 말씀해 주세요."), false);
  assert.equal(needsFollowUp("가까운 의료기관에서 진료를 받으세요."), false);
});

test("does not treat a closing courtesy as a question", () => {
  assert.equal(needsFollowUp("말씀해 주셔서 감사합니다. 건강 조심하세요."), false);
});
