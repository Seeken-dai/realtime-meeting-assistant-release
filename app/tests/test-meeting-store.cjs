const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { DatabaseSync } = require("node:sqlite");
const {
  deleteMeetingRecords,
  loadMeetingRecord,
  saveMeetingRecord,
  attachMeetingAudio,
  attachMeetingHotwords,
  attachMeetingError,
  clearMeetingError,
  listGlossaryTerms,
  saveGlossaryTerm,
  deleteGlossaryTerm,
  listGlossaryTermsForMeeting,
  saveProject,
  deleteProject,
  listProjects,
  addDocuments,
  listLibraryDocuments,
  listDocuments,
  getProjectDocumentIds,
  removeDocument,
  listMeetingMemoryItems,
  listGlossaryCandidates,
  generateMeetingReview,
  saveMeetingMemoryItem,
  promoteGlossaryCandidates,
} = require("../electron/meeting-store.cjs");

const root = fs.mkdtempSync(path.join(os.tmpdir(), "mc-store-playback-"));
const dbPath = path.join(root, "legacy.sqlite");

// 模拟没有录音轴字段的旧数据库；打开 store 时必须幂等补列且不丢数据。
const legacy = new DatabaseSync(dbPath);
legacy.exec(`
  CREATE TABLE transcripts (
    meeting_id TEXT NOT NULL,
    id TEXT NOT NULL,
    speaker TEXT NOT NULL,
    speaker_id TEXT,
    text TEXT NOT NULL,
    is_final INTEGER NOT NULL,
    at INTEGER NOT NULL,
    PRIMARY KEY (meeting_id, id)
  );
  CREATE TABLE suggestion_batches (
    meeting_id TEXT NOT NULL,
    id TEXT NOT NULL,
    elapsed REAL NOT NULL,
    at INTEGER NOT NULL,
    hits_json TEXT NOT NULL,
    PRIMARY KEY (meeting_id, id)
  );
`);
legacy.close();

const startedAt = 1_800_000_000_000;
saveMeetingRecord(dbPath, {
  id: "meeting-playback-test",
  title: "playback timing",
  startedAt,
  endedAt: startedAt + 10_000,
  status: "completed",
  meetingMode: "online",
  audioPath: path.join(root, "recording.wav"),
  audioSeconds: 10,
  transcript: [
    {
      id: "line-1",
      speaker: "我",
      speakerId: "me",
      text: "第一段",
      isFinal: true,
      at: startedAt + 3_000,
      audioStartMs: 500,
      audioEndMs: 2_800,
    },
    {
      id: "line-legacy",
      speaker: "说话人1",
      speakerId: "speaker-1",
      text: "旧记录没有录音轴字段",
      isFinal: true,
      at: startedAt + 6_000,
      audioStartMs: null,
      audioEndMs: null,
    },
  ],
  batches: [
    {
      id: "batch-1",
      elapsed: 1.2,
      at: startedAt + 4_000,
      context: {
        wallStartAt: startedAt + 3_000,
        wallEndAt: startedAt + 4_000,
        audioStartMs: 500,
        audioEndMs: 2_800,
      },
      hits: [
        {
          source: "产品能力.md",
          text: "标准版支持固定三级审批流。",
        },
      ],
      suggestions: [
        {
          intent: "说明标准能力",
          script: "标准版支持固定三级审批流。",
          grounded: true,
          level: "grounded",
          category: "范围",
          references: ["产品能力.md"],
          evidence: [
            {
              source: "产品能力.md",
              quote: "标准版支持固定三级审批流。",
            },
          ],
        },
      ],
    },
    {
      id: "batch-legacy",
      elapsed: 0.8,
      at: startedAt + 2_000,
      hits: [],
      suggestions: [],
    },
  ],
  speakers: [{ id: "me", name: "我", isMe: true }],
  transcriptVersion: "offline",
  transcriptVersions: {
    realtime: {
      transcript: [
        {
          id: "live-1",
          speaker: "说话人1",
          speakerId: "live-speaker-1",
          text: "实时版本",
          isFinal: true,
          at: startedAt + 1_000,
        },
      ],
      speakers: [
        { id: "live-speaker-1", name: "实时说话人", isMe: false },
      ],
      generatedAt: startedAt + 10_000,
    },
    offline: {
      transcript: [
        {
          id: "offline-1",
          speaker: "客户",
          speakerId: "offline-speaker-1",
          text: "会后分离版本",
          isFinal: true,
          at: startedAt + 1_000,
        },
      ],
      speakers: [
        { id: "offline-speaker-1", name: "客户", isMe: false },
      ],
      generatedAt: startedAt + 20_000,
      editedAt: startedAt + 25_000,
    },
  },
  minutes: {
    content: "# 会议纪要\n\n- 已确认结论：通过",
    generatedAt: startedAt + 30_000,
    sourceVersion: "offline",
  },
});

