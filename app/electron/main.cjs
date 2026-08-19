const { app, BrowserWindow, ipcMain, dialog, shell } = require("electron");
const { spawn } = require("node:child_process");
const fs = require("node:fs/promises");
const fsSync = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {
  finalizeOrphanedMeetings,
  listMeetingRecords,
  deleteMeetingRecords,
  loadMeetingRecord,
  saveMeetingRecord,
  attachMeetingAudio,
  attachMeetingHotwords,
  attachMeetingError,
  clearMeetingError,
  listProjects,
  saveProject,
  deleteProject,
  listGlossaryTerms,
  saveGlossaryTerm,
  deleteGlossaryTerm,
  listGlossaryTermsForMeeting,
  listMeetingMemoryItems,
  saveMeetingMemoryItem,
  listGlossaryCandidates,
  saveGlossaryCandidates,
  generateMeetingReview,
  markMeetingReview,
  promoteGlossaryCandidates,
  listDocuments,
  listLibraryDocuments,
  getProjectDocumentIds,
  setProjectDocuments,
  addDocuments,
  removeDocument,
  saveMeetingDocuments,
  renameDocument,
  closeDatabase,
} = require("./meeting-store.cjs");
const {
  createWarmupContext,
  createWarmupCacheEntry,
  resolveWarmupVocabularyId,
  resolveStaleWarmupVocabularyId,
} = require("./meeting-startup.cjs");
const { recommendMeetingScene } = require("./meeting-scene.cjs");

let mainWindow;
let floatingWindow;
let bridgeProcess;
let deviceTestProcess;
let bridgeBuffer = "";
let bridgeStderr = "";
let bridgeReportedFatalError = false;
let bridgeReportedEnded = false;
let recordWriteQueue = Promise.resolve();
let currentScopeFile;
let currentHotwordsFile;
let meetingWarmupCache = null;
let latestStrategyEvent = null;
let activeMeetingRecordId = null;
let activeMeetingHotwordContext = null;
const pendingMeetingHotwords = new Map();

// Keep the data directory stable across development, unpacked, and installed builds.
// Electron otherwise derives userData from productName in packaged builds, which would
// split existing meetings into %APPDATA%/实时会议话术助手 instead of the original
// %APPDATA%/meeting-copilot-desktop directory.
const stableUserDataPath = path.join(
  app.getPath("appData"),
  "meeting-copilot-desktop",
);
app.setPath("userData", stableUserDataPath);

// recording_file 可能先于渲染层的首个 records:save 到达；先暂存三路路径，
// 由首个档案快照合并，避免启动阶段的事件竞态丢掉音轨关联。
const pendingMeetingAudio = new Map();
const pendingMeetingErrors = new Map();

const HOTWORD_VOCABULARY_CACHE_MAX_ENTRIES = 20;

function hotwordVocabularyCachePath() {
  return path.join(app.getPath("userData"), "hotword-vocabulary-cache.json");
}

function readHotwordVocabularyCache() {
  try {
    const raw = JSON.parse(
      fsSync.readFileSync(hotwordVocabularyCachePath(), "utf8"),
    );
    const entries = Array.isArray(raw) ? raw : raw?.entries;
    if (!Array.isArray(entries)) return [];
    return entries.filter((entry) => entry && typeof entry === "object");
  } catch {
    return [];
  }
}

function rememberHotwordVocabulary(entry) {
  if (!entry?.vocabularyId) return;
  const entries = readHotwordVocabularyCache();
  const same = (item) =>
    String(item?.projectId || "") === String(entry.projectId || "") &&
    String(item?.asrProvider || "") === String(entry.asrProvider || "") &&
    String(item?.termFingerprint || "") === String(entry.termFingerprint || "");
  const next = [entry, ...entries.filter((item) => !same(item))].slice(
    0,
    HOTWORD_VOCABULARY_CACHE_MAX_ENTRIES,
  );
  const target = hotwordVocabularyCachePath();
  const temp = `${target}.${process.pid}.tmp`;
  try {
    fsSync.writeFileSync(temp, JSON.stringify({ entries: next }), "utf8");
    fsSync.rmSync(target, { force: true });
    fsSync.renameSync(temp, target);
  } catch (error) {
    fsSync.rmSync(temp, { force: true });
    console.warn("[热词] 最近有效词表缓存写入失败：", error.message || error);
  }
}

function resolvePersistentHotwordVocabularyId(context, allowStale = false) {
  const entries = readHotwordVocabularyCache();
  for (const entry of entries) {
    const vocabularyId = allowStale
      ? resolveStaleWarmupVocabularyId(entry, context)
      : resolveWarmupVocabularyId(entry, context);
    if (vocabularyId) return { vocabularyId, entry };
  }
  return null;
}

// Chromium 不允许两个独立主进程同时占用同一个 userData 目录。
// 开发时重复执行 npm run dev，或双击启动两次，可能表现为 Electron 直接以 -1 退出。
// 第二次启动只唤醒已有窗口，避免争用 profile 与 SQLite/WAL 文件。
const ownsSingleInstanceLock = app.requestSingleInstanceLock();
if (!ownsSingleInstanceLock) {
  console.info("[startup] Another meeting copilot instance is already running.");
  app.exit(0);
} else {
  app.on("second-instance", () => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  });
}

// 开发态从仓库根目录取 Python/Poc；打包态从安装目录的 resources/runtime 取。
// runtime 放在 app.asar 外，避免 Python、ONNX 和动态库被压进 asar 后无法运行。
const projectRoot = app.isPackaged
  ? path.join(process.resourcesPath, "runtime")
  : path.resolve(__dirname, "..", "..");
const pocRoot = path.join(projectRoot, "poc");
const pythonPath = path.join(pocRoot, ".venv", "Scripts", "python.exe");
const bridgePath = path.join(pocRoot, "desktop_bridge.py");
const serviceCheckPath = path.join(pocRoot, "service_check.py");
const enrollVoicePath = path.join(pocRoot, "enroll_voice.py");
const diarizeOfflinePath = path.join(pocRoot, "diarize_offline.py");
const cleanTranscriptPath = path.join(pocRoot, "clean_transcript.py");
const generateMinutesPath = path.join(pocRoot, "generate_minutes.py");
const generateReviewPath = path.join(pocRoot, "generate_review.py");
const documentExtractPath = path.join(pocRoot, "document_extract.py");
const warmupPath = path.join(pocRoot, "warmup_meeting.py");

// 会后复盘是低频任务，允许模型有完整的结构化输出时间；
// 这里的外层截止必须覆盖 Python 内部的单次超时 × 重试次数。
const REVIEW_LLM_TIMEOUT_SECONDS = 30;
const REVIEW_LLM_RETRY_ATTEMPTS = 2;
const REVIEW_ENHANCEMENT_TIMEOUT_MS =
  (REVIEW_LLM_TIMEOUT_SECONDS * REVIEW_LLM_RETRY_ATTEMPTS + 15) * 1000;

function voiceprintDir() {
  return path.join(app.getPath("userData"), "voiceprint");
}

function defaultEnrollWavPath() {
  return path.join(voiceprintDir(), "enroll_me.wav");
}

/**
 * 多段声纹样本。第 1 段沿用 enroll_me.wav（兼容旧数据），后续为
 * enroll_me-2.wav / -3.wav …。多次短录比一次长录更能覆盖音色变化 ——
 * 实测单次 20s 只切得出 3 段 embedding，样本太少。
 * 返回按序号排好的**已存在**样本路径。
 */
function enrollSampleIndexes() {
  // 扫目录而不是从 1 递增探测：后者遇到中间缺口会提前停，把后面的样本变成
  // 「当前不可见、下次录制填补缺口后又带着旧内容复活」的孤儿文件。
  const dir = voiceprintDir();
  if (!fsSync.existsSync(dir)) return [];
  const out = [];
  for (const name of fsSync.readdirSync(dir)) {
    if (name === "enroll_me.wav") out.push(1);
    else {
      const m = /^enroll_me-(\d+)\.wav$/.exec(name);
      if (m) out.push(Number(m[1]));
    }
  }
  return out.sort((a, b) => a - b);
}

function enrollSamplePathAt(index) {
  return index <= 1
    ? defaultEnrollWavPath()
    : path.join(voiceprintDir(), `enroll_me-${index}.wav`);
}

function enrollSamplePaths() {
  return enrollSampleIndexes().map(enrollSamplePathAt);
}

/** 下一个可写入的样本路径（追加录制用）——总是取现有最大序号 +1。 */
function nextEnrollSamplePath() {
  const idx = enrollSampleIndexes();
  return enrollSamplePathAt(idx.length ? idx[idx.length - 1] + 1 : 1);
}

/** 读 wav 头算时长；坏文件返回 0 而不是抛错。 */
function wavSeconds(p) {
  try {
    const st = fsSync.statSync(p);
    const fd = fsSync.openSync(p, "r");
    const buf = Buffer.alloc(44);
    fsSync.readSync(fd, buf, 0, 44, 0);
    fsSync.closeSync(fd);
    const sr = buf.readUInt32LE(24) || 16000;
    return Math.round((Math.max(0, st.size - 44) / 2 / sr) * 10) / 10;
  } catch {
    return 0;
  }
}

/** 桌面端可配置的密钥/连接字段（与 config.py 字段名一致，经环境变量注入 Python） */
const KNOWN_SECRET_KEYS = [
  "XFYUN_APP_ID",
  "XFYUN_API_KEY",
  "XFYUN_LLM_ASR_APP_ID",
  "XFYUN_LLM_ASR_KEY_ID",
  "XFYUN_LLM_ASR_KEY_SECRET",
  "ALIYUN_ASR_KEY",
  "VOLC_APP_KEY",
  "VOLC_ACCESS_KEY",
  "TENCENT_APP_ID",
  "TENCENT_SECRET_ID",
  "TENCENT_SECRET_KEY",
  "MIMO_API_KEY",
  "XFYUN_SPARK_PASSWORD",
  "XFYUN_X2_PASSWORD",
  "XFYUN_X15_PASSWORD",
  "ALIYUN_LLM_KEY",
  "MIMO_LLM_KEY",
  "GEMINI_LLM_KEY",
  "ZHIPU_LLM_KEY",
  "DEEPSEEK_LLM_KEY",
  "MOONSHOT_LLM_KEY",
  "GROK_LLM_KEY",
  "CUSTOM_LLM_BASE_URL",
  "CUSTOM_LLM_MODEL",
  "CUSTOM_LLM_KEY",
];

/**
 * 供应商密钥申请页的**精确**域名白名单（`shell:open-external` 用）。
 * 与渲染进程 App.tsx 的 ASR_CONSOLE / LLM_CONSOLE 配对 ——
 * 那边加了新供应商地址，这里也要加对应域名，否则会被拒。
 *
 * 只列精确 host，不做父域名后缀匹配：登录跳转发生在系统浏览器里，
 * 不经过这个 handler，所以放行 `aliyun.com` 这类父域名只会白扩大攻击面。
 */
const ALLOWED_EXTERNAL_HOSTS = new Set([
  "www.xfyun.cn",
  "console.xfyun.cn",
  "bailian.console.aliyun.com",
  "console.volcengine.com",
  "console.cloud.tencent.com",
  "mimo.mi.com",
  "aistudio.google.com",
  "open.bigmodel.cn",
  "platform.deepseek.com",
  "platform.moonshot.cn",
  "console.x.ai",
]);

function meetingDatabasePath() {
  return path.join(app.getPath("userData"), "meeting-copilot.sqlite");
}

