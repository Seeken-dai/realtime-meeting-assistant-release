export type Screen =
  | "home"
  | "prepare"
  | "meeting"
  | "history"
  | "history-detail"
  | "projects"
  | "knowledge"
  | "glossary"
  | "settings";

export interface BackgroundTaskInfo {
  key: string;
  type: "minutes" | "diarize" | "review" | "test_llm" | "test_asr";
  targetId?: string;
  title?: string;
  status: "running" | "success" | "error";
  message?: string;
  startedAt: number;
}

export type MeetingScene = "general" | "sales" | "requirements";

export type ResponseTone =
  | "direct"
  | "business"
  | "challenger"
  | "collaborative"
  | "custom";

export interface ResponseToneMeta {
  label: string;
  short: string;
  description: string;
}

export const RESPONSE_TONE_META: Record<ResponseTone, ResponseToneMeta> = {
  direct: {
    label: "直率务实（产研内推）",
    short: "直率务实",
    description: "极简干货、直指技术与业务逻辑、直说方案漏洞与执行动作，不带客套寒暄。",
  },
  business: {
    label: "商务稳健（对外客户）",
    short: "商务稳健",
    description: "得体客气、留有余地、严控承诺边界、积极引导对方需求。",
  },
  challenger: {
    label: "敏锐质询（把关挑刺）",
    short: "敏锐质询",
    description: "以质疑、挑刺、找逻辑漏洞与异常边界为主，充当会议的风险守门人。",
  },
  collaborative: {
    label: "温和协调（推进共识）",
    short: "温和协调",
    description: "善于总结分歧、化解冲突、提出折中方案并明确下一步行动。",
  },
  custom: {
    label: "自定义风格",
    short: "自定义",
    description: "使用用户在后台填写的个性化角色与风格提示词。",
  },
};

export interface SceneRecommendation {
  scene: MeetingScene;
  label: string;
  reason: string;
  confidence: "high" | "medium" | "low";
}

export interface RuntimeConfigSnapshot {
  provider: string;
  model: string;
  asrProvider: string;
  asrModel?: string;
  asrLang: string;
  timeoutSeconds: number;
  suggestionCount: number;
  silenceSeconds: number;
  glossaryStatus: string;
  glossaryCount: number;
  responseTone?: ResponseTone;
  customTonePrompt?: string;
}

export type MeetingMemoryKind = "decision" | "action_item";
export type MeetingMemoryStatus = "candidate" | "confirmed" | "rejected";

export interface MeetingMemoryItem {
  id: string;
  kind: MeetingMemoryKind;
  status: MeetingMemoryStatus;
  content: string;
  owner?: string | null;
  dueAt?: string | null;
  evidenceTranscriptId?: string | null;
  evidenceText?: string | null;
  source: "rule" | "model" | "user";
  createdAt: number;
  updatedAt: number;
}

export interface GlossaryCandidate {
  id: string;
  term: string;
  frequency: number;
  weight: number;
  sampleContext: string;
  reason: string;
  source: "frequency" | "title" | "document" | "asr" | "model";
  selected?: boolean;
  createdAt: number;
  updatedAt: number;
}

export interface MeetingReview {
  status: "pending" | "local" | "enhanced" | "failed";
  generatedAt: number;
  enhancedAt?: number | null;
  message?: string | null;
  memoryItems: MeetingMemoryItem[];
  glossaryCandidates: GlossaryCandidate[];
}

export interface InputDevice {
  index: number;
  name: string;
  channels: number;
  sampleRate: number;
  isDefault: boolean;
}

export interface TranscriptItem {
  id: string;
  speaker: string;
  speakerId?: string | null;
  text: string;
  isFinal: boolean;
  at: number;
  /** 相对本场录音起点的毫秒数；用于回放跳转和播放跟随。 */
  audioStartMs?: number | null;
  audioEndMs?: number | null;
}