const loaded = loadMeetingRecord(dbPath, "meeting-playback-test");
assert.equal(loaded.transcript.length, 2);
assert.equal(loaded.transcript[0].audioStartMs, 500);
assert.equal(loaded.transcript[0].audioEndMs, 2_800);
assert.equal(loaded.transcript[1].audioStartMs, null);
assert.equal(loaded.transcript[1].audioEndMs, null);
assert.equal(loaded.transcriptVersion, "offline");
assert.equal(loaded.meetingMode, "online");
assert.equal(
  loaded.transcriptVersions.realtime.transcript[0].text,
  "实时版本",
);
assert.equal(
  loaded.transcriptVersions.offline.speakers[0].name,
  "客户",
);
assert.equal(
  loaded.transcriptVersions.offline.editedAt,
  startedAt + 25_000,
);
assert.equal(loaded.minutes.sourceVersion, "offline");
assert.match(loaded.minutes.content, /已确认结论/);
const staleSnapshot = saveMeetingRecord(dbPath, {
  id: "meeting-playback-test",
  title: "playback timing",
  startedAt,
  endedAt: startedAt + 10_000,
  status: "completed",
  audioSeconds: 0,
  transcript: [],
  batches: [],
});
assert.equal(staleSnapshot.minutes.content, loaded.minutes.content);
assert.equal(staleSnapshot.audioSeconds, 10);
assert.equal(loadMeetingRecord(dbPath, "meeting-playback-test").minutes.content, loaded.minutes.content);
assert.deepEqual(loaded.batches[0].suggestions[0].evidence, [
  {
    source: "产品能力.md",
    quote: "标准版支持固定三级审批流。",
  },
]);
assert.equal(loaded.batches[0].suggestions[0].category, "范围");
assert.deepEqual(loaded.batches[0].context, {
  wallStartAt: startedAt + 3_000,
  wallEndAt: startedAt + 4_000,
  audioStartMs: 500,
  audioEndMs: 2_800,
  approximate: false,
});
assert.equal(
  loaded.batches.find((batch) => batch.id === "batch-legacy").context,
  undefined,
);