function runReviewEnhancement(record, opts = {}) {
  const tmpDir = fsSync.mkdtempSync(path.join(os.tmpdir(), "mc-review-"));
  const inputFile = path.join(tmpDir, "input.json");
  const outFile = path.join(tmpDir, "result.json");
  fsSync.writeFileSync(
    inputFile,
    JSON.stringify({
      title: record.title,
      startedAt: record.startedAt,
      scene: record.scene || "general",
      transcript: record.transcript || [],
    }),
    "utf8",
  );
  const args = [generateReviewPath, "--input", inputFile, "--out", outFile];
  if (opts.provider) args.push("--provider", String(opts.provider));
  if (opts.model) args.push("--model", String(opts.model));
  args.push(
    "--timeout-seconds",
    String(REVIEW_LLM_TIMEOUT_SECONDS),
    "--retry-attempts",
    String(REVIEW_LLM_RETRY_ATTEMPTS),
  );
  return new Promise((resolve) => {
    const child = spawn(pythonPath, args, {
      cwd: pocRoot,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
      env: pythonProcessEnv(),
    });
    let stdout = "";
    let stderr = "";
    let settled = false;
    let timeoutId;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      if (timeoutId) clearTimeout(timeoutId);
      try {
        fsSync.rmSync(tmpDir, { recursive: true, force: true });
      } catch (_) {
        /* ignore temporary cleanup failures */
      }
      resolve(value);
    };
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString("utf8");
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString("utf8");
    });
    child.on("close", (code) => {
      let payload = null;
      try {
        if (fsSync.existsSync(outFile)) {
          payload = JSON.parse(fsSync.readFileSync(outFile, "utf8"));
        } else {
          payload = JSON.parse(
            stdout.trim().split(/\r?\n/).filter(Boolean).at(-1) || "{}",
          );
        }
      } catch {
        payload = null;
      }
      finish({ code, payload, stderr });
    });
    child.on("error", (error) =>
      finish({ code: -1, payload: null, stderr: String(error.message || error) }),
    );
    timeoutId = setTimeout(() => {
      try {
        child.kill();
      } catch (_) {
        /* ignore kill races */
      }
      finish({
        code: -1,
        payload: null,
        stderr:
          `模型增强请求超过 ${REVIEW_ENHANCEMENT_TIMEOUT_MS / 1000} 秒 ` +
          `（单次 ${REVIEW_LLM_TIMEOUT_SECONDS} 秒，最多 ${REVIEW_LLM_RETRY_ATTEMPTS} 次）`,
      });
    }, REVIEW_ENHANCEMENT_TIMEOUT_MS);
  });
}

function applyReviewEnhancement(databasePath, meetingId, payload) {
  const current = loadMeetingRecord(databasePath, meetingId);
  if (!current) throw new Error("找不到该会议记录");
  const existing = new Map(
    (current.memoryItems || []).map((item) => [
      `${item.kind}:${item.content.toLowerCase()}`,
      item,
    ]),
  );
  for (const item of Array.isArray(payload?.memoryItems) ? payload.memoryItems : []) {
    const content = String(item?.content || "").trim();
    if (!content) continue;
    const kind = item.kind === "decision" ? "decision" : "action_item";
    const previous = existing.get(`${kind}:${content.toLowerCase()}`);
    saveMeetingMemoryItem(databasePath, meetingId, {
      id: previous?.id || item.id,
      kind,
      status:
        previous?.status === "confirmed" || previous?.status === "rejected"
          ? previous.status
          : "candidate",
      content,
      owner: item.owner || previous?.owner || null,
      dueAt: item.dueAt || previous?.dueAt || null,
      evidenceTranscriptId: item.evidenceTranscriptId || previous?.evidenceTranscriptId || null,
      evidenceText: item.evidenceText || previous?.evidenceText || null,
      source: "model",
    });
  }
  const candidateMap = new Map(
    (current.glossaryCandidates || []).map((item) => [item.term.toLowerCase(), item]),
  );
  const candidates = [];
  for (const item of Array.isArray(payload?.glossaryCandidates) ? payload.glossaryCandidates : []) {
    const term = String(item?.term || "").trim();
    if (!term) continue;
    const previous = candidateMap.get(term.toLowerCase());
    candidates.push({
      id: previous?.id || item.id,
      term,
      frequency: item.frequency,
      weight: item.weight,
      sampleContext: item.sampleContext,
      reason: item.reason || "模型识别的领域词",
      source: "model",
      selected: Boolean(previous?.selected),
    });
  }
  if (candidates.length) saveGlossaryCandidates(databasePath, meetingId, candidates);
  markMeetingReview(databasePath, meetingId, "enhanced");
  return loadMeetingRecord(databasePath, meetingId);
}

function secretsPath() {
  return path.join(app.getPath("userData"), "meeting-copilot-secrets.json");
}

function loadSecretsSync() {
  try {
    const raw = JSON.parse(fsSync.readFileSync(secretsPath(), "utf8"));
    return raw && typeof raw === "object" ? raw : {};
  } catch {
    return {};
  }
}

function maskSecret(value) {
  const k = String(value || "");
  if (!k) return "";
  return k.length > 10 ? `${k.slice(0, 4)}…${k.slice(-4)}（${k.length}位）` : "****";
}

/** 启动子进程时注入设置页保存的密钥；覆盖同名环境变量，优先于 config.py */
function pythonProcessEnv() {
  const env = { ...process.env, PYTHONIOENCODING: "utf-8" };
  const secrets = loadSecretsSync();
  for (const [key, value] of Object.entries(secrets)) {
    if (value != null && String(value).trim() !== "") {
      env[key] = String(value);
    }
  }
  return env;
}

async function writeSecrets(next) {
  const target = secretsPath();
  const temp = `${target}.tmp`;
  await fs.writeFile(temp, JSON.stringify(next, null, 2), "utf8");
  await fs.rename(temp, target);
}

/** 从 poc/config.py 读出已填字段（明文仅在主进程短暂使用，不回传渲染进程） */
function readConfigPyValues() {
  return new Promise((resolve) => {
    const keysJson = JSON.stringify(KNOWN_SECRET_KEYS);
    const code = [
      "import json,sys",
      "keys=json.loads(sys.argv[1])",
      "try:",
      " import config",
      "except Exception:",
      " print('{}'); sys.exit(0)",
      "out={}",
      "for k in keys:",
      " v=getattr(config,k,None)",
      " if v is not None and str(v).strip()!='': out[k]=str(v)",
      "print(json.dumps(out,ensure_ascii=False))",
    ].join("\n");
    const child = spawn(pythonPath, ["-c", code, keysJson], {
      cwd: pocRoot,
      windowsHide: true,
      env: { ...process.env, PYTHONIOENCODING: "utf-8" },
    });
    let stdout = "";
    child.stdout.on("data", (chunk) => (stdout += chunk.toString("utf8")));
    child.on("error", () => resolve({}));
    child.on("close", () => {
      try {
        resolve(JSON.parse(stdout.trim() || "{}"));
      } catch {
        resolve({});
      }
    });
  });
}

function formatStamp(at) {
  return new Date(at).toLocaleString("zh-CN", { hour12: false });
}

/**
 * 渲染导出内容。
 *
 * 导出会包含建议及其分级与引用来源 —— 这些是判断"当时为什么这么说"的
 * 依据，会后复盘和交接时比转写本身更有价值，不能只导转写。
 */
function renderExport(record, ext) {
  const lines = [];
  const h = (level, text) => (ext === "md" ? `${"#".repeat(level)} ${text}` : text);

  lines.push(h(1, record.title), "");
  lines.push(`时间：${formatStamp(record.startedAt)}`);
  if (record.endedAt) lines.push(`结束：${formatStamp(record.endedAt)}`);
  if (record.projectName) lines.push(`项目：${record.projectName}`);
  if (record.documents?.length) {
    lines.push(
      `知识范围：${record.documents.map((d) => d.name).join("、")}`,
    );
  } else {
    lines.push("知识范围：未使用知识文档");
  }
  lines.push("");

  lines.push(h(2, "转写全文"), "");
  // 导出时合并连续同说话人，与会中展示一致，便于阅读
  const merged = [];
  for (const item of record.transcript || []) {
    const prev = merged[merged.length - 1];
    const same =
      prev &&
      ((item.speakerId && prev.speakerId && item.speakerId === prev.speakerId) ||
        ((!item.speakerId || !prev.speakerId) && item.speaker === prev.speaker));
    if (same) {
      const left = String(prev.text || "").trimEnd();
      const right = String(item.text || "").trimStart();
      const needSpace =
        /[A-Za-z0-9]$/.test(left) && /^[A-Za-z0-9]/.test(right);
      prev.text = needSpace ? `${left} ${right}` : `${left}${right}`;
    } else {
      merged.push({ ...item });
    }
  }
  for (const item of merged) {
    const stamp = new Date(item.at).toLocaleTimeString("zh-CN", { hour12: false });
    lines.push(
      ext === "md"
        ? `**${item.speaker}**（${stamp}）：${item.text}`
        : `[${stamp}] ${item.speaker}：${item.text}`,
    );
    lines.push("");
  }

  if (record.minutes?.content) {
    lines.push(h(2, "会议纪要"), "");
    lines.push(record.minutes.content.trim(), "");
  }

  if (record.batches.length) {
    lines.push(h(2, "会中建议"), "");
    const levelLabel = {
      grounded: "有依据",
      advisory: "经验建议",
      clarify: "无依据·仅澄清",
    };
    for (const batch of [...record.batches].reverse()) {
      lines.push(h(3, `${formatStamp(batch.at)}`), "");
      batch.suggestions.forEach((suggestion, index) => {
        const level =
          levelLabel[suggestion.level] ||
          (suggestion.grounded === false ? "无依据" : "有依据");
        lines.push(`${index + 1}. [${level}] ${suggestion.intent}`);
        lines.push(`   话术：${suggestion.script}`);
        if (suggestion.references?.length) {
          lines.push(`   依据：${suggestion.references.join("、")}`);
        }
        if (suggestion.evidence?.length) {
          suggestion.evidence.forEach((evidence) => {
            lines.push(
              `   已核验原文（${evidence.source || "候选资料"}）：${evidence.quote}`,
            );
          });
        }
        lines.push("");
      });
    }
  }

  lines.push("---", "由「实时会议话术助手」导出。AI 建议仅供参考。");
  return lines.join("\n");
}

function sendMeetingEvent(payload) {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("meeting:event", payload);
  }
  if (floatingWindow && !floatingWindow.isDestroyed()) {
    floatingWindow.webContents.send("meeting:event", payload);
  }
}

function meetingRecordingPaths(meetingId) {
  const safeId = String(meetingId).replace(/[^a-zA-Z0-9_-]/g, "");
  const root = path.join(app.getPath("userData"), "recordings");
  return {
    mixed: path.join(root, `${safeId}.wav`),
    mic: path.join(root, `${safeId}.mic.wav`),
    system: path.join(root, `${safeId}.system.wav`),
  };
}

function persistBridgeAudio(payload) {
  if (!activeMeetingRecordId) return;
  const tracks = {};
  if (payload?.type === "recording_file" && payload.path) {
    tracks.mixed = { path: String(payload.path) };
    for (const [name, track] of Object.entries(payload.tracks || {})) {
      if (track?.path && track.ok !== false) {
        tracks[name] = {
          path: String(track.path),
          seconds: Number(track.seconds),
        };
      }
    }
  } else if (payload?.type === "ended" && payload.audio) {
    const audio = payload.audio;
    if (audio.path && audio.tracks?.mixed?.ok !== false) {
      tracks.mixed = { path: String(audio.path), seconds: Number(audio.seconds) };
    }
    for (const [name, track] of Object.entries(audio.tracks || {})) {
      if (track?.path && track.ok !== false) {
        tracks[name] = {
          path: String(track.path),
          seconds: Number(track.seconds),
        };
      }
    }
  } else {
    return;
  }
  if (Object.keys(tracks).length) {
    const previous = pendingMeetingAudio.get(activeMeetingRecordId) || {};
    const merged = { ...previous };
    for (const [name, track] of Object.entries(tracks)) {
      if (!track?.path) continue;
      merged[name] = {
        ...(merged[name] || {}),
        ...track,
      };
    }
    const attached = attachMeetingAudio(
      meetingDatabasePath(),
      activeMeetingRecordId,
      merged,
    );
    if (attached.updated) pendingMeetingAudio.delete(activeMeetingRecordId);
    else pendingMeetingAudio.set(activeMeetingRecordId, merged);
  }
}

function persistBridgeHotwords(payload) {
  if (!activeMeetingRecordId) return;
  if (payload?.type !== "status" || payload.stage !== "hotwords") return;
  const allowed = new Set(["pending", "empty", "loaded", "degraded", "unsupported"]);
  const status = allowed.has(String(payload.hotwordStatus))
    ? String(payload.hotwordStatus)
    : "degraded";
  const hotwords = {
    status,
    count: Math.max(0, Math.round(Number(payload.hotwordCount) || 0)),
    vocabularyId: payload.vocabularyId ? String(payload.vocabularyId) : null,
    reason: payload.hotwordReason ? String(payload.hotwordReason) : null,
  };
  if (status === "loaded" && hotwords.vocabularyId && activeMeetingHotwordContext) {
    rememberHotwordVocabulary({
      ...activeMeetingHotwordContext,
      vocabularyId: hotwords.vocabularyId,
      at: Date.now(),
    });
  }
  const attached = attachMeetingHotwords(
    meetingDatabasePath(),
    activeMeetingRecordId,
    hotwords,
  );
  if (!attached.updated) {
    pendingMeetingHotwords.set(activeMeetingRecordId, hotwords);
  } else {
    pendingMeetingHotwords.delete(activeMeetingRecordId);
  }
}