/**
 * 说话人档案。
 *
 * ⚠️ 归属存在 speakerId 上而不是每条发言里：重命名/合并要一次性
 * 作用于该说话人的【全部历史与后续发言】（PRD SPK-2）。
 * mergedInto 用于把误分的两个编号指向同一人，保留原编号以便撤销。
 */
export interface SpeakerProfile {
  id: string;              // 讯飞角色编号
  name: string;            // 用户自定义显示名
  isMe: boolean;
  mergedInto?: string | null;
}

export interface ReferenceHit {
  source: string;
  text: string;
}

export interface Suggestion {
  intent: string;
  script: string;
  grounded?: boolean;
  level?: "grounded" | "advisory" | "clarify";
  references?: string[];
  /** 已在本次检索候选原文中逐字核验通过的证据 */
  evidence?: Array<{ source: string; quote: string }>;
  /** 用户当时采纳了这条话术（每批单选） */
  adopted?: boolean;
  /** 程序化校验的改判理由（如"引用了未检索到的文档"），需展示给用户 */
  notice?: string;
  /** 命中内部资料/承诺性表述的提醒（标注而非拦截） */
  sensitive?: string;
  /** 场景化分类；缺省时按通用场景兼容渲染。 */
  category?: string;
}

/**
 * 生成一批建议时实际送给模型的上下文范围。
 * wall* 使用 Unix 毫秒；audio* 使用本场录音起点的毫秒数。
 * 旧会议没有这个字段，界面会按批次生成时间回退到近似定位。
 */
export interface SuggestionContextRange {
  wallStartAt?: number | null;
  wallEndAt?: number | null;
  audioStartMs?: number | null;
  audioEndMs?: number | null;
  approximate?: boolean;
}

export interface SuggestionBatch {
  id: string;
  suggestions: Suggestion[];
  hits: ReferenceHit[];
  elapsed: number;
  at: number;
  /** 本批建议所依据的转写上下文范围（旧批次可缺省）。 */
  context?: SuggestionContextRange | null;
  /**
   * 本批生成失败（模型没吐出合法 JSON，重试后仍失败）。
   * ⚠️ 失败必须显式表达，绝不能把模型原始输出伪装成一条建议 —— 用户会
   *    照着念出去。raw 只进诊断区，不进话术位。
   */
  parseError?: {
    message: string;
    raw?: string;
    kind?: string;
    retryable?: boolean;
    attempts?: number;
    timeoutSeconds?: number | null;
    timeoutStage?: string | null;
    provider?: string;
    model?: string;
    cause?: string;
  };
  /** 诊断用：实际模型、触发方式、超时阶段和生成期间合并的新上下文次数。 */
  runtime?: {
    provider?: string;
    model?: string;
    elapsed?: number;
    trigger?: string;
    timeoutStage?: string | null;
    errorKind?: string;
    retryable?: boolean;
    attempts?: number;
    timeoutSeconds?: number | null;
    mergeCount?: number;
    contextChars?: number;
  };
}

export interface Project {
  id: string;
  name: string;
  note: string;
  createdAt: number;
  updatedAt: number;
  documentCount: number;
  meetingCount: number;
}

export interface KnowledgeDocument {
  id: string;
  projectId: string | null;   // null = 公共资料
  name: string;
  path: string;
  addedAt: number;
  exists: boolean;            // 路径引用可能失效
  size: number;
  modifiedAt: number;
}

/**
 * 专有名词（ASR 热词）。
 * projectId === null → 通用词库；非空 → 绑定项目。
 * 开会时合并「通用 + 本场项目」；仅部分 ASR 会真正读取（见 ASR_GLOSSARY_SUPPORT）。
 */
export interface GlossaryTerm {
  id: string;
  term: string;
  weight: number;
  projectId: string | null;
  note: string;
  createdAt: number;
  updatedAt: number;
  /** 开会合并结果里标记来源 */
  scope?: "general" | "project";
}

/** 会议知识范围快照（存路径，保证文档变动后历史仍可回溯） */
export interface MeetingDocument {
  id: string;
  name: string;
  path: string;
}

export type TranscriptVersionKind = "realtime" | "offline";

