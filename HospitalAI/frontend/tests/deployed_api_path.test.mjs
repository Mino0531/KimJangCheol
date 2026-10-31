import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { apiPath, callAuthorization } from "../../deploy/phone/frontend/api-path.js";

test("the phone page uses the deployed gateway for calls and speech", async () => {
  const html = await readFile(new URL("../index.html", import.meta.url), "utf8");
  const prefix = html.match(/<meta\s+name="hospital-api-prefix"\s+content="([^"]*)"\s*\/?>/i)?.[1];
  const previousDocument = globalThis.document;
  globalThis.document = {querySelector: () => prefix === undefined ? null : {content: prefix}};
  try {
    assert.equal(apiPath("/calls"), "/api/calls");
    assert.equal(apiPath("/calls/test/speech"), "/api/calls/test/speech");
    assert.equal(callAuthorization("test-token")["X-Hospital-Authorization"], "Bearer test-token");
  } finally { globalThis.document = previousDocument; }
});
