import assert from "node:assert/strict";
import { meetingStartupAction } from "../src/meeting-startup.ts";
import { shouldAutoApplySceneRecommendation } from "../src/meeting-scene.ts";

assert.deepEqual(meetingStartupAction("ready", false), {
  sendBeginRecording: true,
  startClock: false,
  cancel: false,
});
assert.equal(meetingStartupAction("ready", true).sendBeginRecording, false);
assert.equal(meetingStartupAction("listening", true).startClock, true);
assert.equal(meetingStartupAction("cancelled", false).cancel, true);
assert.equal(meetingStartupAction("connecting", true).startClock, false);

const low = {
  scene: "requirements" as const,
  label: "需求评审",
  reason: "one weak hit",
  confidence: "low" as const,
};
const medium = { ...low, confidence: "medium" as const };
assert.equal(shouldAutoApplySceneRecommendation(low, false), false);
assert.equal(shouldAutoApplySceneRecommendation(medium, false), true);
assert.equal(shouldAutoApplySceneRecommendation(medium, true), false);

console.log("ok: ready/listening/cancelled actions + scene auto-apply guard");