export interface MeetingTranscriptVersion {
  transcript: TranscriptItem[];
  speakers: SpeakerProfile[];
  generatedAt: number;
  /** 用户最后一次修改该版本正文的时间；用于判断既有纪要是否已过期。 */
  editedAt?: number;
  /** 会后可读性校对的可追溯结果；实时版本通常没有此字段。 */
  cleanup?: {
    status: "skipped" | "ok" | "failed";
    changed: number;
    fallbackChunks: number[];
    chunks: number;
    elapsedSec?: number | null;
    provider?: string | null;
    model?: string | null;
    reason?: string | null;
  };
}

export interface MeetingMinutes {
  content: string;
  generatedAt: number;
  sourceVersion: TranscriptVersionKind;
}

export interface MeetingRecord {
  id: string;
  title: string;
  startedAt: number;
  endedAt?: number;
  /**
   * active 只在本次运行、桥接子进程还活着时成立。
   * interrupted = 上次运行没能正常结束、启动时被收尾的残留（见
   * meeting-store.cjs finalizeOrphanedMeetings）。
   */
  status: "active" | "completed" | "interrupted";
  /** 旧会议没有场景时按通用场景展示。 */
  scene?: MeetingScene;
  responseTone?: ResponseTone;
  customTonePrompt?: string;
  runtimeConfig?: RuntimeConfigSnapshot | null;
  /** 线上记录才使用独立麦克风/系统回环音轨；旧记录默认为线下兼容模式。 */
  meetingMode?: "in_person" | "online";
  projectId?: string | null;
  projectName?: string | null;
  transcript: TranscriptItem[];
  batches: SuggestionBatch[];
  documents?: MeetingDocument[];
  speakers?: SpeakerProfile[];
  /** 当前查看/编辑的转写版本；旧记录默认 realtime。 */
  transcriptVersion?: TranscriptVersionKind;
  /** 运行会后处理后才出现；两个版本各自保留人工改名、合并和逐段改派。 */
  transcriptVersions?: Partial<
    Record<TranscriptVersionKind, MeetingTranscriptVersion>
  >;
  /** AI 生成并本地持久化的 Markdown 会议纪要。 */
  minutes?: MeetingMinutes | null;
  /** 录音文件（边录边写，崩溃也不丢） */
  audioPath?: string | null;
  audioSeconds?: number | null;
  micAudioPath?: string | null;
  micAudioSeconds?: number | null;
  systemAudioPath?: string | null;
  systemAudioSeconds?: number | null;
  /** 本场热词同步结果；只存状态、词数和词表 ID，不存密钥。 */
  hotwords?: {
    status: "pending" | "empty" | "loaded" | "degraded" | "unsupported";
    count: number;
    vocabularyId?: string | null;
    reason?: string | null;
  };
  /** 最近一次桥接/音频/服务错误；用于异常退出后在历史页恢复上下文。 */
  lastError?: {
    stage: string;
    message: string;
    at: number;
    provider?: string;
    model?: string;
    kind?: string;
    timeoutStage?: string;
    cause?: string;
    attempts?: number;
    timeoutSeconds?: number;
    retryable?: boolean;
  };
  review?: MeetingReview | null;
  memoryItems?: MeetingMemoryItem[];
  glossaryCandidates?: GlossaryCandidate[];
}

export interface MeetingEvent {
  type: string;
  [key: string]: unknown;
}

