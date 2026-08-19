const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  WARMUP_CACHE_TTL_MS,
  createWarmupContext,
  createWarmupCacheEntry,
  resolveWarmupVocabularyId,
  resolveStaleWarmupVocabularyId,
} = require("../electron/meeting-startup.cjs");
const { recommendMeetingScene } = require("../electron/meeting-scene.cjs");

const now = 1_000_000;
const glossary = [{ term: "蓝凌", weight: 8, scope: "project" }];
const p1 = createWarmupContext(
  { projectId: "p1", asrProvider: "ALIYUN" },
  glossary,
);
const cache = createWarmupCacheEntry({ vocabularyId: "vocab-p1" }, p1, now);

assert.equal(resolveWarmupVocabularyId(cache, p1, now + 1), "vocab-p1");
assert.equal(
  resolveWarmupVocabularyId(
    cache,
    createWarmupContext({ projectId: "p2", asrProvider: "aliyun" }, glossary),
    now + 1,
  ),
  null,
  "a vocabulary must not cross project boundaries",
);
assert.equal(
  resolveWarmupVocabularyId(
    cache,
    createWarmupContext({ projectId: "p1", asrProvider: "xfyun" }, glossary),
    now + 1,
  ),
  null,
  "a vocabulary must not cross ASR providers",
);
assert.equal(
  resolveWarmupVocabularyId(
    cache,
    createWarmupContext(
      { projectId: "p1", asrProvider: "aliyun" },
      [{ term: "EKP", weight: 8, scope: "project" }],
    ),
    now + 1,
  ),
  null,
  "a vocabulary must be invalidated when terms change",
);
assert.equal(
  resolveWarmupVocabularyId(cache, p1, now + WARMUP_CACHE_TTL_MS),
  null,
  "the 15-minute TTL is exclusive",
);
assert.equal(
  resolveStaleWarmupVocabularyId(cache, p1, now + WARMUP_CACHE_TTL_MS + 1),
  "vocab-p1",
  "a matching stale vocabulary may be reused after the warmup TTL",
);
assert.equal(
  resolveStaleWarmupVocabularyId(cache, p1, now + 31 * 24 * 60 * 60 * 1000),
  null,
  "a stale vocabulary must eventually expire",
);

assert.deepEqual(recommendMeetingScene({ title: "XX 项目需求澄清会" }), {
  scene: "general",
  label: "通用会议",
  reason: "本场标题、项目和已选资料没有明显的业务信号，先使用通用会议。",
  confidence: "low",
});
assert.equal(
  recommendMeetingScene({ title: "客户报价与采购沟通" }).scene,
  "sales",
);
assert.equal(
  recommendMeetingScene({ title: "接口验收评审" }).scene,
  "requirements",
);
assert.equal(
  recommendMeetingScene({ title: "客户需求沟通" }).scene,
  "general",
  "conflicting signals should not silently pick one scene",
);
assert.equal(
  recommendMeetingScene({ documentNames: ["接口说明.md"] }).scene,
  "general",
  "a single weak document-name hit should keep the general scene",
);

const warmupScript = path.resolve(__dirname, "../../poc/warmup_meeting.py");
assert.equal(fs.existsSync(warmupScript), true, "warmup script must exist");
const mainSource = fs.readFileSync(
  path.resolve(__dirname, "../electron/main.cjs"),
  "utf8",
);
assert.match(
  mainSource,
  /const warmupPath = path\.join\(pocRoot, "warmup_meeting\.py"\)/,
  "Electron main must define the warmup script path",
);

console.log("ok: warmup cache scope + scene recommendation + runtime path");
