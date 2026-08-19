import type {
  SuggestionBatch,
  SuggestionContextRange,
  TranscriptItem,
} from "./types";

export interface TimelineRange {
  id: string;
  startMs: number;
  endMs: number;
  approximate: boolean;
}

export interface MergedTimelineRange {
  startMs: number;
  endMs: number;
  approximate: boolean;
  ids: string[];
}

export interface SuggestionLocation {
  batch: SuggestionBatch;
  context: SuggestionContextRange;
  approximate: boolean;
  distanceMs: number;
}

export interface SpeakerDistributionRow {
  key: string;
  name: string;
  durationMs: number;
  percentage: number;
  segments: MergedTimelineRange[];
  approximate: boolean;
}

export interface SpeakerDistribution {
  rows: SpeakerDistributionRow[];
  totalMs: number;
  recordingDurationMs: number;
  approximate: boolean;
}

function finite(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

export function isValidTimelineRange(
  startMs: unknown,
  endMs: unknown,
): startMs is number {
  return (
    finite(startMs) &&
    finite(endMs) &&
    startMs >= 0 &&
    endMs > startMs
  );
}

export function toEpochMs(value: unknown): number | null {
  const number = Number(value);
  if (!Number.isFinite(number)) return null;
  return Math.abs(number) < 10_000_000_000 ? number * 1000 : number;
}

function itemWallAt(item: TranscriptItem): number | null {
  return toEpochMs(item.at);
}

export function mergeTimeRanges(
  ranges: Array<{
    startMs: number;
    endMs: number;
    approximate?: boolean;
    id?: string;
  }>,
  adjacentMs = 0,
): MergedTimelineRange[] {
  const sorted = ranges
    .filter((range) => isValidTimelineRange(range.startMs, range.endMs))
    .map((range) => ({
      startMs: range.startMs,
      endMs: range.endMs,
      approximate: Boolean(range.approximate),
      ids: range.id ? [range.id] : [],
    }))
    .sort((a, b) => a.startMs - b.startMs || a.endMs - b.endMs);
  const merged: MergedTimelineRange[] = [];
  for (const range of sorted) {
    const previous = merged.at(-1);
    if (previous && range.startMs <= previous.endMs + adjacentMs) {
      previous.endMs = Math.max(previous.endMs, range.endMs);
      previous.approximate ||= range.approximate;
      previous.ids.push(...range.ids);
    } else {
      merged.push(range);
    }
  }
  return merged;
}

function validContext(context?: SuggestionContextRange | null) {
  if (!context || typeof context !== "object") return null;
  const wallStartAt = toEpochMs(context.wallStartAt);
  const wallEndAt = toEpochMs(context.wallEndAt);
  const audioStartMs = Number(context.audioStartMs);
  const audioEndMs = Number(context.audioEndMs);
  const hasWall =
    wallStartAt != null &&
    wallEndAt != null &&
    wallEndAt >= wallStartAt;
  const hasAudio = isValidTimelineRange(audioStartMs, audioEndMs);
  if (!hasWall && !hasAudio) return null;
  return {
    wallStartAt: hasWall ? wallStartAt : null,
    wallEndAt: hasWall ? wallEndAt : null,
    audioStartMs: hasAudio ? audioStartMs : null,
    audioEndMs: hasAudio ? audioEndMs : null,
    approximate: Boolean(context.approximate),
  } satisfies SuggestionContextRange;
}

/** 返回显式上下文；旧批次按“生成时刻 - 生成耗时”回退到单点近似范围。 */
export function getSuggestionContext(
  batch: Pick<SuggestionBatch, "at" | "elapsed" | "context">,
): SuggestionContextRange {
  const explicit = validContext(batch.context);
  if (explicit) return explicit;
  const at = toEpochMs(batch.at) ?? Date.now();
  const anchor = at - Math.max(0, Number(batch.elapsed) || 0) * 1000;
  return {
    wallStartAt: anchor,
    wallEndAt: anchor,
    audioStartMs: null,
    audioEndMs: null,
    approximate: true,
  };
}

function itemAudioRange(item: TranscriptItem): TimelineRange | null {
  if (!isValidTimelineRange(item.audioStartMs, item.audioEndMs)) return null;
  return {
    id: item.id,
    startMs: Number(item.audioStartMs),
    endMs: Number(item.audioEndMs),
    approximate: false,
  };
}

function itemFallbackRange(
  item: TranscriptItem,
  fallbackRanges?: ReadonlyMap<string, TimelineRange>,
): TimelineRange | null {
  const explicit = itemAudioRange(item);
  if (explicit) return explicit;
  return fallbackRanges?.get(item.id) || null;
}

function contextEnd(context: SuggestionContextRange): number | null {
  if (isValidTimelineRange(context.audioStartMs, context.audioEndMs)) {
    return Number(context.audioEndMs);
  }
  return toEpochMs(context.wallEndAt);
}

function itemEnd(
  item: TranscriptItem,
  fallbackRanges?: ReadonlyMap<string, TimelineRange>,
): number | null {
  const range = itemFallbackRange(item, fallbackRanges);
  if (range) return range.endMs;
  return itemWallAt(item);
}

/** 找到建议上下文覆盖的转写范围；旧上下文只返回此前最近的一段。 */
export function findTranscriptIdsForContext(
  items: TranscriptItem[],
  context: SuggestionContextRange,
  fallbackRanges?: ReadonlyMap<string, TimelineRange>,
): { ids: string[]; targetId: string | null; approximate: boolean } {
  const candidates = items.filter((item) => item.isFinal !== false);
  if (!candidates.length) return { ids: [], targetId: null, approximate: true };
  const normalized = validContext(context);
  if (!normalized) return { ids: [], targetId: null, approximate: true };

  const hasAudioContext = isValidTimelineRange(
    normalized.audioStartMs,
    normalized.audioEndMs,
  );
  const hasWallContext =
    normalized.wallStartAt != null && normalized.wallEndAt != null;
  const matches = candidates.filter((item) => {
    const audio = itemFallbackRange(item, fallbackRanges);
    if (hasAudioContext && audio) {
      return (
        audio.startMs <= (normalized.audioEndMs as number) &&
        audio.endMs >= (normalized.audioStartMs as number)
      );
    }
    const at = itemWallAt(item);
    return (
      hasWallContext &&
      at != null &&
      at >= (normalized.wallStartAt as number) &&
      at <= (normalized.wallEndAt as number)
    );
  });

  if (normalized.approximate) {
    const target = contextEnd(normalized) ?? Date.now();
    const prior = candidates
      .filter((item) => {
        const at = itemWallAt(item);
        return at != null && at <= target;
      })
      .at(-1);
    const targetItem = prior || candidates[0];
    return {
      ids: targetItem ? [targetItem.id] : [],
      targetId: targetItem?.id || null,
      approximate: true,
    };
  }

  if (matches.length) {
    return {
      ids: matches.map((item) => item.id),
      targetId: matches.at(-1)?.id || null,
      approximate: false,
    };
  }

  const target = contextEnd(normalized) ?? Date.now();
  const nearest = candidates
    .map((item) => ({
      item,
      distance: Math.abs((itemEnd(item, fallbackRanges) ?? target) - target),
    }))
    .sort((a, b) => a.distance - b.distance)[0]?.item;
  return {
    ids: nearest ? [nearest.id] : [],
    targetId: nearest?.id || null,
    approximate: true,
  };
}

function targetRangeForItems(
  items: TranscriptItem[],
  ids: string[],
  fallbackRanges?: ReadonlyMap<string, TimelineRange>,
) {
  const selected = items.filter(
    (item) => ids.includes(item.id) && item.isFinal !== false,
  );
  const audio = selected
    .map((item) => itemFallbackRange(item, fallbackRanges))
    .filter((range): range is TimelineRange => Boolean(range));
  const wall = selected
    .map(itemWallAt)
    .filter((value): value is number => value != null);
  if (!audio.length && !wall.length) return null;
  return {
    startMs: audio.length
      ? Math.min(...audio.map((range) => range.startMs))
      : Math.min(...wall),
    endMs: audio.length
      ? Math.max(...audio.map((range) => range.endMs))
      : Math.max(...wall),
    audioStartMs: audio.length
      ? Math.min(...audio.map((range) => range.startMs))
      : null,
    audioEndMs: audio.length
      ? Math.max(...audio.map((range) => range.endMs))
      : null,
    wallStartMs: wall.length ? Math.min(...wall) : null,
    wallEndMs: wall.length ? Math.max(...wall) : null,
  };
}

/** 从一段转写反查上下文结束位置最近的一批建议。 */
export function findNearestSuggestionBatchForTranscript(
  items: TranscriptItem[],
  batches: SuggestionBatch[],
  ids: string[],
  fallbackRanges?: ReadonlyMap<string, TimelineRange>,
): SuggestionLocation | null {
  const target = targetRangeForItems(items, ids, fallbackRanges);
  if (!target) return null;
  const candidates = batches.filter((batch) => batch.suggestions.length > 0);
  if (!candidates.length) return null;
  const scored = candidates.map((batch) => {
    const context = getSuggestionContext(batch);
    const audioContext = isValidTimelineRange(
      context.audioStartMs,
      context.audioEndMs,
    );
    const compareAudio = audioContext && target.audioStartMs != null;
    const contextStart = compareAudio
      ? (context.audioStartMs as number)
      : toEpochMs(context.wallStartAt);
    const contextEndMs = compareAudio
      ? (context.audioEndMs as number)
      : toEpochMs(context.wallEndAt);
    const targetStart = compareAudio
      ? (target.audioStartMs as number)
      : (target.wallStartMs ?? target.startMs);
    const targetEnd = compareAudio
      ? (target.audioEndMs as number)
      : (target.wallEndMs ?? target.endMs);
    const sameMode =
      contextStart != null &&
      contextEndMs != null &&
      (compareAudio || target.wallStartMs != null);
    const overlaps =
      sameMode &&
      contextStart != null &&
      contextEndMs != null &&
      contextStart <= targetEnd &&
      contextEndMs >= targetStart;
    const distanceMs =
      sameMode && overlaps
        ? Math.abs((contextEndMs as number) - targetEnd)
        : Math.min(
            Math.abs((contextEndMs ?? targetEnd) - targetEnd),
            Math.abs((contextStart ?? targetStart) - targetEnd),
          );
    return {
      batch,
      context,
      approximate: Boolean(context.approximate) || !sameMode,
      distanceMs,
    };
  });
  scored.sort(
    (a, b) => a.distanceMs - b.distanceMs || b.batch.at - a.batch.at,
  );
  return scored[0] || null;
}

export function buildSpeakerDistribution(
  items: TranscriptItem[],
  fallbackRanges: ReadonlyMap<string, TimelineRange>,
  resolveKey: (item: TranscriptItem) => string,
  resolveName: (item: TranscriptItem) => string,
  recordingDurationMs?: number | null,
): SpeakerDistribution {
  const grouped = new Map<
    string,
    { name: string; ranges: Array<{ startMs: number; endMs: number; approximate?: boolean; id?: string }> }
  >();
  for (const item of items) {
    if (item.isFinal === false) continue;
    const range = itemFallbackRange(item, fallbackRanges);
    if (!range) continue;
    const key = resolveKey(item);
    const current = grouped.get(key) || { name: resolveName(item), ranges: [] };
    current.ranges.push({ ...range, id: item.id });
    grouped.set(key, current);
  }

  const rows = Array.from(grouped.entries())
    .map(([key, value]) => {
      const segments = mergeTimeRanges(value.ranges);
      const durationMs = segments.reduce(
        (total, segment) => total + segment.endMs - segment.startMs,
        0,
      );
      return {
        key,
        name: value.name,
        durationMs,
        percentage: 0,
        segments,
        approximate: segments.some((segment) => segment.approximate),
      } satisfies SpeakerDistributionRow;
    })
    .filter((row) => row.durationMs > 0)
    .sort((a, b) => b.durationMs - a.durationMs || a.name.localeCompare(b.name));
  const totalMs = rows.reduce((total, row) => total + row.durationMs, 0);
  for (const row of rows) {
    row.percentage = totalMs ? (row.durationMs / totalMs) * 100 : 0;
  }
  const maxEndMs = rows.reduce(
    (max, row) => Math.max(max, ...row.segments.map((segment) => segment.endMs)),
    0,
  );
  return {
    rows,
    totalMs,
    recordingDurationMs:
      finite(recordingDurationMs) && recordingDurationMs > 0
        ? recordingDurationMs
        : maxEndMs,
    approximate: rows.some((row) => row.approximate),
  };
}