export interface PersistedState {
  meetingTitle: string;
  scene?: MeetingScene;
  theme: "light" | "dark";
  /** 线下：单麦克风 + 说话人识别；线上：麦克风/系统播放双通道。 */
  meetingMode?: "in_person" | "online";
  selectedDevice?: number;
  silenceSeconds: number;
  suggestionCount: number;
  /** 历史配置遗留字段；新建会议时直接忽略，不再读取或写入 */
  lastProjectId?: string | null;
  /** UI 选择的供应商（下场会议生效），空则用 config.py 默认 */
  asrProvider?: string;
  /** UI 选择的 ASR 模型名（如阿里云下可选 qwen-audio / fun-asr 等） */
  asrModel?: string;
  /**
   * 识别语种：zh（中文）/ en（英文）/ zh_en（中英混用，默认）。
   * 限制自动识语种串出日文等；下场会议生效。
   */
  asrLang?: "zh" | "en" | "zh_en";
  llmProvider?: string;
  llmModel?: string;
  /** 会中启用本地声纹认「我」（需已注册 enroll wav） */
  voiceprintEnabled?: boolean;
  /** 声纹阈值，默认 0.65 */
  meThreshold?: number;
  /** 话术应答风格 */
  responseTone?: ResponseTone;
  /** 自定义话术风格补充指令 */
  customTonePrompt?: string;
}

/** 设置页凭证字段状态（主进程只回传打码预览） */
export interface SecretFieldStatus {
  configured: boolean;
  preview: string;
  source: "app" | "config" | null;
}

export interface SecretsStatus {
  fields: Record<string, SecretFieldStatus>;
}

/** 展示用：连续同说话人 final 合并后的气泡 */
export interface DisplayUtterance extends TranscriptItem {
  /** 合并进来的原始段落 id，改派时整组一起动 */
  segmentIds: string[];
  /**
   * 合并前的原始段落，按停顿分行渲染用。
   * ⚠️ 只合并文本会把"说了两句、中间停了 5 秒"压成一整坨，读的人分不出
   *    节奏。保留分段并携带时间，展示时才能按停顿断行。
   */
  segments: Array<{
    id: string;
    text: string;
    at: number;
    audioStartMs?: number | null;
    audioEndMs?: number | null;
  }>;
}

/** 三类服务各自的状态（PRD §6.0 要求分区诊断，故障能定位到具体环节） */
export interface ServiceStatus {
  asr: {
    provider: string;
    label: string;
    model?: string | null;
    ok: boolean;
    keyVar: string | null;
    /** 所有可选 ASR 供应商 id，供设置页下拉 */
    options?: string[];
  };
  llm: {
    provider: string;
    ok: boolean;
    label?: string;
    model?: string;
    baseUrl?: string;
    /** SET-7：实际生效的凭证变量名，避免静默回退误导用户 */
    keyVar: string | null;
    keyPreview: string | null;
    message?: string;
  };
  retrieval: { backend: string; label: string; ok: boolean; note: string };
  providers: Array<{ id: string; label: string; model: string | null }>;
}

export interface LlmTestResult {
  ok: boolean;
  verdict?: "pass" | "warning" | "high_risk";
  targetSeconds?: number;
  label?: string;
  model?: string;
  elapsed?: number;
  reply?: string;
  message?: string;
  hint?: string;
}

export interface BenchResult {
  /** current = 只测当前供应商；all = 对比全部已配置 */
  scope?: "current" | "all";
  results: Array<{
    provider: string;
    label: string;
    model: string;
    ok: boolean;
    avg: number | null;
    max: number | null;
    error: string | null;
  }>;
  fastest: { provider: string; label: string; avg: number } | null;
  targetSeconds: number;
}

/** 读取供应商目录并验证当前模型；目录不可用时回退到候选模型探测。 */
export interface ProbeResult {
  provider: string;
  ok: boolean;
  label?: string;
  message?: string;
  source?: "catalog" | "fallback";
  catalogCount?: number;
  discoveryError?: string;
  results?: Array<{
    model: string;
    ok: boolean;
    elapsed?: number;
    verified?: boolean;
    source?: "catalog" | "probe" | "fallback";
    error?: string;
    hint?: string;
  }>;
  fastest?: { model: string; elapsed: number } | null;
}

export interface AsrTestResult {
  provider: string;
  ok: boolean;
  label?: string;
  elapsed?: number;
  message?: string;
}

/** 本机数据占用情况（SET-4） */
export interface DataInfo {
  root: string;
  dbPath: string;
  recordingsDir: string;
  dbBytes: number;
  audioBytes: number;
  audioCount: number;
}