function safePersistedErrorMessage(value) {
  return String(value || "")
    .replace(
      /(api[_ -]?key|access[_ -]?key|secret|token|password)\s*[:=]\s*[^\s,;]+/gi,
      "$1=[redacted]",
    )
    .trim()
    .slice(0, 500);
}

function isBenignAsrShutdownMessage(stage, message) {
  const normalized = String(message || "")
    .replace(/\s+/g, " ")
    .trim();
  return (
    (stage === "bridge" || stage === "asr_stop") &&
    /^\[(?:阿里云|讯飞|火山引擎|腾讯云)\]\s*连接已关闭[。.]?$/.test(normalized)
  );
}

function persistBridgeError(payload) {
  if (!activeMeetingRecordId || payload?.type !== "error") return;
  const stage = String(payload.stage || "meeting").slice(0, 80);
  const message = safePersistedErrorMessage(payload.message);
  if (!message) return;
  if (isBenignAsrShutdownMessage(stage, message)) return;
  const error = {
    stage,
    message,
    at: Date.now(),
  };
  const attached = attachMeetingError(
    meetingDatabasePath(),
    activeMeetingRecordId,
    error,
  );
  if (!attached.updated) {
    pendingMeetingErrors.set(activeMeetingRecordId, error);
  } else {
    pendingMeetingErrors.delete(activeMeetingRecordId);
  }
}

function recoverExpectedMeetingAudio(meetingId) {
  if (!meetingId) return;
  const paths = meetingRecordingPaths(meetingId);
  const tracks = {};
  for (const [name, audioPath] of Object.entries(paths)) {
    if (!fsSync.existsSync(audioPath)) continue;
    const bytes = fsSync.statSync(audioPath).size;
    if (bytes <= 44) continue;
    // 本应用固定写 16 kHz / 16-bit / 单声道 PCM WAV；44 字节为标准头。
    const seconds =
      bytes > 44 ? Math.round(((bytes - 44) / 32_000) * 10) / 10 : null;
    if (!(seconds > 0)) continue;
    tracks[name] = { path: audioPath, seconds };
  }
  if (Object.keys(tracks).length) {
    attachMeetingAudio(meetingDatabasePath(), meetingId, tracks);
  }
}

function consumeBridgeOutput(chunk) {
  bridgeBuffer += chunk.toString("utf8");
  const lines = bridgeBuffer.split(/\r?\n/);
  bridgeBuffer = lines.pop() || "";
  for (const line of lines) {
    if (!line.trim()) continue;
    try {
      const payload = JSON.parse(line);
      if (payload.type === "suggestions") latestStrategyEvent = payload;
      if (payload.type === "ended") bridgeReportedEnded = true;
      if (payload.type === "error" && payload.fatal === true) {
        bridgeReportedFatalError = true;
      }
      persistBridgeAudio(payload);
      persistBridgeHotwords(payload);
      persistBridgeError(payload);
      sendMeetingEvent(payload);
    } catch {
      sendMeetingEvent({ type: "diagnostic", message: line });
    }
  }
}

function loadRenderer(window, mode) {
  const query = mode ? `?mode=${encodeURIComponent(mode)}` : "";
  if (process.env.VITE_DEV_SERVER_URL) {
    window.loadURL(`${process.env.VITE_DEV_SERVER_URL}${query}`);
  } else if (!app.isPackaged) {
    window.loadURL(`http://127.0.0.1:5173${query}`);
  } else {
    window.loadFile(path.join(__dirname, "..", "dist", "index.html"), {
      query: mode ? { mode } : {},
    });
  }
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1080,
    minHeight: 680,
    backgroundColor: "#f4f5f3",
    title: "实时会议话术助手",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  loadRenderer(mainWindow);
}

function createFloatingWindow() {
  if (floatingWindow && !floatingWindow.isDestroyed()) {
    floatingWindow.show();
    floatingWindow.focus();
    return floatingWindow;
  }
  floatingWindow = new BrowserWindow({
    width: 440,
    height: 280,
    minWidth: 300,
    minHeight: 38,
    frame: false,
    alwaysOnTop: true,
    resizable: true,
    transparent: true,
    backgroundColor: "#00000000",
    hasShadow: true,
    roundedCorners: true,
    title: "当前应答策略",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });
  floatingWindow.setAlwaysOnTop(true, "floating");
  floatingWindow._expandedHeight = 280;
  floatingWindow.on("closed", () => {
    floatingWindow = undefined;
  });
  floatingWindow.webContents.on("did-finish-load", () => {
    if (latestStrategyEvent && floatingWindow && !floatingWindow.isDestroyed()) {
      floatingWindow.webContents.send("meeting:event", latestStrategyEvent);
    }
  });
  loadRenderer(floatingWindow, "floating");
  return floatingWindow;
}

async function pathExists(target) {
  try {
    await fs.access(target);
    return true;
  } catch {
    return false;
  }
}

