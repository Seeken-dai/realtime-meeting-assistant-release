import {
  buildSpeakerDistribution,
  findNearestSuggestionBatchForTranscript,
  findTranscriptIdsForContext,
  getSuggestionContext,
  mergeTimeRanges,
} from "../src/timeline.ts";

const assert = (condition: unknown, message: string) => {
  if (!condition) throw new Error(message);
};

const merged = mergeTimeRanges(
  [
    { id: "a", startMs: 0, endMs: 100 },
    { id: "b", startMs: 90, endMs: 150 },
    { id: "c", startMs: 150, endMs: 180 },
    { id: "d", startMs: 240, endMs: 260 },
  ],
  0,
);
assert(merged.length === 2, "overlapping and adjacent ranges should merge");
assert(merged[0].startMs === 0 && merged[0].endMs === 180, "merged range bounds are wrong");
assert(merged[0].ids.join(",") === "a,b,c", "merged range should retain source ids");

const items = [
  {
    id: "t1",
    speaker: "我",
    speakerId: "me",
    text: "第一段",
    isFinal: true,
    at: 1_800_000_000,
    audioStartMs: 0,
    audioEndMs: 100,
  },
  {
    id: "t2",
    speaker: "我",
    speakerId: "me",
    text: "第二段",
    isFinal: true,
    at: 1_800_000_000.2,
    audioStartMs: 90,
    audioEndMs: 200,
  },
  {
    id: "t3",
    speaker: "对方",
    speakerId: "other",
    text: "第三段",
    isFinal: true,
    at: 1_800_000_000.4,
    audioStartMs: 200,
    audioEndMs: 300,
  },
  {
    id: "no-time",
    speaker: "对方",
    speakerId: "other",
    text: "没有时间",
    isFinal: true,
    at: 1_800_000_000.6,
    audioStartMs: null,
    audioEndMs: null,
  },
];

const distribution = buildSpeakerDistribution(
  items,
  new Map(),
  (item) => item.speakerId || item.speaker,
  (item) => item.speaker,
  1_000,
);
assert(distribution.rows.length === 2, "missing-time transcript should be excluded");
assert(Math.abs(distribution.rows[0].percentage + distribution.rows[1].percentage - 100) < 0.001, "speaker percentages should sum to 100%");
assert(distribution.rows[0].durationMs === 200, "same-speaker overlap should count once");

const estimatedDistribution = buildSpeakerDistribution(
  items.map((item) => ({ ...item, audioStartMs: null, audioEndMs: null })),
  new Map([
    ["t1", { id: "t1", startMs: 0, endMs: 100, approximate: true }],
    ["t2", { id: "t2", startMs: 100, endMs: 200, approximate: true }],
  ]),
  (item) => item.speakerId || item.speaker,
  (item) => item.speaker,
  1_000,
);
assert(estimatedDistribution.approximate, "estimated timeline should be marked approximate");

const oldContext = getSuggestionContext({
  at: 1_800_000_002,
  elapsed: 1.5,
  context: null,
});
assert(oldContext.approximate === true, "old batches should use approximate context");
const oldLocation = findTranscriptIdsForContext(items, oldContext);
assert(oldLocation.ids.length === 1 && oldLocation.approximate, "old context should locate one nearest prior transcript");
const oldNearest = findNearestSuggestionBatchForTranscript(
  items,
  [
    {
      id: "legacy-batch",
      at: 1_800_000_000.45,
      elapsed: 0.1,
      context: null,
      suggestions: [{ script: "旧记录" }],
    },
  ],
  ["t3"],
  new Map([
    ["t1", { id: "t1", startMs: 0, endMs: 100, approximate: true }],
    ["t2", { id: "t2", startMs: 100, endMs: 200, approximate: true }],
    ["t3", { id: "t3", startMs: 200, endMs: 300, approximate: true }],
  ]),
);
assert(oldNearest?.batch.id === "legacy-batch" && oldNearest.approximate, "old batch should match by wall-clock fallback");

const batches = [
  {
    id: "batch-old",
    at: 1_800_000_001,
    elapsed: 1,
    context: { audioStartMs: 0, audioEndMs: 200, approximate: false },
    suggestions: [{ script: "旧" }],
  },
  {
    id: "batch-newer-tie",
    at: 1_800_000_003,
    elapsed: 1,
    context: { audioStartMs: 200, audioEndMs: 400, approximate: false },
    suggestions: [{ script: "新" }],
  },
];
const nearest = findNearestSuggestionBatchForTranscript(items, batches, ["t3"]);
assert(nearest?.batch.id === "batch-newer-tie", "nearest batch should use context end and resolve ties by recency");
const located = findTranscriptIdsForContext(items, batches[1].context);
assert(located.ids.join(",") === "t2,t3", "exact context should highlight the complete range");

console.log("ok: timeline merge + location + speaker distribution");