saveMeetingRecord(dbPath, {
  id: "meeting-audio-recovery",
  title: "audio recovery",
  startedAt,
  status: "completed",
  transcript: [],
  batches: [],
});
const recoveredAudioPath = path.join(root, "recovered.wav");
const recoveredMicPath = path.join(root, "recovered.mic.wav");
const recoveredSystemPath = path.join(root, "recovered.system.wav");
assert.equal(
  attachMeetingAudio(
    dbPath,
    "meeting-audio-recovery",
    {
      mixed: { path: recoveredAudioPath, seconds: 727.7 },
      mic: { path: recoveredMicPath, seconds: 727.6 },
      system: { path: recoveredSystemPath, seconds: 727.7 },
    },
  ).updated,
  true,
);
const recovered = loadMeetingRecord(dbPath, "meeting-audio-recovery");
assert.equal(recovered.audioPath, recoveredAudioPath);
assert.equal(recovered.audioSeconds, 727.7);
assert.equal(recovered.micAudioPath, recoveredMicPath);
assert.equal(recovered.micAudioSeconds, 727.6);
assert.equal(recovered.systemAudioPath, recoveredSystemPath);
assert.equal(recovered.systemAudioSeconds, 727.7);
assert.equal(
  attachMeetingHotwords(dbPath, "meeting-audio-recovery", {
    status: "loaded",
    count: 6,
    vocabularyId: "vocab-test-id",
  }).updated,
  true,
);
const recoveredWithHotwords = loadMeetingRecord(dbPath, "meeting-audio-recovery");
assert.deepEqual(recoveredWithHotwords.hotwords, {
  status: "loaded",
  count: 6,
  vocabularyId: "vocab-test-id",
  reason: null,
});
assert.equal(
  attachMeetingError(dbPath, "meeting-audio-recovery", {
    stage: "system_audio",
    message: "系统回环设备已断开",
    at: startedAt + 12_000,
  }).updated,
  true,
);
assert.deepEqual(loadMeetingRecord(dbPath, "meeting-audio-recovery").lastError, {
  stage: "system_audio",
  message: "系统回环设备已断开",
  at: startedAt + 12_000,
});
assert.equal(
  attachMeetingError(dbPath, "meeting-audio-recovery", {
    stage: "minutes",
    message: "会议纪要服务（gemini / gemini-3.6-flash：请求超时（30 秒），已尝试 2 次）",
    at: startedAt + 13_000,
    provider: "gemini",
    model: "gemini-3.6-flash",
    kind: "timeout",
    timeoutStage: "llm",
    cause: "Request timed out.",
    attempts: 2,
    timeoutSeconds: 30,
    retryable: true,
  }).updated,
  true,
);
assert.deepEqual(loadMeetingRecord(dbPath, "meeting-audio-recovery").lastError, {
  stage: "minutes",
  message: "会议纪要服务（gemini / gemini-3.6-flash：请求超时（30 秒），已尝试 2 次）",
  at: startedAt + 13_000,
  provider: "gemini",
  model: "gemini-3.6-flash",
  kind: "timeout",
  timeoutStage: "llm",
  cause: "Request timed out.",
  attempts: 2,
  timeoutSeconds: 30,
  retryable: true,
});
const diagnosticRecord = loadMeetingRecord(dbPath, "meeting-audio-recovery");
saveMeetingRecord(dbPath, diagnosticRecord);
assert.deepEqual(loadMeetingRecord(dbPath, "meeting-audio-recovery").lastError, {
  stage: "minutes",
  message: "会议纪要服务（gemini / gemini-3.6-flash：请求超时（30 秒），已尝试 2 次）",
  at: startedAt + 13_000,
  provider: "gemini",
  model: "gemini-3.6-flash",
  kind: "timeout",
  timeoutStage: "llm",
  cause: "Request timed out.",
  attempts: 2,
  timeoutSeconds: 30,
  retryable: true,
});
assert.equal(
  attachMeetingError(dbPath, "meeting-audio-recovery", {
    stage: "bridge",
    message: "[阿里云] 连接已关闭",
    at: startedAt + 14_000,
  }).updated,
  true,
);
assert.equal(loadMeetingRecord(dbPath, "meeting-audio-recovery").lastError, undefined);
assert.equal(
  attachMeetingError(dbPath, "meeting-audio-recovery", {
    stage: "final",
    message: "会议纪要服务请求超时",
    at: startedAt + 15_000,
  }).updated,
  true,
);
assert.equal(
  clearMeetingError(dbPath, "meeting-audio-recovery", ["minutes", "facts", "final"]).updated,
  true,
);
assert.equal(loadMeetingRecord(dbPath, "meeting-audio-recovery").lastError, undefined);
assert.equal(
  deleteMeetingRecords(dbPath, ["meeting-audio-recovery"]).deleted,
  1,
);

const deleted = deleteMeetingRecords(dbPath, ["meeting-playback-test"]);
assert.equal(deleted.deleted, 1);
assert.deepEqual(deleted.audioPaths, [path.join(root, "recording.wav")]);
assert.equal(loadMeetingRecord(dbPath, "meeting-playback-test"), null);
assert.equal(deleteMeetingRecords(dbPath, ["meeting-playback-test"]).deleted, 0);