function runBridgeOnce(args) {
  return new Promise((resolve, reject) => {
    const child = spawn(pythonPath, [bridgePath, ...args], {
      cwd: pocRoot,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
      env: pythonProcessEnv(),
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => (stdout += chunk.toString("utf8")));
    child.stderr.on("data", (chunk) => (stderr += chunk.toString("utf8")));
    child.on("error", reject);
    child.on("close", (code) => {
      if (code !== 0) return reject(new Error(stderr.trim() || `bridge exited ${code}`));
      const lines = stdout.trim().split(/\r?\n/).filter(Boolean);
      try {
        resolve(JSON.parse(lines.at(-1) || "{}"));
      } catch {
        reject(new Error("麦克风列表返回格式无法解析"));
      }
    });
  });
}

function extractDocument(filePath, maxChars = 0) {
  return new Promise((resolve, reject) => {
    const args = [documentExtractPath, String(filePath)];
    if (maxChars > 0) args.push("--max-chars", String(maxChars));
    const child = spawn(pythonPath, args, {
      cwd: pocRoot,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
      env: pythonProcessEnv(),
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => (stdout += chunk.toString("utf8")));
    child.stderr.on("data", (chunk) => (stderr += chunk.toString("utf8")));
    child.on("error", reject);
    child.on("close", (code) => {
      if (code !== 0) {
        reject(new Error(stderr.trim() || `文档解析进程退出（${code}）`));
        return;
      }
      try {
        resolve(JSON.parse(stdout.trim() || "{}"));
      } catch {
        reject(new Error("文档解析结果格式无效"));
      }
    });
  });
}

app.whenReady().then(() => {
  ipcMain.handle("runtime:status", async () => ({
    desktop: true,
    pythonReady: await pathExists(pythonPath),
    bridgeReady: await pathExists(bridgePath),
    configPresent: await pathExists(path.join(pocRoot, "config.py")),
  }));

  ipcMain.handle("floating:open", async () => {
    createFloatingWindow();
    return { ok: true };
  });

  ipcMain.handle("floating:close", async () => {
    if (floatingWindow && !floatingWindow.isDestroyed()) floatingWindow.close();
    return { ok: true };
  });

  ipcMain.handle("floating:set-preferences", async (_event, preferences = {}) => {
    if (!floatingWindow || floatingWindow.isDestroyed()) {
      return { ok: false };
    }
    if (typeof preferences.alwaysOnTop === "boolean") {
      floatingWindow.setAlwaysOnTop(preferences.alwaysOnTop, "floating");
    }
    if (typeof preferences.collapsed === "boolean") {
      const bounds = floatingWindow.getBounds();
      if (preferences.collapsed) {
        if (!floatingWindow._expandedHeight || floatingWindow._expandedHeight < 100) {
          floatingWindow._expandedHeight = Math.max(bounds.height, 240);
        }
        floatingWindow.setSize(bounds.width, 38);
      } else {
        const targetHeight = floatingWindow._expandedHeight || 280;
        floatingWindow.setSize(bounds.width, targetHeight);
      }
    }
    return { ok: true, alwaysOnTop: preferences.alwaysOnTop };
  });

  ipcMain.handle("meeting:list-input-devices", async () => {
    return runBridgeOnce(["--list-devices"]);
  });

  ipcMain.handle("meeting:test-input-device", async (_event, device) => {
    if (bridgeProcess) throw new Error("请先结束当前会议再测试麦克风");
    if (deviceTestProcess) throw new Error("麦克风测试正在进行");
    return new Promise((resolve, reject) => {
      const child = spawn(
        pythonPath,
        [bridgePath, "--test-device", String(device), "--test-duration", "6"],
        {
          cwd: pocRoot,
          windowsHide: true,
          stdio: ["ignore", "pipe", "pipe"],
          env: pythonProcessEnv(),
        },
      );
      deviceTestProcess = child;
      let buffer = "";
      let deviceTestError = "";
      child.stdout.on("data", (chunk) => {
        buffer += chunk.toString("utf8");
        const lines = buffer.split(/\r?\n/);
        buffer = lines.pop() || "";
        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const payload = JSON.parse(line);
            if (payload.type === "error") {
              deviceTestError = payload.message || "";
            }
            sendMeetingEvent(payload);
          } catch {
            sendMeetingEvent({ type: "diagnostic", message: line });
          }
        }
      });
      child.stderr.on("data", (chunk) => {
        sendMeetingEvent({
          type: "diagnostic",
          message: chunk.toString("utf8").trim(),
        });
      });
      child.on("error", (error) => {
        deviceTestProcess = undefined;
        reject(error);
      });
      child.on("close", (code) => {
        deviceTestProcess = undefined;
        if (code === 0) resolve({ ok: true });
        else {
          reject(
            new Error(
              deviceTestError || `麦克风测试失败（错误码 ${code}）`,
            ),
          );
        }
      });
    });
  });

  ipcMain.handle("meeting:recommend-scene", async (_event, input = {}) =>
    recommendMeetingScene(input));

  ipcMain.handle("meeting:warmup", async (_event, options = {}) => {
    if (!(await pathExists(pythonPath)) || !(await pathExists(warmupPath))) {
      return {
        ok: false,
        error: "本地 Python 或预热脚本不可用",
        steps: [],
      };
    }
    const args = [warmupPath];
    const asrProvider = options.asrProvider ? String(options.asrProvider) : "";
    if (asrProvider) args.push("--asr-provider", asrProvider);
    for (const p of enrollSamplePaths()) {
      args.push("--check-enroll", p);
    }
    let hotwordsFile;
    let warmupContext;
    try {
      const glossary = listGlossaryTermsForMeeting(
        meetingDatabasePath(),
        options.projectId || null,
      );
      warmupContext = createWarmupContext(options, glossary);
      const warmedVocabularyId = resolveWarmupVocabularyId(
        meetingWarmupCache,
        warmupContext,
      );
      const persistentVocabulary = warmedVocabularyId
        ? null
        : resolvePersistentHotwordVocabularyId(warmupContext, true);
      const cachedVocabularyId =
        warmedVocabularyId || persistentVocabulary?.vocabularyId || null;
      if (cachedVocabularyId) {
        const reused = Boolean(persistentVocabulary);
        return {
          ok: true,
          vocabularyId: cachedVocabularyId,
          termCount: glossary.length,
          elapsedMs: 0,
          steps: [
            {
              name: "hotwords",
              ok: true,
              message: reused
                ? "已复用最近有效词表，未重复请求阿里热词配额"
                : "已复用本次会话词表，未重复请求阿里热词配额",
              termCount: glossary.length,
              vocabularyId: cachedVocabularyId,
            },
          ],
          error: null,
          cached: true,
          reused,
          warning: reused
            ? "已复用最近有效词表，未重复请求阿里热词配额"
            : null,
        };
      }
      hotwordsFile = path.join(
        os.tmpdir(),
        `meeting-hotwords-warmup-${Date.now()}-${Math.random().toString(36).slice(2, 8)}.json`,
      );
      fsSync.writeFileSync(
        hotwordsFile,
        JSON.stringify({
          terms: glossary.map((item) => ({
            text: item.term,
            weight: item.weight,
            scope: item.scope,
          })),
          projectId: options.projectId || null,
        }),
        "utf8",
      );
      args.push("--hotwords-file", hotwordsFile);
    } catch (error) {
      return {
        ok: false,
        error: `专有名词预热失败：${error.message || error}`,
        steps: [],
      };
    }
    try {
      const result = await new Promise((resolve) => {
        const child = spawn(pythonPath, args, {
          cwd: pocRoot,
          windowsHide: true,
          stdio: ["ignore", "pipe", "pipe"],
          env: pythonProcessEnv(),
        });
        let stdout = "";
        let stderr = "";
        child.stdout.on("data", (chunk) => {
          stdout += chunk.toString("utf8");
        });
        child.stderr.on("data", (chunk) => {
          stderr += chunk.toString("utf8");
        });
        child.on("error", (error) => {
          resolve({
            ok: false,
            error: error.message,
            steps: [],
          });
        });
        child.on("close", (code) => {
          const line = stdout
            .trim()
            .split(/\r?\n/)
            .filter(Boolean)
            .at(-1);
          try {
            const parsed = line ? JSON.parse(line) : null;
            if (parsed && typeof parsed === "object") {
              resolve(parsed);
              return;
            }
          } catch {
            // fall through
          }
          resolve({
            ok: code === 0,
            error: stderr.trim() || `预热退出码 ${code}`,
            steps: [],
            raw: stdout.slice(0, 500),
          });
        });
      });
      let vocabularyId = result?.vocabularyId || null;
      let reused = false;
      if (vocabularyId) {
        meetingWarmupCache = createWarmupCacheEntry(
          result,
          warmupContext,
        );
        rememberHotwordVocabulary(meetingWarmupCache);
      } else {
        const fallback = resolvePersistentHotwordVocabularyId(
          warmupContext,
          true,
        );
        if (fallback?.vocabularyId) {
          vocabularyId = fallback.vocabularyId;
          reused = true;
        }
      }
      return {
        // 找到同一项目/同一词表指纹的最近有效云端词表后，本次会前准备仍可用；
        // 不应因为本次同步重试失败而让界面把它显示成不可用。
        ok: Boolean(result?.ok) || reused,
        vocabularyId,
        termCount: result?.termCount || 0,
        elapsedMs: result?.elapsedMs || 0,
        steps: Array.isArray(result?.steps) ? result.steps : [],
        error: reused ? null : result?.error || null,
        cached: Boolean(vocabularyId),
        reused,
        warning: reused
          ? "本次热词同步失败，已复用同一词表的最近有效版本"
          : result?.warning || null,
      };
    } finally {
      if (hotwordsFile) fsSync.rmSync(hotwordsFile, { force: true });
    }
  });

  ipcMain.handle("meeting:begin-recording", async () => {
    if (!bridgeProcess) return { ok: false, reason: "no-bridge" };
    try {
      bridgeProcess.stdin.write(
        `${JSON.stringify({ command: "begin_recording" })}\n`,
      );
      return { ok: true };
    } catch (error) {
      return { ok: false, reason: error.message || "write-failed" };
    }
  });

  ipcMain.handle("meeting:start", async (_event, options = {}) => {
    if (bridgeProcess) throw new Error("已有会议正在运行");
    latestStrategyEvent = null;
    activeMeetingHotwordContext = null;
    sendMeetingEvent({ type: "strategy_reset" });
    const args = [bridgePath];
    if (options.me) args.push("--me", String(options.me));
    if (Number.isInteger(options.device)) args.push("--device", String(options.device));
    if (options.meetingMode === "online") {
      args.push("--meeting-mode", "online");
    }
    if (["general", "sales", "requirements"].includes(options.scene)) {
      args.push("--scene", String(options.scene));
    }
    // 供应商/模型均可由 UI 切换（此前只能改 config.py）
    if (options.provider) args.push("--provider", String(options.provider));
    if (options.llmModel) args.push("--llm-model", String(options.llmModel));
    if (options.asrProvider) args.push("--asr-provider", String(options.asrProvider));
    if (options.asrModel) args.push("--asr-model", String(options.asrModel));
    // 识别语种：zh / en / zh_en。限制自动识语种串出日文等
    if (options.asrLang) args.push("--asr-lang", String(options.asrLang));
    // 设置页「建议触发」。⚠️ 这两项在 2026-07-27 之前只存在于界面，从没传下来过
    const silence = Number(options.silenceSeconds);
    if (Number.isFinite(silence) && silence > 0 && silence <= 15) {
      args.push("--silence-seconds", String(silence));
    }
    const count = Number(options.suggestionCount);
    if (Number.isInteger(count) && count >= 1 && count <= 5) {
      args.push("--suggestion-count", String(count));
    }

    // 方案 D1：本地声纹认「我」。默认读 userData/voiceprint/ 下的全部样本。
    // ⚠️ 这里加的每个参数，desktop_bridge.py 的 argparse 都必须声明，
    //    否则 exit(2)，界面表现为「会议一开就结束」（踩过，见 HANDOFF §7）。
    const enrollWavs = options.enrollWav
      ? [options.enrollWav]
      : options.voiceprint === false
        ? []
        : enrollSamplePaths();
    const usable = enrollWavs.filter((p) => p && fsSync.existsSync(p));
    if (usable.length && options.meetingMode !== "online") {
      for (const p of usable) args.push("--enroll-wav", p);
      const th = Number(options.meThreshold);
      if (Number.isFinite(th) && th > 0 && th < 1) {
        args.push("--me-threshold", String(th));
      }
    }

    // 知识范围隔离：把本场选中的文档路径落到临时文件再传给桥接层。
    // 用文件而非命令行参数，避免文档较多时超出 Windows 命令行长度限制。
    // ⚠️ 必须显式传递（哪怕是空数组），否则 Python 侧会回退到全局 docs/ 目录，
    //    造成跨项目串用资料。
    // 录音落盘：按会议 ID 存到 userData/recordings 下，边录边写防崩溃丢失
    if (options.meetingId) {
      const recordingPaths = meetingRecordingPaths(options.meetingId);
      args.push("--audio-out", recordingPaths.mixed);
      if (options.meetingMode === "online") {
        args.push("--mic-audio-out", recordingPaths.mic);
        args.push("--system-audio-out", recordingPaths.system);
      }
    }

    if (Array.isArray(options.documents)) {
      const paths = options.documents
        .map((doc) => String(doc?.path || ""))
        .filter(Boolean);
      const scopeFile = path.join(
        os.tmpdir(),
        `meeting-scope-${Date.now()}-${Math.random().toString(36).slice(2, 8)}.json`,
      );
      fsSync.writeFileSync(scopeFile, JSON.stringify(paths), "utf8");
      args.push("--docs-file", scopeFile);
      currentScopeFile = scopeFile;
    }

    // 专有名词：通用 + 本场项目，写临时文件交给桥接层同步阿里热词。
    // 空数组也显式传递，避免旧进程残留。若预热缓存命中则带上 vocabulary_id 跳过云端同步。
    try {
      const glossary = listGlossaryTermsForMeeting(
        meetingDatabasePath(),
        options.projectId || null,
      );
      const warmupContext = createWarmupContext(options, glossary);
      activeMeetingHotwordContext = warmupContext;
      const hotwordsFile = path.join(
        os.tmpdir(),
        `meeting-hotwords-${Date.now()}-${Math.random().toString(36).slice(2, 8)}.json`,
      );
      fsSync.writeFileSync(
        hotwordsFile,
        JSON.stringify({
          terms: glossary.map((item) => ({
            text: item.term,
            weight: item.weight,
            scope: item.scope,
          })),
          projectId: options.projectId || null,
        }),
        "utf8",
      );
      args.push("--hotwords-file", hotwordsFile);
      currentHotwordsFile = hotwordsFile;
      const warmedVocabularyId = resolveWarmupVocabularyId(
        meetingWarmupCache,
        warmupContext,
      );
      const persistentFallback = warmedVocabularyId
        ? null
        : resolvePersistentHotwordVocabularyId(warmupContext, true);
      const vocabularyId =
        warmedVocabularyId || persistentFallback?.vocabularyId || null;
      if (vocabularyId) {
        args.push("--vocabulary-id", vocabularyId);
        sendMeetingEvent({
          type: "diagnostic",
          message: persistentFallback
            ? "会前同步失败，复用最近有效的专有名词词表"
            : "使用会前预同步的专有名词词表",
        });
      }
    } catch (error) {
      sendMeetingEvent({
        type: "diagnostic",
        message: `专有名词加载失败，将不带热词开会：${error.message || error}`,
      });
    }

    bridgeBuffer = "";
    bridgeStderr = "";
    bridgeReportedFatalError = false;
    bridgeReportedEnded = false;
    activeMeetingRecordId = options.meetingId ? String(options.meetingId) : null;
    if (activeMeetingRecordId) pendingMeetingHotwords.delete(activeMeetingRecordId);
    if (activeMeetingRecordId) pendingMeetingAudio.delete(activeMeetingRecordId);
    if (activeMeetingRecordId) pendingMeetingErrors.delete(activeMeetingRecordId);
    bridgeProcess = spawn(pythonPath, args, {
      cwd: pocRoot,
      windowsHide: true,
      stdio: ["pipe", "pipe", "pipe"],
      env: pythonProcessEnv(),
    });
    bridgeProcess.stdout.on("data", consumeBridgeOutput);
    bridgeProcess.stderr.on("data", (chunk) => {
      const message = chunk.toString("utf8");
      bridgeStderr += message;
      sendMeetingEvent({ type: "diagnostic", message: message.trim() });
    });
    bridgeProcess.on("error", (error) => {
      bridgeReportedFatalError = true;
      const message = safePersistedErrorMessage(error.message);
      persistBridgeError({ type: "error", stage: "bridge", message });
      sendMeetingEvent({
        type: "error",
        stage: "bridge",
        message: error.message,
        fatal: true,
      });
      try {
        recoverExpectedMeetingAudio(activeMeetingRecordId);
      } catch (recoveryError) {
        sendMeetingEvent({
          type: "diagnostic",
          message: `录音档案关联失败：${recoveryError.message || recoveryError}`,
        });
      }
      // 保留 bridgeProcess 引用直到 close；否则 error 与 close 之间若收到新的
      // start 请求，旧 close 会误把新会议的 activeMeetingRecordId 清掉。
    });
    bridgeProcess.on("close", (code) => {
      const closingMeetingRecordId = activeMeetingRecordId;
      try {
        // recording_file 可能早于渲染层首次建档，ended 也可能因窗口刷新而无人消费。
        // 进程退出时用确定性的文件名再补一次，保证 WAV 与会议档案最终一致。
        recoverExpectedMeetingAudio(closingMeetingRecordId);
      } catch (error) {
        sendMeetingEvent({
          type: "diagnostic",
          message: `录音档案关联失败：${error.message || error}`,
        });
      }
      if (code !== 0 && !bridgeReportedFatalError && !bridgeReportedEnded) {
        bridgeReportedFatalError = true;
        const stderrLines = bridgeStderr
          .trim()
          .split(/\r?\n/)
          .map((line) => line.trim())
          .filter(Boolean)
          .filter((line) => !isBenignAsrShutdownMessage("bridge", line));
        const message =
          stderrLines.at(-1) || `本地服务异常退出（错误码 ${code}）`;
        persistBridgeError({ type: "error", stage: "bridge", message });
        sendMeetingEvent({
          type: "error",
          stage: "bridge",
          message,
          fatal: true,
        });
      }
      sendMeetingEvent({ type: "bridge_closed", code });
      bridgeProcess = undefined;
      activeMeetingRecordId = null;
      activeMeetingHotwordContext = null;
      bridgeStderr = "";
      if (currentScopeFile) {
        fsSync.rmSync(currentScopeFile, { force: true });
        currentScopeFile = undefined;
      }
      if (currentHotwordsFile) {
        fsSync.rmSync(currentHotwordsFile, { force: true });
        currentHotwordsFile = undefined;
      }
    });
    return { ok: true };
  });

  ipcMain.handle("meeting:stop", async () => {
    // ⚠️ 必须如实回报"没有在跑的会议"。此前无论如何都返回 ok，渲染进程便
    //    以为停止指令已发出、坐等永远不会到来的 ended 事件 —— 应用重启后
    //    遗留的"进行中"会议因此根本停不掉。返回 false 让渲染进程改走本地收尾。
    if (!bridgeProcess) return { ok: false, reason: "no-bridge" };
    bridgeProcess.stdin.write(`${JSON.stringify({ command: "stop" })}\n`);
    return { ok: true };
  });

  ipcMain.handle("meeting:set-controls", async (_event, controls = {}) => {
    if (!bridgeProcess) throw new Error("会议尚未开始");
    bridgeProcess.stdin.write(
      `${JSON.stringify({
        command: "set_controls",
        recordingPaused: controls.recordingPaused,
        suggestionsPaused: controls.suggestionsPaused,
      })}\n`,
    );
    return { ok: true };
  });

  ipcMain.handle("meeting:ask", async (_event, question) => {
    if (!bridgeProcess) throw new Error("会议尚未开始");
    bridgeProcess.stdin.write(`${JSON.stringify({ command: "ask", question })}\n`);
    return { ok: true };
  });

  // 手动索取一批建议。自动建议受冷却/增量闸门限制（防刷屏），用户明确
  // 要的时候不该被那些闸门挡住，因此走独立命令绕过。
  ipcMain.handle("meeting:suggest-now", async () => {
    if (!bridgeProcess) throw new Error("会议尚未开始");
    bridgeProcess.stdin.write(`${JSON.stringify({ command: "suggest_now" })}\n`);
    return { ok: true };
  });

  // 会中认领"我是几号"。角色编号要等真正开口才出现，无法会前指定。
  ipcMain.handle("meeting:set-me", async (_event, speakerId) => {
    if (!bridgeProcess) return { ok: false };
    bridgeProcess.stdin.write(
      `${JSON.stringify({ command: "set_me", speakerId })}\n`,
    );
    return { ok: true };
  });

  // ── 声纹注册（方案 D1）────────────────────────────────
  ipcMain.handle("voiceprint:status", async () => {
    const paths = enrollSamplePaths();
    if (!paths.length) {
      return { ok: false, path: defaultEnrollWavPath(), seconds: 0, bytes: 0, samples: [] };
    }
    try {
      const samples = paths.map((p, i) => ({
        index: i + 1,
        path: p,
        seconds: wavSeconds(p),
        bytes: fsSync.statSync(p).size,
        mtime: fsSync.statSync(p).mtimeMs,
      }));
      const totalSeconds =
        Math.round(samples.reduce((a, s) => a + s.seconds, 0) * 10) / 10;
      return {
        ok: true,
        path: paths[0],
        samples,
        sampleCount: samples.length,
        totalSeconds,
        // 旧字段：保持为「全部样本合计」，这样徽标不用改也显示总时长
        seconds: totalSeconds,
        bytes: samples.reduce((a, s) => a + s.bytes, 0),
        mtime: samples[samples.length - 1].mtime,
      };
    } catch (error) {
      return {
        ok: false,
        path: paths[0],
        samples: [],
        message: String(error.message || error),
      };
    }
  });

  ipcMain.handle("voiceprint:enroll", async (_event, options = {}) => {
    if (bridgeProcess) throw new Error("请先结束当前会议再录制声纹");
    if (deviceTestProcess) throw new Error("请先结束麦克风测试");
    fsSync.mkdirSync(voiceprintDir(), { recursive: true });
    // append=true 追加一段样本；否则清空重来（旧行为）。
    // 默认改为追加：用户反馈"重复录制会删掉之前的"是反直觉的。
    if (options.append === false) {
      for (const p of enrollSamplePaths()) fsSync.rmSync(p, { force: true });
    }
    const outPath = options.append === false
      ? defaultEnrollWavPath()
      : nextEnrollSamplePath();
    const seconds = Math.min(60, Math.max(10, Number(options.seconds) || 20));
    const args = [enrollVoicePath, "--out", outPath, "--seconds", String(seconds)];
    if (Number.isInteger(options.device)) {
      args.push("--device", String(options.device));
    }
    return await new Promise((resolve, reject) => {
      const child = spawn(pythonPath, args, {
        cwd: pocRoot,
        windowsHide: true,
        stdio: ["ignore", "pipe", "pipe"],
        env: pythonProcessEnv(),
      });
      let stdout = "";
      let stderr = "";
      child.stdout.on("data", (chunk) => {
        const text = chunk.toString("utf8");
        stdout += text;
        for (const line of text.split(/\r?\n/)) {
          if (!line.trim()) continue;
          try {
            const evt = JSON.parse(line);
            sendMeetingEvent({ type: "voiceprint_enroll", ...evt });
          } catch (_) {
            /* ignore non-json */
          }
        }
      });
      child.stderr.on("data", (chunk) => {
        stderr += chunk.toString("utf8");
      });
      child.on("error", reject);
      child.on("close", (code) => {
        if (code === 0) {
          resolve({ ok: true, path: outPath });
        } else {
          reject(
            new Error(
              stderr.trim() ||
                stdout.trim() ||
                `声纹录制失败（退出码 ${code}）`,
            ),
          );
        }
      });
    });
  });

  ipcMain.handle("voiceprint:clear", async () => {
    try {
      for (const p of enrollSamplePaths()) fsSync.rmSync(p, { force: true });
      return { ok: true };
    } catch (error) {
      return { ok: false, message: String(error.message || error) };
    }
  });

  // 删掉最后一段样本：某次录砸了（音量低/被打断）不必全部重来
  ipcMain.handle("voiceprint:remove-last", async () => {
    try {
      const paths = enrollSamplePaths();
      if (!paths.length) return { ok: false, message: "没有可删除的样本" };
      fsSync.rmSync(paths[paths.length - 1], { force: true });
      return { ok: true, remaining: enrollSamplePaths().length };
    } catch (error) {
      return { ok: false, message: String(error.message || error) };
    }
  });

  // ── 服务诊断与测速（SET-1 / SET-5 / SET-7）──────────
  function runServiceCheck(flags, timeoutMs) {
    const argv = Array.isArray(flags) ? flags : [flags];
    return new Promise((resolve, reject) => {
      const child = spawn(pythonPath, [serviceCheckPath, ...argv], {
        cwd: pocRoot,
        windowsHide: true,
        env: pythonProcessEnv(),
      });
      let stdout = "";
      let stderr = "";
      const timer = setTimeout(() => {
        child.kill();
        reject(new Error("服务检测超时"));
      }, timeoutMs);
      child.stdout.on("data", (chunk) => (stdout += chunk.toString("utf8")));
      child.stderr.on("data", (chunk) => (stderr += chunk.toString("utf8")));
      child.on("error", (error) => {
        clearTimeout(timer);
        reject(error);
      });
      child.on("close", () => {
        clearTimeout(timer);
        const line = stdout.trim().split(/\r?\n/).filter(Boolean).pop();
        if (!line) {
          return reject(new Error(stderr.trim().slice(-300) || "检测未返回结果"));
        }
        try {
          resolve(JSON.parse(line));
        } catch {
          reject(new Error("检测结果解析失败"));
        }
      });
    });
  }

  ipcMain.handle("services:status", async (_event, opts = {}) => {
    const argv = ["--status"];
    if (opts.provider) argv.push("--provider", String(opts.provider));
    if (opts.asrProvider) argv.push("--asr-provider", String(opts.asrProvider));
    return runServiceCheck(argv, 30_000);
  });

  ipcMain.handle("services:test-llm", async (_event, options) => {
    const argv = ["--test-llm"];
    const provider = typeof options === "string" ? options : options?.provider;
    const model = typeof options === "object" ? options?.model : undefined;
    const scene = typeof options === "object" ? options?.scene : undefined;
    if (provider) argv.push("--provider", String(provider));
    if (model) argv.push("--llm-model", String(model));
    if (scene) argv.push("--scene", String(scene));
    return runServiceCheck(argv, 20_000);
  });

  // 探测某供应商可用的模型名
  ipcMain.handle("services:probe-llm", async (_event, provider) => {
    const argv = ["--probe-llm"];
    if (provider) argv.push("--provider", String(provider));
    return runServiceCheck(argv, 120_000);
  });

  // 测某 ASR 供应商连通性（可带识别语种与模型）
  ipcMain.handle("services:test-asr", async (_event, opts) => {
    const argv = ["--test-asr"];
    // 兼容旧调用：直接传字符串 provider
    const asrProvider =
      typeof opts === "string" ? opts : opts?.asrProvider || opts?.provider;
    const asrLang = typeof opts === "string" ? undefined : opts?.asrLang;
    const asrModel = typeof opts === "string" ? undefined : opts?.asrModel;
    if (asrProvider) argv.push("--asr-provider", String(asrProvider));
    if (asrModel) argv.push("--asr-model", String(asrModel));
    if (asrLang) argv.push("--asr-lang", String(asrLang));
    return runServiceCheck(argv, 40_000);
  });

  // 测速：默认当前供应商；opts.all=true 时对比全部已配置家
  ipcMain.handle("services:bench", async (_event, opts = {}) => {
    const argv = ["--bench"];
    if (opts && opts.all) {
      argv.push("--bench-all");
    } else {
      if (opts?.provider) argv.push("--provider", String(opts.provider));
      if (opts?.model) argv.push("--llm-model", String(opts.model));
    }
    // 全量对比要跑多家，给足超时；单家短一些
    return runServiceCheck(argv, opts?.all ? 300_000 : 90_000);
  });

  // ── 项目 ──────────────────────────────────────────
  ipcMain.handle("projects:list", async () =>
    listProjects(meetingDatabasePath()));

  ipcMain.handle("projects:save", async (_event, project) =>
    saveProject(meetingDatabasePath(), project));

  ipcMain.handle("projects:delete", async (_event, id) =>
    deleteProject(meetingDatabasePath(), id));

  // ── 专有名词 / ASR 热词 ────────────────────────────
  ipcMain.handle("glossary:list", async (_event, scope) =>
    listGlossaryTerms(meetingDatabasePath(), scope));

  ipcMain.handle("glossary:save", async (_event, term) =>
    saveGlossaryTerm(meetingDatabasePath(), term || {}));

  ipcMain.handle("glossary:delete", async (_event, id) =>
    deleteGlossaryTerm(meetingDatabasePath(), id));

  ipcMain.handle("glossary:for-meeting", async (_event, projectId) =>
    listGlossaryTermsForMeeting(meetingDatabasePath(), projectId || null));

  // ── 知识文档（路径引用）────────────────────────────
  ipcMain.handle("documents:list", async (_event, projectId) =>
    listDocuments(
      meetingDatabasePath(),
      projectId === undefined ? undefined : projectId || null,
    ));

  let lastDocumentPickDirectory = "";

  async function scanPathsForDocuments(inputPaths) {
    const supportedExts = new Set([".md", ".txt", ".docx", ".pdf"]);
    const ignoredDirNames = new Set([
      "node_modules",
      ".git",
      ".svn",
      ".vscode",
      ".idea",
      "__pycache__",
      ".next",
      "dist",
      "build",
    ]);
    const discovered = [];

    async function walk(targetPath) {
      try {
        const stat = await fs.stat(targetPath);
        if (stat.isDirectory()) {
          const baseName = path.basename(targetPath);
          if (ignoredDirNames.has(baseName) || baseName.startsWith(".")) {
            return;
          }
          const entries = await fs.readdir(targetPath);
          for (const entry of entries) {
            await walk(path.join(targetPath, entry));
          }
        } else if (stat.isFile()) {
          const ext = path.extname(targetPath).toLowerCase();
          if (supportedExts.has(ext)) {
            discovered.push(targetPath);
          }
        }
      } catch {
        // 忽略无权限或已移动的异常路径
      }
    }

    for (const rawPath of inputPaths || []) {
      if (!rawPath) continue;
      await walk(String(rawPath));
    }
    return discovered;
  }

  async function processAndAddDocuments(scope, rawPaths) {
    const discoveredPaths = await scanPathsForDocuments(rawPaths);
    if (!discoveredPaths.length) {
      return {
        added: 0,
        discoveredCount: 0,
        documents:
          scope === undefined
            ? listLibraryDocuments(meetingDatabasePath())
            : listDocuments(meetingDatabasePath(), scope),
        errors: [],
      };
    }

    // 记忆最近使用目录
    const firstValid = discoveredPaths[0] || rawPaths[0];
    if (firstValid) {
      try {
        const stat = await fs.stat(firstValid);
        lastDocumentPickDirectory = stat.isDirectory()
          ? firstValid
          : path.dirname(firstValid);
      } catch {
        // ignore
      }
    }

    const checked = await Promise.all(
      discoveredPaths.map(async (filePath) => {
        try {
          const parsed = await extractDocument(filePath, 1);
          return parsed.ok
            ? { path: filePath, error: null }
            : { path: filePath, error: parsed.message || "无法解析" };
        } catch (error) {
          return { path: filePath, error: error.message || String(error) };
        }
      }),
    );
    const validPaths = checked
      .filter((item) => !item.error)
      .map((item) => item.path);
    const response = addDocuments(meetingDatabasePath(), scope, validPaths);
    return {
      ...response,
      discoveredCount: discoveredPaths.length,
      errors: checked
        .filter((item) => item.error)
        .map((item) => ({
          name: path.basename(item.path),
          message: item.error,
        })),
    };
  }

  ipcMain.handle("documents:pick", async (_event, projectId) => {
    // 只登记路径、不拷贝文件：用户在自己编辑器改完即刻生效。
    // projectId 省略（知识库页）→ 导入到全局库；给了则同时关联到该项目。
    const scope = projectId || undefined;
    const defaultPath =
      lastDocumentPickDirectory && (await pathExists(lastDocumentPickDirectory))
        ? lastDocumentPickDirectory
        : app.getPath("documents");
    const result = await dialog.showOpenDialog(mainWindow, {
      title: "选择知识文档（支持多选）",
      defaultPath,
      properties: ["openFile", "multiSelections"],
      filters: [
        { name: "知识文档", extensions: ["md", "txt", "docx", "pdf"] },
      ],
    });
    if (result.canceled || !result.filePaths.length) {
      return {
        added: 0,
        documents:
          scope === undefined
            ? listLibraryDocuments(meetingDatabasePath())
            : listDocuments(meetingDatabasePath(), scope),
      };
    }
    return await processAndAddDocuments(scope, result.filePaths);
  });

  ipcMain.handle("documents:pick-folder", async (_event, projectId) => {
    const scope = projectId || undefined;
    const defaultPath =
      lastDocumentPickDirectory && (await pathExists(lastDocumentPickDirectory))
        ? lastDocumentPickDirectory
        : app.getPath("documents");
    const result = await dialog.showOpenDialog(mainWindow, {
      title: "选择包含知识文档的文件夹（将递归扫描并导入所有文档）",
      defaultPath,
      properties: ["openDirectory", "multiSelections"],
    });
    if (result.canceled || !result.filePaths.length) {
      return {
        added: 0,
        documents:
          scope === undefined
            ? listLibraryDocuments(meetingDatabasePath())
            : listDocuments(meetingDatabasePath(), scope),
      };
    }
    return await processAndAddDocuments(scope, result.filePaths);
  });

  ipcMain.handle("documents:add-paths", async (_event, filePaths, projectId) => {
    const scope = projectId || undefined;
    return await processAndAddDocuments(scope, filePaths);
  });

  ipcMain.handle("documents:remove", async (_event, id) =>
    removeDocument(meetingDatabasePath(), id));

  ipcMain.handle("documents:rename", async (_event, id, name) =>
    renameDocument(meetingDatabasePath(), id, name));

  // 项目↔文档 关联（可用资料维护）
  ipcMain.handle("projects:documents:get", async (_event, projectId) =>
    getProjectDocumentIds(meetingDatabasePath(), projectId));

  ipcMain.handle("projects:documents:set", async (_event, projectId, docIds) =>
    setProjectDocuments(meetingDatabasePath(), projectId, docIds));

  // MTG-3：把录音读成 data URL 交给渲染进程播放。
  // 不直接暴露 file:// 路径，避免渲染进程获得任意文件读取能力。
  ipcMain.handle("records:audio", async (_event, id) => {
    const record = loadMeetingRecord(meetingDatabasePath(), id);
    if (!record?.audioPath) return { ok: false, message: "本场会议没有录音" };
    try {
      const buffer = await fs.readFile(record.audioPath);
      return {
        ok: true,
        dataUrl: `data:audio/wav;base64,${buffer.toString("base64")}`,
        seconds: record.audioSeconds,
      };
    } catch (error) {
      return { ok: false, message: `录音文件已丢失：${error.message}` };
    }
  });


  ipcMain.handle("records:minutes:generate", async (_event, meetingId, opts = {}) => {
    // 版本切换和人工改派通过 records:save 排队写入；纪要必须等它们落库后再读，
    // 否则用户刚切到“会后分离”就点生成，可能仍拿到上一个版本。
    await recordWriteQueue.catch(() => undefined);
    const record = loadMeetingRecord(meetingDatabasePath(), meetingId);
    if (!record) return { ok: false, message: "找不到会议" };
    if (!(record.transcript || []).some((item) => item.isFinal)) {
      return { ok: false, message: "本场没有可用于生成纪要的最终转写" };
    }
    const tmpDir = fsSync.mkdtempSync(path.join(os.tmpdir(), "mc-minutes-"));
    const inputFile = path.join(tmpDir, "input.json");
    const outFile = path.join(tmpDir, "result.json");
    try {
      fsSync.writeFileSync(
        inputFile,
        JSON.stringify({
          title: record.title,
          startedAt: record.startedAt,
          scene: record.scene || "general",
          transcript: record.transcript || [],
          memoryItems: record.memoryItems || [],
        }),
        "utf8",
      );
      const args = [
        generateMinutesPath,
        "--input",
        inputFile,
        "--out",
        outFile,
      ];
      if (opts.provider) args.push("--provider", String(opts.provider));
      if (opts.model) args.push("--model", String(opts.model));
      const result = await new Promise((resolve) => {
        const child = spawn(pythonPath, args, {
          cwd: pocRoot,
          windowsHide: true,
          stdio: ["ignore", "pipe", "pipe"],
          env: pythonProcessEnv(),
        });
        let stdout = "";
        let stderr = "";
        child.stdout.on("data", (chunk) => {
          stdout += chunk.toString("utf8");
        });
        child.stderr.on("data", (chunk) => {
          stderr += chunk.toString("utf8");
        });
        child.on("close", (code) => {
          let payload = null;
          try {
            if (fsSync.existsSync(outFile)) {
              payload = JSON.parse(fsSync.readFileSync(outFile, "utf8"));
            } else {
              payload = JSON.parse(
                stdout.trim().split(/\r?\n/).filter(Boolean).at(-1) || "{}",
              );
            }
          } catch {
            payload = null;
          }
          resolve({ code, payload, stderr });
        });
        child.on("error", (error) =>
          resolve({ code: -1, payload: null, stderr: String(error.message || error) }),
        );
      });
      if (!result.payload?.ok || !result.payload.content) {
        const diagnostic =
          result.payload?.diagnostic &&
          typeof result.payload.diagnostic === "object"
            ? result.payload.diagnostic
            : {};
        const message =
          result.payload?.error ||
          result.stderr.trim() ||
          `纪要生成失败（退出码 ${result.code}）`;
        let failedRecord;
        try {
          attachMeetingError(meetingDatabasePath(), meetingId, {
            ...diagnostic,
            stage: diagnostic.stage || "minutes",
            provider:
              diagnostic.provider ||
              opts.provider ||
              record.runtimeConfig?.provider ||
              null,
            model:
              diagnostic.model ||
              opts.model ||
              record.runtimeConfig?.model ||
              null,
            message: safePersistedErrorMessage(message),
            at: Date.now(),
          });
          failedRecord = loadMeetingRecord(meetingDatabasePath(), meetingId);
        } catch (_) {
          /* 纪要失败本身不能被诊断落库失败遮住。 */
        }
        return {
          ok: false,
          message,
          diagnostic,
          ...(failedRecord ? { record: failedRecord } : {}),
        };
      }
      const updated = {
        ...record,
        minutes: {
          content: String(result.payload.content),
          generatedAt: Date.now(),
          sourceVersion:
            record.transcriptVersion === "offline" ? "offline" : "realtime",
        },
      };
      const databasePath = meetingDatabasePath();
      saveMeetingRecord(databasePath, updated);
      // 成功生成纪要后清掉本次纪要链路留下的旧超时诊断；其它阶段的
      // ASR/录音错误仍保留，避免“有内容但页面一直显示旧失败”。
      clearMeetingError(databasePath, meetingId, ["minutes", "facts", "final"]);
      const saved = loadMeetingRecord(databasePath, meetingId);
      return {
        ok: true,
        record: saved,
        summary: {
          elapsedSec: result.payload.elapsedSec,
          chunks: result.payload.chunks,
          provider: result.payload.provider,
          model: result.payload.model,
          timeoutSeconds: result.payload.timeoutSeconds,
          retryAttempts: result.payload.retryAttempts,
          evidenceMarkerCount: result.payload.evidenceMarkerCount,
          pendingEvidenceCount: result.payload.pendingEvidenceCount,
        },
      };
    } catch (error) {
      return { ok: false, message: String(error.message || error) };
    } finally {
      try {
        fsSync.rmSync(tmpDir, { recursive: true, force: true });
      } catch (_) {
        /* ignore */
      }
    }
  });

  ipcMain.handle("records:review:generate", async (_event, meetingId, opts = {}) => {
    await recordWriteQueue.catch(() => undefined);
    try {
      const databasePath = meetingDatabasePath();
      const localRecord = generateMeetingReview(databasePath, meetingId);
      sendMeetingEvent({
        type: "review_updated",
        meetingId: String(meetingId),
        review: localRecord?.review || null,
      });
      if (!opts?.enhance) return { ok: true, record: localRecord };

      const enhancement = await runReviewEnhancement(localRecord, opts);
      if (enhancement.payload?.ok) {
        const enhanced = applyReviewEnhancement(
          databasePath,
          meetingId,
          enhancement.payload,
        );
        sendMeetingEvent({
          type: "review_updated",
          meetingId: String(meetingId),
          review: enhanced?.review || null,
        });
        return { ok: true, record: enhanced };
      }

      const message =
        enhancement.payload?.error ||
        enhancement.stderr.trim() ||
        `模型增强失败（退出码 ${enhancement.code}）`;
      const fallback = markMeetingReview(databasePath, meetingId, "failed", message);
      sendMeetingEvent({
        type: "review_updated",
        meetingId: String(meetingId),
        review: fallback?.review || null,
      });
      return { ok: true, record: fallback, message };
    } catch (error) {
      const message = String(error.message || error);
      try {
        markMeetingReview(meetingDatabasePath(), meetingId, "failed", message);
      } catch (_) {
        /* keep the original error */
      }
      return { ok: false, message };
    }
  });

  ipcMain.handle("records:memory:save", async (_event, meetingId, item = {}) => {
    const saved = saveMeetingMemoryItem(meetingDatabasePath(), meetingId, item);
    sendMeetingEvent({ type: "memory_updated", meetingId: String(meetingId), item: saved });
    return saved;
  });

  ipcMain.handle("records:glossary-candidates:save", async (_event, meetingId, candidates = []) => ({
    ok: true,
    candidates: saveGlossaryCandidates(meetingDatabasePath(), meetingId, candidates),
  }));

  ipcMain.handle("records:glossary-candidates:promote", async (_event, meetingId, candidateIds = []) => ({
    ok: true,
    terms: promoteGlossaryCandidates(meetingDatabasePath(), meetingId, candidateIds),
  }));

  // 会后本地说话人分离（录音 + 可选声纹注册）
  ipcMain.handle("records:diarize", async (_event, meetingId, opts = {}) => {
    const record = loadMeetingRecord(meetingDatabasePath(), meetingId);
    if (!record) return { ok: false, message: "找不到会议" };
    if (!record.audioPath || !fsSync.existsSync(record.audioPath)) {
      return { ok: false, message: "本场没有可用录音，无法离线分离" };
    }
    const isOnline = record.meetingMode === "online";
    if (isOnline && (!record.systemAudioPath || !fsSync.existsSync(record.systemAudioPath))) {
      return {
        ok: false,
        message:
          "本场线上会议只有历史混音，没有系统独立音轨；为避免把“我”重新猜错，已保留实时版，不执行破坏性会后分离。",
      };
    }
    const tmpDir = fsSync.mkdtempSync(path.join(os.tmpdir(), "mc-diarize-"));
    const transcriptFile = path.join(tmpDir, "transcript.json");
    const outFile = path.join(tmpDir, "result.json");
    const cleanupInputFile = path.join(tmpDir, "cleanup-input.json");
    const cleanupOutFile = path.join(tmpDir, "cleanup-result.json");
    try {
      // 会后分离永远从“实时转写”版本重新生成，不能拿上一次离线结果继续分，
      // 否则用户来回切换几次后两个版本会互相污染。
      const realtimeVersion = record.transcriptVersions?.realtime || {
        transcript: record.transcript || [],
        speakers: record.speakers || [],
        generatedAt: Number(record.endedAt || Date.now()),
      };
      fsSync.writeFileSync(
        transcriptFile,
        JSON.stringify(realtimeVersion.transcript || []),
        "utf8",
      );
      // startedAt 是用户点击开始的时刻；Python 服务和 WAV 写入器稍后才启动。
      // 真实会议两者可差 2.7–35.2 秒。会后对齐必须传录音真正的墙上零点，
      // 否则 VAD 音频轴与 transcript.at 会整场错位。
      const recordingStartedAt =
        record.status === "completed" &&
        Number(record.endedAt) > Number(record.startedAt) &&
        Number(record.audioSeconds) > 0
          ? Math.max(
              Number(record.startedAt),
              Number(record.endedAt) - Number(record.audioSeconds) * 1000,
            )
          : Number(record.startedAt || 0);
      const args = [diarizeOfflinePath];
      if (isOnline) {
        args.push(
          "--meeting-mode",
          "online",
          "--system-wav",
          record.systemAudioPath,
        );
        if (record.micAudioPath && fsSync.existsSync(record.micAudioPath)) {
          args.push("--mic-wav", record.micAudioPath);
        }
        const speakerCount = Number(opts.speakerCount);
        if (Number.isInteger(speakerCount) && speakerCount >= 2 && speakerCount <= 12) {
          args.push("--speaker-count", String(speakerCount));
        }
      } else {
        args.push("--wav", record.audioPath);
        const speakerCount = Number(opts.speakerCount);
        if (Number.isInteger(speakerCount) && speakerCount >= 2 && speakerCount <= 12) {
          args.push("--speaker-count", String(speakerCount));
        }
      }
      args.push(
        "--transcript-json",
        transcriptFile,
        "--started-at",
        String(recordingStartedAt),
        "--out-json",
        outFile,
      );
      // 会后分离同样吃全部注册样本（diarize_offline 的 --enroll 也可重复传）
      const enrollList = isOnline
        ? []
        : opts.enrollWav
        ? [opts.enrollWav]
        : opts.voiceprint === false
          ? []
          : enrollSamplePaths();
      for (const p of enrollList.filter((x) => x && fsSync.existsSync(x))) {
        args.push("--enroll", p);
      }
      const th = Number(opts.meThreshold);
      if (Number.isFinite(th) && th > 0 && th < 1) {
        args.push("--me-threshold", String(th));
      }
      const cth = Number(opts.clusterThreshold);
      if (Number.isFinite(cth) && cth > 0 && cth < 1) {
        args.push("--cluster-th", String(cth));
      }

      const { code, stdout, stderr } = await new Promise((resolve) => {
        const child = spawn(pythonPath, args, {
          cwd: pocRoot,
          windowsHide: true,
          stdio: ["ignore", "pipe", "pipe"],
          env: pythonProcessEnv(),
        });
        let stdout = "";
        let stderr = "";
        child.stdout.on("data", (c) => {
          stdout += c.toString("utf8");
        });
        child.stderr.on("data", (c) => {
          stderr += c.toString("utf8");
        });
        child.on("close", (code) => resolve({ code, stdout, stderr }));
        child.on("error", (err) =>
          resolve({ code: -1, stdout: "", stderr: String(err.message || err) }),
        );
      });

      let result = null;
      if (fsSync.existsSync(outFile)) {
        result = JSON.parse(fsSync.readFileSync(outFile, "utf8"));
      } else {
        // fallback parse last json line
        const line = stdout
          .trim()
          .split(/\r?\n/)
          .filter(Boolean)
          .pop();
        if (line) result = JSON.parse(line);
      }
      if (!result || !result.ok) {
        return {
          ok: false,
          message:
            (result && result.error) ||
            stderr.trim() ||
            `分离失败（退出码 ${code}）`,
        };
      }

      // 回写会议档案
      const diarizedSpeakers = (result.speakers || []).map((s) => ({
        id: String(s.id),
        name: String(s.name || s.id),
        isMe: Boolean(s.isMe),
        mergedInto: null,
      }));
      const speakers = isOnline
        ? [
            { id: "me", name: "我", isMe: true, mergedInto: null },
            ...diarizedSpeakers.filter((speaker) => speaker.id !== "me"),
          ]
        : diarizedSpeakers;
      const transcript = Array.isArray(result.transcript)
        ? result.transcript.map((item) => ({
            ...item,
            speaker: item.speaker,
            speakerId: item.speakerId == null ? null : String(item.speakerId),
          }))
        : realtimeVersion.transcript;

      // 会后不应只重新贴说话人标签：实时 ASR 已经出现同音字/乱码时，
      // 用户看到的仍然是一段无法阅读的文字。这里保留 realtime 原文，
      // 再用当前配置的 LLM 做逐行、保守的可读性校对；校对失败不阻断
      // 会后分离，仍保存原始离线版本。
      let readableTranscript = transcript;
      let cleanupSummary = {
        status: "skipped",
        changed: 0,
        fallbackChunks: [],
        chunks: 0,
        elapsedSec: null,
        provider: null,
        model: null,
        reason: null,
      };
      try {
        const glossaryTerms = listGlossaryTermsForMeeting(
          meetingDatabasePath(),
          record.projectId || null,
        ).map((item) => item.term);
        const glossaryCandidates = (Array.isArray(record.glossaryCandidates)
          ? record.glossaryCandidates
          : listGlossaryCandidates(meetingDatabasePath(), record.id)
        )
          .map((item) => ({
            term: String(item?.term || "").trim(),
            sampleContext: String(item?.sampleContext || "")
              .trim()
              .slice(0, 120),
          }))
          .filter((item) => item.term)
          .slice(0, 120);
        const documentNames = (Array.isArray(record.documents)
          ? record.documents
          : []
        )
          .map((item) => String(item?.name || "").trim())
          .filter(Boolean)
          .slice(0, 40);
        fsSync.writeFileSync(
          cleanupInputFile,
          JSON.stringify(
            {
              title: record.title,
              scene: record.scene || "general",
              startedAt: record.startedAt,
              glossaryTerms,
              glossaryCandidates,
              documentNames,
              transcript,
            },
            null,
            2,
          ),
          "utf8",
        );
        const cleanupArgs = [
          cleanTranscriptPath,
          "--input",
          cleanupInputFile,
          "--out",
          cleanupOutFile,
        ];
        const cleanupProvider = String(
          opts.provider || record.runtimeConfig?.provider || "",
        ).trim();
        const cleanupModel = String(
          opts.model || record.runtimeConfig?.model || "",
        ).trim();
        if (cleanupProvider) cleanupArgs.push("--provider", cleanupProvider);
        if (cleanupModel) cleanupArgs.push("--model", cleanupModel);
        const cleanupResult = await new Promise((resolve) => {
          const child = spawn(pythonPath, cleanupArgs, {
            cwd: pocRoot,
            windowsHide: true,
            stdio: ["ignore", "pipe", "pipe"],
            env: pythonProcessEnv(),
          });
          let stdout = "";
          let stderr = "";
          let settled = false;
          const timer = setTimeout(() => {
            if (settled) return;
            settled = true;
            child.kill();
            resolve({ code: -1, stdout, stderr: "会后转写整理超时" });
          }, 8 * 60 * 1000);
          child.stdout.on("data", (chunk) => {
            stdout += chunk.toString("utf8");
          });
          child.stderr.on("data", (chunk) => {
            stderr += chunk.toString("utf8");
          });
          child.on("close", (code) => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            resolve({ code, stdout, stderr });
          });
          child.on("error", (error) => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            resolve({
              code: -1,
              stdout,
              stderr: String(error.message || error),
            });
          });
        });
        let cleanupPayload = null;
        if (fsSync.existsSync(cleanupOutFile)) {
          cleanupPayload = JSON.parse(fsSync.readFileSync(cleanupOutFile, "utf8"));
        } else {
          const line = cleanupResult.stdout
            .trim()
            .split(/\r?\n/)
            .filter(Boolean)
            .pop();
          if (line) cleanupPayload = JSON.parse(line);
        }
        if (cleanupPayload?.ok && Array.isArray(cleanupPayload.transcript)) {
          readableTranscript = cleanupPayload.transcript.map((item) => ({
            ...item,
            speaker: item.speaker,
            speakerId: item.speakerId == null ? null : String(item.speakerId),
          }));
          cleanupSummary = {
            status: "ok",
            changed: Number(cleanupPayload.changed || 0),
            fallbackChunks: Array.isArray(cleanupPayload.fallbackChunks)
              ? cleanupPayload.fallbackChunks
              : [],
            chunks: Number(cleanupPayload.chunks || 0),
            elapsedSec: cleanupPayload.elapsedSec ?? null,
            provider: cleanupPayload.provider || cleanupProvider || null,
            model: cleanupPayload.model || cleanupModel || null,
            reason: null,
          };
        } else {
          cleanupSummary = {
            ...cleanupSummary,
            status: "failed",
            provider: cleanupProvider || null,
            model: cleanupModel || null,
            reason: safePersistedErrorMessage(
              cleanupPayload?.error ||
                cleanupPayload?.diagnostic?.message ||
                cleanupResult.stderr ||
                `会后整理未返回有效结果（退出码 ${cleanupResult.code}）`,
            ),
          };
        }
      } catch (cleanupError) {
        cleanupSummary = {
          ...cleanupSummary,
          status: "failed",
          provider: String(opts.provider || record.runtimeConfig?.provider || "") || null,
          model: String(opts.model || record.runtimeConfig?.model || "") || null,
          reason: safePersistedErrorMessage(cleanupError.message || cleanupError),
        };
        console.warn("会后转写整理失败，保留原始离线转写：", cleanupError);
      }

      const updated = {
        ...record,
        transcript: readableTranscript,
        speakers,
        // realtime 保留为原始基线；会后默认展示分离 + 可读性校对版本。
        transcriptVersion: "offline",
        transcriptVersions: {
          ...(record.transcriptVersions || {}),
          realtime: realtimeVersion,
          offline: {
            transcript: readableTranscript,
            speakers,
            generatedAt: Date.now(),
            cleanup: {
              status: cleanupSummary.status,
              changed: cleanupSummary.changed,
              fallbackChunks: cleanupSummary.fallbackChunks,
              chunks: cleanupSummary.chunks,
              elapsedSec: cleanupSummary.elapsedSec,
              provider: cleanupSummary.provider,
              model: cleanupSummary.model,
              reason: cleanupSummary.reason,
            },
          },
        },
      };
      const saved = saveMeetingRecord(meetingDatabasePath(), updated);
      return {
        ok: true,
        record: saved,
        summary: {
          speakerCount: result.speakerCount,
          segmentCount: result.segmentCount,
          otherClusters: result.otherClusters ?? result.remoteClusters,
          enrollUsed: result.enrollUsed,
          elapsedSec: result.elapsedSec,
          durationSec: result.durationSec,
          // 有多少条过长的 final 被按说话人切开了（见 diarize_offline.align_transcript）
          splitItems: result.splitItems,
          transcriptItems: result.transcriptItems,
          meDecision: result.meDecision || (isOnline ? "fixed_microphone" : undefined),
          confidence: result.quality?.status || result.confidence,
          qualityReasons: result.quality?.reasons || [],
          cleanupStatus: cleanupSummary.status,
          cleanupChanged: cleanupSummary.changed,
          cleanupFallbackChunks: cleanupSummary.fallbackChunks,
          cleanupChunks: cleanupSummary.chunks,
          cleanupElapsedSec: cleanupSummary.elapsedSec,
          cleanupProvider: cleanupSummary.provider,
          cleanupModel: cleanupSummary.model,
          cleanupReason: cleanupSummary.reason,
          systemAudioOnly: Boolean(result.systemAudioOnly),
          remoteClusters: result.remoteClusters,
          note: result.note,
        },
      };
    } catch (error) {
      return { ok: false, message: String(error.message || error) };
    } finally {
      try {
        fsSync.rmSync(tmpDir, { recursive: true, force: true });
      } catch (_) {
        /* ignore */
      }
    }
  });

  ipcMain.handle("documents:preview", async (_event, filePath) => {
    try {
      return await extractDocument(String(filePath), 20000);
    } catch (error) {
      return { ok: false, message: `无法读取：${error.message}` };
    }
  });

  ipcMain.handle("records:list", async () => {
    return listMeetingRecords(meetingDatabasePath());
  });

  ipcMain.handle("records:delete", async (_event, rawIds) => {
    const ids = Array.from(
      new Set(
        (Array.isArray(rawIds) ? rawIds : [rawIds])
          .map((id) => String(id || "").trim())
          .filter(Boolean),
      ),
    );
    if (!ids.length) return { ok: false, canceled: true, deleted: 0 };
    if (activeMeetingRecordId && ids.includes(activeMeetingRecordId)) {
      throw new Error("进行中的会议不能删除，请先结束会议");
    }

    const records = ids
      .map((id) => loadMeetingRecord(meetingDatabasePath(), id))
      .filter(Boolean);
    if (!records.length) return { ok: true, deleted: 0 };
    const audioCount = records.filter((record) => record.audioPath).length;
    const answer = await dialog.showMessageBox(mainWindow, {
      type: "warning",
      buttons: ["取消", records.length === 1 ? "确认删除" : `删除 ${records.length} 场`],
      defaultId: 0,
      cancelId: 0,
      title: records.length === 1 ? "删除会议记录" : "批量删除会议记录",
      message:
        records.length === 1
          ? `确定删除「${records[0].title}」吗？`
          : `确定永久删除选中的 ${records.length} 场会议吗？`,
      detail:
        `转写、建议、纪要${audioCount ? `和 ${audioCount} 份本地录音` : ""}将永久删除。` +
        "知识库原文件不会被删除，此操作不可撤销。",
    });
    if (answer.response !== 1) {
      return { ok: false, canceled: true, deleted: 0 };
    }

    recordWriteQueue = recordWriteQueue
      .catch(() => undefined)
      .then(() => deleteMeetingRecords(meetingDatabasePath(), ids));
    const result = await recordWriteQueue;

    // 数据库里的路径不能直接当删除目标。只允许清理本应用 recordings 目录内的文件；
    // 即使数据库被篡改，也不能借删除会议去删用户任意文件。
    const recordingsRoot = path.resolve(app.getPath("userData"), "recordings");
    const allowedPrefix = `${recordingsRoot}${path.sep}`.toLowerCase();
    const candidates = new Set(result.audioPaths || []);
    for (const id of ids) {
      const safeId = id.replace(/[^a-zA-Z0-9_-]/g, "");
      candidates.add(path.join(recordingsRoot, `${safeId}.wav`));
      candidates.add(path.join(recordingsRoot, `${safeId}.mic.wav`));
      candidates.add(path.join(recordingsRoot, `${safeId}.system.wav`));
    }
    let audioDeleted = 0;
    for (const candidate of candidates) {
      const resolved = path.resolve(String(candidate));
      if (!resolved.toLowerCase().startsWith(allowedPrefix)) continue;
      try {
        if (fsSync.existsSync(resolved)) {
          fsSync.rmSync(resolved, { force: true });
          audioDeleted += 1;
        }
      } catch (error) {
        console.warn("删除会议录音失败：", resolved, error);
      }
    }
    return { ok: true, deleted: result.deleted, audioDeleted };
  });

  ipcMain.handle("records:save-documents", async (_event, meetingId, documents) =>
    saveMeetingDocuments(meetingDatabasePath(), meetingId, documents));

  // ── 数据设置（SET-4）────────────────────────────
  ipcMain.handle("data:info", async () => {
    const root = app.getPath("userData");
    const dbPath = meetingDatabasePath();
    const recordingsDir = path.join(root, "recordings");
    let dbBytes = 0;
    let audioBytes = 0;
    let audioCount = 0;
    try {
      dbBytes = fsSync.statSync(dbPath).size;
    } catch {}
    try {
      for (const file of fsSync.readdirSync(recordingsDir)) {
        audioBytes += fsSync.statSync(path.join(recordingsDir, file)).size;
        audioCount += 1;
      }
    } catch {}
    return { root, dbPath, recordingsDir, dbBytes, audioBytes, audioCount };
  });

  ipcMain.handle("data:reveal", async () => {
    shell.openPath(app.getPath("userData"));
    return { ok: true };
  });

  // 打开供应商密钥申请页。⚠️ 只放行白名单域名 + https：
  // 渲染进程若被注入，openExternal 会变成任意程序/URL 的启动器。
  ipcMain.handle("shell:open-external", async (_event, rawUrl) => {
    let parsed;
    try {
      parsed = new URL(String(rawUrl));
    } catch {
      throw new Error("地址无效");
    }
    if (parsed.protocol !== "https:") throw new Error("只允许 https 地址");
    const host = parsed.hostname.toLowerCase();
    if (!ALLOWED_EXTERNAL_HOSTS.has(host)) {
      throw new Error(`未在白名单内的域名：${host}`);
    }
    await shell.openExternal(parsed.toString());
    return { ok: true };
  });

  ipcMain.handle("data:clear", async () => {
    // 二次确认放在主进程：渲染进程被绕过也不会误删
    const answer = await dialog.showMessageBox(mainWindow, {
      type: "warning",
      buttons: ["取消", "确认清空"],
      defaultId: 0,
      cancelId: 0,
      title: "清空全部本地数据",
      message: "将删除所有会议记录、转写、建议与录音文件。",
      detail:
        "知识库中登记的文档只会解除登记，原文件保留在磁盘上。此操作不可撤销。",
    });
    if (answer.response !== 1) return { ok: false, canceled: true };
    if (bridgeProcess) throw new Error("请先结束当前会议再清空数据");
    closeDatabase();
    for (const target of [
      meetingDatabasePath(),
      `${meetingDatabasePath()}-wal`,
      `${meetingDatabasePath()}-shm`,
    ]) {
      fsSync.rmSync(target, { force: true });
    }
    fsSync.rmSync(path.join(app.getPath("userData"), "recordings"), {
      recursive: true,
      force: true,
    });
    return { ok: true };
  });

  ipcMain.handle("records:export", async (_event, id, format) => {
    const record = loadMeetingRecord(meetingDatabasePath(), id);
    if (!record) throw new Error("找不到该会议记录");
    const ext = format === "txt" ? "txt" : "md";
    const safeTitle = String(record.title).replace(/[\\/:*?"<>|]/g, "_");
    const result = await dialog.showSaveDialog(mainWindow, {
      title: "导出会议记录",
      defaultPath: `${safeTitle}.${ext}`,
      filters: [
        ext === "md"
          ? { name: "Markdown", extensions: ["md"] }
          : { name: "纯文本", extensions: ["txt"] },
      ],
    });
    if (result.canceled || !result.filePath) return { ok: false, canceled: true };
    await fs.writeFile(result.filePath, renderExport(record, ext), "utf8");
    return { ok: true, path: result.filePath };
  });

  ipcMain.handle("records:load", async (_event, id) => {
    return loadMeetingRecord(meetingDatabasePath(), id);
  });

  ipcMain.handle("records:save", async (_event, record) => {
    const pending = record?.id
      ? pendingMeetingHotwords.get(String(record.id))
      : undefined;
    const pendingAudio = record?.id
      ? pendingMeetingAudio.get(String(record.id))
      : undefined;
    const pendingError = record?.id
      ? pendingMeetingErrors.get(String(record.id))
      : undefined;
    const recordToSave = {
      ...(record || {}),
      ...(pending && !record?.hotwords ? { hotwords: pending } : {}),
      ...(pendingAudio
        ? {
            audioPath: record?.audioPath || pendingAudio.mixed?.path || null,
            audioSeconds:
              record?.audioSeconds ?? pendingAudio.mixed?.seconds ?? null,
            micAudioPath:
              record?.micAudioPath || pendingAudio.mic?.path || null,
            micAudioSeconds:
              record?.micAudioSeconds ?? pendingAudio.mic?.seconds ?? null,
            systemAudioPath:
              record?.systemAudioPath || pendingAudio.system?.path || null,
            systemAudioSeconds:
              record?.systemAudioSeconds ?? pendingAudio.system?.seconds ?? null,
          }
        : {}),
      ...(pendingError ? { lastError: pendingError } : {}),
    };
    recordWriteQueue = recordWriteQueue
      .catch(() => undefined)
      .then(() => {
        const saved = saveMeetingRecord(meetingDatabasePath(), recordToSave);
        if (pending) pendingMeetingHotwords.delete(String(record.id));
        if (pendingAudio) pendingMeetingAudio.delete(String(record.id));
        if (pendingError) pendingMeetingErrors.delete(String(record.id));
        return saved;
      });
    return recordWriteQueue;
  });

  ipcMain.handle("storage:load", async () => {
    const target = path.join(app.getPath("userData"), "meeting-copilot-state.json");
    try {
      return JSON.parse(await fs.readFile(target, "utf8"));
    } catch {
      return null;
    }
  });

  ipcMain.handle("storage:save", async (_event, state) => {
    const target = path.join(app.getPath("userData"), "meeting-copilot-state.json");
    const temp = `${target}.tmp`;
    await fs.writeFile(temp, JSON.stringify(state, null, 2), "utf8");
    await fs.rename(temp, target);
    return { ok: true };
  });

  // ── API 凭证（仅主进程存明文；渲染进程只见打码预览）────────
  ipcMain.handle("secrets:status", async () => {
    const appSecrets = loadSecretsSync();
    const configSecrets = await readConfigPyValues();
    const fields = {};
    for (const key of KNOWN_SECRET_KEYS) {
      if (appSecrets[key] != null && String(appSecrets[key]).trim() !== "") {
        fields[key] = {
          configured: true,
          preview: maskSecret(appSecrets[key]),
          source: "app",
        };
      } else if (
        configSecrets[key] != null &&
        String(configSecrets[key]).trim() !== ""
      ) {
        fields[key] = {
          configured: true,
          preview: maskSecret(configSecrets[key]),
          source: "config",
        };
      } else {
        fields[key] = { configured: false, preview: "", source: null };
      }
    }
    return { fields };
  });

  /**
   * 部分更新密钥。
   * - 传入非空字符串：写入/覆盖
   * - 传入空字符串：删除该键（回退到 config.py 若有）
   * - 未出现的键：保持不变
   */
  ipcMain.handle("secrets:save", async (_event, patch = {}) => {
    const current = loadSecretsSync();
    const next = { ...current };
    for (const [key, value] of Object.entries(patch || {})) {
      if (!KNOWN_SECRET_KEYS.includes(key)) continue;
      if (value == null || String(value).trim() === "") {
        delete next[key];
      } else {
        next[key] = String(value).trim();
      }
    }
    await writeSecrets(next);
    return { ok: true };
  });

  /** 把 config.py 里已有非空字段拷进设置页存储，便于迁移后不再改文件 */
  ipcMain.handle("secrets:import-config", async () => {
    const fromConfig = await readConfigPyValues();
    const current = loadSecretsSync();
    const next = { ...current };
    let imported = 0;
    for (const [key, value] of Object.entries(fromConfig)) {
      if (!KNOWN_SECRET_KEYS.includes(key)) continue;
      if (next[key] != null && String(next[key]).trim() !== "") continue;
      next[key] = String(value);
      imported += 1;
    }
    await writeSecrets(next);
    return { ok: true, imported, total: Object.keys(fromConfig).length };
  });

  // 启动先按确定性的会议 ID 扫描三路录音，再收尾上次遗留的"进行中"会议。
  // 这样即使应用在 bridge close 回调之前突然退出，下一次启动仍能把已写 WAV
  // 关联回档案；必须赶在窗口加载前，否则渲染进程会先读到无录音/僵尸记录。
  try {
    for (const record of listMeetingRecords(meetingDatabasePath())) {
      try {
        recoverExpectedMeetingAudio(record.id);
      } catch (error) {
        console.error(`回挂会议 ${record.id} 的录音失败：`, error);
      }
    }
  } catch (error) {
    console.error("启动扫描会议录音失败：", error);
  }
  try {
    finalizeOrphanedMeetings(meetingDatabasePath());
  } catch (error) {
    console.error("收尾遗留会议失败：", error);
  }

  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("before-quit", () => {
  if (bridgeProcess) bridgeProcess.kill();
  if (deviceTestProcess) deviceTestProcess.kill();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
