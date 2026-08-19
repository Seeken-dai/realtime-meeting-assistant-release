const WARMUP_CACHE_TTL_MS = 15 * 60 * 1000;
// 阿里词表是云端持久资源，不应因为应用重启或会前预热超时就立刻退回无热词。
// 兜底仍要求项目、服务商和词表指纹完全一致，并限制最大陈旧时间。
const WARMUP_STALE_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000;

function normalizeProjectId(value) {
  if (value === null || value === undefined || value === "") return null;
  return String(value);
}

function normalizeAsrProvider(value) {
  return String(value || "").trim().toLowerCase();
}

function glossaryFingerprint(glossary = []) {
  return JSON.stringify(
    (Array.isArray(glossary) ? glossary : [])
      .map((item) => [
        String(item?.term || item?.text || "").trim(),
        Number(item?.weight || 0),
        String(item?.scope || ""),
      ])
      .filter(([term]) => Boolean(term))
      .sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b))),
  );
}

function createWarmupContext(options = {}, glossary = []) {
  return {
    projectId: normalizeProjectId(options.projectId),
    asrProvider: normalizeAsrProvider(options.asrProvider),
    termFingerprint: glossaryFingerprint(glossary),
  };
}

function createWarmupCacheEntry(result, context, at = Date.now()) {
  const vocabularyId = String(result?.vocabularyId || "").trim();
  if (!vocabularyId) return null;
  return {
    vocabularyId,
    projectId: normalizeProjectId(context?.projectId),
    asrProvider: normalizeAsrProvider(context?.asrProvider),
    termFingerprint: String(context?.termFingerprint || ""),
    at: Number(at),
  };
}

function sameWarmupContext(cache, context) {
  return (
    normalizeProjectId(cache?.projectId) === normalizeProjectId(context?.projectId) &&
    normalizeAsrProvider(cache?.asrProvider) ===
      normalizeAsrProvider(context?.asrProvider) &&
    String(cache?.termFingerprint || "") === String(context?.termFingerprint || "")
  );
}

function cacheAgeMs(cache, now = Date.now()) {
  const age = Number(now) - Number(cache?.at || 0);
  return Number.isFinite(age) ? age : Number.POSITIVE_INFINITY;
}

function resolveWarmupVocabularyId(
  cache,
  context,
  now = Date.now(),
  ttlMs = WARMUP_CACHE_TTL_MS,
) {
  if (!cache?.vocabularyId) return null;
  if (!sameWarmupContext(cache, context)) return null;
  const age = cacheAgeMs(cache, now);
  if (age < 0 || age >= ttlMs) return null;
  return String(cache.vocabularyId);
}

function resolveStaleWarmupVocabularyId(
  cache,
  context,
  now = Date.now(),
  maxAgeMs = WARMUP_STALE_MAX_AGE_MS,
) {
  if (!cache?.vocabularyId || !sameWarmupContext(cache, context)) return null;
  const age = cacheAgeMs(cache, now);
  if (age < 0 || age > maxAgeMs) return null;
  return String(cache.vocabularyId);
}

module.exports = {
  WARMUP_CACHE_TTL_MS,
  WARMUP_STALE_MAX_AGE_MS,
  glossaryFingerprint,
  createWarmupContext,
  createWarmupCacheEntry,
  resolveWarmupVocabularyId,
  resolveStaleWarmupVocabularyId,
};