// ── 会后复盘：本地规则、状态保留、候选词批量提升 ──
saveMeetingRecord(dbPath, {
  id: "meeting-review-test",
  title: "需求评审 公式定义器",
  startedAt,
  status: "completed",
  scene: "requirements",
  runtimeConfig: {
    provider: "test",
    model: "fast-test",
    asrProvider: "test-asr",
    asrLang: "zh",
    timeoutSeconds: 12,
    suggestionCount: 3,
    silenceSeconds: 3,
    glossaryStatus: "loaded",
    glossaryCount: 2,
  },
  documents: [{ id: "doc-review", name: "公式定义器接口说明.md", path: "missing.md" }],
  transcript: [
    {
      id: "review-line-1",
      speaker: "我",
      speakerId: "me",
      text: "确认采用公式定义器处理这条规则。",
      isFinal: true,
      at: startedAt + 1_000,
    },
    {
      id: "review-line-2",
      speaker: "对方",
      speakerId: "other",
      text: "下周由产品负责整理公式定义器接口，公式定义器需要保留原始字段。",
      isFinal: true,
      at: startedAt + 2_000,
    },
  ],
  batches: [],
});
const localReview = generateMeetingReview(dbPath, "meeting-review-test");
assert.equal(localReview.scene, "requirements");
assert.equal(localReview.review.status, "local");
assert.ok(localReview.memoryItems.some((item) => item.kind === "decision"));
assert.ok(localReview.memoryItems.some((item) => item.kind === "action_item"));
const localDecision = localReview.memoryItems.find((item) => item.kind === "decision");
saveMeetingMemoryItem(dbPath, "meeting-review-test", {
  ...localDecision,
  status: "confirmed",
  source: "user",
});
const rerunReview = generateMeetingReview(dbPath, "meeting-review-test");
assert.equal(
  rerunReview.memoryItems.find((item) => item.id === localDecision.id).status,
  "confirmed",
);
const formulaCandidate = listGlossaryCandidates(dbPath, "meeting-review-test").find(
  (item) => item.term === "公式定义器",
);
assert.ok(formulaCandidate);
const promoted = promoteGlossaryCandidates(
  dbPath,
  "meeting-review-test",
  [formulaCandidate.id],
);
assert.equal(promoted.length, 1);
assert.equal(listGlossaryTerms(dbPath, "general").some((item) => item.term === "公式定义器"), true);
assert.ok(listMeetingMemoryItems(dbPath, "meeting-review-test").length >= 2);
assert.equal(deleteMeetingRecords(dbPath, ["meeting-review-test"]).deleted, 1);
deleteGlossaryTerm(dbPath, promoted[0].id);

// ── 专有名词：通用 + 项目，开会合并时项目覆盖通用 ──
const generalA = saveGlossaryTerm(dbPath, { term: "三快", weight: 3 });
const generalB = saveGlossaryTerm(dbPath, { term: "Webhook", weight: 4 });
assert.equal(listGlossaryTerms(dbPath, "general").length, 2);
const project = saveProject(dbPath, { name: "名词库项目" });
const projectTerm = saveGlossaryTerm(dbPath, {
  term: "三快",
  weight: 5,
  projectId: project.id,
});
assert.equal(listGlossaryTerms(dbPath, project.id).length, 1);
const merged = listGlossaryTermsForMeeting(dbPath, project.id);
assert.equal(merged.length, 2);
const sankuai = merged.find((item) => item.term === "三快");
assert.equal(sankuai.weight, 5);
assert.equal(sankuai.scope, "project");
assert.equal(merged.find((item) => item.term === "Webhook").scope, "general");
deleteGlossaryTerm(dbPath, generalA.id);
deleteGlossaryTerm(dbPath, generalB.id);
deleteGlossaryTerm(dbPath, projectTerm.id);
assert.equal(listGlossaryTerms(dbPath, "all").length, 0);


// ── 项目删除时保留全局知识库文档 ──
const sampleDocPath = path.join(root, "项目业务规则.md");
fs.writeFileSync(sampleDocPath, "# 业务规则\n\n1. 测试文档内容");
const docProject = saveProject(dbPath, { name: "知识库测试项目" });
const importResult = addDocuments(dbPath, docProject.id, [sampleDocPath]);
assert.equal(importResult.added, 1);
assert.equal(listLibraryDocuments(dbPath).some((d) => d.name === "项目业务规则.md"), true);
assert.equal(listDocuments(dbPath, docProject.id).length, 1);
const projBeforeDelete = listProjects(dbPath).find((p) => p.id === docProject.id);
assert.equal(projBeforeDelete.documentCount, 1);

// 删除项目
const deleteResult = deleteProject(dbPath, docProject.id);
assert.equal(deleteResult.ok, true);
assert.equal(listProjects(dbPath).some((p) => p.id === docProject.id), false);
// 项目删除后，该文档依然完好保留在全局知识库中
const libDocsAfter = listLibraryDocuments(dbPath);
const preservedDoc = libDocsAfter.find((d) => d.name === "项目业务规则.md");
assert.ok(preservedDoc);
assert.equal(preservedDoc.path, sampleDocPath);
// 清理测试文档
removeDocument(dbPath, preservedDoc.id);
assert.equal(listLibraryDocuments(dbPath).some((d) => d.name === "项目业务规则.md"), false);

console.log(
  "ok: meeting store migration + transcript versions + suggestion context + evidence + cascade delete + glossary",
);