export interface DesktopBridge {
  runtimeStatus(): Promise<{
    desktop: boolean;
    pythonReady: boolean;
    bridgeReady: boolean;
    configPresent: boolean;
    activeMeetingId?: string | null;
  }>;
  getActiveMeeting?(): Promise<{ active: boolean; meetingId: string | null }>;
  listInputDevices(): Promise<{ type: "devices"; devices: InputDevice[] }>;
  testInputDevice(device: number): Promise<{ ok: boolean }>;
  startMeeting(options: {
    /** 用于决定录音落盘路径 */
    meetingId?: string;
    me?: string;
    device?: number;
    meetingMode?: "in_person" | "online";
    scene?: MeetingScene;
    responseTone?: ResponseTone;
    customTonePrompt?: string;
    /** 本场归属项目：用于合并项目专有名词 */
    projectId?: string | null;
    provider?: string;      // LLM 供应商
    llmModel?: string;      // LLM 模型名（UI 探测选中）
    asrProvider?: string;   // ASR 供应商
    asrModel?: string;      // ASR 模型名（如阿里云下模型选型）
    /** 识别语种：zh / en / zh_en */
    asrLang?: string;
    /** false 时不附带声纹注册文件 */
    voiceprint?: boolean;
    enrollWav?: string;
    meThreshold?: number;
    /** 设置页「建议触发」：对方停顿多久后给建议 / 每批几条 */
    silenceSeconds?: number;
    suggestionCount?: number;
    /** 本场会议的知识范围。必须显式传（哪怕空数组），否则会退回全局库 */
    documents?: MeetingDocument[];
  }): Promise<{ ok: boolean }>;
  /** 会前预热：导入重依赖、预同步热词（不打开麦克风） */
  warmupMeeting(options?: {
    projectId?: string | null;
    asrProvider?: string;
  }): Promise<{
    ok: boolean;
    vocabularyId?: string | null;
    termCount?: number;
    elapsedMs?: number;
    steps?: Array<{ name: string; ok: boolean; message?: string }>;
    error?: string | null;
    cached?: boolean;
    reused?: boolean;
    warning?: string | null;
  }>;
  /** 准备完成后正式开麦/录音；会议计时应从此后的 listening 起算 */
  beginMeetingRecording(): Promise<{ ok: boolean; reason?: string }>;
  recommendMeetingScene(input: {
    title: string;
    projectName?: string | null;
    documentNames?: string[];
  }): Promise<SceneRecommendation>;
  setMeetingControls(controls: {
    recordingPaused?: boolean;
    suggestionsPaused?: boolean;
  }): Promise<{ ok: boolean }>;
  /** ok=false 表示没有在跑的桥接进程，调用方需自行做本地收尾 */
  stopMeeting(): Promise<{ ok: boolean; reason?: string }>;
  ask(question: string): Promise<{ ok: boolean }>;
  /** 手动索取一批建议，绕过自动建议的冷却/增量闸门 */
  suggestNow(): Promise<{ ok: boolean }>;
  setMeSpeaker(speakerId: string | null): Promise<{ ok: boolean }>;
  voiceprintStatus(): Promise<{
    ok: boolean;
    path?: string;
    /** 全部样本合计时长（多段注册） */
    seconds?: number;
    bytes?: number;
    mtime?: number;
    message?: string;
    samples?: Array<{
      index: number;
      path: string;
      seconds: number;
      bytes: number;
      mtime: number;
    }>;
    sampleCount?: number;
    totalSeconds?: number;
  }>;
  enrollVoiceprint(options?: {
    seconds?: number;
    device?: number;
    /** 默认追加一段；传 false 表示清空重来 */
    append?: boolean;
  }): Promise<{ ok: boolean; path?: string }>;
  clearVoiceprint(): Promise<{ ok: boolean; message?: string }>;
  removeLastVoiceprintSample(): Promise<{
    ok: boolean;
    remaining?: number;
    message?: string;
  }>;
  listMeetingRecords(): Promise<MeetingRecord[]>;
  loadMeetingRecord(id: string): Promise<MeetingRecord | null>;
  saveMeetingRecord(record: MeetingRecord): Promise<MeetingRecord>;
  deleteMeetingRecords(ids: string[]): Promise<{
    ok: boolean;
    canceled?: boolean;
    deleted: number;
    audioDeleted?: number;
  }>;
  saveMeetingDocuments(
    meetingId: string,
    documents: MeetingDocument[],
  ): Promise<{ ok: boolean }>;
  exportMeetingRecord(
    id: string,
    format: "md" | "txt",
  ): Promise<{ ok: boolean; canceled?: boolean; path?: string }>;
  listProjects(): Promise<Project[]>;
  saveProject(project: {
    id?: string;
    name: string;
    note?: string;
  }): Promise<Project>;
  deleteProject(id: string): Promise<{ ok: boolean }>;
  listGlossaryTerms(
    scope?: "general" | "all" | string,
  ): Promise<GlossaryTerm[]>;
  saveGlossaryTerm(term: {
    id?: string;
    term: string;
    weight?: number;
    projectId?: string | null;
    note?: string;
  }): Promise<GlossaryTerm>;
  deleteGlossaryTerm(id: string): Promise<{ ok: boolean }>;
  listGlossaryTermsForMeeting(
    projectId?: string | null,
  ): Promise<GlossaryTerm[]>;
  getProjectDocuments(projectId: string): Promise<string[]>;
  setProjectDocuments(
    projectId: string,
    docIds: string[],
  ): Promise<{ ok: boolean }>;
  listDocuments(projectId?: string | null): Promise<KnowledgeDocument[]>;
  pickDocuments(
    projectId?: string | null,
  ): Promise<{
    added: number;
    discoveredCount?: number;
    documents: KnowledgeDocument[];
    errors?: Array<{ name: string; message: string }>;
  }>;
  pickDocumentFolder(
    projectId?: string | null,
  ): Promise<{
    added: number;
    discoveredCount?: number;
    documents: KnowledgeDocument[];
    errors?: Array<{ name: string; message: string }>;
  }>;
  getPathForFile?(file: File): string;
  addDocumentPaths(
    filePaths: string[],
    projectId?: string | null,
  ): Promise<{
    added: number;
    discoveredCount?: number;
    documents: KnowledgeDocument[];
    errors?: Array<{ name: string; message: string }>;
  }>;
  removeDocument(id: string): Promise<{ ok: boolean }>;
  renameDocument(id: string, name: string): Promise<{ ok: boolean }>;
  loadMeetingAudio(
    id: string,
  ): Promise<{ ok: boolean; dataUrl?: string; seconds?: number; message?: string }>;
  diarizeMeeting(
    id: string,
    opts?: {
      enrollWav?: string;
      voiceprint?: boolean;
      meThreshold?: number;
      clusterThreshold?: number;
      speakerCount?: number;
      /** 会后整理时覆盖会议保存的 LLM 配置；不传则沿用会议运行时配置。 */
      provider?: string;
      model?: string;
      cleanTranscript?: boolean;
    },
  ): Promise<{
    ok: boolean;
    message?: string;
    record?: MeetingRecord;
    summary?: {
      speakerCount?: number;
      segmentCount?: number;
      otherClusters?: number;
      enrollUsed?: boolean;
      elapsedSec?: number;
      durationSec?: number;
      /** 被按说话人切开的过长 final 条数 / 切完后的总条数 */
      splitItems?: number;
      transcriptItems?: number;
      /** cluster = 按簇均分认出「我」；threshold = 退回逐段阈值；none = 没注册声纹 */
      meDecision?: string;
      confidence?: string;
      qualityReasons?: string[];
      systemAudioOnly?: boolean;
      remoteClusters?: number;
      /** 会后逐行可读性校对结果。 */
      cleanupStatus?: "skipped" | "ok" | "failed";
      cleanupChanged?: number;
      cleanupFallbackChunks?: number[];
      cleanupChunks?: number;
      cleanupElapsedSec?: number | null;
      cleanupProvider?: string | null;
      cleanupModel?: string | null;
      cleanupReason?: string | null;
      note?: string;
    };
  }>;
  generateMeetingMinutes(
    id: string,
    opts?: { provider?: string; model?: string },
  ): Promise<{
    ok: boolean;
    message?: string;
    diagnostic?: {
      stage?: string;
      kind?: string;
      retryable?: boolean;
      attempts?: number;
      timeoutSeconds?: number | null;
      timeoutStage?: string | null;
      provider?: string;
      model?: string;
      cause?: string;
    };
    record?: MeetingRecord;
    summary?: {
      elapsedSec?: number;
      chunks?: number;
      provider?: string;
      model?: string;
      timeoutSeconds?: number;
      retryAttempts?: number;
      evidenceMarkerCount?: number;
      pendingEvidenceCount?: number;
    };
  }>;
  generateMeetingReview(
    id: string,
    opts?: { enhance?: boolean; provider?: string; model?: string },
  ): Promise<{ ok: boolean; message?: string; record?: MeetingRecord }>;
  saveMeetingMemoryItem(
    meetingId: string,
    item: Partial<MeetingMemoryItem> & { id?: string },
  ): Promise<MeetingMemoryItem>;
  saveGlossaryCandidates(
    meetingId: string,
    candidates: GlossaryCandidate[],
  ): Promise<{ ok: boolean; candidates: GlossaryCandidate[] }>;
  promoteGlossaryCandidates(
    meetingId: string,
    candidateIds: string[],
  ): Promise<{ ok: boolean; terms: GlossaryTerm[] }>;
  openFloatingStrategy(): Promise<{ ok: boolean }>;
  closeFloatingStrategy(): Promise<{ ok: boolean }>;
  setFloatingStrategyPreferences(preferences: {
    alwaysOnTop?: boolean;
    contentProtection?: boolean;
    collapsed?: boolean;
    fontSize?: "small" | "medium" | "large";
    tint?: string;
    opacity?: number;
  }): Promise<{ ok: boolean; alwaysOnTop?: boolean }>;
  dataInfo(): Promise<DataInfo>;
  revealDataFolder(): Promise<{ ok: boolean }>;
  /** 用系统浏览器打开供应商密钥申请页；主进程有 https + 域名白名单校验 */
  openExternal(url: string): Promise<{ ok: boolean }>;
  clearAllData(): Promise<{ ok: boolean; canceled?: boolean }>;
  previewDocument(
    path: string,
  ): Promise<{ ok: boolean; text?: string; message?: string }>;
  serviceStatus(opts?: {
    provider?: string;
    asrProvider?: string;
  }): Promise<ServiceStatus>;
  testLlm(
    opts?: string | { provider?: string; model?: string; scene?: MeetingScene },
  ): Promise<LlmTestResult>;
  probeLlm(provider?: string): Promise<ProbeResult>;
  testAsr(
    opts?: string | { asrProvider?: string; asrLang?: string; asrModel?: string },
  ): Promise<AsrTestResult>;
  benchmarkProviders(opts?: {
    all?: boolean;
    provider?: string;
    model?: string;
  }): Promise<BenchResult>;
  loadState(): Promise<PersistedState | null>;
  saveState(state: PersistedState): Promise<{ ok: boolean }>;
  /** 凭证状态（仅打码预览，不含明文） */
  secretsStatus(): Promise<SecretsStatus>;
  /** 部分写入/清除密钥（空字符串=清除该键） */
  saveSecrets(patch: Record<string, string>): Promise<{ ok: boolean }>;
  /** 从 poc/config.py 导入尚未在应用内配置的密钥 */
  importSecretsFromConfig(): Promise<{
    ok: boolean;
    imported: number;
    total: number;
  }>;
  onMeetingEvent(callback: (event: MeetingEvent) => void): () => void;
}

declare global {
  interface Window {
    meetingCopilot?: DesktopBridge;
  }
}
