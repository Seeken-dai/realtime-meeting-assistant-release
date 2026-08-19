import {
  buildPlaybackRanges,
  estimatePlaybackOffsetMs,
  playbackRangeAt,
} from "../src/playback.ts";

const assert = (condition: unknown, message: string) => {
  if (!condition) throw new Error(message);
};

const sameSpeakerRanges = [
  {
    id: "short",
    startMs: 830,
    endMs: 1248,
    approximate: false,
    speakerKey: "id:spk2",
  },
  {
    id: "long",
    startMs: 830,
    endMs: 2082,
    approximate: false,
    speakerKey: "id:spk2",
  },
];
assert(
  playbackRangeAt(sameSpeakerRanges, 900)?.id === "short",
  "same-start nested ranges should expose the short utterance first",
);
assert(
  playbackRangeAt(sameSpeakerRanges, 1300)?.id === "long",
  "the longer nested range should take over after the short one ends",
);

const overlapRanges = [
  {
    id: "previous",
    startMs: 0,
    endMs: 1000,
    approximate: false,
    speakerKey: "id:me",
  },
  {
    id: "next",
    startMs: 300,
    endMs: 1300,
    approximate: false,
    speakerKey: "id:me",
  },
  {
    id: "interrupt",
    startMs: 500,
    endMs: 700,
    approximate: false,
    speakerKey: "id:other",
  },
];
assert(
  playbackRangeAt(overlapRanges, 600, "previous")?.id === "interrupt",
  "a different speaker may still take over during an overlap",
);
assert(
  playbackRangeAt(overlapRanges.slice(0, 2), 600, "previous")?.id ===
    "previous",
  "same-speaker overlap should hold the previous row until its end",
);
assert(
  playbackRangeAt(overlapRanges.slice(0, 2), 1100, "previous")?.id === "next",
  "same-speaker overlap should switch after the previous row ends",
);

const startedAt = 1_000_000;
const calibration = [
  {
    id: "legacy-1",
    speaker: "我",
    speakerId: "me",
    text: "第一句",
    isFinal: true,
    at: startedAt + 3_000,
    audioStartMs: 700,
    audioEndMs: 1_100,
  },
  {
    id: "legacy-2",
    speaker: "我",
    speakerId: "me",
    text: "第二句",
    isFinal: true,
    at: startedAt + 6_000,
    audioStartMs: 3_700,
    audioEndMs: 4_100,
  },
];
const legacy = calibration.map((item) => ({
  ...item,
  audioStartMs: 0,
  audioEndMs: 0,
}));
assert(
  estimatePlaybackOffsetMs(legacy, calibration, startedAt) === 1900,
  "legacy offset should use the per-meeting median calibration",
);
const calibratedRanges = buildPlaybackRanges(
  legacy,
  startedAt,
  startedAt + 10_000,
  "completed",
  10,
  calibration,
);
assert(
  calibratedRanges.find((range) => range.id === "legacy-1")?.endMs === 1100,
  "calibrated fallback should place the first legacy line on the audio axis",
);
assert(
  calibratedRanges.find((range) => range.id === "legacy-2")?.endMs === 4100,
  "calibrated fallback should place later legacy lines consistently",
);

console.log("ok: playback overlap, pre-roll support, and legacy calibration");
