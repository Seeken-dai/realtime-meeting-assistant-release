import type { TranscriptItem } from "./types";

/** 播放跟随用的区间；speakerKey 只用于处理同一说话人的边界重叠。 */
export interface PlaybackRange {
  id: string;
  startMs: number;
  endMs: number;
  approximate: boolean;
  speakerKey: string;
}

/** 没有显式录音区间的旧记录的安全默认值。 */
export const DEFAULT_LEGACY_PLAYBACK_OFFSET_MS = 2000;

const MIN_PLAYBACK_OFFSET_MS = 500;
const MAX_PLAYBACK_OFFSET_MS = 8000;

function isValidRange(startMs: unknown, endMs: unknown): startMs is number {
  return (
    typeof startMs === "number" &&
    Number.isFinite(startMs) &&
    typeof endMs === "number" &&
    Number.isFinite(endMs) &&
    startMs >= 0 &&
    endMs > startMs
  );
}

function median(values: number[]) {
  const sorted = [...values].sort((a, b) => a - b);
  if (!sorted.length) return 0;
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2
    ? sorted[middle]
    : (sorted[middle - 1] + sorted[middle]) / 2;
}

function speakerKeyOf(item: Pick<TranscriptItem, "speakerId" | "speaker">) {
  return item.speakerId
    ? `id:${item.speakerId}`
    : `name:${String(item.speaker || "").trim()}`;
}

/**
 * 从同一会议的另一份显式时间轴估算旧实时记录的识别延迟。
 *
 * 旧记录的 `at` 是 ASR 结果送达墙上时间，另一版本的 `audioEndMs` 是录音轴
 * 结束时间；两者之差就是本场可观测的识别延迟。没有可配对的显式区间时，
 * 退回 2 秒，而不是用一个会把普通会议整体推早的固定 3 秒。
 */
export function estimatePlaybackOffsetMs(
  items: readonly TranscriptItem[],
  calibrationItems: readonly TranscriptItem[] | undefined,
  recordingStartedAt: number,
  defaultOffsetMs = DEFAULT_LEGACY_PLAYBACK_OFFSET_MS,
) {
  const references = new Map<string, number>();
  for (const item of [
    ...(calibrationItems || []),
    ...items,
  ]) {
    if (
      !item.isFinal ||
      references.has(item.id) ||
      !isValidRange(item.audioStartMs, item.audioEndMs)
    ) {
      continue;
    }
    references.set(item.id, Number(item.audioEndMs));
  }

  const lags = items
    .filter(
      (item) =>
        item.isFinal &&
        !isValidRange(item.audioStartMs, item.audioEndMs),
    )
    .map((item) => {
      const referenceEnd = references.get(item.id);
      const at = Number(item.at);
      if (referenceEnd == null || !Number.isFinite(at)) return null;
      return at - recordingStartedAt - referenceEnd;
    })
    .filter(
      (value): value is number =>
        value != null &&
        Number.isFinite(value) &&
        value >= MIN_PLAYBACK_OFFSET_MS &&
        value <= MAX_PLAYBACK_OFFSET_MS,
    );

  if (!lags.length) {
    return Math.max(
      MIN_PLAYBACK_OFFSET_MS,
      Math.min(MAX_PLAYBACK_OFFSET_MS, Math.round(defaultOffsetMs)),
    );
  }
  return Math.round(
    Math.max(
      MIN_PLAYBACK_OFFSET_MS,
      Math.min(MAX_PLAYBACK_OFFSET_MS, median(lags)),
    ),
  );
}

/** 把转写变成录音轴区间；显式区间优先，旧记录才走墙上时间回退。 */
export function buildPlaybackRanges(
  items: readonly TranscriptItem[],
  startedAt: number,
  endedAt: number | undefined,
  status: "active" | "completed" | "interrupted",
  audioSeconds?: number | null,
  calibrationItems?: readonly TranscriptItem[],
): PlaybackRange[] {
  const durationMs =
    audioSeconds && audioSeconds > 0
      ? audioSeconds * 1000
      : Number.POSITIVE_INFINITY;
  const inferredRecordingStartedAt =
    status === "completed" &&
    endedAt != null &&
    Number.isFinite(endedAt) &&
    Number.isFinite(durationMs)
      ? Math.max(startedAt, endedAt - durationMs)
      : startedAt;
  const fallbackOffsetMs = estimatePlaybackOffsetMs(
    items,
    calibrationItems,
    inferredRecordingStartedAt,
  );
  let previousBoundaryMs = 0;
  return items
    .filter((item) => item.isFinal)
    .sort((a, b) => a.at - b.at)
    .map((item) => {
      const hasExplicitRange = isValidRange(
        item.audioStartMs,
        item.audioEndMs,
      );
      const explicitStart = hasExplicitRange
        ? Number(item.audioStartMs)
        : null;
      const explicitEnd = hasExplicitRange ? Number(item.audioEndMs) : null;
      const estimatedEnd = Math.max(
        0,
        Math.min(
          durationMs,
          item.at - inferredRecordingStartedAt - fallbackOffsetMs,
        ),
      );
      const endMs = Math.max(
        0,
        Math.min(durationMs, explicitEnd ?? estimatedEnd),
      );
      const startMs =
        explicitStart ?? Math.max(0, Math.min(endMs, previousBoundaryMs));
      previousBoundaryMs = Math.max(previousBoundaryMs, endMs);
      return {
        id: item.id,
        startMs,
        endMs: Math.max(startMs + 200, endMs),
        approximate: !hasExplicitRange,
        speakerKey: speakerKeyOf(item),
      };
    })
    .sort((a, b) => a.startMs - b.startMs || a.endMs - b.endMs);
}

/**
 * 找到当前应该高亮的行。
 *
 * 会后分离为了避免切掉首尾音，区间之间允许有少量重叠；同一说话人的
 * 重叠应视为边界保护，而不是让下一行提前高亮。跨说话人的重叠仍保留
 * “后开始者优先”，以便真实插话可以切换。
 */
export function playbackRangeAt(
  ranges: readonly PlaybackRange[],
  currentMs: number,
  heldId?: string | null,
) {
  let low = 0;
  let high = ranges.length - 1;
  let candidate = -1;
  while (low <= high) {
    const mid = (low + high) >> 1;
    if (ranges[mid].startMs <= currentMs) {
      candidate = mid;
      low = mid + 1;
    } else {
      high = mid - 1;
    }
  }
  if (candidate < 0) return null;

  const matching = [] as PlaybackRange[];
  for (let index = candidate; index >= 0; index -= 1) {
    const range = ranges[index];
    if (currentMs <= range.endMs) matching.push(range);
  }
  if (!matching.length) return null;

  const latestStart = matching[0].startMs;
  const latestMatches = matching.filter(
    (range) => range.startMs === latestStart,
  );
  // 同起点嵌套时先播放短区间，结束后再落到长区间，避免短句永远不高亮。
  let latest = latestMatches.reduce((best, range) =>
    range.endMs < best.endMs ? range : best,
  );

  if (heldId) {
    const held = matching.find((range) => range.id === heldId);
    if (held && held.speakerKey === latest.speakerKey) {
      latest = held;
    }
  }
  return latest;
}
