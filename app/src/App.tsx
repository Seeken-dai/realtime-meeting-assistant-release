import {
  Archive,
  ArrowLeft,
  BookOpen,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  FolderKanban,
  CircleStop,
  Clock3,
  Copy,
  Download,
  ExternalLink,
  FileText,
  Gauge,
  Headphones,
  History,
  Home,
  Library,
  ListChecks,
  Lock,
  Maximize2,
  MessageSquareText,
  Mic2,
  MicOff,
  Minimize2,
  Moon,
  Pause,
  PanelLeftClose,
  PanelLeftOpen,
  Pencil,
  PictureInPicture2,
  Pin,
  PinOff,
  Play,
  Plus,
  Radio,
  RotateCcw,
  Search,
  Send,
  Settings,
  ShieldAlert,
  Sparkles,
  Split,
  SlidersHorizontal,
  Sun,
  Tags,
  Trash2,
  Type,
  UploadCloud,
  FolderPlus,
  UserRoundCheck,
  Volume2,
  Wand2,
  Loader2,
  X,
} from "lucide-react";
import {
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { demoBatch, demoTranscript } from "./demo";
import { meetingStartupAction } from "./meeting-startup";
import { shouldAutoApplySceneRecommendation } from "./meeting-scene";
import type {
  BackgroundTaskInfo,
  InputDevice,
  KnowledgeDocument,
  GlossaryTerm,
  MeetingDocument,
  MeetingRecord,
  MeetingEvent,
  PersistedState,
  Project,
  Screen,
  ReferenceHit,
  SpeakerProfile,
  ServiceStatus,
  LlmTestResult,
  BenchResult,
  ProbeResult,
  AsrTestResult,
  DataInfo,
  DisplayUtterance,
  SecretsStatus,
  Suggestion,
  SuggestionBatch,
  SuggestionContextRange,
  MeetingScene,
  SceneRecommendation,
  RuntimeConfigSnapshot,
  MeetingMemoryItem,
  GlossaryCandidate,
  TranscriptItem,
  DesktopBridge,
} from "./types";
import {
  buildSpeakerDistribution,
  findNearestSuggestionBatchForTranscript,
  findTranscriptIdsForContext,
  getSuggestionContext,
} from "./timeline";

function getHotwordReasonLabel(reason?: string | null) {
  const value = String(reason || "").trim();
  return value === "阿里热词同步失败或凭证未配置"
    ? "旧版本未记录具体失败原因"
    : value;
}
import {
  buildPlaybackRanges,
  playbackRangeAt,
} from "./playback";
import type { PlaybackRange } from "./playback";

/**
 * 专有名词库始终可维护；这里只描述「本应用是否会把词库传给该 ASR」。
 * 厂商另有控制台热词能力（如讯飞）不代表会读我们本地词库。
 */
const ASR_GLOSSARY_SUPPORT: Record<
  string,
  { readsLibrary: boolean; note: string }
> = {
  aliyun: {
    readsLibrary: true,
    note: "开会时同步到阿里预编译热词（Paraformer，约 500 词上限）",
  },
  xfyun: {
    readsLibrary: false,
    note: "本版不会读取本地专有名词库；讯飞控制台可另配个性化热词，与本库无关",
  },
  "xfyun-llm": {
    readsLibrary: false,
    note: "本版不会读取本地专有名词库",
  },
  tencent: {
    readsLibrary: false,
    note: "厂商支持临时热词，本版尚未把本地词库接上，开会暂不生效",
  },
  volcano: {
    readsLibrary: false,
    note: "厂商支持热词直传，本版尚未把本地词库接上，开会暂不生效",
  },
  mimo: {
    readsLibrary: false,
    note: "无用户热词表接口；维护词库不影响该转写服务",
  },
};

const ALIYUN_ASR_MODELS = [
  {
    id: "qwen-audio-3.0-asr-flash-streaming",
    label: "Qwen-Audio 3.0 流式（推荐）",
  },
  {
    id: "fun-asr-realtime",
    label: "FunASR 实时（多语种与方言）",
  },
  {
    id: "paraformer-realtime-v2",
    label: "Paraformer v2（经典基线）",
  },
];

function generateDefaultMeetingTitle() {
  const d = new Date();
  const year = d.getFullYear();
  const month = d.getMonth() + 1;
  const day = d.getDate();
  const hours = String(d.getHours()).padStart(2, "0");
  const minutes = String(d.getMinutes()).padStart(2, "0");
  return `${year}年${month}月${day}日${hours}:${minutes}`;
}

const initialState: PersistedState = {
  meetingTitle: "",
  scene: "general",
  theme: "light",
  meetingMode: "in_person",
  silenceSeconds: 2,
  suggestionCount: 3,
  asrLang: "zh_en",
};

function mergePersistedState(saved: Partial<PersistedState>): PersistedState {
  const next = { ...initialState, ...saved };
  if (
    !next.meetingTitle ||
    ["xx项目需求澄清会", "xx 项目需求澄清会"].includes(
      String(next.meetingTitle || "").trim().toLowerCase(),
    )
  ) {
    next.meetingTitle = generateDefaultMeetingTitle();
  }
  return next;
}

const SCENE_META: Record<
  MeetingScene,
  { label: string; short: string; description: string; categories: string[] }
> = {
  general: {
    label: "通用会议",
    short: "通用",
    description: "澄清、总结、风险和下一步。",
    categories: ["澄清", "总结", "风险", "下一步"],
  },
  sales: {
    label: "售前沟通",
    short: "售前",
    description: "客户目标、痛点、异议、能力边界和推进动作。",
    categories: ["客户目标", "客户痛点", "异议", "能力边界", "商务承诺", "推进动作"],
  },
  requirements: {
    label: "需求评审",
    short: "评审",
    description: "范围、业务规则、接口、异常、验收和责任人。",
    categories: ["范围", "业务规则", "数据与接口", "异常分支", "验收标准", "责任人与待确认项"],
  },
};

/** 各供应商在设置页需要填写的凭证字段（键名与 config.py / 环境变量一致） */
const ASR_CREDENTIAL_FIELDS: Record<
  string,
  Array<{ key: string; label: string; hint?: string }>
> = {
  xfyun: [
    { key: "XFYUN_APP_ID", label: "App ID" },
    {
      key: "XFYUN_API_KEY",
      label: "API Key",
      hint: "「实时语音转写」服务下的 Key，不是星火密码",
    },
  ],
  "xfyun-llm": [
    {
      key: "XFYUN_LLM_ASR_APP_ID",
      label: "应用 App ID",
      hint: "开放平台「我的应用」里的 APPID；也可填标准版同一 App ID。与 Access Key 不是同一个",
    },
    { key: "XFYUN_LLM_ASR_KEY_ID", label: "Access Key ID" },
    {
      key: "XFYUN_LLM_ASR_KEY_SECRET",
      label: "Access Key Secret",
      hint: "控制台 → 实时语音转写大模型 → 服务接口认证信息",
    },
  ],
  aliyun: [{ key: "ALIYUN_ASR_KEY", label: "API Key" }],
  volcano: [
    { key: "VOLC_APP_KEY", label: "App Key" },
    { key: "VOLC_ACCESS_KEY", label: "Access Key" },
  ],
  tencent: [
    { key: "TENCENT_APP_ID", label: "App ID" },
    { key: "TENCENT_SECRET_ID", label: "Secret ID" },
    { key: "TENCENT_SECRET_KEY", label: "Secret Key" },
  ],
  mimo: [{ key: "MIMO_API_KEY", label: "API Key" }],
};

const LLM_CREDENTIAL_FIELDS: Record<
  string,
  Array<{ key: string; label: string; hint?: string }>
> = {
  xfyun: [
    {
      key: "XFYUN_SPARK_PASSWORD",
      label: "经典系列 APIPassword",
      hint: "4.0Ultra 等经典模型专用",
    },
  ],
  "xfyun-x2-flash": [
    {
      key: "XFYUN_X2_PASSWORD",
      label: "X2 / X2-Flash APIPassword",
      hint: "与经典系列密码相互独立",
    },
  ],
  "xfyun-x2": [{ key: "XFYUN_X2_PASSWORD", label: "X2 APIPassword" }],
  "xfyun-x1.5": [{ key: "XFYUN_X15_PASSWORD", label: "X1.5 APIPassword" }],
  aliyun: [{ key: "ALIYUN_LLM_KEY", label: "API Key" }],
  mimo: [{ key: "MIMO_LLM_KEY", label: "API Key" }],
  gemini: [{ key: "GEMINI_LLM_KEY", label: "API Key" }],
  zhipu: [{ key: "ZHIPU_LLM_KEY", label: "API Key" }],
  deepseek: [{ key: "DEEPSEEK_LLM_KEY", label: "API Key" }],
  moonshot: [{ key: "MOONSHOT_LLM_KEY", label: "API Key" }],
  grok: [{ key: "GROK_LLM_KEY", label: "API Key" }],
  custom: [
    { key: "CUSTOM_LLM_BASE_URL", label: "Base URL" },
    { key: "CUSTOM_LLM_MODEL", label: "模型名" },
    { key: "CUSTOM_LLM_KEY", label: "API Key（本地可填任意值）" },
  ],
};

/**
 * 各供应商的密钥申请 / 控制台地址（凭证区「去申请」按钮）。
 * ⚠️ 新增地址时必须同步在 `electron/main.cjs` 的 `ALLOWED_EXTERNAL_HOSTS`
 * 里加上域名，否则主进程会拒绝打开。
 */
type ProviderConsole = { url: string; label: string };

const ASR_CONSOLE: Record<string, ProviderConsole> = {
  xfyun: {
    url: "https://www.xfyun.cn/service/rtasr",
    label: "讯飞 · 实时语音转写",
  },
  "xfyun-llm": {
    url: "https://console.xfyun.cn/services/new_rta",
    label: "讯飞控制台 · 转写大模型",
  },
  aliyun: {
    url: "https://bailian.console.aliyun.com/",
    label: "阿里云百炼控制台",
  },
  volcano: {
    url: "https://console.volcengine.com/speech/app",
    label: "火山引擎 · 语音技术",
  },
  tencent: {
    url: "https://console.cloud.tencent.com/asr",
    label: "腾讯云 · 语音识别",
  },
  mimo: { url: "https://mimo.mi.com/", label: "小米 MiMo" },
};

const LLM_CONSOLE: Record<string, ProviderConsole> = {
  // 星火各系列的 APIPassword 在同一个控制台，进去后选对应模型服务
  xfyun: { url: "https://console.xfyun.cn/", label: "讯飞控制台 · 星火经典" },
  "xfyun-x2-flash": {
    url: "https://console.xfyun.cn/",
    label: "讯飞控制台 · 星火 X2",
  },
  "xfyun-x2": { url: "https://console.xfyun.cn/", label: "讯飞控制台 · 星火 X2" },
  "xfyun-x1.5": {
    url: "https://console.xfyun.cn/",
    label: "讯飞控制台 · 星火 X1.5",
  },
  aliyun: {
    url: "https://bailian.console.aliyun.com/",
    label: "阿里云百炼控制台",
  },
  mimo: { url: "https://mimo.mi.com/", label: "小米 MiMo" },
  gemini: { url: "https://aistudio.google.com/apikey", label: "Google AI Studio" },
  zhipu: { url: "https://open.bigmodel.cn/", label: "智谱开放平台" },
  deepseek: {
    url: "https://platform.deepseek.com/api_keys",
    label: "DeepSeek 开放平台",
  },
  moonshot: {
    url: "https://platform.moonshot.cn/console/api-keys",
    label: "月之暗面 Kimi",
  },
  grok: { url: "https://console.x.ai/", label: "xAI Console" },
  // custom 指向自建/本地端点（Ollama、vLLM、网关），没有统一申请地址
};

/** 中文为主：相邻片段直接拼接；拉丁字母之间补空格 */
/**
 * 音量电平的独立发布订阅。
 *
 * ⚠️ 不能放在 App 的 useState 里：电平每秒更新若干次，而 App 是整个界面的
 *    根组件，一次 setState 就要重渲染几百条转写 + 全部建议卡 + 提问输入框。
 *    真机验证中这表现为"会中打字很卡"。挪到外部 store 后，只有真正订阅它的
 *    音量条组件会重渲染，转写和输入框完全不受影响。
 */
const audioLevelStore = {
  value: 0.08,
  listeners: new Set<() => void>(),
  subscribe(listener: () => void) {
    audioLevelStore.listeners.add(listener);
    return () => {
      audioLevelStore.listeners.delete(listener);
    };
  },
  get() {
    return audioLevelStore.value;
  },
  set(next: number) {
    if (next === audioLevelStore.value) return;
    audioLevelStore.value = next;
    for (const listener of audioLevelStore.listeners) listener();
  },
};

function useAudioLevel() {
  return useSyncExternalStore(audioLevelStore.subscribe, audioLevelStore.get);
}

/**
 * 判定两段发言之间是否有值得断行的停顿。
 *
 * ⚠️ 只有 final 到达时刻 at，没有真实的起说时间，所以要先把后一段自己的
 *    说话时长扣掉，剩下的才近似是"停顿"。语速按每字约 200ms 估算 —— 粗糙，
 *    但阈值取到 3 秒以上时足够稳，不会把正常连说切碎。
 *    ASR 若日后能返回句首偏移（bg），把这里换成真实值即可。
 */
const PAUSE_BREAK_MS = 3000;
const MS_PER_CHAR = 200;

function hasPauseBetween(
  previous: { at: number },
  current: { at: number; text: string },
) {
  const spoken = current.text.length * MS_PER_CHAR;
  return current.at - previous.at - spoken >= PAUSE_BREAK_MS;
}

/**
 * 说话人配色。
 *
 * ⚠️ 必须按【解析后的】说话人 id 取色，而不是显示名：重命名之后颜色不能变，
 *    否则用户刚认出"蓝色是张工"，一改名颜色就跳，反而更难跟。
 *    「我」不参与轮转，固定用界面主色，保证一眼能分出敌我。
 */
const SPEAKER_COLORS = [
  "var(--speaker-1)",
  "var(--speaker-2)",
  "var(--speaker-3)",
  "var(--speaker-4)",
  "var(--speaker-5)",
  "var(--speaker-6)",
];

function speakerColor(key: string | null, isMine: boolean) {
  if (isMine) return "var(--brass)";
  if (!key) return "var(--graphite-3)";
  let hash = 0;
  for (let i = 0; i < key.length; i += 1) {
    hash = (hash * 31 + key.charCodeAt(i)) >>> 0;
  }
  return SPEAKER_COLORS[hash % SPEAKER_COLORS.length];
}

/** 会前设备检测的音量条。自订阅电平，不牵动父组件重渲染。 */
function DeviceLevelBars() {
  const level = useAudioLevel();
  return (
    <div className="level-bars" aria-label="麦克风音量示意">
      {Array.from({ length: 20 }).map((_, index) => (
        <i key={index} className={index / 20 < level ? "on" : ""} />
      ))}
    </div>
  );
}

/** 会中顶栏的电平指示。同上，隔离在自己的重渲染域里。 */
function LiveMeter() {
  const level = useAudioLevel();
  return (
    <div className="live-meter">
      {Array.from({ length: 12 }).map((_, index) => (
        <i
          key={index}
          className={index / 12 < level ? "on" : ""}
          style={{ height: `${7 + ((index * 7) % 13)}px` }}
        />
      ))}
    </div>
  );
}

/**
 * 会中提问输入框。
 *
 * ⚠️ 输入内容【自己管】，不上抛到 App：受控输入放在根组件时，每敲一个字
 *    都会重渲染整棵树（几百条转写 + 全部建议卡）。真机验证里"录制过程中
 *    输入提问很卡"就是这么来的。只在提交时把值交出去。
 */
const AskDock = memo(function AskDock({
  onAsk,
}: {
  onAsk: (value: string) => void;
}) {
  const [value, setValue] = useState("");
  function submit() {
    const trimmed = value.trim();
    if (!trimmed) return;
    setValue("");
    onAsk(trimmed);
  }
  return (
    <div className="ask-dock">
      <div className="ask-box">
        <input
          value={value}
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") submit();
          }}
          placeholder="随时提问，会结合会议上下文与知识库"
        />
        <button onClick={submit} aria-label="发送问题">
          <Send size={17} />
        </button>
      </div>
      <span>AI 建议仅供参考，请结合实际判断</span>
    </div>
  );
});

/**
 * 一条转写气泡。
 *
 * memo 化：转写每来一段就要重渲染整个列表，长会议里会有几百条。属性都是
 * 稳定引用（合并结果由 useMemo 产出），因此 memo 能挡掉绝大部分无谓渲染。
 */
const Utterance = memo(function Utterance({
  item,
  shown,
  colorKey,
  onOpenSpeakerMenu,
  onLocateSuggestion,
  canLocateSuggestion = true,
  isLocated = false,
  onRegister,
}: {
  item: DisplayUtterance;
  shown: string;
  colorKey: string;
  onOpenSpeakerMenu: (
    speakerId: string,
    itemId: string,
    segmentIds: string[],
    rect: DOMRect,
    scope: "segment" | "speaker",
  ) => void;
  onLocateSuggestion?: (item: DisplayUtterance) => void;
  canLocateSuggestion?: boolean;
  isLocated?: boolean;
  onRegister?: (node: HTMLElement | null) => void;
}) {
  const mine = shown === "我";
  const lines = useMemo(() => groupSegmentsByPause(item.segments), [item.segments]);
  return (
    <article
      className={`utterance ${mine ? "mine" : ""} ${item.isFinal ? "" : "interim"} ${
        isLocated ? "is-located" : ""
      }`}
      ref={onRegister}
      style={
        { "--speaker-color": speakerColor(colorKey, mine) } as React.CSSProperties
      }
    >
      <div className="utterance-meta">
        <button
          className="speaker-name actionable"
          onClick={(event) => {
            if (!item.speakerId) return;
            const rect = (event.target as HTMLElement).getBoundingClientRect();
            // 点名字 = 整人操作（重命名 / 标记我 / 全部改派）
            onOpenSpeakerMenu(
              item.speakerId,
              item.id,
              item.segmentIds,
              rect,
              "speaker",
            );
          }}
          title={item.speakerId ? "点击：调整这位说话人（全部发言）" : ""}
        >
          {shown}
          {mine && <UserRoundCheck size={13} />}
          {item.speakerId && <ChevronDown size={12} />}
        </button>
        <time>{formatTime(item.at)}</time>
        {onLocateSuggestion && (
          <button
            className="locate-suggestion-line-button"
            type="button"
            disabled={!canLocateSuggestion}
            title={
              canLocateSuggestion
                ? "定位相关话术"
                : "这段转写没有关联话术建议"
            }
            aria-label={canLocateSuggestion ? "定位相关话术" : "没有关联话术建议"}
            onClick={() => onLocateSuggestion(item)}
          >
            <MessageSquareText size={12} />
          </button>
        )}
      </div>
      {/*
        停顿分行：同一个人连着说的几段合成一条气泡，但中间停顿超过阈值的
        地方要断开。整坨压在一起时读的人分不出说话节奏（真机验证反馈）。

        每一行都可以单独点开改派 —— 声纹认错时错的往往只是其中一小段，
        只能整条气泡一起改的话，用户得先把对的那部分也改坏再改回来。
      */}
      <div className="utterance-body">
        {lines.map((line) => (
          <p
            className={`utterance-text ${item.speakerId ? "actionable-line" : ""}`}
            key={line.id}
            title={item.speakerId ? "点击：只改这一段的归属" : ""}
            onClick={(event) => {
              if (!item.speakerId) return;
              const rect = (
                event.currentTarget as HTMLElement
              ).getBoundingClientRect();
              onOpenSpeakerMenu(
                item.speakerId,
                line.id,
                line.ids,
                rect,
                "segment",
              );
            }}
          >
            {line.text}
          </p>
        ))}
      </div>
    </article>
  );
});

function formatTimecode(ms: number) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const parts = [Math.floor(total / 3600), Math.floor(total / 60) % 60, total % 60];
  return parts.map((n) => String(n).padStart(2, "0")).join(":");
}

/**
 * 会议已进行时长。
 *
 * ⚠️ 这里原先是原型阶段写死的 "00:04:12"，从未实现过 —— 用户报的"计时到
 *    00:04:12 就不动了"其实是它压根没在走。
 *    暂停录制【不停表】：用户要知道的是这场会开了多久，而不是录了多久；
 *    录制状态已经由左侧红点单独表达了。
 */
function Timecode({
  startedAt,
  endedAt,
}: {
  startedAt: number | null;
  endedAt?: number;
}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!startedAt || endedAt) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [startedAt, endedAt]);
  if (!startedAt) return <div className="timecode">--:--:--</div>;
  return (
    <div className="timecode">
      {formatTimecode((endedAt || now) - startedAt)}
    </div>
  );
}

function joinSpeech(a: string, b: string): string {
  const left = a.trimEnd();
  const right = b.trimStart();
  if (!left) return right;
  if (!right) return left;
  const needSpace =
    /[A-Za-z0-9]$/.test(left) && /^[A-Za-z0-9]/.test(right);
  return needSpace ? `${left} ${right}` : `${left}${right}`;
}

/**
 * 固定定位菜单：下方空间不够时改向上弹出。
 * 上翻时用 translateY(-100%) 把菜单底边贴在触发点上方，
 * 避免按「预估高度」反推 top 导致菜单飞到很远的地方。
 */
function placeFixedMenu(
  rect: DOMRect,
  opts: { width?: number; estimatedHeight?: number } = {},
): { x: number; y: number; openAbove: boolean } {
  const width = opts.width ?? 240;
  const estimatedHeight = opts.estimatedHeight ?? 220;
  const pad = 8;
  const x = Math.min(
    Math.max(pad, rect.left),
    Math.max(pad, window.innerWidth - width - pad),
  );
  const spaceBelow = window.innerHeight - rect.bottom - pad;
  const spaceAbove = rect.top - pad;
  const need = Math.min(estimatedHeight, window.innerHeight * 0.7);
  const openAbove = spaceBelow < Math.min(need, 180) && spaceAbove > spaceBelow;
  // 下翻：顶边贴在 rect 下方；上翻：底边贴在 rect 上方（配合 translateY(-100%)）
  const y = openAbove ? Math.max(pad, rect.top - 4) : rect.bottom + 4;
  return { x, y, openAbove };
}

function fixedMenuStyle(pos: {
  x: number;
  y: number;
  openAbove: boolean;
}): { left: number; top: number; transform?: string; maxHeight?: string } {
  // 上翻时底边贴触发点；同时把 max-height 压在触发点上方，避免顶出窗口
  const maxAbove = Math.max(120, pos.y - 8);
  return {
    left: pos.x,
    top: pos.y,
    transform: pos.openAbove ? "translateY(-100%)" : undefined,
    maxHeight: pos.openAbove ? `min(70vh, ${maxAbove}px)` : undefined,
  };
}

/** 气泡级改派时，优先落到已有「对方」，没有才允许新建。 */
function preferredOtherSpeakerId(
  speakerIds: string[],
  names: Record<string, string>,
  meIds: Set<string>,
  currentId: string | null,
): string | null {
  const candidates = speakerIds.filter(
    (id) => id && id !== currentId && !meIds.has(id),
  );
  if (candidates.includes("other")) return "other";
  const namedOther = candidates.find(
    (id) => names[id] === "对方" || names[id] === "对方（系统）",
  );
  if (namedOther) return namedOther;
  if (candidates.length === 1) return candidates[0];
  return candidates[0] || null;
}

/**
 * 连续同一说话人的 final 合并成一条气泡（中间结果仍单独挂在末尾）。
 * 原始段落仍按条保存，只影响展示与「这段改派」。
 */
function mergeConsecutiveTranscript(
  items: TranscriptItem[],
  speakerKey: (item: TranscriptItem) => string = (item) =>
    item.speakerId || `name:${item.speaker}`,
): DisplayUtterance[] {
  const result: DisplayUtterance[] = [];
  const asSegment = (item: TranscriptItem) => ({
    id: item.id,
    text: item.text,
    at: item.at,
    audioStartMs: item.audioStartMs,
    audioEndMs: item.audioEndMs,
  });
  for (const item of items) {
    if (!item.isFinal) {
      result.push({ ...item, segmentIds: [item.id], segments: [asSegment(item)] });
      continue;
    }
    const prev = result[result.length - 1];
    const curKey = speakerKey(item);
    const prevKey = prev ? speakerKey(prev) : null;
    if (prev?.isFinal && prevKey && prevKey === curKey) {
      prev.text = joinSpeech(prev.text, item.text);
      prev.segmentIds.push(item.id);
      prev.segments.push(asSegment(item));
      // 保留首句时间；说话人字段以最新为准
      prev.speaker = item.speaker;
      prev.speakerId = item.speakerId;
    } else {
      result.push({ ...item, segmentIds: [item.id], segments: [asSegment(item)] });
    }
  }
  return result;
}

/**
 * 把一条气泡的原始分段按停顿归成若干行。
 * 同一行内的分段仍然拼接（正常连说不该被 ASR 的碎切割开）。
 *
 * ⚠️ 只拼接【碎片】。会后分离会把一条过长的 final 按说话人边界切成几条，
 *    那些是完整的句子，长度上限就是为了让用户能针对一小段单独改派——
 *    再按停顿把它们粘回去，等于把刚切开的段落又合上，界面重新变成一大坨。
 *    所以够长的分段一律自成一行。
 */
const LINE_KEEP_ALONE_CHARS = 24;

function groupSegmentsByPause(
  segments: DisplayUtterance["segments"],
): Array<{
  id: string;
  ids: string[];
  text: string;
  at: number;
  audioStartMs?: number | null;
  audioEndMs?: number | null;
}> {
  const lines: Array<{
    id: string;
    ids: string[];
    text: string;
    at: number;
    audioStartMs?: number | null;
    audioEndMs?: number | null;
  }> = [];
  // ⚠️ 停顿要跟【紧邻的上一段】比，不能跟当前行的首段比：行内已经拼了几段时，
  //    首段时间早得多，会把正常连说误判成长停顿。
  let previous: { at: number; text: string } | null = null;
  for (const segment of segments) {
    const last = lines[lines.length - 1];
    const substantial =
      segment.text.length >= LINE_KEEP_ALONE_CHARS ||
      (previous?.text.length ?? 0) >= LINE_KEEP_ALONE_CHARS;
    if (last && previous && !substantial && !hasPauseBetween(previous, segment)) {
      last.text = joinSpeech(last.text, segment.text);
      last.ids.push(segment.id);
      if (segment.audioStartMs != null) {
        last.audioStartMs =
          last.audioStartMs == null
            ? segment.audioStartMs
            : Math.min(last.audioStartMs, segment.audioStartMs);
      }
      if (segment.audioEndMs != null) {
        last.audioEndMs =
          last.audioEndMs == null
            ? segment.audioEndMs
            : Math.max(last.audioEndMs, segment.audioEndMs);
      }
    } else {
      lines.push({ ...segment, ids: [segment.id] });
    }
    previous = segment;
  }
  return lines;
}

/**
 * 把展示层合并的一行正文回写到底层转写。
 *
 * 一行可能由数个连续 ASR 碎片拼成。纠错后将它们收敛为首个片段，时间范围
 * 扩到整行、结束时刻沿用最后一片；否则只能把新文字硬拆回多个片段，
 * 导出时会出现半截词和重复说话人。
 */
function replaceTranscriptLineText(
  items: TranscriptItem[],
  ids: string[],
  nextText: string,
): TranscriptItem[] {
  const idSet = new Set(ids);
  const selected = items.filter((item) => idSet.has(item.id));
  if (!selected.length) return items;
  const anchor = selected[0];
  const starts = selected
    .map((item) => item.audioStartMs)
    .filter((value): value is number => value != null && Number.isFinite(value));
  const ends = selected
    .map((item) => item.audioEndMs)
    .filter((value): value is number => value != null && Number.isFinite(value));
  const merged: TranscriptItem = {
    ...anchor,
    text: nextText,
    at: Math.max(...selected.map((item) => item.at)),
    audioStartMs: starts.length ? Math.min(...starts) : anchor.audioStartMs,
    audioEndMs: ends.length ? Math.max(...ends) : anchor.audioEndMs,
  };
  return items.flatMap((item) => {
    if (!idSet.has(item.id)) return [item];
    return item.id === anchor.id ? [merged] : [];
  });
}

const DEMO_AUDIO_SECONDS = 48;
const DEMO_AUDIO_WINDOWS = [
  [2, 8, 220],
  [10, 16, 277],
  [18, 26, 247],
  [27, 31, 247],
  [36, 42, 247],
] as const;

/** 网页演示用低音量测试音；只用于验证录音轴交互，不模拟真实语音。 */
function createDemoMeetingAudioUrl() {
  const sampleRate = 8000;
  const samples = sampleRate * DEMO_AUDIO_SECONDS;
  const buffer = new ArrayBuffer(44 + samples * 2);
  const view = new DataView(buffer);
  const writeAscii = (offset: number, value: string) => {
    for (let i = 0; i < value.length; i += 1) {
      view.setUint8(offset + i, value.charCodeAt(i));
    }
  };
  writeAscii(0, "RIFF");
  view.setUint32(4, 36 + samples * 2, true);
  writeAscii(8, "WAVE");
  writeAscii(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeAscii(36, "data");
  view.setUint32(40, samples * 2, true);
  for (let index = 0; index < samples; index += 1) {
    const second = index / sampleRate;
    const window = DEMO_AUDIO_WINDOWS.find(
      ([start, end]) => second >= start && second <= end,
    );
    const sample = window
      ? Math.sin(second * Math.PI * 2 * window[2]) * 0.035
      : 0;
    view.setInt16(44 + index * 2, Math.round(sample * 32767), true);
  }
  return URL.createObjectURL(new Blob([buffer], { type: "audio/wav" }));
}

const navItems: Array<{
  screen: Screen;
  label: string;
  icon: typeof Home;
}> = [
  { screen: "home", label: "首页", icon: Home },
  { screen: "prepare", label: "新建会议", icon: Plus },
  { screen: "history", label: "会议历史", icon: History },
  { screen: "projects", label: "项目", icon: FolderKanban },
  { screen: "knowledge", label: "知识库", icon: Library },
  { screen: "glossary", label: "专有名词", icon: Tags },
];

function formatTime(at: number) {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(at);
}

/**
 * 建议的依据分级。
 *
 * ⚠️ 缺省值必须是最保守的 clarify，不能是 grounded。真机验证踩过这个坑：
 *    桥接层字段名对不上导致 level 恒为 undefined，兜底又默认"有依据"，
 *    于是一场没有关联任何文档的会议里，361 条建议全部显示"有依据"。
 *    分级缺失时宁可少说，也不能把无依据伪装成有依据。
 */
function levelOf(suggestion: Suggestion) {
  if (suggestion.level) return suggestion.level;
  if (suggestion.grounded === true) return "grounded";
  return "clarify";
}

function formatRecordDate(at: number) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "long",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(at);
}

function formatDuration(record: MeetingRecord) {
  const end = record.endedAt || Date.now();
  const minutes = Math.max(1, Math.round((end - record.startedAt) / 60_000));
  if (minutes < 60) return `${minutes} 分钟`;
  return `${Math.floor(minutes / 60)} 小时 ${String(minutes % 60).padStart(2, "0")} 分`;
}

function meetingStats(record: MeetingRecord) {
  const speakers = new Set(
    record.transcript
      .filter((item) => item.isFinal)
      .map((item) => item.speakerId || `name:${item.speaker}`),
  ).size;
  const suggestions = record.batches.reduce(
    (total, batch) => total + batch.suggestions.length,
    0,
  );
  return {
    speakers,
    suggestions,
    transcript: record.transcript.filter((item) => item.isFinal).length,
  };
}

function formatSpeakerDuration(ms: number) {
  const totalSeconds = Math.max(0, Math.round(ms / 1000));
  if (totalSeconds < 60) return `${totalSeconds} 秒`;
  return `${Math.floor(totalSeconds / 60)} 分 ${String(totalSeconds % 60).padStart(2, "0")} 秒`;
}

function SpeakerDistributionPopover({
  count,
  distribution,
}: {
  count: number;
  distribution: ReturnType<typeof buildSpeakerDistribution>;
}) {
  const [open, setOpen] = useState(false);
  const closeOnBlur = (event: React.FocusEvent<HTMLDivElement>) => {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
      setOpen(false);
    }
  };
  return (
    <div
      className="speaker-summary-anchor"
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)}
      onBlur={closeOnBlur}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          setOpen(false);
        }
      }}
    >
      <button
        type="button"
        className="speaker-summary-trigger"
        aria-label={`${count} 位说话人，查看发言分布`}
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <strong>{count}</strong>
        <span>位说话人</span>
      </button>
      {open && (
        <div className="speaker-distribution-popover" role="dialog" aria-label="说话人分布">
          <header>
            <div>
              <strong>说话人分布</strong>
              <small>按有效发言时长计算</small>
            </div>
            <span>{formatSpeakerDuration(distribution.totalMs)}</span>
          </header>
          {distribution.rows.length === 0 ? (
            <div className="speaker-distribution-empty">
              <span>没有可用的发言时间数据</span>
              <small>新记录会在录音轴完整后显示分布。</small>
            </div>
          ) : (
            <div className="speaker-distribution-list">
              {distribution.rows.map((row) => {
                const color = speakerColor(row.key, row.name === "我");
                const duration = Math.max(distribution.recordingDurationMs, 1);
                return (
                  <div className="speaker-distribution-row" key={row.key}>
                    <div className="speaker-distribution-meta">
                      <span
                        className="speaker-distribution-swatch"
                        style={{ background: color }}
                      />
                      <strong title={row.name}>{row.name}</strong>
                      <span>{formatSpeakerDuration(row.durationMs)}</span>
                      <em>{row.percentage.toFixed(1)}%</em>
                    </div>
                    <div
                      className="speaker-distribution-timeline"
                      aria-label={`${row.name} 在整场录音中的发言时间轴`}
                    >
                      {row.segments.map((segment, index) => (
                        <i
                          key={`${row.key}-${segment.startMs}-${index}`}
                          style={{
                            left: `${Math.max(0, (segment.startMs / duration) * 100)}%`,
                            width: `${Math.max(
                              0.8,
                              ((segment.endMs - segment.startMs) / duration) * 100,
                            )}%`,
                            background: color,
                          }}
                        />
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
          {distribution.approximate && (
            <p className="speaker-distribution-note">部分时间为估算</p>
          )}
        </div>
      )}
    </div>
  );
}

function FloatingStrategyWindow() {
  const preferenceKey = "meeting-copilot-floating-preferences";
  const tintOptions = [
    { value: "#f4f5f3", label: "纸白" },
    { value: "#faf1dd", label: "暖黄" },
    { value: "#e9eef0", label: "雾蓝" },
    { value: "#182127", label: "深墨" },
  ];
  const [preferences, setPreferences] = useState<{
    opacity: number;
    tint: string;
    fontSize: "small" | "medium" | "large";
    alwaysOnTop: boolean;
    contentProtection: boolean;
    collapsed: boolean;
  }>(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(preferenceKey) || "null");
      return {
        opacity: Math.min(100, Math.max(45, Number(saved?.opacity) || 88)),
        tint: String(saved?.tint || "#f4f5f3"),
        fontSize:
          saved?.fontSize === "small" || saved?.fontSize === "large"
            ? saved.fontSize
            : "medium",
        alwaysOnTop: saved?.alwaysOnTop !== false,
        contentProtection: saved?.contentProtection !== false,
        collapsed: Boolean(saved?.collapsed),
      };
    } catch {
      return {
        opacity: 88,
        tint: "#f4f5f3",
        fontSize: "medium",
        alwaysOnTop: true,
        contentProtection: true,
        collapsed: false,
      };
    }
  });

  const [batches, setBatches] = useState<SuggestionBatch[]>([]);
  const [selectedBatchIndex, setSelectedBatchIndex] = useState(0);
  const [generating, setGenerating] = useState(false);
  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const [appearanceOpen, setAppearanceOpen] = useState(false);
  const [isHovered, setIsHovered] = useState(false);
  const [hasNewPending, setHasNewPending] = useState(false);
  const [recordingPaused, setRecordingPaused] = useState(false);

  const hoverRef = useRef(false);
  hoverRef.current = isHovered;

  useEffect(() => {
    document.documentElement.dataset.window = "floating";
    return () => {
      delete document.documentElement.dataset.window;
    };
  }, []);

  useEffect(() => {
    localStorage.setItem(preferenceKey, JSON.stringify(preferences));
    void window.meetingCopilot?.setFloatingStrategyPreferences({
      alwaysOnTop: preferences.alwaysOnTop,
      contentProtection: preferences.contentProtection,
      collapsed: preferences.collapsed,
      fontSize: preferences.fontSize,
      tint: preferences.tint,
      opacity: preferences.opacity,
    });
  }, [preferences]);

  useEffect(() => {
    const bridge = window.meetingCopilot;
    if (!bridge) return;
    return bridge.onMeetingEvent((event) => {
      if (event.type === "strategy_reset") {
        setBatches([]);
        setSelectedBatchIndex(0);
        setGenerating(false);
        setHasNewPending(false);
      }
      if (event.type === "controls") {
        setRecordingPaused(Boolean(event.recordingPaused));
      }
      if (event.type === "status") {
        if (event.stage === "ended" || event.stage === "cancelled") {
          void window.meetingCopilot?.closeFloatingStrategy();
        }
      }
      if (event.type === "ended") {
        void window.meetingCopilot?.closeFloatingStrategy();
      }
      if (event.type === "suggestion_status") {
        setGenerating(event.status === "generating");
      }
      if (event.type === "suggestions") {
        setGenerating(false);
        const incoming = (event.suggestions || []) as Suggestion[];
        const newBatch: SuggestionBatch = {
          id: `batch-${Date.now()}`,
          suggestions: incoming,
          hits: (event.hits || []) as ReferenceHit[],
          elapsed: Number(event.elapsed || 0),
          at:
            Number(event.at || Date.now()) *
            (Number(event.at) < 10_000_000_000 ? 1000 : 1),
        };

        setBatches((current) => [newBatch, ...current].slice(0, 10));

        // 鼠标指向时防冲刷暂锁：若鼠标正停在悬浮窗内，暂不自动跳到最新批，避免正在阅读被顶替
        if (hoverRef.current) {
          setHasNewPending(true);
        } else {
          setSelectedBatchIndex(0);
          setHasNewPending(false);
        }
      }
    });
  }, []);

  const handleMouseEnter = useCallback(() => {
    setIsHovered(true);
  }, []);

  const handleMouseLeave = useCallback(() => {
    setIsHovered(false);
    // 鼠标移出后自动解冻，若有新策略则刷新至最新
    setHasNewPending((pending) => {
      if (pending) {
        setSelectedBatchIndex(0);
      }
      return false;
    });
  }, []);

  const handleSuggestNow = useCallback(async () => {
    if (generating || !window.meetingCopilot?.suggestNow) return;
    setGenerating(true);
    try {
      await window.meetingCopilot.suggestNow();
    } catch {
      setGenerating(false);
    }
  }, [generating]);

  const handleTogglePause = useCallback(async () => {
    if (!window.meetingCopilot?.setMeetingControls) return;
    const next = !recordingPaused;
    setRecordingPaused(next);
    await window.meetingCopilot.setMeetingControls({ recordingPaused: next });
  }, [recordingPaused]);

  const handleStopMeeting = useCallback(async () => {
    if (!window.meetingCopilot?.stopMeeting) return;
    await window.meetingCopilot.stopMeeting();
    void window.meetingCopilot.closeFloatingStrategy();
  }, []);

  const copyScript = useCallback((script: string, key: string) => {
    void navigator.clipboard.writeText(script);
    setCopiedKey(key);
    window.setTimeout(() => setCopiedKey(null), 1400);
  }, []);

  const toggleCollapsed = useCallback(() => {
    setPreferences((current) => ({
      ...current,
      collapsed: !current.collapsed,
    }));
  }, []);

  const activeBatch = batches[selectedBatchIndex] || batches[0];
  const activeSuggestions = activeBatch ? activeBatch.suggestions : [];

  const fontSizeScale =
    preferences.fontSize === "small"
      ? 0.88
      : preferences.fontSize === "large"
        ? 1.15
        : 1;

  return (
    <div
      className={`floating-strategy-window ${
        preferences.tint === "#182127" ? "dark-tint" : ""
      } ${preferences.collapsed ? "is-collapsed" : ""} font-${preferences.fontSize}`}
      style={
        {
          "--floating-tint": preferences.tint,
          "--floating-opacity": `${preferences.opacity}%`,
          "--floating-font-scale": fontSizeScale,
        } as React.CSSProperties
      }
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
    >
      <header className="floating-strategy-header">
        <div className="floating-header-left">
          <span className="channel-dot brass" />
          <strong className="floating-header-title">应答策略</strong>
          {generating && (
            <span className="floating-generating-badge" title="正在生成建议…">
              <Loader2 size={12} className="spin" />
              {!preferences.collapsed && <span>生成中</span>}
            </span>
          )}
          {recordingPaused && (
            <span className="floating-paused-badge" title="会议录制已暂停">
              <Pause size={10} /> 已暂停
            </span>
          )}
          {!preferences.collapsed && isHovered && (
            <span className="floating-hover-freeze-tag" title="鼠标停留中：已暂锁当前话术，移开后自动解冻">
              <Lock size={10} /> 悬停锁存
            </span>
          )}
          {!preferences.collapsed && batches.length > 1 && (
            <div className="floating-batch-nav">
              <button
                type="button"
                className="floating-nav-btn"
                disabled={selectedBatchIndex >= batches.length - 1}
                title="查看上一轮建议"
                onClick={() =>
                  setSelectedBatchIndex((i) =>
                    Math.min(batches.length - 1, i + 1),
                  )
                }
              >
                <ChevronLeft size={12} />
              </button>
              <span className="floating-batch-indicator">
                {selectedBatchIndex === 0
                  ? "最新"
                  : `前${selectedBatchIndex}轮`}
              </span>
              <button
                type="button"
                className="floating-nav-btn"
                disabled={selectedBatchIndex <= 0}
                title="查看更新的建议"
                onClick={() =>
                  setSelectedBatchIndex((i) => Math.max(0, i - 1))
                }
              >
                <ChevronRight size={12} />
              </button>
            </div>
          )}
          {preferences.collapsed && activeSuggestions.length > 0 && (
            <span className="floating-collapsed-summary">
              {activeSuggestions[0]?.intent || activeSuggestions[0]?.script}
            </span>
          )}
        </div>

        <div className="floating-window-controls">
          <button
            className={`icon-button ${generating ? "is-busy" : ""}`}
            aria-label="立即生成最新建议"
            title={generating ? "正在生成中…" : "立即生成建议 (主动触发)"}
            disabled={generating}
            onClick={handleSuggestNow}
          >
            {generating ? (
              <Loader2 size={13} className="spin" />
            ) : (
              <Wand2 size={13} />
            )}
          </button>
          <button
            className={`icon-button ${recordingPaused ? "active" : ""}`}
            aria-label={recordingPaused ? "恢复会议录制" : "暂停会议录制"}
            title={recordingPaused ? "当前已暂停录制，点击恢复" : "暂停会议录制"}
            onClick={handleTogglePause}
          >
            {recordingPaused ? <Play size={13} /> : <Pause size={13} />}
          </button>
          <button
            className="icon-button floating-stop-btn"
            aria-label="结束会议"
            title="结束当前会议"
            onClick={handleStopMeeting}
          >
            <CircleStop size={13} />
          </button>
          <button
            className="icon-button"
            aria-label={preferences.collapsed ? "展开悬浮窗" : "收起为胶囊态"}
            title={preferences.collapsed ? "展开悬浮窗" : "收起为胶囊态"}
            onClick={toggleCollapsed}
          >
            {preferences.collapsed ? (
              <Maximize2 size={13} />
            ) : (
              <Minimize2 size={13} />
            )}
          </button>
          <button
            className={`icon-button ${preferences.alwaysOnTop ? "active" : ""}`}
            aria-label={preferences.alwaysOnTop ? "取消置顶" : "保持置顶"}
            title={preferences.alwaysOnTop ? "当前置顶，点击取消" : "点击保持置顶"}
            onClick={() =>
              setPreferences((current) => ({
                ...current,
                alwaysOnTop: !current.alwaysOnTop,
              }))
            }
          >
            {preferences.alwaysOnTop ? <Pin size={13} /> : <PinOff size={13} />}
          </button>
          <button
            className={`icon-button ${appearanceOpen ? "active" : ""}`}
            aria-label="悬浮窗外观"
            title="背景与透明度"
            onClick={() => setAppearanceOpen((value) => !value)}
          >
            <SlidersHorizontal size={13} />
          </button>
          <button
            className="icon-button floating-close"
            aria-label="关闭悬浮窗"
            onClick={() => void window.meetingCopilot?.closeFloatingStrategy()}
          >
            <X size={14} />
          </button>
        </div>
      </header>

      {appearanceOpen && !preferences.collapsed && (
        <div className="floating-appearance-panel">
          <div className="floating-appearance-row">
            <span>背景</span>
            <div className="floating-tints">
              {tintOptions.map((option) => (
                <button
                  key={option.value}
                  className={preferences.tint === option.value ? "active" : ""}
                  style={{ background: option.value }}
                  title={option.label}
                  aria-label={option.label}
                  onClick={() =>
                    setPreferences((current) => ({
                      ...current,
                      tint: option.value,
                    }))
                  }
                />
              ))}
            </div>
          </div>
          <div className="floating-appearance-row">
            <span>字号</span>
            <div className="floating-font-buttons">
              {(["small", "medium", "large"] as const).map((size) => (
                <button
                  key={size}
                  type="button"
                  className={`floating-font-btn ${
                    preferences.fontSize === size ? "active" : ""
                  }`}
                  onClick={() =>
                    setPreferences((current) => ({
                      ...current,
                      fontSize: size,
                    }))
                  }
                >
                  {size === "small" ? "紧凑" : size === "large" ? "大字" : "标准"}
                </button>
              ))}
            </div>
          </div>
          <label className="floating-opacity-control">
            <span>透明度</span>
            <input
              type="range"
              min={45}
              max={100}
              step={1}
              value={preferences.opacity}
              onChange={(event) =>
                setPreferences((current) => ({
                  ...current,
                  opacity: Number(event.target.value),
                }))
              }
            />
            <em>{preferences.opacity}%</em>
          </label>
          <div className="floating-appearance-row floating-protection-row">
            <span>防投屏</span>
            <label
              className="floating-toggle-label"
              title="开启后，钉钉、腾讯会议等投屏/录屏时将自动隐藏悬浮窗（仅本机可见）"
            >
              <input
                type="checkbox"
                checked={preferences.contentProtection}
                onChange={(event) =>
                  setPreferences((current) => ({
                    ...current,
                    contentProtection: event.target.checked,
                  }))
                }
              />
              <span className="floating-toggle-text">
                {preferences.contentProtection
                  ? "隐形模式（投屏不显示）"
                  : "常规模式（投屏可见）"}
              </span>
            </label>
          </div>
        </div>
      )}

      {hasNewPending && !preferences.collapsed && (
        <div
          className="floating-new-badge-bar"
          onClick={() => {
            setSelectedBatchIndex(0);
            setHasNewPending(false);
          }}
          title="点击立即切换至最新建议"
        >
          <Sparkles size={12} />
          <span>新一轮策略已就绪，点击或移开鼠标自动更新</span>
        </div>
      )}

      {!preferences.collapsed && (
        <main className="floating-strategy-body">
          {activeSuggestions.length === 0 ? (
            generating ? (
              <div className="floating-skeleton-container">
                <div className="floating-skeleton-card">
                  <div className="skeleton-line short shimmer" />
                  <div className="skeleton-line long shimmer" />
                  <div className="skeleton-line medium shimmer" />
                </div>
                <div className="floating-skeleton-card">
                  <div className="skeleton-line short shimmer" />
                  <div className="skeleton-line long shimmer" />
                </div>
                <div className="floating-generating-hint">
                  <Loader2 size={13} className="spin" />
                  <span>正在结合对话上下文与知识库生成策略…</span>
                </div>
              </div>
            ) : (
              <div className="floating-empty">
                <Sparkles size={18} />
                <strong>等待下一轮建议</strong>
                <span>停顿约 2 秒后会自动给出建议，亦可点击右上角魔法棒立即生成。</span>
              </div>
            )
          ) : (
            activeSuggestions.map((suggestion, index) => {
              const level = levelOf(suggestion);
              const cardKey = `${activeBatch?.id || "cur"}-${suggestion.intent}-${index}`;
              const isCopied = copiedKey === cardKey;
              return (
                <article
                  className={`floating-strategy-card ${index === 0 ? "primary" : ""} ${level}`}
                  key={cardKey}
                >
                  <div className="floating-card-head">
                    <div className="floating-card-title-group">
                      <span className="floating-order-tag">
                        {index === 0 ? "优先回应" : `备选 ${index + 1}`}
                      </span>
                      {suggestion.category && (
                        <span className="floating-category-tag">
                          {suggestion.category}
                        </span>
                      )}
                      <span className={`floating-evidence-tag ${level}`}>
                        {level === "grounded" ? (
                          <>
                            <Check size={11} /> 有依据
                          </>
                        ) : level === "advisory" ? (
                          <>
                            <Sparkles size={11} /> 经验建议
                          </>
                        ) : (
                          <>
                            <ShieldAlert size={11} /> 仅澄清
                          </>
                        )}
                      </span>
                      <strong className="floating-intent-title">
                        {suggestion.intent}
                      </strong>
                    </div>

                    <button
                      className={`floating-copy-action-btn ${isCopied ? "copied" : ""}`}
                      aria-label="复制话术"
                      title="点击复制完整发言话术"
                      onClick={() => copyScript(suggestion.script, cardKey)}
                    >
                      {isCopied ? (
                        <>
                          <Check size={12} /> <span>已复制</span>
                        </>
                      ) : (
                        <>
                          <Copy size={12} /> <span>复制</span>
                        </>
                      )}
                    </button>
                  </div>

                  <p className="floating-script-text">{suggestion.script}</p>

                  {suggestion.sensitive && (
                    <div className="floating-card-alert sensitive">
                      <ShieldAlert size={12} />
                      <span>{suggestion.sensitive}</span>
                    </div>
                  )}

                  {suggestion.notice && (
                    <div className="floating-card-alert notice">
                      <ShieldAlert size={12} />
                      <span>{suggestion.notice}</span>
                    </div>
                  )}
                </article>
              );
            })
          )}
        </main>
      )}
    </div>
  );
}

function formatMeetingStartupError(event: MeetingEvent) {
  const detail = String(event.message || "").trim();
  const stage = String(event.stage || "");
  const descriptions: Record<string, { title: string; action: string }> = {
    microphone: {
      title: "麦克风启动失败",
      action: "请确认设备仍已连接、未被其它程序独占，并在会前页重新测试麦克风。",
    },
    system_audio: {
      title: "系统声音捕获失败",
      action:
        "请确认会议软件使用 Windows 当前默认播放设备；切换耳机或扬声器后需重新开始会议。",
    },
    asr_credentials: {
      title: "语音转写服务启动失败",
      action: "请到设置 → 语音转写检查供应商、凭证和服务开通状态。",
    },
    asr_service: {
      title: "语音转写服务连接失败",
      action: "请检查网络和供应商服务状态；录音尚未开始，可修正后重新进入会议。",
    },
    llm_credentials: {
      title: "建议模型启动失败",
      action: "请到设置 → 话术建议检查供应商、模型名和对应凭证。",
    },
    llm_service: {
      title: "建议模型初始化失败",
      action: "请检查模型配置与本地兼容端点是否可访问。",
    },
    model_load: {
      title: "本地模型或知识库加载失败",
      action: "请检查本地模型文件与 Python 依赖；若只影响声纹，可关闭声纹后继续。",
    },
    voiceprint: {
      title: "声纹识别暂不可用",
      action: "本场将回退到普通说话人逻辑；会后仍可人工改名和改派。",
    },
    recording_file: {
      title: "录音文件写入失败",
      action: "转写与建议可继续，但本场可能没有可回放的录音，请检查数据目录。",
    },
    asr_stop: {
      title: "转写服务收尾异常",
      action: "会议记录仍会继续保存；请在历史详情确认音频与最后几段转写。",
    },
    audio: {
      title: "音频通道启动失败",
      action: "请重新测试麦克风；线上会议还需确认 Windows 默认播放设备可用。",
    },
    bridge: {
      title: "本地会议服务启动失败",
      action: "请先运行 M4 一键回归；若仍失败，保留启动窗口中的最后一条错误信息。",
    },
  };
  const description = descriptions[stage] || {
    title: "会议启动失败",
    action: "请根据错误信息检查设置后重试。",
  };
  return `${description.title}${detail ? `：${detail}` : ""} ${description.action}`;
}

export function App() {
  const mode = new URLSearchParams(window.location.search).get("mode");
  return mode === "floating" ? <FloatingStrategyWindow /> : <MainApp />;
}

function MainApp() {
  const [screen, setScreen] = useState<Screen>("home");
  const [railExpanded, setRailExpanded] = useState(true);
  const [persisted, setPersisted] = useState(initialState);
  const [runtime, setRuntime] = useState({
    desktop: false,
    pythonReady: false,
    bridgeReady: false,
    configPresent: false,
  });
  const [devices, setDevices] = useState<InputDevice[]>([]);
  const [deviceError, setDeviceError] = useState("");
  const [meetingStatus, setMeetingStatus] = useState<
    "idle" | "starting" | "live" | "stopping" | "ended" | "error"
  >("idle");
  const [statusMessage, setStatusMessage] = useState("尚未开始");
  /** 会前预热（热词同步、依赖导入等） */
  const [warmupLabel, setWarmupLabel] = useState("尚未预热");
  const [recordingCue, setRecordingCue] = useState(false);
  const beginRecordingSent = useRef(false);
  const warmupRequestIdRef = useRef(0);
  const [transcript, setTranscript] = useState<TranscriptItem[]>([]);
  const [batches, setBatches] = useState<SuggestionBatch[]>([]);
  const [memoryItems, setMemoryItems] = useState<MeetingMemoryItem[]>([]);
  const [glossaryCandidates, setGlossaryCandidates] = useState<GlossaryCandidate[]>([]);
  const [sceneRecommendation, setSceneRecommendation] = useState<SceneRecommendation | null>(null);
  const [sceneSelectionTouched, setSceneSelectionTouched] = useState(false);
  const [deviceTestStatus, setDeviceTestStatus] = useState<
    "idle" | "testing" | "success" | "error"
  >("idle");
  const [recordingPaused, setRecordingPaused] = useState(false);
  const [suggestionsPaused, setSuggestionsPaused] = useState(false);
  /**
   * 正在生成建议。
   *
   * ⚠️ 桥接层一直在发 `suggestion_status`，渲染层却从来没接过 ——
   *    表现就是点了「现在给建议」什么反应都没有，用户不知道点没点上，
   *    只能干等 6-8 秒（真机反馈）。这个状态就是为了把那段空白填上。
   */
  const [suggesting, setSuggesting] = useState(false);
  const [hotwordsStatus, setHotwordsStatus] = useState<
    NonNullable<MeetingRecord["hotwords"]> | null
  >(null);
  const [lastMeetingError, setLastMeetingError] = useState<
    MeetingRecord["lastError"]
  >(undefined);
  const [records, setRecords] = useState<MeetingRecord[]>([]);
  const [activeMeetingId, setActiveMeetingId] = useState<string | null>(null);
  const activeMeetingIdRef = useRef<string | null>(null);
  const [meetingStartedAt, setMeetingStartedAt] = useState<number | null>(null);
  const [meetingEndedAt, setMeetingEndedAt] = useState<number | undefined>();
  const [selectedRecordId, setSelectedRecordId] = useState<string | null>(null);
  const [toast, setToast] = useState("");
  // ── 知识范围（项目 / 文档 / 本场选择）──
  const [projects, setProjects] = useState<Project[]>([]);
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [activeProjectId, setActiveProjectId] = useState<string | null>(null);
  // 本场会议勾选的文档 ID；只有这些会进入检索
  const [selectedDocIds, setSelectedDocIds] = useState<string[]>([]);
  // 说话人档案：id → 显示名 / 是否是我 / 是否已合并到别人
  const [speakers, setSpeakers] = useState<Record<string, SpeakerProfile>>({});
  // scope=segment：只改点中的气泡/行；scope=speaker：改这个说话人的全部发言
  const [speakerMenu, setSpeakerMenu] = useState<{
    scope: "segment" | "speaker";
    speakerId: string;
    itemId: string;
    segmentIds: string[];
    x: number;
    y: number;
    openAbove: boolean;
  } | null>(null);
  const [renaming, setRenaming] = useState<{ id: string; value: string } | null>(
    null,
  );
  // 本场录音落盘结果，会议结束时由桥接层回传
  const [recordedAudio, setRecordedAudio] = useState<{
    path: string;
    seconds: number;
    tracks?: Partial<
      Record<
        "mixed" | "mic" | "system",
        { path?: string; seconds?: number | null; ok?: boolean; error?: string }
      >
    >;
  } | null>(null);
  // 转写连接异常状态；为 null 表示正常
  const [asrConnection, setAsrConnection] = useState<{
    state: string;
    message: string;
  } | null>(null);
  const asrChannelErrors = useRef<
    Record<string, { state: string; message: string }>
  >({});
  // fatal error 后桥接进程通常紧接着退出。bridge_closed 不能再把明确错误
  // 覆盖成“会议已正常结束”，否则用户会失去唯一可操作的失败原因。
  const meetingFatalError = useRef(false);
  const idCounter = useRef(0);
  // 正在流式接收的回答卡片 id；null 表示当前没有进行中的流式回答
  const answerStreamId = useRef<string | null>(null);

  useEffect(() => {
    activeMeetingIdRef.current = activeMeetingId;
  }, [activeMeetingId]);

  /** 顺着合并链找到最终归属的说话人（合并可能是链式的） */
  function resolveSpeakerId(
    id: string | null | undefined,
    table = speakers,
  ): string | null {
    if (!id) return null;
    let current = id;
    const seen = new Set<string>();
    while (table[current]?.mergedInto && !seen.has(current)) {
      seen.add(current);
      current = table[current].mergedInto!;
    }
    return current;
  }

  /** 某条发言最终显示的说话人名（重命名/合并/标记我都在此收敛） */
  function displaySpeaker(item: TranscriptItem): string {
    const resolved = resolveSpeakerId(item.speakerId);
    if (!resolved) return item.speaker;
    const profile = speakers[resolved];
    if (!profile) return item.speaker;
    return profile.isMe ? "我" : profile.name;
  }

  const meSpeakerId = useMemo(
    () => Object.values(speakers).find((s) => s.isMe)?.id ?? null,
    [speakers],
  );

  /** 可作为合并/改派目标的说话人：排除自己和已被合并掉的 */
  function otherSpeakers(excludeId: string) {
    return Object.values(speakers).filter(
      (profile) => profile.id !== excludeId && !profile.mergedInto,
    );
  }

  /** 从一批发言中批量登记说话人（恢复历史会议、演示模式用） */
  function registerSpeakersFrom(items: TranscriptItem[], meId?: string | null) {
    setSpeakers((current) => {
      const next = { ...current };
      for (const item of items) {
        if (!item.speakerId || next[item.speakerId]) continue;
        next[item.speakerId] = {
          id: item.speakerId,
          name:
            item.speakerId === "me"
              ? "我"
              : item.speakerId === "other"
                ? "对方"
                : item.speaker && item.speaker !== "我"
                  ? item.speaker
                  : `说话人${item.speakerId}`,
          isMe:
            item.speakerId === "me" ||
            (meId ? item.speakerId === meId : item.speaker === "我"),
          mergedInto: null,
        };
      }
      return next;
    });
  }

  /** 收到新的角色编号时登记；已存在则保持用户改过的名字 */
  function ensureSpeaker(speakerId: string | null | undefined) {
    if (!speakerId) return;
    const defaultName =
      speakerId === "me"
        ? "我"
        : speakerId === "other"
          ? "对方"
          : `说话人${speakerId}`;
    setSpeakers((current) =>
      current[speakerId]
        ? current
        : {
            ...current,
            [speakerId]: {
              id: speakerId,
              name: defaultName,
              isMe: speakerId === "me",
              mergedInto: null,
            },
          },
    );
  }

  function renameSpeaker(id: string, name: string) {
    const trimmed = name.trim();
    if (!trimmed) return;
    setSpeakers((current) => ({
      ...current,
      [id]: { ...current[id], name: trimmed },
    }));
    notify(`已重命名为「${trimmed}」，该说话人的全部发言同步更新`);
  }

  /** 标记为"我"。全场唯一，并同步告知桥接层以切换建议立场 */
  function markAsMe(id: string) {
    setSpeakers((current) => {
      const next: Record<string, SpeakerProfile> = {};
      for (const [key, profile] of Object.entries(current)) {
        next[key] = { ...profile, isMe: key === id };
      }
      return next;
    });
    void window.meetingCopilot?.setMeSpeaker(id).catch(() => undefined);
    notify("已标记为「我」，我说话时不再触发建议");
  }

  /**
   * 把 sourceId 的全部发言硬改派到 targetId。
   *
   * ⚠️ 以前只写 mergedInto 软链接：会中显示对了，但落库/重载后合并链丢失，
   *    历史「实时转写」归属会和会中完全对不上。这里直接改 speakerId。
   */
  function mergeSpeaker(sourceId: string, targetId: string) {
    if (sourceId === targetId) return;
    const source = speakers[sourceId];
    const target = speakers[targetId];
    // 全量改派的目标身份优先：把「我」改派给「对方」时，不能把目标
    // 反向升级成「我」。同时把历史脏数据里的 other.isMe=true 清掉。
    const targetIsMe = targetId !== "other" && Boolean(target?.isMe);
    const sourceWasMe = Boolean(source?.isMe);
    const targetName = targetIsMe ? "我" : target?.name || targetId;
    setTranscript((current) =>
      current.map((item) =>
        item.speakerId === sourceId
          ? { ...item, speakerId: targetId, speaker: targetName }
          : item,
      ),
    );
    setSpeakers((current) => {
      const next: Record<string, SpeakerProfile> = { ...current };
      if (target) {
        for (const [key, profile] of Object.entries(next)) {
          next[key] = {
            ...profile,
            isMe: key === targetId
              ? targetIsMe
              : profile.isMe && key !== sourceId,
          };
        }
      }
      delete next[sourceId];
      return next;
    });
    if (sourceWasMe && !targetIsMe) {
      // 当前「我」被明确改派到对方后，不要把对方继续当成我；
      // 后续可在正确的说话人菜单上重新标记「我」。
      void window.meetingCopilot?.setMeSpeaker(null).catch(() => undefined);
    } else if (targetIsMe) {
      void window.meetingCopilot?.setMeSpeaker(targetId).catch(() => undefined);
    }
    notify(
      `已将「${sourceWasMe ? "我" : source?.name || sourceId}」的全部发言归到「${targetName}」`,
    );
  }

  /** 采纳某批里的某条建议（每批单选，再点取消）。触发既有自动保存落库。 */
  function adoptSuggestion(batchId: string, position: number) {
    setBatches((current) =>
      current.map((batch) =>
        batch.id !== batchId
          ? batch
          : {
              ...batch,
              suggestions: batch.suggestions.map((s, i) => ({
                ...s,
                // 目标条 toggle；同批其它条一律清除（每批单选）
                adopted: i === position ? !s.adopted : false,
              })),
            },
      ),
    );
  }

  /**
   * 把选中的发言拆出来，归给一个新建的说话人。
   *
   * ⚠️ 这是「整人合并」的反向操作。仅用于确实出现第三人时；
   *    两人会里把误标成「我」的段落挪走，应优先改派给已有「对方」。
   *
   * 新说话人用 local- 前缀，避免与 ASR 返回的数字编号撞号。
   */
  function splitToNewSpeaker(itemIds: string[], label?: string) {
    if (!itemIds.length) return;
    const newId = `local-${Date.now().toString(36)}`;
    const ordinal =
      Object.values(speakers).filter((p) => !p.mergedInto).length + 1;
    const name = label || `说话人${ordinal}`;
    setSpeakers((current) => ({
      ...current,
      [newId]: {
        id: newId,
        name,
        isMe: false,
        mergedInto: null,
      },
    }));
    const idSet = new Set(itemIds);
    setTranscript((current) =>
      current.map((item) =>
        idSet.has(item.id)
          ? { ...item, speakerId: newId, speaker: name }
          : item,
      ),
    );
    notify(`已拆分为「${name}」，可再点名字重命名`);
  }

  /** 把一段（可含合并气泡内多条）发言改派给另一个说话人 */
  function reassignUtterance(
    itemIds: string | string[],
    targetSpeakerId: string,
    targetLabel?: string,
  ) {
    const idSet = new Set(Array.isArray(itemIds) ? itemIds : [itemIds]);
    const targetIsMe =
      targetSpeakerId !== "other" &&
      (targetSpeakerId === "me" || Boolean(speakers[targetSpeakerId]?.isMe));
    const label =
      targetLabel ||
      (targetIsMe
        ? "我"
        : speakers[targetSpeakerId]?.name || targetSpeakerId);
    setSpeakers((current) => ({
      ...current,
      [targetSpeakerId]: {
        ...(current[targetSpeakerId] || {}),
        id: targetSpeakerId,
        name: current[targetSpeakerId]?.name || (targetIsMe ? "我" : label),
        isMe: targetIsMe,
        mergedInto: current[targetSpeakerId]?.mergedInto || null,
      },
    }));
    setTranscript((current) =>
      current.map((item) =>
        idSet.has(item.id)
          ? { ...item, speakerId: targetSpeakerId, speaker: label }
          : item,
      ),
    );
    notify(`已把这段改派给「${label}」`);
  }

  /** 气泡误标时：优先落到已有对方；没有则创建规范的 other，而不是 speaker-N */
  function reassignUtteranceToOther(itemIds: string | string[]) {
    const ids = Array.isArray(itemIds) ? itemIds : [itemIds];
    const meIds = new Set(
      Object.values(speakers)
        .filter((s) => s.isMe)
        .map((s) => s.id),
    );
    const preferred = preferredOtherSpeakerId(
      Object.keys(speakers),
      Object.fromEntries(
        Object.values(speakers).map((s) => [s.id, s.name]),
      ),
      meIds,
      speakerMenu?.speakerId || null,
    );
    if (preferred) {
      reassignUtterance(ids, preferred);
      return;
    }
    reassignUtterance(ids, "other", "对方");
  }

  // 当前项目挑选的"可用资料"文档 id（多对多关联，会前范围沿用这些）
  const [projectDocIds, setProjectDocIds] = useState<string[]>([]);

  /**
   * 本场可选文档：
   * - 有项目 → 该项目勾选的「可用资料」
   * - 无项目 → 全局知识库（会议可不属于任何项目）
   */
  const availableDocuments = useMemo(
    () =>
      activeProjectId
        ? documents.filter((doc) => projectDocIds.includes(doc.id))
        : documents,
    [documents, projectDocIds, activeProjectId],
  );

  async function refreshKnowledge() {
    if (!window.meetingCopilot) return;
    const [nextProjects, nextDocuments] = await Promise.all([
      window.meetingCopilot.listProjects(),
      window.meetingCopilot.listDocuments(), // 全局库
    ]);
    setProjects(nextProjects);
    setDocuments(nextDocuments);
    return { nextProjects, nextDocuments };
  }

  // 切换项目时加载它挑选的可用资料
  useEffect(() => {
    if (!window.meetingCopilot || !activeProjectId) {
      setProjectDocIds([]);
      return;
    }
    void window.meetingCopilot
      .getProjectDocuments(activeProjectId)
      .then(setProjectDocIds)
      .catch(() => setProjectDocIds([]));
  }, [activeProjectId, documents]);

  const selectedDevice = useMemo(
    () => devices.find((item) => item.index === persisted.selectedDevice),
    [devices, persisted.selectedDevice],
  );

  useEffect(() => {
    document.documentElement.dataset.theme = persisted.theme;
  }, [persisted.theme]);

  useEffect(() => {
    let active = true;
    if (!window.meetingCopilot) return;
    const dispose = window.meetingCopilot.onMeetingEvent(handleMeetingEvent);
    async function bootstrap() {
      const [saved, status, meetingRecords] = await Promise.all([
        window.meetingCopilot!.loadState(),
        window.meetingCopilot!.runtimeStatus(),
        window.meetingCopilot!.listMeetingRecords(),
      ]);
      if (!active) return;
      if (saved) setPersisted(mergePersistedState(saved));
      setRuntime(status);
      setRecords(meetingRecords);
    }
    void bootstrap();
    return () => {
      active = false;
      dispose();
    };
  }, []);

  useEffect(() => {
    if (window.meetingCopilot) {
      void window.meetingCopilot.saveState(persisted);
    } else {
      localStorage.setItem("meeting-copilot-state", JSON.stringify(persisted));
    }
  }, [persisted]);

  useEffect(() => {
    if (!window.meetingCopilot) {
      const saved = localStorage.getItem("meeting-copilot-state");
      if (saved) setPersisted(mergePersistedState(JSON.parse(saved)));
      const savedRecords = localStorage.getItem("meeting-copilot-records");
      if (savedRecords) setRecords(JSON.parse(savedRecords));
      return;
    }
    void (async () => {
      await refreshKnowledge();
      // 旧版本保存过 lastProjectId，但新会议不再继承上场项目。
      // 保留字段以兼容旧配置，读取时直接忽略。
      setActiveProjectId(null);
      setSelectedDocIds([]);
    })();
  }, []);

  // 默认勾选规则：
  //   选了项目 → 自动全选该项目的可用资料（项目就代表"这场会该用哪些料"）
  //   没选项目 → 一份都不勾。全局知识库可能有几十份不相关文档，
  //              默认全勾会把无关资料塞进检索，稀释建议质量。
  // 用户可用「全选 / 全不选」快速切换。
  useEffect(() => {
    setSelectedDocIds(
      activeProjectId
        ? availableDocuments.filter((doc) => doc.exists).map((doc) => doc.id)
        : [],
    );
  }, [availableDocuments, activeProjectId]);

  useEffect(() => {
    if (screen !== "prepare") return;
    if (!persisted.meetingTitle) {
      setPersisted((cur) => ({ ...cur, meetingTitle: generateDefaultMeetingTitle() }));
    }
    void runMeetingWarmup();
  }, [screen, activeProjectId, persisted.asrProvider]);

  useEffect(() => {
    if (screen !== "prepare") return;
    let active = true;
    const input = {
      title: persisted.meetingTitle,
      projectName: projects.find((project) => project.id === activeProjectId)?.name || null,
      documentNames: availableDocuments
        .filter((document) => selectedDocIds.includes(document.id))
        .map((document) => document.name),
    };
    if (!window.meetingCopilot) {
      const corpus = [input.title, input.projectName, ...input.documentNames].join(" ").toLowerCase();
      const scene: MeetingScene = /(需求|评审|澄清|接口|验收|业务规则|异常|范围|prd)/i.test(corpus)
        ? "requirements"
        : /(售前|客户|报价|商务|方案|招投标|采购|销售|预算)/i.test(corpus)
          ? "sales"
          : "general";
      if (active) {
        setSceneRecommendation({
          scene,
          label: SCENE_META[scene].label,
          reason: scene === "general" ? "没有明显的业务信号，先使用通用会议。" : `命中了${SCENE_META[scene].short}场景关键词。`,
          confidence: scene === "general" ? "low" : "medium",
        });
      }
      return () => {
        active = false;
      };
    }
    void window.meetingCopilot
      .recommendMeetingScene(input)
      .then((recommendation) => {
        if (active) setSceneRecommendation(recommendation);
      })
      .catch(() => {
        if (active) setSceneRecommendation(null);
      });
    return () => {
      active = false;
    };
  }, [
    activeProjectId,
    availableDocuments,
    persisted.meetingTitle,
    projects,
    screen,
    selectedDocIds,
  ]);

  // 新建会议默认跟随当前推荐；用户一旦手动点过场景，就尊重手动选择。
  useEffect(() => {
    if (
      screen !== "prepare" ||
      !sceneRecommendation ||
      !shouldAutoApplySceneRecommendation(
        sceneRecommendation,
        sceneSelectionTouched,
      )
    ) {
      return;
    }
    setPersisted((current) =>
      current.scene === sceneRecommendation.scene
        ? current
        : { ...current, scene: sceneRecommendation.scene },
    );
  }, [sceneRecommendation, sceneSelectionTouched, screen]);

  useEffect(() => {
    if (!activeMeetingId || !meetingStartedAt) return;
    const record: MeetingRecord = {
      id: activeMeetingId,
      title: persisted.meetingTitle.trim() || "未命名会议",
      startedAt: meetingStartedAt,
      endedAt: meetingEndedAt,
      status:
        meetingStatus === "ended" || meetingStatus === "error"
          ? "completed"
          : "active",
      scene: persisted.scene || "general",
      runtimeConfig: {
        provider: persisted.llmProvider || "config.py 默认",
        model: persisted.llmModel || "供应商默认模型",
        asrProvider: persisted.asrProvider || "config.py 默认",
        asrLang: persisted.asrLang || "zh_en",
        timeoutSeconds: 12,
        suggestionCount: persisted.suggestionCount,
        silenceSeconds: persisted.silenceSeconds,
        glossaryStatus: hotwordsStatus?.status || "pending",
        glossaryCount: hotwordsStatus?.count || 0,
      },
      meetingMode: persisted.meetingMode || "in_person",
      projectId: activeProjectId,
      // 落库时固化合并链：speakerId 改写到最终归属，名字与会中显示一致。
      // 只存 soft mergedInto 会在重载时丢失，历史「实时转写」会和会中对不上。
      transcript: transcript
        .filter((item) => item.isFinal)
        .map((item) => {
          const resolvedId = resolveSpeakerId(item.speakerId);
          return {
            ...item,
            speakerId: resolvedId,
            speaker: displaySpeaker({ ...item, speakerId: resolvedId }),
          };
        }),
      batches,
      speakers: Object.values(speakers).filter((profile) => !profile.mergedInto),
      audioPath: recordedAudio?.path ?? null,
      audioSeconds: recordedAudio?.seconds ?? null,
      micAudioPath: recordedAudio?.tracks?.mic?.path ?? null,
      micAudioSeconds: recordedAudio?.tracks?.mic?.seconds ?? null,
      systemAudioPath: recordedAudio?.tracks?.system?.path ?? null,
      systemAudioSeconds: recordedAudio?.tracks?.system?.seconds ?? null,
      hotwords: hotwordsStatus || undefined,
      lastError: lastMeetingError,
      // 不在此处重复写快照：会中自动保存频繁触发，
      // 快照已在 startMeeting 时落库，数据层对空数组也不会覆写
      documents: [],
    };
    const timer = window.setTimeout(() => {
      void saveRecord(record).catch(() => notify("会议记录保存失败"));
    }, 250);
    return () => window.clearTimeout(timer);
  }, [
    activeMeetingId,
    activeProjectId,
    batches,
    meetingEndedAt,
    meetingStartedAt,
    meetingStatus,
    persisted.meetingMode,
    persisted.meetingTitle,
    recordedAudio,
    hotwordsStatus,
    lastMeetingError,
    memoryItems,
    glossaryCandidates,
    persisted.scene,
    speakers,
    transcript,
  ]);

  async function refreshMeetingReview(meetingId: string) {
    if (!window.meetingCopilot) return;
    const result = await window.meetingCopilot.generateMeetingReview(meetingId, {
      enhance: false,
      provider: persisted.llmProvider,
      model: persisted.llmModel,
    });
    if (!result.ok || !result.record) return;
    if (activeMeetingIdRef.current === meetingId) {
      setMemoryItems(result.record.memoryItems || []);
      setGlossaryCandidates(result.record.glossaryCandidates || []);
    }
    setRecords((current) => [
      result.record!,
      ...current.filter((item) => item.id !== result.record!.id),
    ]);
    // 本地候选先立即可见；模型增强在后台补充责任人、期限和领域词，
    // 失败时主进程只标记失败状态，不会覆盖本地结果。
    window.setTimeout(() => {
      void enhanceMeetingReview(meetingId).catch(() => undefined);
    }, 300);
  }

  async function enhanceMeetingReview(meetingId: string) {
    if (!window.meetingCopilot?.generateMeetingReview) return;
    const result = await window.meetingCopilot.generateMeetingReview(meetingId, {
      enhance: true,
      provider: persisted.llmProvider,
      model: persisted.llmModel,
    });
    if (!result.record) return;
    if (activeMeetingIdRef.current === meetingId) {
      setMemoryItems(result.record.memoryItems || []);
      setGlossaryCandidates(result.record.glossaryCandidates || []);
    }
    setRecords((current) => [
      result.record!,
      ...current.filter((item) => item.id !== result.record!.id),
    ]);
  }

  function handleMeetingEvent(event: MeetingEvent) {
    // 转写连接状态（PRD TRS-6）：录音不中断，但必须让用户知道
    // 这段时间没有转写，否则他会误以为"没人说话"
    if (event.type === "knowledge_scope") {
      const parseErrors = Array.isArray(event.parseErrors)
        ? (event.parseErrors as Array<{ path?: string; message?: string }>)
        : [];
      if (parseErrors.length) {
        const first = parseErrors[0];
        const name = String(first.path || "")
          .replaceAll("\\", "/")
          .split("/")
          .at(-1);
        notify(
          `${parseErrors.length} 份知识文档未能解析：${name || "未知文档"}（${first.message || "无法解析"}）`,
        );
      }
      return;
    }
    if (event.type === "voiceprint_ready") {
      ensureSpeaker("me");
      ensureSpeaker("other");
      setSpeakers((current) => {
        const next: Record<string, SpeakerProfile> = { ...current };
        for (const [key, profile] of Object.entries(next)) {
          next[key] = { ...profile, isMe: key === "me" };
        }
        if (!next.me) {
          next.me = { id: "me", name: "我", isMe: true, mergedInto: null };
        } else {
          next.me = { ...next.me, isMe: true, name: next.me.name || "我" };
        }
        return next;
      });
      setStatusMessage("声纹认「我」已就绪");
      return;
    }
    if (event.type === "me_changed") {
      const sid = typeof event.speakerId === "string" ? event.speakerId : null;
      if (sid) {
        ensureSpeaker(sid);
        setSpeakers((current) => {
          const next: Record<string, SpeakerProfile> = {};
          for (const [key, profile] of Object.entries(current)) {
            next[key] = { ...profile, isMe: key === sid };
          }
          if (!next[sid]) {
            next[sid] = {
              id: sid,
              name: sid === "me" ? "我" : `说话人${sid}`,
              isMe: true,
              mergedInto: null,
            };
          }
          return next;
        });
      }
      return;
    }
    if (event.type === "asr_connection") {
      const state = String(event.state || "");
      const message = String(event.message || "");
      const channel = String(event.channel || "microphone");
      if (state === "connected" || state === "reconnected") {
        delete asrChannelErrors.current[channel];
      } else {
        asrChannelErrors.current[channel] = { state, message };
      }
      const failing = Object.entries(asrChannelErrors.current)[0];
      setAsrConnection(
        failing
          ? {
              state: failing[1].state,
              message: `${failing[0] === "system" ? "系统音频" : "麦克风"}：${failing[1].message}`,
            }
          : null,
      );
      if (state === "reconnected") notify("转写连接已恢复");
      return;
    }
    if (event.type === "ended" && event.audio) {
      const audio = event.audio as {
        path?: string;
        seconds?: number;
        tracks?: Record<
          string,
          { path?: string; seconds?: number | null; ok?: boolean; error?: string }
        >;
      };
      setRecordedAudio((current) => ({
        path: String(audio.path || current?.path || ""),
        seconds: Number(audio.seconds ?? current?.seconds ?? 0),
        tracks: {
          ...(current?.tracks || {}),
          ...(audio.tracks || {}),
        },
      }));
    }
    if (event.type === "recording_file" && event.path) {
      const tracks = (event.tracks || {}) as Record<
        string,
        { path?: string; seconds?: number | null; ok?: boolean; error?: string }
      >;
      setRecordedAudio((current) => ({
        path: String(event.path),
        seconds: Number(current?.seconds || 0),
        tracks: { ...(current?.tracks || {}), ...tracks },
      }));
    }
    if (event.type === "status") {
      setStatusMessage(String(event.message || ""));
      if (event.stage === "asr_config" && event.message) {
        notify(String(event.message));
      }
      if (event.stage === "hotwords") {
        const allowed = new Set([
          "pending",
          "empty",
          "loaded",
          "degraded",
          "unsupported",
        ]);
        const status = String(event.hotwordStatus || "degraded");
        setHotwordsStatus({
          status: allowed.has(status)
            ? (status as NonNullable<MeetingRecord["hotwords"]>["status"])
            : "degraded",
          count: Math.max(0, Math.round(Number(event.hotwordCount) || 0)),
          vocabularyId: event.vocabularyId
            ? String(event.vocabularyId)
            : null,
          reason: event.hotwordReason ? String(event.hotwordReason) : null,
        });
      }
      // 准备完成：提示用户后，再通知桥接层正式开麦/录音（会议计时从 listening 起算）
      const startupAction = meetingStartupAction(
        String(event.stage || ""),
        beginRecordingSent.current,
      );
      if (startupAction.sendBeginRecording) {
        beginRecordingSent.current = true;
        setRecordingCue(true);
        setStatusMessage("准备完成，即将正式开始录制…");
        window.setTimeout(() => {
          void window.meetingCopilot?.beginMeetingRecording?.().then((result) => {
            if (result && result.ok === false) {
              setStatusMessage(
                `无法正式开录：${result.reason || "未知错误"}，请结束并重试`,
              );
            }
          });
        }, 150);
      }
      if (startupAction.startClock) {
        const liveAt = Date.now();
        setMeetingStartedAt(liveAt);
        setMeetingStatus("live");
        setRecordingCue(false);
        const mid = activeMeetingIdRef.current;
        if (mid && window.meetingCopilot?.saveMeetingRecord) {
          void window.meetingCopilot.saveMeetingRecord({
            id: mid,
            title: persisted.meetingTitle,
            startedAt: liveAt,
            status: "active",
            meetingMode: persisted.meetingMode || "in_person",
            scene: persisted.scene || "general",
            projectId: activeProjectId,
            transcript: [],
            batches: [],
          });
        }
      }
      if (startupAction.cancel) {
        setRecordingCue(false);
        setMeetingStatus("ended");
      }
      return;
    }
    if (event.type === "audio_level") {
      audioLevelStore.set(Number(event.level || 0));
      return;
    }
    if (event.type === "controls") {
      setRecordingPaused(Boolean(event.recordingPaused));
      setSuggestionsPaused(Boolean(event.suggestionsPaused));
      setStatusMessage(
        event.recordingPaused
          ? "录制已暂停，不会转写暂停期间的声音"
          : "麦克风已连接，正在听取会议",
      );
      return;
    }
    if (event.type === "device_test_status") {
      if (event.status === "listening") setDeviceTestStatus("testing");
      if (event.status === "completed") setDeviceTestStatus("success");
      return;
    }
    if (event.type === "transcript") {
      const item: TranscriptItem = {
        id: event.id ? String(event.id) : `live-${++idCounter.current}`,
        speaker: String(event.speaker || "未知说话人"),
        speakerId: event.speakerId ? String(event.speakerId) : null,
        text: String(event.text || ""),
        isFinal: Boolean(event.isFinal),
        at: Number(event.at || Date.now()) * (Number(event.at) < 10_000_000_000 ? 1000 : 1),
        audioStartMs:
          event.audioStartMs == null ? null : Number(event.audioStartMs),
        audioEndMs:
          event.audioEndMs == null ? null : Number(event.audioEndMs),
      };
      ensureSpeaker(item.speakerId);
      setTranscript((current) => {
        if (!item.isFinal) {
          return [...current.filter((entry) => entry.isFinal), item];
        }
        return [...current.filter((entry) => entry.isFinal), item];
      });
      return;
    }
    if (event.type === "transcript_patch_last") {
      const append = String(event.append || "");
      if (!append) return;
      setTranscript((current) => {
        let lastFinalIndex = -1;
        for (let index = current.length - 1; index >= 0; index -= 1) {
          if (current[index].isFinal) {
            lastFinalIndex = index;
            break;
          }
        }
        if (lastFinalIndex < 0) return current;
        const updated = [...current];
        const target = updated[lastFinalIndex];
        updated[lastFinalIndex] = {
          ...target,
          text: target.text.endsWith(append)
            ? target.text
            : `${target.text}${append}`,
        };
        return updated;
      });
      return;
    }
    if (event.type === "suggestion_status") {
      if (event.status === "generating") {
        setSuggesting(true);
      } else {
        setSuggesting(false);
        // skipped 时桥接层会带一句原因（如"还没有转写内容"），别让点击悄无声息
        if (event.message) notify(String(event.message));
      }
      return;
    }
    if (event.type === "memory_updated") {
      if (
        event.meetingId &&
        activeMeetingIdRef.current &&
        String(event.meetingId) !== activeMeetingIdRef.current
      ) {
        return;
      }
      const item = event.item as MeetingMemoryItem | undefined;
      if (item?.id) {
        setMemoryItems((current) => [
          item,
          ...current.filter((entry) => entry.id !== item.id),
        ]);
      }
      return;
    }
    if (event.type === "review_updated") {
      const meetingId = event.meetingId ? String(event.meetingId) : null;
      const review = event.review as MeetingRecord["review"];
      if (meetingId && review) {
        setRecords((current) =>
          current.map((item) =>
            item.id !== meetingId
              ? item
              : {
                  ...item,
                  review,
                  memoryItems: review.memoryItems || item.memoryItems || [],
                  glossaryCandidates:
                    review.glossaryCandidates || item.glossaryCandidates || [],
                },
          ),
        );
      }
      if (
        event.meetingId &&
        activeMeetingIdRef.current &&
        String(event.meetingId) !== activeMeetingIdRef.current
      ) {
        return;
      }
      if (review) {
        setMemoryItems(review.memoryItems || []);
        setGlossaryCandidates(review.glossaryCandidates || []);
      }
      return;
    }
    if (event.type === "suggestions") {
      setSuggesting(false);
      const incomingMemory = Array.isArray(event.memoryCandidates)
        ? (event.memoryCandidates as MeetingMemoryItem[])
        : [];
      if (incomingMemory.length) {
        setMemoryItems((current) => {
          const next = new Map(current.map((item) => [item.id, item]));
          for (const item of incomingMemory) {
            const normalized: MeetingMemoryItem = {
              id: String(item.id || `memory-${Date.now()}`),
              kind: item.kind === "decision" ? "decision" : "action_item",
              status: item.status || "candidate",
              content: String(item.content || "").trim(),
              owner: item.owner || null,
              dueAt: item.dueAt || null,
              evidenceTranscriptId: item.evidenceTranscriptId || null,
              evidenceText: item.evidenceText || null,
              source: item.source === "model" ? "model" : "rule",
              createdAt: Number(item.createdAt || Date.now()),
              updatedAt: Number(item.updatedAt || Date.now()),
            };
            if (!normalized.content) continue;
            const previous = next.get(normalized.id);
            next.set(normalized.id, previous?.status === "confirmed" ? previous : normalized);
          }
          return Array.from(next.values());
        });
      }
      setBatches((current) => [
        {
          id: `batch-${++idCounter.current}`,
          suggestions: (event.suggestions || []) as Suggestion[],
          hits: (event.hits || []) as SuggestionBatch["hits"],
          context:
            event.context && typeof event.context === "object"
              ? (event.context as SuggestionContextRange)
              : undefined,
          parseError:
            (event.parseError as SuggestionBatch["parseError"]) || undefined,
          runtime:
            event.runtime && typeof event.runtime === "object"
              ? (event.runtime as SuggestionBatch["runtime"])
              : {
                  provider: String(event.provider || ""),
                  model: String(event.model || ""),
                  elapsed: Number(event.elapsed || 0),
                  trigger: String(event.trigger || "auto"),
                },
          elapsed: Number(event.elapsed || 0),
          at: Number(event.at || Date.now()) * (Number(event.at) < 10_000_000_000 ? 1000 : 1),
        },
        ...current,
      ]);
      return;
    }
    // ASK-3 流式回答：先建占位卡，再逐段追加，首字尽快出现
    if (event.type === "answer_status" && event.status === "generating") {
      const id = `answer-${++idCounter.current}`;
      answerStreamId.current = id;
      const placeholder: SuggestionBatch = {
        id,
        suggestions: [
          {
            // 占位阶段还不知道检索结果，先按最保守的分级显示，
            // answer 事件到达时再按实际 hits 定级
            level: "clarify",
            grounded: false,
            intent: `你的问题：${String(event.question || "")}`,
            script: "",
            references: [],
          },
        ],
        hits: [],
        elapsed: 0,
        at: Date.now(),
      };
      setBatches((current) => [placeholder, ...current]);
      return;
    }
    if (event.type === "answer_delta") {
      const id = answerStreamId.current;
      const delta = String(event.delta || "");
      if (!id) return;
      setBatches((current) =>
        current.map((batch) =>
          batch.id === id
            ? {
                ...batch,
                suggestions: [
                  {
                    ...batch.suggestions[0],
                    script: batch.suggestions[0].script + delta,
                  },
                ],
              }
            : batch,
        ),
      );
      return;
    }
    if (event.type === "answer") {
      const id = answerStreamId.current;
      const refs = ((event.hits || []) as Array<{ source?: string }>)
        .map((hit) => hit.source)
        .filter(Boolean) as string[];
      const hits = (event.hits || []) as SuggestionBatch["hits"];
      // 依据分级由【实际检索结果】决定，不能硬编码。检索为空 = 回答里没有
      // 知识库依据，此时模型给的是经验判断，标 advisory 而非"有依据"。
      const answerLevel: Suggestion["level"] = hits.length
        ? "grounded"
        : "advisory";
      // 若走了流式，用最终文本收尾并补上引用；否则新建一张卡
      if (id) {
        setBatches((current) =>
          current.map((batch) =>
            batch.id === id
              ? {
                  ...batch,
                  hits,
                  context:
                    event.context && typeof event.context === "object"
                      ? (event.context as SuggestionContextRange)
                      : batch.context,
                  elapsed: Number(event.elapsed || 0),
                  suggestions: [
                    {
                      ...batch.suggestions[0],
                      level: answerLevel,
                      grounded: hits.length > 0,
                      script: String(event.answer || batch.suggestions[0].script),
                      references: refs,
                    },
                  ],
                }
              : batch,
          ),
        );
        answerStreamId.current = null;
        return;
      }
      const answer: SuggestionBatch = {
        id: `answer-${++idCounter.current}`,
        suggestions: [
          {
            level: answerLevel,
            grounded: hits.length > 0,
            intent: `你的问题：${String(event.question || "")}`,
            script: String(event.answer || ""),
            references: refs,
          },
        ],
        hits,
        context:
          event.context && typeof event.context === "object"
            ? (event.context as SuggestionContextRange)
            : undefined,
        elapsed: Number(event.elapsed || 0),
        at: Date.now(),
      };
      setBatches((current) => [answer, ...current]);
      return;
    }
    if (event.type === "error") {
      const errorMessage = String(event.message || "").trim();
      if (activeMeetingId && errorMessage && event.stage !== "device_test") {
        setLastMeetingError({
          stage: String(event.stage || "meeting"),
          message: errorMessage,
          at: Date.now(),
        });
      }
      if (event.stage === "device_test") {
        setDeviceTestStatus("error");
        return;
      }
      // 生成失败也要把"生成中"收掉，否则按钮会一直转
      if (event.stage === "suggestion") {
        setSuggesting(false);
        setStatusMessage(String(event.message || "建议生成失败"));
        return;
      }
      const nonFatalStages = new Set([
        "voiceprint",
        "recording_file",
        "asr_stop",
      ]);
      if (event.fatal === false || nonFatalStages.has(String(event.stage || ""))) {
        notify(formatMeetingStartupError(event));
        return;
      }
      meetingFatalError.current = true;
      setMeetingStatus("error");
      setMeetingEndedAt((current) => current || Date.now());
      setStatusMessage(formatMeetingStartupError(event));
      return;
    }
    if (event.type === "ended" || event.type === "bridge_closed") {
      if (event.type === "bridge_closed" && meetingFatalError.current) return;
      setSuggesting(false);
      setMeetingStatus("ended");
      setMeetingEndedAt((current) => current || Date.now());
      setStatusMessage("会议已结束，记录保存在本机");
      if (activeMeetingId && window.meetingCopilot) {
        const reviewId = activeMeetingId;
        window.setTimeout(() => {
          void refreshMeetingReview(reviewId).catch(() => undefined);
        }, 650);
      }
    }
  }

  async function refreshDevices() {
    setDeviceError("");
    setDeviceTestStatus("idle");
    audioLevelStore.set(0);
    if (!window.meetingCopilot) {
      setDevices([
        {
          index: 0,
          name: "系统默认麦克风（浏览器演示）",
          channels: 1,
          sampleRate: 48_000,
          isDefault: true,
        },
      ]);
      setPersisted((current) => ({ ...current, selectedDevice: 0 }));
      return;
    }
    try {
      const result = await window.meetingCopilot.listInputDevices();
      setDevices(result.devices);
      const preferred =
        result.devices.find((device) => device.isDefault) || result.devices[0];
      if (preferred) {
        setPersisted((current) => ({
          ...current,
          selectedDevice: result.devices.some(
            (device) => device.index === current.selectedDevice,
          )
            ? current.selectedDevice
            : preferred.index,
        }));
      }
    } catch (error) {
      setDeviceError(error instanceof Error ? error.message : "无法读取麦克风");
    }
  }

  async function testInputDevice() {
    if (persisted.selectedDevice === undefined) return;
    setDeviceTestStatus("testing");
    audioLevelStore.set(0);
    if (!window.meetingCopilot) {
      audioLevelStore.set(0.55);
      window.setTimeout(() => setDeviceTestStatus("success"), 900);
      return;
    }
    try {
      await window.meetingCopilot.testInputDevice(persisted.selectedDevice);
      setDeviceTestStatus("success");
    } catch {
      setDeviceTestStatus("error");
    }
  }

  async function runMeetingWarmup() {
    const requestId = ++warmupRequestIdRef.current;
    if (!window.meetingCopilot?.warmupMeeting) {
      if (requestId === warmupRequestIdRef.current) {
        setWarmupLabel("桌面服务未连接，跳过预热");
      }
      return;
    }
    setWarmupLabel("正在预热：专有名词 / 依赖…");
    try {
      const result = await window.meetingCopilot.warmupMeeting({
        projectId: activeProjectId,
        asrProvider: persisted.asrProvider,
      });
      if (requestId !== warmupRequestIdRef.current) return;
      if (result?.ok) {
        const count = Number(result.termCount || 0);
        const hot =
          count > 0
            ? result.reused
              ? `同步失败，已复用最近有效词表 ${count} 个`
              : result.vocabularyId
                ? result.warning
                  ? `同步受限，已复用已有词表 ${count} 个`
                  : `热词已预同步 ${count} 个`
                : `热词 ${count} 个（未拿到词表 ID）`
            : "无热词需同步";
        setWarmupLabel(
          `会前准备就绪 · ${hot}${
            result.elapsedMs ? ` · ${result.elapsedMs}ms` : ""
          }`,
        );
      } else {
        setWarmupLabel(
          `会前预热部分失败：${result?.error || "可仍开始会议"}`,
        );
      }
    } catch (error) {
      if (requestId !== warmupRequestIdRef.current) return;
      setWarmupLabel(
        `会前预热失败：${error instanceof Error ? error.message : String(error)}`,
      );
    }
  }

  async function startMeeting() {
    // recordId 用创建时刻；会议计时 startedAt 等到正式开录（listening）再写
    const createdAt = Date.now();
    const recordId = `meeting-${createdAt}-${Math.random().toString(36).slice(2, 8)}`;
    // 本场知识范围：只取当前项目下被勾选、且文件仍存在的文档。
    // 失效文档不传给 Python，避免它在启动时才报错。
    const scopeDocuments: MeetingDocument[] = availableDocuments
      .filter((doc) => selectedDocIds.includes(doc.id) && doc.exists)
      .map((doc) => ({ id: doc.id, name: doc.name, path: doc.path }));
    setScreen("meeting");
    setTranscript([]);
    setBatches([]);
    setMemoryItems([]);
    setGlossaryCandidates([]);
    setActiveMeetingId(recordId);
    setMeetingStartedAt(null);
    setMeetingEndedAt(undefined);
    setSelectedRecordId(null);
    setRecordingPaused(false);
    setSuggestionsPaused(false);
    setRecordedAudio(null);
    setRecordingCue(false);
    beginRecordingSent.current = false;
    setHotwordsStatus({
      status: "pending",
      count: 0,
      vocabularyId: null,
      reason: null,
    });
    setLastMeetingError(undefined);
    setSpeakers({});
    asrChannelErrors.current = {};
    meetingFatalError.current = false;
    setAsrConnection(null);
    setMeetingStatus("starting");
    setStatusMessage("正在准备麦克风、声纹与转写服务…");
    if (!window.meetingCopilot) {
      setTimeout(() => {
        const liveAt = Date.now();
        setMeetingStartedAt(liveAt);
        const timedDemo = demoTranscript.map((item, index) => {
          const [audioStart, audioEnd] = DEMO_AUDIO_WINDOWS[index];
          return {
            ...item,
            at: liveAt + audioEnd * 1000,
            audioStartMs: audioStart * 1000,
            audioEndMs: audioEnd * 1000,
          };
        });
        setTranscript(timedDemo);
        setBatches([demoBatch]);
        // 演示模式同样登记说话人，保证说话人菜单行为与真实链路一致
        registerSpeakersFrom(timedDemo, "2");
        setMeetingStatus("live");
        setStatusMessage("演示模式：未连接桌面服务");
      }, 650);
      return;
    }
    try {
      await window.meetingCopilot.startMeeting({
        meetingId: recordId,
        device: persisted.selectedDevice,
        meetingMode: persisted.meetingMode || "in_person",
        scene: persisted.scene || "general",
        projectId: activeProjectId,
        documents: scopeDocuments,
        // UI 选中的供应商/模型（空则 Python 侧用 config.py 默认）
        provider: persisted.llmProvider,
        llmModel: persisted.llmModel,
        asrProvider: persisted.asrProvider,
        asrModel: persisted.asrModel,
        asrLang: persisted.asrLang || "zh_en",
        // 设置页「建议触发」——这两项以前只存在于界面，改了没有任何效果
        silenceSeconds: persisted.silenceSeconds,
        suggestionCount: persisted.suggestionCount,
      });
      // 立刻落一次快照：即使会议异常中断，也能说清当时用了哪些资料
      // startedAt 先用创建时间占位，正式开录后会改写为 listening 时刻
      await window.meetingCopilot.saveMeetingRecord({
        id: recordId,
        title: persisted.meetingTitle,
        startedAt: createdAt,
        status: "active",
        meetingMode: persisted.meetingMode || "in_person",
        scene: persisted.scene || "general",
        projectId: activeProjectId,
        runtimeConfig: {
          provider: persisted.llmProvider || "config.py 默认",
          model: persisted.llmModel || "供应商默认模型",
          asrProvider: persisted.asrProvider || "config.py 默认",
          asrModel: persisted.asrModel,
          asrLang: persisted.asrLang || "zh_en",
          timeoutSeconds: 12,
          suggestionCount: persisted.suggestionCount,
          silenceSeconds: persisted.silenceSeconds,
          glossaryStatus: "pending",
          glossaryCount: 0,
        },
        hotwords: { status: "pending", count: 0 },
        transcript: [],
        batches: [],
        documents: scopeDocuments,
      });
    } catch (error) {
      meetingFatalError.current = true;
      setMeetingStatus("error");
      setMeetingEndedAt(Date.now());
      setStatusMessage(
        formatMeetingStartupError({
          type: "error",
          stage: "bridge",
          message: error instanceof Error ? error.message : "启动失败",
        }),
      );
    }
  }

  async function stopMeeting() {
    setMeetingStatus("stopping");
    setStatusMessage("正在安全结束录音");
    if (!window.meetingCopilot) {
      setMeetingStatus("ended");
      setMeetingEndedAt(Date.now());
      setRecordedAudio({
        path: "browser-demo-playback.wav",
        seconds: DEMO_AUDIO_SECONDS,
      });
      setStatusMessage("演示会议已结束");
      return;
    }
    const result = await window.meetingCopilot.stopMeeting();
    // ⚠️ 桥接进程已经不在了（崩溃、或这条记录来自上次运行）：不会再有 ended
    //    事件回来，必须就地收尾。此前这里坐等事件，界面就永远卡在"正在结束"。
    if (!result.ok) {
      setMeetingStatus("ended");
      setMeetingEndedAt((current) => current || Date.now());
      setStatusMessage("会议已结束（后台服务此前已退出）");
    }
  }

  /**
   * 收尾一条遗留的"进行中"记录（不是本次运行开的那场）。
   *
   * 桥接进程随应用退出，所以这类记录不可能还在跑；直接改写状态即可。
   */
  async function finalizeStaleRecord(recordId: string) {
    const record =
      (window.meetingCopilot
        ? await window.meetingCopilot.loadMeetingRecord(recordId)
        : records.find((item) => item.id === recordId)) || null;
    if (!record) return notify("这条会议记录无法读取");
    const lastAt = record.transcript.at(-1)?.at;
    await saveRecord({
      ...record,
      status: "interrupted",
      endedAt: record.endedAt || lastAt || record.startedAt,
    });
    notify("已将这场会议标记为异常中断");
  }

  async function saveRecord(record: MeetingRecord) {
    setRecords((current) => {
      const next = [record, ...current.filter((item) => item.id !== record.id)].sort(
        (a, b) => b.startedAt - a.startedAt,
      );
      if (!window.meetingCopilot) {
        localStorage.setItem("meeting-copilot-records", JSON.stringify(next));
      }
      return next;
    });
    if (window.meetingCopilot) {
      const saved = await window.meetingCopilot.saveMeetingRecord(record);
      setRecords((current) =>
        [saved, ...current.filter((item) => item.id !== saved.id)].sort(
          (a, b) => b.startedAt - a.startedAt,
        ),
      );
    }
  }

  const [backgroundTasks, setBackgroundTasks] = useState<
    Record<string, BackgroundTaskInfo>
  >({});

  async function runGenerateMinutes(recordId: string, recordTitle: string) {
    if (!window.meetingCopilot?.generateMeetingMinutes) {
      notify("当前环境不支持自动生成会议纪要");
      return;
    }
    const taskKey = `minutes:${recordId}`;
    setBackgroundTasks((prev) => ({
      ...prev,
      [taskKey]: {
        key: taskKey,
        type: "minutes",
        targetId: recordId,
        title: recordTitle,
        status: "running",
        message: "正在梳理结论、待办和风险…",
        startedAt: Date.now(),
      },
    }));
    notify(`已在后台开始为《${recordTitle || "会议"}》生成纪要…`);
    try {
      const result = await window.meetingCopilot.generateMeetingMinutes(
        recordId,
        {
          provider: persisted.llmProvider,
          model: persisted.llmModel,
        },
      );
      if (!result.ok || !result.record) {
        const errorMsg = result.message || "会议纪要生成失败";
        setBackgroundTasks((prev) => ({
          ...prev,
          [taskKey]: {
            ...prev[taskKey],
            status: "error",
            message: errorMsg,
          },
        }));
        notify(`《${recordTitle || "会议"}》纪要生成失败：${errorMsg}`);
        if (result.record) void saveRecord(result.record);
        return;
      }
      void saveRecord(result.record);
      const elapsed = result.summary?.elapsedSec;
      const successMsg = `已基于${
        result.record.minutes?.sourceVersion === "offline"
          ? "会后整理"
          : "实时转写"
      }生成${elapsed != null ? ` · ${elapsed}s` : ""}`;
      setBackgroundTasks((prev) => ({
        ...prev,
        [taskKey]: {
          ...prev[taskKey],
          status: "success",
          message: successMsg,
        },
      }));
      notify(`《${recordTitle || "会议"}》会议纪要已生成完成！`);
    } catch (error) {
      const errorMsg =
        error instanceof Error ? error.message : "会议纪要生成失败";
      setBackgroundTasks((prev) => ({
        ...prev,
        [taskKey]: {
          ...prev[taskKey],
          status: "error",
          message: errorMsg,
        },
      }));
      notify(`《${recordTitle || "会议"}》纪要生成失败：${errorMsg}`);
    }
  }

  async function runDiarizeMeeting(
    recordId: string,
    recordTitle: string,
    opts: {
      splitChars?: number;
      enrollMode?: string;
      forceReextract?: boolean;
      cleanup?: boolean;
      meThreshold?: number;
      clusterThreshold?: number;
      provider?: string;
      model?: string;
      cleanTranscript?: boolean;
    },
  ) {
    if (!window.meetingCopilot?.diarizeMeeting) {
      notify("当前环境不支持说话人分离");
      return;
    }
    const taskKey = `diarize:${recordId}`;
    setBackgroundTasks((prev) => ({
      ...prev,
      [taskKey]: {
        key: taskKey,
        type: "diarize",
        targetId: recordId,
        title: recordTitle,
        status: "running",
        message: "正在离线转写与分离说话人…",
        startedAt: Date.now(),
      },
    }));
    notify(`已在后台开始为《${recordTitle || "会议"}》整理发言与分离说话人…`);
    try {
      const result = await window.meetingCopilot.diarizeMeeting(recordId, opts);
      if (!result.ok || !result.record) {
        const errorMsg = result.message || "说话人分离失败";
        setBackgroundTasks((prev) => ({
          ...prev,
          [taskKey]: {
            ...prev[taskKey],
            status: "error",
            message: errorMsg,
          },
        }));
        notify(`《${recordTitle || "会议"}》说话人分离失败：${errorMsg}`);
        return;
      }
      void saveRecord(result.record);
      const s = result.summary;
      const successMsg = `完成：${s?.speakerCount ?? "?"} 人 · ${
        s?.segmentCount ?? "?"
      } 段语音${s?.elapsedSec != null ? ` · ${s.elapsedSec}s` : ""}`;
      setBackgroundTasks((prev) => ({
        ...prev,
        [taskKey]: {
          ...prev[taskKey],
          status: "success",
          message: successMsg,
        },
      }));
      notify(`《${recordTitle || "会议"}》说话人分离与文字整理完成！`);
    } catch (error) {
      const errorMsg =
        error instanceof Error ? error.message : "说话人分离失败";
      setBackgroundTasks((prev) => ({
        ...prev,
        [taskKey]: {
          ...prev[taskKey],
          status: "error",
          message: errorMsg,
        },
      }));
      notify(`《${recordTitle || "会议"}》说话人分离失败：${errorMsg}`);
    }
  }

  async function runGenerateReview(
    recordId: string,
    recordTitle: string,
    enhance = false,
  ) {
    if (!window.meetingCopilot?.generateMeetingReview) {
      notify("当前环境不支持会后复盘");
      return;
    }
    const taskKey = `review:${recordId}`;
    setBackgroundTasks((prev) => ({
      ...prev,
      [taskKey]: {
        key: taskKey,
        type: "review",
        targetId: recordId,
        title: recordTitle,
        status: "running",
        message: enhance ? "正在用模型补全决策与待办…" : "正在生成本地复盘…",
        startedAt: Date.now(),
      },
    }));
    notify(
      `已在后台开始为《${recordTitle || "会议"}》${
        enhance ? "增强决策与待办" : "提取会后复盘"
      }…`,
    );
    try {
      const result = await window.meetingCopilot.generateMeetingReview(
        recordId,
        {
          enhance,
          provider: persisted.llmProvider,
          model: persisted.llmModel,
        },
      );
      if (!result.ok || !result.record) {
        const errorMsg = result.message || "复盘提取失败";
        setBackgroundTasks((prev) => ({
          ...prev,
          [taskKey]: { ...prev[taskKey], status: "error", message: errorMsg },
        }));
        notify(`《${recordTitle || "会议"}》复盘提取失败：${errorMsg}`);
        return;
      }
      void saveRecord(result.record);
      setBackgroundTasks((prev) => ({
        ...prev,
        [taskKey]: {
          ...prev[taskKey],
          status: "success",
          message: "复盘已更新",
        },
      }));
      notify(`《${recordTitle || "会议"}》会后复盘更新完成！`);
    } catch (error) {
      const errorMsg =
        error instanceof Error ? error.message : "复盘提取失败";
      setBackgroundTasks((prev) => ({
        ...prev,
        [taskKey]: { ...prev[taskKey], status: "error", message: errorMsg },
      }));
      notify(`《${recordTitle || "会议"}》复盘提取失败：${errorMsg}`);
    }
  }

  async function exportRecord(recordId: string, format: "md" | "txt") {
    if (!window.meetingCopilot) return notify("演示模式下无法导出");
    try {
      const result = await window.meetingCopilot.exportMeetingRecord(
        recordId,
        format,
      );
      if (result.canceled) return;
      notify(result.ok ? `已导出到 ${result.path}` : "导出失败");
    } catch (error) {
      notify(error instanceof Error ? error.message : "导出失败");
    }
  }

  async function deleteRecords(recordIds: string[]) {
    const ids = Array.from(new Set(recordIds)).filter(Boolean);
    if (!ids.length) return false;
    try {
      if (!window.meetingCopilot) {
        const confirmed = window.confirm(
          `确定永久删除选中的 ${ids.length} 场会议吗？转写、建议和演示录音将一并删除。`,
        );
        if (!confirmed) return false;
        setRecords((current) => {
          const next = current.filter((record) => !ids.includes(record.id));
          localStorage.setItem("meeting-copilot-records", JSON.stringify(next));
          return next;
        });
      } else {
        const result = await window.meetingCopilot.deleteMeetingRecords(ids);
        if (result.canceled) return false;
        if (!result.ok) {
          notify("会议删除失败");
          return false;
        }
        setRecords((current) =>
          current.filter((record) => !ids.includes(record.id)),
        );
      }
      if (selectedRecordId && ids.includes(selectedRecordId)) {
        setSelectedRecordId(null);
        setScreen("history");
      }
      notify(ids.length === 1 ? "会议已删除" : `已删除 ${ids.length} 场会议`);
      return true;
    } catch (error) {
      notify(error instanceof Error ? error.message : "会议删除失败");
      return false;
    }
  }

  /** 本次运行开着的会议还在跑吗（决定能否"返回"而不是"回看"） */
  const meetingInProgress =
    activeMeetingId !== null &&
    ["starting", "live", "stopping"].includes(meetingStatus);

  async function openRecord(recordId: string) {
    // ⚠️ 正在进行的那场要回到【控制台】，不能进历史回看：历史页没有结束按钮，
    //    此前从侧栏点开任何其它页面后，这场会就成了没有入口的孤儿 —— 既回不去
    //    也停不掉。
    if (recordId === activeMeetingId && meetingInProgress) {
      setScreen("meeting");
      return;
    }
    let record = records.find((item) => item.id === recordId) || null;
    if (window.meetingCopilot) {
      record = await window.meetingCopilot.loadMeetingRecord(recordId);
      if (record) {
        setRecords((current) => [
          record!,
          ...current.filter((item) => item.id !== recordId),
        ]);
      }
    }
    if (!record) {
      notify("这条会议记录无法读取");
      return;
    }
    setSelectedRecordId(record.id);
    setScreen("history-detail");
  }

  async function toggleRecording() {
    const nextRecordingPaused = !recordingPaused;
    const nextSuggestionsPaused = nextRecordingPaused
      ? true
      : suggestionsPaused;
    setRecordingPaused(nextRecordingPaused);
    setSuggestionsPaused(nextSuggestionsPaused);
    if (nextRecordingPaused) audioLevelStore.set(0);
    setStatusMessage(
      nextRecordingPaused
        ? "录制已暂停，不会转写暂停期间的声音"
        : "麦克风已连接，正在听取会议",
    );
    if (window.meetingCopilot) {
      try {
        await window.meetingCopilot.setMeetingControls({
          recordingPaused: nextRecordingPaused,
          suggestionsPaused: nextSuggestionsPaused,
        });
      } catch (error) {
        setRecordingPaused(!nextRecordingPaused);
        setSuggestionsPaused(suggestionsPaused);
        setStatusMessage(error instanceof Error ? error.message : "切换录制状态失败");
      }
    }
  }

  async function toggleSuggestions() {
    const nextSuggestionsPaused = !suggestionsPaused;
    setSuggestionsPaused(nextSuggestionsPaused);
    if (window.meetingCopilot) {
      try {
        await window.meetingCopilot.setMeetingControls({
          suggestionsPaused: nextSuggestionsPaused,
        });
      } catch {
        setSuggestionsPaused(!nextSuggestionsPaused);
      }
    }
  }

  async function updateMemoryItem(id: string, patch: Partial<MeetingMemoryItem>) {
    const current = memoryItems.find((item) => item.id === id);
    if (!current) return;
    const next = { ...current, ...patch, source: "user" as const, updatedAt: Date.now() };
    setMemoryItems((items) => items.map((item) => (item.id === id ? next : item)));
    if (activeMeetingId && window.meetingCopilot) {
      try {
        const saved = await window.meetingCopilot.saveMeetingMemoryItem(activeMeetingId, next);
        setMemoryItems((items) => items.map((item) => (item.id === id ? saved : item)));
      } catch {
        notify("记忆项保存失败");
      }
    }
  }

  /** 手动索取一批建议（绕过自动建议的冷却与增量闸门） */
  const suggestNow = useCallback(() => {
    if (!window.meetingCopilot) return notify("演示模式下无法生成建议");
    window.meetingCopilot
      .suggestNow()
      .catch((error) =>
        notify(error instanceof Error ? error.message : "生成建议失败"),
      );
  }, []);

  const sendQuestion = useCallback(async (raw: string) => {
    const value = raw.trim();
    if (!value) return;
    if (window.meetingCopilot && meetingStatus === "live") {
      await window.meetingCopilot.ask(value);
      return;
    }
    setBatches((current) => [
      {
        ...demoBatch,
        id: `q-${Date.now()}`,
        at: Date.now(),
        suggestions: [
          {
            level: "grounded",
            grounded: true,
            intent: `你的问题：${value}`,
            script:
              "建议先确认审批规则是否需要业务人员自行维护，再决定采用配置化规则还是定制开发。",
            references: ["产品功能清单.md"],
          },
        ],
      },
      ...current,
    ]);
  }, [meetingStatus]);

  function notify(message: string) {
    setToast(message);
    window.setTimeout(() => setToast(""), 1800);
  }

  /** 所有“新建会议”入口都从这里进入，确保本场从不继承上场项目或文档勾选。 */
  function enterPrepare(mode?: "in_person" | "online") {
    setActiveProjectId(null);
    setProjectDocIds([]);
    setSelectedDocIds([]);
    setSceneRecommendation(null);
    setSceneSelectionTouched(false);
    setPersisted((current) => ({
      ...current,
      meetingTitle: generateDefaultMeetingTitle(),
      scene: "general",
      ...(mode ? { meetingMode: mode } : {}),
    }));
    setScreen("prepare");
  }

  const selectedRecord =
    records.find((record) => record.id === selectedRecordId) || null;

  return (
    <div className="app-shell">
      <aside className={`rail ${railExpanded ? "expanded" : ""}`} aria-label="主导航">
        <button
          className="brand-mark"
          onClick={() => setScreen("home")}
          aria-label="实时会议话术助手"
        >
          <span className="brand-glyph">话</span>
          <span className="rail-label">话术助手</span>
        </button>
        <div className="rail-nav">
          {/*
            会议进行中时常驻的返回入口。会议控制台不在 navItems 里（它没有
            常态入口），所以一旦切走就没有任何地方能回来 —— 这里补上。
          */}
          {meetingInProgress && (
            <button
              className={`rail-button live-return ${
                screen === "meeting" ? "active" : ""
              }`}
              onClick={() => setScreen("meeting")}
              title="返回进行中的会议"
              aria-label="返回进行中的会议"
            >
              <Radio size={19} strokeWidth={1.8} />
              <span className="rail-label">进行中的会议</span>
            </button>
          )}
          {navItems.map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.screen}
                className={`rail-button ${
                  screen === item.screen ||
                  (item.screen === "history" && screen === "history-detail")
                    ? "active"
                    : ""
                }`}
                onClick={() =>
                  item.screen === "prepare"
                    ? enterPrepare()
                    : setScreen(item.screen)
                }
                title={item.label}
                aria-label={item.label}
              >
                <Icon size={19} strokeWidth={1.8} />
                <span className="rail-label">{item.label}</span>
              </button>
            );
          })}
        </div>
        <div className="rail-bottom">
          <button
            className={`rail-button ${screen === "settings" ? "active" : ""}`}
            onClick={() => setScreen("settings")}
            title="设置"
            aria-label="设置"
          >
            <Settings size={19} strokeWidth={1.8} />
            <span className="rail-label">设置</span>
          </button>
          <button
            className="rail-button"
            onClick={() =>
              setPersisted((current) => ({
                ...current,
                theme: current.theme === "light" ? "dark" : "light",
              }))
            }
            title="切换主题"
            aria-label="切换主题"
          >
            {persisted.theme === "light" ? (
              <Moon size={19} strokeWidth={1.8} />
            ) : (
              <Sun size={19} strokeWidth={1.8} />
            )}
            <span className="rail-label">切换主题</span>
          </button>
          <button
            className="rail-button"
            onClick={() => setRailExpanded((value) => !value)}
            title={railExpanded ? "收起导航" : "展开导航"}
            aria-label={railExpanded ? "收起导航" : "展开导航"}
          >
            {railExpanded ? (
              <PanelLeftClose size={19} strokeWidth={1.8} />
            ) : (
              <PanelLeftOpen size={19} strokeWidth={1.8} />
            )}
            <span className="rail-label">收起导航</span>
          </button>
        </div>
      </aside>

      <main
        className={`workspace ${
          screen === "history-detail" ? "detail-workspace" : ""
        }`}
      >
        {screen === "home" && (
          <HomeScreen
            runtime={runtime}
            records={records}
            onPrepare={() => enterPrepare()}
            onQuickStart={(mode) => enterPrepare(mode)}
            onOpenRecord={(id) => void openRecord(id)}
            onViewAll={() => setScreen("history")}
          />
        )}
        {screen === "prepare" && (
          <PrepareScreen
            persisted={persisted}
            devices={devices}
            deviceError={deviceError}
            onChange={setPersisted}
            onSelectScene={(scene) => {
              setSceneSelectionTouched(true);
              setPersisted((current) => ({ ...current, scene }));
            }}
            onSelectDevice={(device) => {
              setPersisted((current) => ({ ...current, selectedDevice: device }));
              setDeviceTestStatus("idle");
              audioLevelStore.set(0);
            }}
            onRefreshDevices={refreshDevices}
            onTestDevice={testInputDevice}
            deviceTestStatus={deviceTestStatus}
            onStart={startMeeting}
            sceneRecommendation={sceneRecommendation}
            projects={projects}
            activeProjectId={activeProjectId}
            onSelectProject={(id) => {
              setActiveProjectId(id);
            }}
            documents={availableDocuments}
            selectedDocIds={selectedDocIds}
            onToggleDoc={(id) =>
              setSelectedDocIds((current) =>
                current.includes(id)
                  ? current.filter((item) => item !== id)
                  : [...current, id],
              )
            }
            onSelectAllDocs={() =>
              setSelectedDocIds(
                availableDocuments.filter((d) => d.exists).map((d) => d.id),
              )
            }
            onClearDocs={() => setSelectedDocIds([])}
            onManageKnowledge={() => setScreen("knowledge")}
            onManageProjects={() => setScreen("projects")}
            warmupLabel={warmupLabel}
            onWarmup={() => void runMeetingWarmup()}
          />
        )}
        {screen === "meeting" && (
          <MeetingScreen
            title={persisted.meetingTitle}
            status={meetingStatus}
            statusMessage={statusMessage}
            recordingCue={recordingCue}
            startedAt={meetingStartedAt}
            endedAt={meetingEndedAt}
            transcript={transcript}
            batches={batches}
            scene={persisted.scene || "general"}
            memoryItems={memoryItems}
            recordingPaused={recordingPaused}
            suggestionsPaused={suggestionsPaused}
            onAsk={sendQuestion}
            onSuggestNow={suggestNow}
            suggesting={suggesting}
            onOpenFloating={() =>
              void window.meetingCopilot?.openFloatingStrategy()
            }
            onPauseRecording={toggleRecording}
            onPauseSuggestions={toggleSuggestions}
            onStop={stopMeeting}
            onCopy={(value) => {
              void navigator.clipboard.writeText(value);
              notify("话术已复制");
            }}
            onAdopt={adoptSuggestion}
            onUpdateMemory={updateMemoryItem}
            asrConnection={asrConnection}
            resolveSpeakerName={displaySpeaker}
            resolveSpeakerKey={(item) =>
              resolveSpeakerId(item.speakerId) || `name:${item.speaker}`
            }
            onOpenSpeakerMenu={(speakerId, itemId, segmentIds, rect, scope) => {
              const pos = placeFixedMenu(rect, {
                width: 240,
                estimatedHeight: scope === "speaker" ? 280 : 220,
              });
              setSpeakerMenu({
                scope,
                speakerId,
                itemId,
                segmentIds,
                x: pos.x,
                y: pos.y,
                openAbove: pos.openAbove,
              });
            }}
          />
        )}
        {screen === "history" && (
          <HistoryScreen
            records={records}
            liveMeetingId={meetingInProgress ? activeMeetingId : null}
            onOpen={(id) => void openRecord(id)}
            onFinalizeStale={(id) => void finalizeStaleRecord(id)}
            onDelete={deleteRecords}
          />
        )}
        {screen === "history-detail" && selectedRecord && (
          <HistoryDetailScreen
            record={selectedRecord}
            onBack={() => setScreen("history")}
            onCopy={(value) => {
              void navigator.clipboard.writeText(value);
              notify("话术已复制");
            }}
            onExport={(format) => void exportRecord(selectedRecord.id, format)}
            onDelete={() => deleteRecords([selectedRecord.id])}
            onNotify={notify}
            onPersist={(updated) => {
              // saveRecord 会更新 records 状态，selectedRecord 随之重新派生
              void saveRecord(updated);
            }}
            llmProvider={persisted.llmProvider}
            llmModel={persisted.llmModel}
            backgroundTasks={backgroundTasks}
            onGenerateMinutes={runGenerateMinutes}
            onDiarizeMeeting={runDiarizeMeeting}
            onGenerateReview={runGenerateReview}
          />
        )}
        {screen === "projects" && (
          <ProjectsScreen
            projects={projects}
            documents={documents}
            activeProjectId={activeProjectId}
            onSelectProject={(id) => {
              setActiveProjectId(id);
            }}
            onRefresh={refreshKnowledge}
            onNotify={notify}
          />
        )}
        {screen === "knowledge" && (
          <KnowledgeScreen
            documents={documents}
            onRefresh={refreshKnowledge}
            onNotify={notify}
          />
        )}
        {screen === "glossary" && (
          <GlossaryScreen
            projects={projects}
            activeProjectId={activeProjectId}
            onSelectProject={(id) => {
              setActiveProjectId(id);
            }}
            onNotify={notify}
          />
        )}
        {screen === "settings" && (
          <SettingsScreen persisted={persisted} onChange={setPersisted} onNotify={notify} />
        )}
      </main>

      {speakerMenu && (
        <>
          <div
            className="menu-backdrop"
            onClick={() => {
              setSpeakerMenu(null);
              setRenaming(null);
            }}
          />
          <div
            className="speaker-menu"
            style={fixedMenuStyle(speakerMenu)}
          >
            {speakerMenu.scope === "speaker" ? (
              renaming?.id === speakerMenu.speakerId ? (
                <form
                  className="rename-form"
                  onSubmit={(event) => {
                    event.preventDefault();
                    renameSpeaker(speakerMenu.speakerId, renaming.value);
                    setRenaming(null);
                    setSpeakerMenu(null);
                  }}
                >
                  <input
                    autoFocus
                    value={renaming.value}
                    placeholder="如：客户-张总"
                    onChange={(event) =>
                      setRenaming({ ...renaming, value: event.target.value })
                    }
                  />
                  <button className="button primary small" type="submit">
                    保存
                  </button>
                </form>
              ) : (
                <>
                  <div className="menu-label">调整这位说话人的全部发言</div>
                  <button
                    onClick={() =>
                      setRenaming({
                        id: speakerMenu.speakerId,
                        value: speakers[speakerMenu.speakerId]?.name || "",
                      })
                    }
                  >
                    重命名说话人…
                  </button>
                  <button
                    onClick={() => {
                      markAsMe(speakerMenu.speakerId);
                      setSpeakerMenu(null);
                    }}
                    disabled={
                      meSpeakerId === speakerMenu.speakerId ||
                      speakerMenu.speakerId.startsWith("local-")
                    }
                    title={
                      speakerMenu.speakerId.startsWith("local-")
                        ? "拆分出来的说话人无法作为「我」，请在原始说话人上标记"
                        : ""
                    }
                  >
                    {meSpeakerId === speakerMenu.speakerId
                      ? "已标记为「我」"
                      : "标记为「我」"}
                  </button>
                  {otherSpeakers(speakerMenu.speakerId).length > 0 && (
                    <>
                      <div className="menu-divider" />
                      <div className="menu-label">
                        将该说话人的全部发言改派给
                      </div>
                      {otherSpeakers(speakerMenu.speakerId).map((profile) => (
                        <button
                          key={`merge-${profile.id}`}
                          onClick={() => {
                            mergeSpeaker(speakerMenu.speakerId, profile.id);
                            setSpeakerMenu(null);
                          }}
                        >
                          {profile.isMe ? "我" : profile.name}
                        </button>
                      ))}
                    </>
                  )}
                </>
              )
            ) : (
              <>
                <div className="menu-label">只改这一段的归属</div>
                {otherSpeakers(speakerMenu.speakerId).map((profile) => (
                  <button
                    key={`reassign-${profile.id}`}
                    onClick={() => {
                      reassignUtterance(
                        speakerMenu.segmentIds.length
                          ? speakerMenu.segmentIds
                          : speakerMenu.itemId,
                        profile.id,
                      );
                      setSpeakerMenu(null);
                    }}
                  >
                    {profile.isMe ? "我" : profile.name}
                  </button>
                ))}
                {otherSpeakers(speakerMenu.speakerId).length === 0 && (
                  <button
                    onClick={() => {
                      reassignUtteranceToOther(
                        speakerMenu.segmentIds.length
                          ? speakerMenu.segmentIds
                          : speakerMenu.itemId,
                      );
                      setSpeakerMenu(null);
                    }}
                  >
                    对方
                  </button>
                )}
                <div className="menu-divider" />
                <button
                  onClick={() => {
                    splitToNewSpeaker(
                      speakerMenu.segmentIds.length
                        ? speakerMenu.segmentIds
                        : [speakerMenu.itemId],
                    );
                    setSpeakerMenu(null);
                  }}
                >
                  <Split size={13} /> 新建说话人（第三人）
                </button>
              </>
            )}
          </div>
        </>
      )}

      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}

function HomeScreen({
  runtime,
  records,
  onPrepare,
  onQuickStart,
  onOpenRecord,
  onViewAll,
}: {
  runtime: {
    desktop: boolean;
    pythonReady: boolean;
    bridgeReady: boolean;
    configPresent: boolean;
  };
  records: MeetingRecord[];
  onPrepare: () => void;
  onQuickStart: (mode: "in_person" | "online") => void;
  onOpenRecord: (id: string) => void;
  onViewAll: () => void;
}) {
  return (
    <div className="page home-page">
      <header className="page-heading">
        <div>
          <div className="eyebrow">会议工作台</div>
          <h1>每一场会议，都有依据可查、重点可回看。</h1>
          <p>实时转写、区分说话人、结合资料生成回应建议，并把关键内容留给会后复盘。</p>
        </div>
        <button className="button primary large" onClick={onPrepare}>
          <Mic2 size={18} /> 新建会议
        </button>
      </header>

      <section className="readiness-strip">
          <div className="readiness-title">
           <Radio size={18} />
           <div>
             <strong>本机服务状态</strong>
             <span>桌面端、语音服务和建议服务</span>
           </div>
         </div>
         <StatusPill ok={runtime.desktop} label="桌面端" />
         <StatusPill ok={runtime.pythonReady} label="语音服务" />
         <StatusPill ok={runtime.bridgeReady} label="建议服务" />
         <StatusPill ok={runtime.configPresent} label="本地配置" />
      </section>

      <div className="home-grid">
        <section className="recent-panel">
          <div className="section-heading">
            <div>
              <h2>最近会议</h2>
              <p>记录只保存在这台电脑上</p>
            </div>
            <button className="text-button" onClick={onViewAll}>
              查看全部 <ChevronRight size={15} />
            </button>
          </div>
          {records.length === 0 ? (
            <div className="recent-empty">
              <Archive size={20} />
              <strong>还没有会议记录</strong>
              <span>结束第一场会议后，转写和话术建议会出现在这里。</span>
            </div>
          ) : (
            records.slice(0, 3).map((record, index) => {
              const stats = meetingStats(record);
              const date = new Date(record.startedAt);
              return (
                <button
                  className="meeting-row"
                  key={record.id}
                  onClick={() => onOpenRecord(record.id)}
                >
                  <span className={`meeting-date ${index === 0 ? "" : "quiet"}`}>
                    <strong>{date.getDate()}</strong>
                    <small>{date.getMonth() + 1}月</small>
                  </span>
                  <span className="meeting-row-main">
                    <strong>{record.title}</strong>
                    <small>
                      {formatDuration(record)} · {stats.speakers} 位说话人 ·{" "}
                      {stats.suggestions} 条建议
                    </small>
                  </span>
                  <span className="meeting-row-tail">
                    {formatRecordDate(record.startedAt)}
                    <ChevronRight size={16} />
                  </span>
                </button>
              );
            })
          )}
        </section>

        <aside className="brief-panel quick-start-panel">
           <div className="section-heading">
             <div>
               <h2>快速开始</h2>
               <p>选择会议方式，马上进入会前准备</p>
             </div>
           </div>
           <button
             className="quick-start-option"
             onClick={() => onQuickStart("in_person")}
           >
             <Mic2 size={18} />
             <span>
               <strong>线下会议</strong>
               <small>一个麦克风，识别我与其他说话人。</small>
             </span>
             <ChevronRight size={15} />
           </button>
           <button
             className="quick-start-option"
             onClick={() => onQuickStart("online")}
           >
             <Headphones size={18} />
             <span>
               <strong>线上会议</strong>
               <small>分别采集麦克风与系统声音。</small>
             </span>
             <ChevronRight size={15} />
           </button>
           <button className="button secondary full" onClick={onPrepare}>
             自定义会前准备
           </button>
         </aside>
      </div>
    </div>
  );
}

function StatusPill({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span className={`status-pill ${ok ? "ok" : "pending"}`}>
      {ok ? <Check size={13} /> : <Clock3 size={13} />}
      {label}
    </span>
  );
}

function CheckLine({
  checked,
  label,
  detail,
}: {
  checked: boolean;
  label: string;
  detail: string;
}) {
  return (
    <div className="check-line">
      <span className={`check-dot ${checked ? "checked" : ""}`}>
        {checked ? <Check size={13} /> : <Clock3 size={13} />}
      </span>
      <span>
        <strong>{label}</strong>
        <small>{detail}</small>
      </span>
    </div>
  );
}

function PrepareScreen({
  persisted,
  devices,
  deviceError,
  onChange,
  onSelectScene,
  onSelectDevice,
  onRefreshDevices,
  onTestDevice,
  deviceTestStatus,
  onStart,
  sceneRecommendation,
  projects,
  activeProjectId,
  onSelectProject,
  documents,
  selectedDocIds,
  onToggleDoc,
  onSelectAllDocs,
  onClearDocs,
  onManageKnowledge,
  onManageProjects,
  warmupLabel,
  onWarmup,
}: {
  persisted: PersistedState;
  devices: InputDevice[];
  deviceError: string;
  onChange: (state: PersistedState) => void;
  onSelectScene: (scene: MeetingScene) => void;
  onSelectDevice: (device: number) => void;
  onRefreshDevices: () => void;
  onTestDevice: () => void;
  deviceTestStatus: "idle" | "testing" | "success" | "error";
  onStart: () => void;
  sceneRecommendation: SceneRecommendation | null;
  projects: Project[];
  activeProjectId: string | null;
  onSelectProject: (id: string | null) => void;
  documents: KnowledgeDocument[];
  selectedDocIds: string[];
  onToggleDoc: (id: string) => void;
  onSelectAllDocs: () => void;
  onClearDocs: () => void;
  onManageKnowledge: () => void;
  onManageProjects: () => void;
  warmupLabel: string;
  onWarmup: () => void;
}) {
  useEffect(() => {
    if (devices.length === 0) void onRefreshDevices();
  }, []);
  useEffect(() => {
    void onWarmup();
  }, [activeProjectId, persisted.asrProvider]);
  return (
    <div className="page narrow-page">
      <header className="page-heading compact">
        <div>
          <div className="eyebrow">会前准备</div>
          <h1>确认这场会议听什么、参考什么。</h1>
          <p>开始后仍可修改说话人和暂停自动建议。进入本页会自动预热热词与依赖，缩短点「开始」后的等待。</p>
        </div>
      </header>
      <div className="warmup-banner">
        <Radio size={16} />
        <div>
          <strong>会前预热</strong>
          <span>{warmupLabel}</span>
        </div>
        <button type="button" className="button ghost small" onClick={onWarmup}>
          重新预热
        </button>
      </div>
      <div className="form-section">
        <div className="form-section-label">
          <MessageSquareText size={18} />
          <div>
            <strong>会议信息</strong>
            <span>用于本地记录和会后查找</span>
          </div>
        </div>
        <div className="form-fields">
          <label className="field">
            <span>会议标题</span>
            <input
              value={persisted.meetingTitle}
              placeholder="例如：XX 项目需求澄清会"
              onChange={(event) =>
                onChange({ ...persisted, meetingTitle: event.target.value })
              }
            />
          </label>
          <label className="field">
            <span>本场识别语种</span>
            <select
              value={persisted.asrLang || "zh_en"}
              onChange={(event) =>
                onChange({
                  ...persisted,
                  asrLang: event.target.value as "zh" | "en" | "zh_en",
                })
              }
              title="中文为主的线下会议建议选中文；有完整英文句子时选中英混用"
            >
              <option value="zh">中文（中文会议推荐）</option>
              <option value="en">英文</option>
              <option value="zh_en">中英混用</option>
            </select>
            <small className="field-hint">
              本场会议会使用该选择；KMS、EKP、Markdown 等专有名词仍交给热词词表处理。
            </small>
          </label>
        </div>
      </div>
      <div className="form-section scene-section">
        <div className="form-section-label">
          <Sparkles size={18} />
          <div>
            <strong>会议场景</strong>
            <span>场景会影响建议分类、追问重点和会后纪要结构</span>
          </div>
        </div>
        <div className="scene-picker-wrap">
          {sceneRecommendation && (
            <div className="scene-recommendation">
              <span className="scene-recommendation-mark">推荐</span>
              <span>
                <strong>{sceneRecommendation.label}</strong>
                <small>{sceneRecommendation.reason}</small>
              </span>
            </div>
          )}
          <div className="scene-picker" role="radiogroup" aria-label="会议场景">
            {(Object.entries(SCENE_META) as Array<[MeetingScene, (typeof SCENE_META)[MeetingScene]]>).map(
              ([scene, meta]) => (
                <button
                  key={scene}
                  type="button"
                  className={
                    (persisted.scene || "general") === scene ? "active" : ""
                  }
                  onClick={() => onSelectScene(scene)}
                  role="radio"
                  aria-checked={(persisted.scene || "general") === scene}
                >
                  <strong>{meta.label}</strong>
                  <small>{meta.description}</small>
                  {sceneRecommendation?.scene === scene && <em>建议采用</em>}
                </button>
              ),
            )}
          </div>
        </div>
      </div>
      <div className="form-section">
        <div className="form-section-label">
          <Radio size={18} />
          <div>
            <strong>会议方式</strong>
            <span>决定如何识别“我”和“对方”</span>
          </div>
        </div>
        <div className="meeting-mode-picker-wrap">
          <div className="meeting-mode-picker">
            <button
              className={(persisted.meetingMode || "in_person") === "in_person" ? "active" : ""}
              onClick={() =>
                onChange({ ...persisted, meetingMode: "in_person" })
              }
            >
              <Mic2 size={17} />
              <span>
                <strong>线下会议</strong>
                <small>一个麦克风，使用说话人识别</small>
              </span>
            </button>
            <button
              className={persisted.meetingMode === "online" ? "active" : ""}
              onClick={() => onChange({ ...persisted, meetingMode: "online" })}
            >
              <Headphones size={17} />
              <span>
                <strong>线上会议</strong>
                <small>麦克风是我，系统播放是对方</small>
              </span>
            </button>
          </div>
          {persisted.meetingMode === "online" && (
            <div className="online-mode-note">
              自动捕获 Windows 当前默认播放设备。请使用耳机，避免扬声器声音再次进入麦克风；
              两路独立转写会产生约双倍 ASR 用量。
            </div>
          )}
        </div>
      </div>
      <div className="form-section">
        <div className="form-section-label">
          <Headphones size={18} />
          <div>
            <strong>{persisted.meetingMode === "online" ? "我的麦克风" : "麦克风"}</strong>
            <span>
              {persisted.meetingMode === "online"
                ? "这一路转写将固定标记为“我”"
                : "由本机语音服务直接采集"}
            </span>
          </div>
        </div>
        <div className="form-fields">
          <label className="field">
            <span>输入设备</span>
            <select
              value={persisted.selectedDevice ?? ""}
              onChange={(event) => onSelectDevice(Number(event.target.value))}
            >
              <option value="" disabled>
                请选择麦克风
              </option>
              {devices.map((device) => (
                <option key={device.index} value={device.index}>
                  {device.name}
                  {device.isDefault ? "（默认）" : ""}
                </option>
              ))}
            </select>
          </label>
          <div className="device-check">
            <DeviceLevelBars />
            <span>
              {deviceTestStatus === "testing" && "正在测试，请对着麦克风说话…"}
              {deviceTestStatus === "success" && "测试完成，设备可以正常拾音"}
              {deviceTestStatus === "error" && "测试失败，请更换设备或查看启动窗口"}
              {deviceTestStatus === "idle" &&
                (devices.length ? "设备已识别，建议开始前测试一次" : "正在读取设备…")}
            </span>
            <button
              className="button ghost small"
              onClick={onTestDevice}
              disabled={deviceTestStatus === "testing" || persisted.selectedDevice === undefined}
            >
              <Volume2 size={14} />
              {deviceTestStatus === "testing" ? "测试中" : "测试麦克风"}
            </button>
            <button className="text-button" onClick={onRefreshDevices}>
              重新检测
            </button>
          </div>
          {deviceError && <div className="inline-error">{deviceError}</div>}
        </div>
      </div>
      <div className="form-section">
        <div className="form-section-label">
          <FolderKanban size={18} />
          <div>
            <strong>所属项目</strong>
            <span>可选。不选则本场会议不归属任何项目</span>
          </div>
          {projects.length > 0 && (
            <button className="button ghost" onClick={onManageProjects}>
              管理项目
            </button>
          )}
        </div>
        <div className="project-picker-wrap">
          <div className="project-picker">
            <button
              type="button"
              className={`project-chip optional ${
                activeProjectId === null ? "active" : ""
              }`}
              onClick={() => onSelectProject(null)}
            >
              <strong>不归属项目</strong>
              <small>从知识库任选资料</small>
            </button>
            {projects.map((project) => (
              <button
                type="button"
                key={project.id}
                className={`project-chip ${
                  project.id === activeProjectId ? "active" : ""
                }`}
                onClick={() => onSelectProject(project.id)}
              >
                <strong>{project.name}</strong>
                <small>{project.documentCount} 份文档</small>
              </button>
            ))}
          </div>
          {projects.length === 0 && (
            <p className="field-hint" style={{ marginTop: 10 }}>
              还没有项目。可直接开会，或去{" "}
              <button type="button" className="text-button" onClick={onManageProjects}>
                项目
              </button>{" "}
              页创建。
            </p>
          )}
        </div>
      </div>
      <div className="form-section">
        <div className="form-section-label">
          <BookOpen size={18} />
          <div>
            <strong>本场参考文档</strong>
            <span>
              {activeProjectId
                ? `仅列出该项目的可用资料，已自动全选（${selectedDocIds.length}/${documents.length}）；只有勾选的会进入检索`
                : `未选项目时列出知识库全部文档，默认不勾选（已选 ${selectedDocIds.length}）；只有勾选的会进入检索`}
            </span>
          </div>
          <button className="button ghost" onClick={onManageKnowledge}>
            知识库
          </button>
        </div>
        <div className="document-picker-wrap">
          {documents.length > 0 && (
            <div className="settings-row" style={{ gap: 8, marginBottom: 10 }}>
              <button
                type="button"
                className="button ghost small"
                onClick={onSelectAllDocs}
                disabled={documents.filter((d) => d.exists).length === 0}
              >
                全选
              </button>
              <button
                type="button"
                className="button ghost small"
                onClick={onClearDocs}
                disabled={selectedDocIds.length === 0}
              >
                全部取消勾选
              </button>
            </div>
          )}
          {documents.length === 0 ? (
            <div className="empty-hint">
              {activeProjectId
                ? "该项目还没有可用资料。可到项目页勾选，或本场不选文档开会。"
                : "知识库为空。未选文档时 AI 只给经验建议，不会引用资料。"}
              <button className="button ghost" onClick={onManageKnowledge}>
                去知识库
              </button>
            </div>
          ) : (
            <div className="document-list">
              {documents.map((doc) => {
                const checked = selectedDocIds.includes(doc.id);
                return (
                  <label
                    className={`document-check ${doc.exists ? "" : "missing"}`}
                    key={doc.id}
                  >
                    <input
                      type="checkbox"
                      checked={checked && doc.exists}
                      disabled={!doc.exists}
                      onChange={() => onToggleDoc(doc.id)}
                    />
                    <span className="custom-check">
                      <Check size={13} />
                    </span>
                    <FileText size={17} />
                    <span>
                      <strong>{doc.name}</strong>
                      <small>
                        {doc.exists
                          ? activeProjectId
                            ? "项目可用资料 · 本地关键词索引"
                            : "知识库 · 本地关键词索引"
                          : "⚠️ 原文件已移动或删除，无法使用"}
                      </small>
                    </span>
                  </label>
                );
              })}
            </div>
          )}
        </div>
      </div>
      <div className="sticky-actions">
        <div>
          <ShieldAlert size={16} />
          请确保已告知参会方会议会被录音
        </div>
        <button
          className="button primary large"
          onClick={onStart}
          disabled={!persisted.meetingTitle.trim() || devices.length === 0}
        >
          <Mic2 size={18} /> 开始会议
        </button>
      </div>
    </div>
  );
}

function MeetingScreen({
  title,
  status,
  statusMessage,
  recordingCue,
  startedAt,
  endedAt,
  transcript,
  batches,
  scene,
  memoryItems,
  onUpdateMemory,
  recordingPaused,
  suggestionsPaused,
  onAsk,
  onSuggestNow,
  suggesting,
  onOpenFloating,
  onPauseRecording,
  onPauseSuggestions,
  onStop,
  onCopy,
  onAdopt,
  asrConnection,
  resolveSpeakerName,
  resolveSpeakerKey,
  onOpenSpeakerMenu,
}: {
  title: string;
  status: string;
  statusMessage: string;
  recordingCue: boolean;
  startedAt: number | null;
  endedAt?: number;
  transcript: TranscriptItem[];
  batches: SuggestionBatch[];
  scene: MeetingScene;
  memoryItems: MeetingMemoryItem[];
  recordingPaused: boolean;
  suggestionsPaused: boolean;
  onAsk: (value: string) => void;
  onSuggestNow: () => void;
  suggesting: boolean;
  onOpenFloating: () => void;
  onPauseRecording: () => void;
  onPauseSuggestions: () => void;
  onStop: () => void;
  onCopy: (value: string) => void;
  onAdopt: (batchId: string, position: number) => void;
  onUpdateMemory: (id: string, patch: Partial<MeetingMemoryItem>) => void;
  asrConnection: { state: string; message: string } | null;
  resolveSpeakerName: (item: TranscriptItem) => string;
  resolveSpeakerKey: (item: TranscriptItem) => string;
  onOpenSpeakerMenu: (
    speakerId: string,
    itemId: string,
    segmentIds: string[],
    rect: DOMRect,
    scope: "segment" | "speaker",
  ) => void;
}) {
  // TRS-7：新内容自动滚到底；用户上翻查看历史时暂停自动滚动，
  // 避免"正在读旧内容却被强行拽到最新"。
  const streamRef = useRef<HTMLDivElement>(null);
  const suggestionStreamRef = useRef<HTMLDivElement>(null);
  const transcriptItemRefs = useRef(new Map<string, HTMLElement>());
  const suggestionBatchRefs = useRef(new Map<string, HTMLElement>());
  const [pinnedToBottom, setPinnedToBottom] = useState(true);
  const [locatedTranscriptIds, setLocatedTranscriptIds] = useState<string[]>([]);
  const [locatedBatchId, setLocatedBatchId] = useState<string | null>(null);

  const displayTranscript = useMemo(
    () => mergeConsecutiveTranscript(transcript, resolveSpeakerKey),
    [transcript, resolveSpeakerKey],
  );

  function locateSuggestionBatch(batch: SuggestionBatch) {
    const location = findTranscriptIdsForContext(
      transcript,
      getSuggestionContext(batch),
    );
    if (!location.ids.length) return;
    setLocatedTranscriptIds(location.ids);
    setLocatedBatchId(batch.id);
    window.requestAnimationFrame(() => {
      transcriptItemRefs.current
        .get(location.targetId || location.ids.at(-1) || "")
        ?.scrollIntoView({ block: "center", behavior: "smooth" });
    });
  }

  function locateSuggestionFromTranscript(item: DisplayUtterance) {
    const match = findNearestSuggestionBatchForTranscript(
      transcript,
      batches,
      item.segmentIds,
    );
    if (!match) return;
    const contextSelection = findTranscriptIdsForContext(
      transcript,
      match.context,
    );
    setLocatedTranscriptIds(
      contextSelection.ids.length ? contextSelection.ids : item.segmentIds,
    );
    setLocatedBatchId(match.batch.id);
    window.requestAnimationFrame(() => {
      suggestionBatchRefs.current
        .get(match.batch.id)
        ?.scrollIntoView({ block: "center", behavior: "smooth" });
    });
  }

  useEffect(() => {
    const el = streamRef.current;
    if (el && pinnedToBottom) el.scrollTop = el.scrollHeight;
  }, [displayTranscript, pinnedToBottom]);

  function onStreamScroll() {
    const el = streamRef.current;
    if (!el) return;
    const atBottom =
      el.scrollHeight - el.scrollTop - el.clientHeight < 48;
    setPinnedToBottom(atBottom);
  }

  function jumpToLatest() {
    const el = streamRef.current;
    if (el) el.scrollTop = el.scrollHeight;
    setPinnedToBottom(true);
  }

  return (
    <div className="meeting-console">
      {status === "starting" && (
        <div className="meeting-prepare-overlay" role="status">
          <div className="meeting-prepare-card">
            <div className="eyebrow">会议尚未正式开始</div>
            <h2>{recordingCue ? "即将开始录制" : "正在准备"}</h2>
            <p>
              {recordingCue
                ? "设备与模型已就绪。确认提示后将开始录音、转写与计时。"
                : "正在加载知识库、声纹、专有名词并连接转写服务。此阶段不会记入会议时长，也不会作为录音零点。"}
            </p>
            <div className="meeting-prepare-status">
              <span className={`pulse-dot ${recordingCue ? "ready" : ""}`} />
              <strong>{statusMessage}</strong>
            </div>
            {recordingCue && (
              <div className="meeting-prepare-cue">正式开始录制</div>
            )}
          </div>
        </div>
      )}
      <header className="transport">
        <div
          className={`record-indicator ${
            status === "live" && !recordingPaused ? "live" : ""
          } ${recordingPaused ? "paused" : ""} ${
            status === "starting" ? "preparing" : ""
          }`}
        >
          <span />
        </div>
        <div className="transport-title">
          <strong>{title}</strong>
          <span>
            {status === "starting"
              ? recordingCue
                ? "准备完成 · 即将开录"
                : "准备中 · 尚未计时"
              : statusMessage}
          </span>
        </div>
        <Timecode startedAt={startedAt} endedAt={endedAt} />
        <LiveMeter />
        <div className="transport-spacer" />
        <button
          className={`button ghost recording-control ${
            recordingPaused ? "paused" : ""
          }`}
          onClick={onPauseRecording}
          disabled={status !== "live"}
        >
          {recordingPaused ? <Mic2 size={16} /> : <MicOff size={16} />}
          {recordingPaused ? "恢复录制" : "暂停录制"}
        </button>
        <button className="button ghost" onClick={onPauseSuggestions}>
          {suggestionsPaused ? <Play size={16} /> : <Pause size={16} />}
          {suggestionsPaused ? "恢复建议" : "暂停建议"}
        </button>
        <button
          className="button danger"
          onClick={onStop}
          disabled={status === "stopping" || status === "ended"}
        >
          <CircleStop size={16} /> 结束会议
        </button>
      </header>
      <div className="channel-grid">
        <section className="channel transcript-channel">
          <header className="channel-heading">
            <div>
              <span className="channel-dot cyan" />
              <strong>实时转写</strong>
            </div>
            <span>
              {displayTranscript.filter((item) => item.isFinal).length} 段发言
            </span>
          </header>
          {asrConnection && (
            <div
              className={`asr-banner ${
                asrConnection.state === "failed" ? "failed" : ""
              }`}
            >
              <ShieldAlert size={15} />
              <span>
                <strong>
                  {asrConnection.state === "failed"
                    ? "转写已停止"
                    : "转写连接中断，正在重连"}
                </strong>
                <small>{asrConnection.message}</small>
              </span>
              <em>录音仍在继续，这段内容会后可回听</em>
            </div>
          )}
          <div
            className="channel-stream"
            ref={streamRef}
            onScroll={onStreamScroll}
          >
            {displayTranscript.length === 0 ? (
              <EmptyState
                icon={Volume2}
                title="等待第一段声音"
                detail="开始说话后，中间结果会先以浅色显示。"
              />
            ) : (
              displayTranscript.map((item) => (
                <Utterance
                  key={item.segmentIds.join("-") || item.id}
                  item={item}
                  shown={resolveSpeakerName(item)}
                  colorKey={resolveSpeakerKey(item)}
                  isLocated={item.segmentIds.some((id) =>
                    locatedTranscriptIds.includes(id),
                  )}
                  canLocateSuggestion={Boolean(
                    findNearestSuggestionBatchForTranscript(
                      transcript,
                      batches,
                      item.segmentIds,
                    ),
                  )}
                  onLocateSuggestion={locateSuggestionFromTranscript}
                  onRegister={(node) => {
                    for (const id of item.segmentIds) {
                      if (node) transcriptItemRefs.current.set(id, node);
                      else transcriptItemRefs.current.delete(id);
                    }
                  }}
                  onOpenSpeakerMenu={onOpenSpeakerMenu}
                />
              ))
            )}
          </div>
          {!pinnedToBottom && displayTranscript.length > 0 && (
            <button className="jump-latest" onClick={jumpToLatest}>
              <ChevronDown size={14} /> 回到最新
            </button>
          )}
        </section>
        <section className="channel suggestion-channel">
          <header className="channel-heading">
            <div>
              <span className="channel-dot brass" />
              <strong>AI 话术建议</strong>
            </div>
            <div className="suggestion-heading-actions">
              <button
                className="button ghost small floating-launch"
                onClick={onOpenFloating}
                title="打开始终置顶的精简应答策略窗"
              >
                <PictureInPicture2 size={14} /> 悬浮策略
              </button>
            {/*
              自动建议受冷却与增量闸门限制（防刷屏），代价是用户"现在就想要"
              的时候可能要等。因此给一个手动出口，绕过那些闸门立即生成。

              ⚠️ 这颗按钮原来是 ghost tiny，在会议中几乎看不见，点下去也没有
                 任何反馈（生成要 6-8 秒，用户不知道点上没有）。开会时它是
                 用户唯一的主动出口，必须一眼能找到、点了立刻有回应。
            */}
            <button
              className={`button primary small suggest-now ${suggesting ? "is-busy" : ""}`}
              onClick={onSuggestNow}
              disabled={status !== "live" || suggesting}
              title={
                suggesting
                  ? "正在生成，请稍候"
                  : "立即基于最近的对话生成一批建议（绕过等待与冷却）"
              }
            >
              {suggesting ? (
                <>
                  <Loader2 size={14} className="spin" /> 生成中…
                </>
              ) : (
                <>
                  <Wand2 size={14} /> 现在给建议
                </>
              )}
            </button>
            </div>
          </header>
          <div
            className="channel-stream suggestion-stream"
            ref={suggestionStreamRef}
          >
            {/* 生成中占位：6-8 秒的空白必须有东西顶着，否则用户以为没点上 */}
            {suggesting && (
              <div className="suggestion-generating">
                <Loader2 size={14} className="spin" />
                <span>正在结合最近的对话生成建议…</span>
              </div>
            )}
            {suggestionsPaused && (
              <div className="suggestion-paused-note">
                <Pause size={14} />
                <span>
                  <strong>自动建议已暂停</strong>
                  {recordingPaused
                    ? "已随录制同步暂停；仍可单独恢复建议并继续手动提问。"
                    : "历史建议仍可查看；恢复后才会生成新建议。"}
                </span>
              </div>
            )}
            {batches.length === 0 ? (
              <EmptyState
                icon={suggestionsPaused ? Pause : Sparkles}
                title={suggestionsPaused ? "暂无历史建议" : "等待对方发言结束"}
                detail={
                  suggestionsPaused
                    ? "恢复自动建议后，新建议会显示在这里。"
                    : "建议会在停顿约 2 秒后生成。"
                }
              />
            ) : (
              batches.map((batch, index) => (
                <SuggestionBatchGroup
                  key={batch.id}
                  batch={batch}
                  isLatest={index === 0}
                  onCopy={onCopy}
                  onAdopt={onAdopt}
                  onRetry={onSuggestNow}
                  onLocateTranscript={locateSuggestionBatch}
                  canLocateTranscript={transcript.length > 0}
                  isLocated={locatedBatchId === batch.id}
                  batchRef={(node) => {
                    if (node) suggestionBatchRefs.current.set(batch.id, node);
                    else suggestionBatchRefs.current.delete(batch.id);
                  }}
                />
              ))
            )}
          </div>
          <AskDock onAsk={onAsk} />
          <MemorySidebar
            scene={scene}
            items={memoryItems}
            onUpdate={onUpdateMemory}
          />
        </section>
      </div>
    </div>
  );
}

function MemorySidebar({
  scene,
  items,
  onUpdate,
}: {
  scene: MeetingScene;
  items: MeetingMemoryItem[];
  onUpdate: (id: string, patch: Partial<MeetingMemoryItem>) => void;
}) {
  const [open, setOpen] = useState(true);
  const [editing, setEditing] = useState<{ id: string; value: string } | null>(null);
  const visible = items.filter((item) => item.status !== "rejected");
  return (
    <aside className={`memory-sidebar ${open ? "open" : "collapsed"}`}>
      <button
        type="button"
        className="memory-sidebar-toggle"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
      >
        <span>
          <ListChecks size={15} />
          <strong>决策与待办</strong>
          <em>{visible.length}</em>
        </span>
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
      </button>
      {open && (
        <div className="memory-sidebar-body">
          <p className="memory-sidebar-hint">
            {SCENE_META[scene].short}场景候选会结合明确语句提取；确认后才会进入正式复盘。
          </p>
          {visible.length === 0 ? (
            <div className="memory-empty">出现“确定 / 负责 / 截止”等明确表达后，会显示在这里。</div>
          ) : (
            visible.map((item) => (
              <article className={`memory-item ${item.status}`} key={item.id}>
                <div className="memory-item-top">
                  <span className={`memory-kind ${item.kind}`}>
                    {item.kind === "decision" ? "决策" : "待办"}
                  </span>
                  <span className="memory-status">
                    {item.status === "confirmed" ? "已确认" : "候选"}
                  </span>
                </div>
                {editing?.id === item.id ? (
                  <div className="memory-edit-row">
                    <input
                      autoFocus
                      value={editing.value}
                      onChange={(event) => setEditing({ ...editing, value: event.target.value })}
                    />
                    <button
                      type="button"
                      className="icon-button"
                      onClick={() => {
                        onUpdate(item.id, { content: editing.value, status: "candidate" });
                        setEditing(null);
                      }}
                    >
                      <Check size={13} />
                    </button>
                  </div>
                ) : (
                  <p>{item.content}</p>
                )}
                {(item.owner || item.dueAt) && (
                  <small className="memory-meta">
                    {[item.owner && `负责人 ${item.owner}`, item.dueAt && `截止 ${item.dueAt}`]
                      .filter(Boolean)
                      .join(" · ")}
                  </small>
                )}
                <div className="memory-item-actions">
                  <button type="button" onClick={() => onUpdate(item.id, { status: "confirmed" })}>
                    <Check size={12} /> 确认
                  </button>
                  <button type="button" onClick={() => setEditing({ id: item.id, value: item.content })}>
                    <Pencil size={12} /> 编辑
                  </button>
                  <button type="button" onClick={() => onUpdate(item.id, { status: "rejected" })}>
                    <X size={12} /> 驳回
                  </button>
                </div>
              </article>
            ))
          )}
        </div>
      )}
    </aside>
  );
}

/**
 * 一批建议。memo 化：会中每来一段转写就要重渲染建议栏，而历史批次是不变的。
 */
const SuggestionBatchGroup = memo(function SuggestionBatchGroup({
  batch,
  isLatest,
  latestLabel = "本轮",
  onCopy,
  onAdopt,
  onRetry,
  onLocateTranscript,
  canLocateTranscript = true,
  batchRef,
  isLocated = false,
}: {
  batch: SuggestionBatch;
  isLatest: boolean;
  latestLabel?: string;
  onCopy: (value: string) => void;
  /** 提供则「采纳」按钮可交互（会议中）；不提供则只读展示采纳标记（历史） */
  onAdopt?: (batchId: string, position: number) => void;
  /** 生成失败时的重试入口；历史回看不提供 */
  onRetry?: () => void;
  /** 定位到本批建议所依据的转写上下文 */
  onLocateTranscript?: (batch: SuggestionBatch) => void;
  canLocateTranscript?: boolean;
  batchRef?: (node: HTMLElement | null) => void;
  isLocated?: boolean;
}) {
  // 采纳后同批其余条目降权，让"我当时选了哪条"一眼可见（此前只有边框色差别）
  const adoptedIndex = batch.suggestions.findIndex((s) => s.adopted);
  return (
    <section
      className={`suggestion-batch ${isLatest ? "latest" : ""} ${
        isLocated ? "located" : ""
      }`}
      ref={batchRef}
    >
      <div className="batch-heading">
        <span className={`batch-marker ${isLatest ? "latest" : ""}`}>
          {isLatest ? latestLabel : formatTime(batch.at).slice(0, 5)}
        </span>
        <span className="batch-title">
          <strong>
            {batch.parseError
              ? "本轮生成失败"
              : `${batch.suggestions.length} 条话术建议`}
          </strong>
          <small>
            {formatTime(batch.at)} 生成 · 耗时 {batch.elapsed.toFixed(1)}s
            {batch.runtime?.model ? ` · ${batch.runtime.model}` : ""}
            {batch.runtime?.mergeCount ? ` · 合并 ${batch.runtime.mergeCount} 次` : ""}
          </small>
        </span>
      </div>
      {/*
        生成失败必须长得【不像建议】。旧实现把模型的原始输出塞进话术位，
        它和正常建议外观完全一致，用户可能照着念出去（真机验证中出现过）。
        原始输出只收进可折叠的诊断区，供排查用。
      */}
      {batch.parseError && (
        <div className="batch-failure">
          <div className="batch-failure-head">
            <ShieldAlert size={15} />
            <span>模型没有返回可用的建议，本轮已跳过。</span>
            {onRetry && (
              <button className="button ghost small" onClick={onRetry}>
                重试
              </button>
            )}
          </div>
          <details>
            <summary>查看诊断信息</summary>
            <p>{batch.parseError.message}</p>
            {batch.parseError.raw && <pre>{batch.parseError.raw}</pre>}
          </details>
        </div>
      )}
      <div className="batch-body">
        {batch.suggestions.map((suggestion, index) => (
          <SuggestionCard
            key={`${batch.id}-${index}`}
            suggestion={suggestion}
            position={index + 1}
            total={batch.suggestions.length}
            onCopy={onCopy}
            hits={batch.hits}
            // 同批已有采纳、且不是这一条 → 降权显示
            dimmed={adoptedIndex >= 0 && adoptedIndex !== index}
            onAdopt={onAdopt ? () => onAdopt(batch.id, index) : undefined}
            onLocateTranscript={
              onLocateTranscript ? () => onLocateTranscript(batch) : undefined
            }
            locateDisabled={!canLocateTranscript}
          />
        ))}
      </div>
    </section>
  );
});

function SuggestionCard({
  suggestion,
  position,
  total,
  onCopy,
  hits = [],
  dimmed = false,
  onAdopt,
  onLocateTranscript,
  locateDisabled = false,
}: {
  suggestion: Suggestion;
  position: number;
  total: number;
  onCopy: (value: string) => void;
  /** 本批次检索到的片段，用于点击引用时展示原文 */
  hits?: ReferenceHit[];
  /** 同批已有别的条目被采纳 → 本条降权，突出"当时选的是哪条" */
  dimmed?: boolean;
  /** 提供则「采纳」按钮可点（会议中）；不提供则只读展示 */
  onAdopt?: () => void;
  /** 定位到该批建议关联的转写 */
  onLocateTranscript?: () => void;
  locateDisabled?: boolean;
}) {
  const level = levelOf(suggestion);
  const [openRef, setOpenRef] = useState<string | null>(null);
  const badge = {
    grounded: ["有依据", Check],
    advisory: ["经验建议", Sparkles],
    clarify: ["仅澄清", ShieldAlert],
  } as const;
  const [label, Icon] = badge[level];
  return (
    <article
      className={`suggestion-card ${level} ${
        suggestion.adopted ? "adopted" : ""
      } ${dimmed ? "dimmed" : ""}`}
    >
      <div className="suggestion-top">
        <div className="suggestion-identity">
          <span className="suggestion-order">
            建议 {position}/{total}
          </span>
          <span className={`evidence-badge ${level}`}>
            <Icon size={13} /> {label}
          </span>
          {suggestion.adopted && (
            <span className="adopted-badge">
              <Check size={12} /> 已采纳
            </span>
          )}
        </div>
        <div className="suggestion-actions">
          {onAdopt && (
            <button
              className={`icon-button adopt-button ${
                suggestion.adopted ? "on" : ""
              }`}
              onClick={onAdopt}
              title="标记为我当时采纳的话术（每批只能采纳一条）"
            >
              <Check size={15} />{" "}
              <span>{suggestion.adopted ? "取消采纳" : "采纳"}</span>
            </button>
          )}
          {onLocateTranscript && (
            <button
              className="icon-button locate-suggestion-button"
              onClick={onLocateTranscript}
              disabled={locateDisabled}
              title={
                locateDisabled
                  ? "这场会议没有可定位的转写"
                  : "定位到这批话术关联的转写"
              }
            >
              <MessageSquareText size={14} /> <span>定位转写</span>
            </button>
          )}
          <button className="icon-button" onClick={() => onCopy(suggestion.script)}>
            <Copy size={15} /> <span>复制</span>
          </button>
        </div>
      </div>
      <div className="intent-line">
        {suggestion.category && <span className="suggestion-category">{suggestion.category}</span>}
        {suggestion.intent}
      </div>
      <p className="script-line">{suggestion.script}</p>
      {/*
        校验改判理由。模型的 type 自评不可信，后端 _validate() 会核对引用是否
        真的出现在本次检索结果里并据此降级 —— 降级的【理由】必须让用户看见，
        否则他只知道结果是"仅澄清"，不知道是因为模型编了个不存在的出处。
      */}
      {suggestion.notice && (
        <div className="suggestion-notice">
          <ShieldAlert size={13} />
          <span>{suggestion.notice}</span>
        </div>
      )}
      {/* 内部资料/承诺性表述提醒：标注而非拦截，说不说由用户判断 */}
      {suggestion.sensitive && (
        <div className="suggestion-notice sensitive">
          <ShieldAlert size={13} />
          <span>{suggestion.sensitive}</span>
        </div>
      )}
      {suggestion.evidence && suggestion.evidence.length > 0 && (
        <div className="verified-evidence">
          <div className="verified-evidence-label">
            <Check size={13} />
            <strong>原文已核验</strong>
          </div>
          {suggestion.evidence.map((evidence, index) => (
            <div
              className="verified-evidence-quote"
              key={`${evidence.source}-${index}`}
            >
              <q>{evidence.quote}</q>
              <small>{evidence.source}</small>
            </div>
          ))}
        </div>
      )}
      {suggestion.references && suggestion.references.length > 0 && (
        <div className="references">
          <BookOpen size={14} />
          {suggestion.references.map((reference) => (
            <button
              key={reference}
              className={openRef === reference ? "active" : ""}
              onClick={() =>
                setOpenRef(openRef === reference ? null : reference)
              }
            >
              {reference}
            </button>
          ))}
        </div>
      )}
      {/*
        RAG-3：点击引用展开【原文片段】，而不只是文件名。
        ⚠️ 这是 M1 验证出的关键防线：模型会引用真实文档但内容是编的
        （实测把"金额分级审批"案例说成"审计存证"案例）。只显示文件名时
        用户无从判断；显示原文，一眼就能看出对不上。
      */}
      {openRef && (
        <div className="reference-excerpt">
          {(() => {
            const hit = hits.find((item) => item.source === openRef);
            if (!hit) {
              return (
                <span className="excerpt-missing">
                  该片段未随本次检索返回，无法核对原文
                </span>
              );
            }
            return (
              <>
                <strong>{hit.source}</strong>
                <p>{hit.text}</p>
                <small>请核对上面的话术是否确实出自这段原文</small>
              </>
            );
          })()}
        </div>
      )}
    </article>
  );
}

function EmptyState({
  icon: Icon,
  title,
  detail,
}: {
  icon: typeof Sparkles;
  title: string;
  detail: string;
}) {
  return (
    <div className="empty-state">
      <span>
        <Icon size={22} />
      </span>
      <strong>{title}</strong>
      <p>{detail}</p>
    </div>
  );
}

function HistoryScreen({
  records,
  liveMeetingId,
  onOpen,
  onFinalizeStale,
  onDelete,
}: {
  records: MeetingRecord[];
  /** 本次运行真正在跑的那场；其余的 active 都是遗留残留 */
  liveMeetingId: string | null;
  onOpen: (id: string) => void;
  onFinalizeStale: (id: string) => void;
  onDelete: (ids: string[]) => Promise<boolean>;
}) {
  const [query, setQuery] = useState("");
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const normalizedQuery = query.trim().toLowerCase();
  const filtered = records.filter((record) => {
    if (!normalizedQuery) return true;
    return [
      record.title,
      ...record.transcript.map((item) => `${item.speaker} ${item.text}`),
      ...record.batches.flatMap((batch) =>
        batch.suggestions.map(
          (suggestion) => `${suggestion.intent} ${suggestion.script}`,
        ),
      ),
    ]
      .join(" ")
      .toLowerCase()
      .includes(normalizedQuery);
  });
  const selectableIds = filtered
    .filter((record) => record.id !== liveMeetingId)
    .map((record) => record.id);
  const allVisibleSelected =
    selectableIds.length > 0 &&
    selectableIds.every((id) => selectedIds.has(id));

  useEffect(() => {
    const existing = new Set(records.map((record) => record.id));
    setSelectedIds(
      (current) => new Set([...current].filter((id) => existing.has(id))),
    );
  }, [records]);

  function toggleSelection(id: string) {
    if (id === liveMeetingId) return;
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function deleteSelected() {
    const ids = [...selectedIds];
    if (!ids.length) return;
    if (await onDelete(ids)) {
      setSelectedIds(new Set());
      setSelectionMode(false);
    }
  }

  return (
    <div className="page">
      <header className="page-heading">
        <div>
          <div className="eyebrow">本地档案</div>
          <h1>会议历史</h1>
          <p>搜索转写、建议和问答；所有记录只保存在本机。</p>
        </div>
        <div className="history-heading-tools">
          <label className="search-box">
            <Search size={17} />
            <input
              placeholder="搜索标题、转写或建议"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          <button
            className={`button ${selectionMode ? "secondary" : "ghost"}`}
            onClick={() => {
              setSelectionMode((current) => !current);
              setSelectedIds(new Set());
            }}
            disabled={records.length === 0}
          >
            {selectionMode ? <X size={15} /> : <ListChecks size={15} />}
            {selectionMode ? "退出管理" : "批量管理"}
          </button>
        </div>
      </header>
      {selectionMode && (
        <div className="history-selection-bar">
          <div>
            <strong>已选 {selectedIds.size} 场</strong>
            <span>删除会同时清理这些会议的本地录音</span>
          </div>
          <div>
            <button
              className="button ghost small"
              onClick={() =>
                setSelectedIds(allVisibleSelected ? new Set() : new Set(selectableIds))
              }
              disabled={selectableIds.length === 0}
            >
              {allVisibleSelected ? "取消全选" : `全选当前 ${selectableIds.length} 场`}
            </button>
            <button
              className="button danger small"
              onClick={() => void deleteSelected()}
              disabled={selectedIds.size === 0}
            >
              <Trash2 size={14} /> 删除所选
            </button>
          </div>
        </div>
      )}
      {filtered.length === 0 ? (
        <EmptyState
          icon={Archive}
          title={records.length === 0 ? "还没有会议档案" : "没有匹配的会议"}
          detail={
            records.length === 0
              ? "结束会议后，转写、建议和问答会自动保存在本机。"
              : "换一个标题、说话内容或建议关键词试试。"
          }
        />
      ) : (
        <div className="archive-list">
          {filtered.map((record) => {
            const stats = meetingStats(record);
            // status=active 但不是本次在跑的那场 = 上次没能正常结束的残留。
            // 正常情况下启动时已被收尾，这里兜住"会中崩溃、库里还没改"的窗口。
            const stale =
                record.status === "active" && record.id !== liveMeetingId;
              const live = record.id === liveMeetingId;
              const selected = selectedIds.has(record.id);
              return (
              <div
                className={`archive-row-wrap ${selected ? "selected" : ""}`}
                key={record.id}
              >
              {selectionMode && (
                <button
                  className={`archive-select-toggle ${selected ? "checked" : ""}`}
                  onClick={() => toggleSelection(record.id)}
                  disabled={live}
                  aria-label={selected ? `取消选择 ${record.title}` : `选择 ${record.title}`}
                  aria-pressed={selected}
                  title={live ? "进行中的会议不能删除" : undefined}
                >
                  {selected && <Check size={13} />}
                </button>
              )}
              <button
                className="archive-row"
                onClick={() =>
                  selectionMode ? toggleSelection(record.id) : onOpen(record.id)
                }
              >
                <span className="archive-icon">
                  <Archive size={18} />
                </span>
                <span className="archive-main">
                  <strong>{record.title}</strong>
                  <small>
                    {formatDuration(record)} · {stats.transcript} 段发言 ·{" "}
                    {stats.speakers} 位说话人 · {stats.suggestions} 条建议
                  </small>
                </span>
                <span
                  className={`record-status ${live ? "active" : ""} ${
                    record.status === "interrupted" || stale ? "interrupted" : ""
                  }`}
                >
                  {live
                    ? "进行中"
                    : stale || record.status === "interrupted"
                      ? "异常中断"
                      : "已结束"}
                </span>
                <time>{formatRecordDate(record.startedAt)}</time>
                <ChevronRight size={17} />
              </button>
              {/* 残留记录的收尾出口：没有它，这条会永远显示"进行中" */}
              {stale && (
                <button
                  className="button ghost small archive-finalize"
                  onClick={() => onFinalizeStale(record.id)}
                >
                  <CircleStop size={15} /> 结束这场会议
                </button>
              )}
              {!selectionMode && !live && (
                <button
                  className="icon-button archive-delete"
                  onClick={() => void onDelete([record.id])}
                  title={`删除「${record.title}」`}
                  aria-label={`删除「${record.title}」`}
                >
                  <Trash2 size={15} />
                </button>
              )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/** 会议录音远场偏轻：回放时增益 + 软限幅，不改磁盘上的原 wav。 */
const PLAYBACK_BOOST_OPTIONS = [
  { value: 1, label: "原音" },
  { value: 2.5, label: "增强" },
  { value: 4, label: "更响" },
] as const;

function BoostedMeetingAudio({
  src,
  seekRequest,
  onTimeChange,
  onPlayingChange,
}: {
  src: string;
  seekRequest: { seconds: number; token: number } | null;
  onTimeChange: (seconds: number) => void;
  onPlayingChange: (playing: boolean) => void;
}) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const graphRef = useRef<{
    ctx: AudioContext;
    gain: GainNode;
  } | null>(null);
  const wiredRef = useRef(false);
  // 默认增强：实测语音 RMS ~-25 dBFS，约 +8 dB 更接近听感
  const [boost, setBoost] = useState(2.5);

  useEffect(() => {
    if (graphRef.current) {
      graphRef.current.gain.gain.value = boost;
    }
  }, [boost]);

  useEffect(() => {
    return () => {
      const graph = graphRef.current;
      graphRef.current = null;
      wiredRef.current = false;
      if (graph) void graph.ctx.close();
    };
  }, [src]);

  function ensurePlaybackGraph() {
    const el = audioRef.current;
    if (!el) return;
    if (wiredRef.current) {
      void graphRef.current?.ctx.resume();
      if (graphRef.current) graphRef.current.gain.gain.value = boost;
      return;
    }
    const AudioCtx =
      window.AudioContext ||
      (window as unknown as { webkitAudioContext: typeof AudioContext })
        .webkitAudioContext;
    const ctx = new AudioCtx();
    const source = ctx.createMediaElementSource(el);
    const gain = ctx.createGain();
    gain.gain.value = boost;
    // 峰值可能已接近 -4 dBFS，增益后用压缩器顶住削波
    const compressor = ctx.createDynamicsCompressor();
    compressor.threshold.value = -6;
    compressor.knee.value = 12;
    compressor.ratio.value = 12;
    compressor.attack.value = 0.003;
    compressor.release.value = 0.25;
    source.connect(gain);
    gain.connect(compressor);
    compressor.connect(ctx.destination);
    graphRef.current = { ctx, gain };
    wiredRef.current = true;
    void ctx.resume();
  }

  useEffect(() => {
    const el = audioRef.current;
    if (!el || !seekRequest) return;
    const applySeek = () => {
      el.currentTime = Math.max(
        0,
        Math.min(Number.isFinite(el.duration) ? el.duration : seekRequest.seconds, seekRequest.seconds),
      );
      ensurePlaybackGraph();
      void el.play();
    };
    if (el.readyState >= 1) {
      applySeek();
      return;
    }
    el.addEventListener("loadedmetadata", applySeek, { once: true });
    return () => el.removeEventListener("loadedmetadata", applySeek);
  }, [seekRequest]);

  return (
    <div className="audio-player">
      <audio
        ref={audioRef}
        controls
        src={src}
        preload="metadata"
        onPlay={() => {
          ensurePlaybackGraph();
          onPlayingChange(true);
        }}
        onPause={() => onPlayingChange(false)}
          onEnded={() => {
            onPlayingChange(false);
            onTimeChange(-1);
          }}
        onTimeUpdate={(event) => onTimeChange(event.currentTarget.currentTime)}
        onSeeked={(event) => onTimeChange(event.currentTarget.currentTime)}
      />
      <label className="audio-boost" title="仅影响本机回放听感，不会改写原始录音">
        <Volume2 size={14} />
        <select
          value={boost}
          onChange={(e) => setBoost(Number(e.target.value))}
          aria-label="回放音量增强"
        >
          {PLAYBACK_BOOST_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </label>
      <span className="audio-boost-hint">仅回放放大，不改原文件</span>
    </div>
  );
}

function renderMarkdownInline(
  text: string,
  onEvidenceClick?: (transcriptId: string) => void,
) {
  const evidencePattern = /\[证据\s+id=([^\]\s]+)\s+t=([^\]]+)\]|\[证据 待确认\]/g;
  const nodes: React.ReactNode[] = [];
  let cursor = 0;
  let match: RegExpExecArray | null;
  let nodeIndex = 0;

  const renderText = (value: string, keyPrefix: string) =>
    value
      .split(/(\*\*[^*]+\*\*|`[^`]+`)/g)
      .filter(Boolean)
      .map((part, index) => {
        const key = `${keyPrefix}-text-${index}`;
        if (part.startsWith("**") && part.endsWith("**")) {
          return <strong key={key}>{part.slice(2, -2)}</strong>;
        }
        if (part.startsWith("`") && part.endsWith("`")) {
          return <code key={key}>{part.slice(1, -1)}</code>;
        }
        return <span key={key}>{part}</span>;
      });

  while ((match = evidencePattern.exec(text))) {
    if (match.index > cursor) {
      nodes.push(...renderText(text.slice(cursor, match.index), `inline-${nodeIndex++}`));
    }
    const raw = match[0];
    const transcriptId = match[1] || "";
    const timestamp = match[2] || "";
    const pending = raw === "[证据 待确认]";
    const label = pending ? "证据待确认" : `原文 ${timestamp}`;
    const title = pending
      ? "这条内容暂时没有可定位的原文"
      : `转写片段 ${transcriptId} · 点击定位 ${timestamp}`;
    const key = `inline-evidence-${nodeIndex++}`;
    if (!pending && onEvidenceClick) {
      nodes.push(
        <button
          type="button"
          className="minutes-evidence-chip actionable"
          key={key}
          title={title}
          onClick={() => onEvidenceClick(transcriptId)}
        >
          <Clock3 size={11} /> {label}
        </button>,
      );
    } else {
      nodes.push(
        <span
          className={`minutes-evidence-chip ${pending ? "pending" : ""}`}
          key={key}
          title={title}
        >
          <Clock3 size={11} /> {label}
        </span>,
      );
    }
    cursor = match.index + raw.length;
  }
  if (cursor < text.length) {
    nodes.push(...renderText(text.slice(cursor), `inline-${nodeIndex}`));
  }
  return nodes.length ? nodes : renderText(text, "inline-empty");
}

function stripMinutesEvidenceMarkers(source: string) {
  return source
    .replace(/\[证据\s+id=[^\]\s]+(?:\s+t=[^\]]+)?\]|\[证据 待确认\]/g, " ")
    .replace(/[ \t]+([，。；、：！？,.!?])/g, "$1")
    .replace(/^[ \t]+|[ \t]+$/gm, "");
}

/**
 * 纪要内容来自受控提示词，但仍按普通文本解析而不是注入 HTML。
 * 这里覆盖纪要会实际产出的标题、列表、任务项、表格、引用和分隔线；
 * 复制按钮复制过滤证据标记后的纯净文本。
 */
function MarkdownDocument({
  source,
  onEvidenceClick,
}: {
  source: string;
  onEvidenceClick?: (transcriptId: string) => void;
}) {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const blocks: React.ReactNode[] = [];
  const isBlockStart = (line: string, next = "") =>
    /^(#{1,6})\s+/.test(line) ||
    /^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line) ||
    /^\s*[-*]\s+/.test(line) ||
    /^\s*\d+\.\s+/.test(line) ||
    /^\s*>\s?/.test(line) ||
    /^```/.test(line) ||
    (line.includes("|") && /^\s*\|?[\s:|-]+\|\s*$/.test(next));

  for (let index = 0; index < lines.length; ) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }

    if (line.startsWith("```")) {
      const code: string[] = [];
      index += 1;
      while (index < lines.length && !lines[index].startsWith("```")) {
        code.push(lines[index]);
        index += 1;
      }
      index += 1;
      blocks.push(
        <pre className="markdown-code-block" key={`code-${index}`}>
          <code>{code.join("\n")}</code>
        </pre>,
      );
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const level = Math.min(4, heading[1].length);
      const content = renderMarkdownInline(heading[2], onEvidenceClick);
      if (level === 1) blocks.push(<h1 key={`h-${index}`}>{content}</h1>);
      if (level === 2) blocks.push(<h2 key={`h-${index}`}>{content}</h2>);
      if (level === 3) blocks.push(<h3 key={`h-${index}`}>{content}</h3>);
      if (level === 4) blocks.push(<h4 key={`h-${index}`}>{content}</h4>);
      index += 1;
      continue;
    }

    if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) {
      blocks.push(<hr key={`hr-${index}`} />);
      index += 1;
      continue;
    }

    if (
      line.includes("|") &&
      index + 1 < lines.length &&
      /^\s*\|?[\s:|-]+\|\s*$/.test(lines[index + 1])
    ) {
      const cells = (value: string) =>
        value
          .trim()
          .replace(/^\||\|$/g, "")
          .split("|")
          .map((cell) => cell.trim());
      const header = cells(line);
      const rows: string[][] = [];
      index += 2;
      while (index < lines.length && lines[index].includes("|")) {
        rows.push(cells(lines[index]));
        index += 1;
      }
      blocks.push(
        <div className="markdown-table-wrap" key={`table-${index}`}>
          <table>
            <thead>
              <tr>
                {header.map((cell, cellIndex) => (
                  <th key={cellIndex}>{renderMarkdownInline(cell, onEvidenceClick)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {header.map((_, cellIndex) => (
                    <td key={cellIndex}>
                      {renderMarkdownInline(row[cellIndex] || "", onEvidenceClick)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }

    if (/^\s*[-*]\s+/.test(line)) {
      const items: Array<{ text: string; task: boolean; checked: boolean }> = [];
      while (index < lines.length) {
        const match = lines[index].match(/^\s*[-*]\s+(.+)$/);
        if (!match) break;
        const task = match[1].match(/^\[([ xX])\]\s*(.*)$/);
        items.push({
          text: task ? task[2] : match[1],
          task: Boolean(task),
          checked: task ? task[1].toLowerCase() === "x" : false,
        });
        index += 1;
      }
      blocks.push(
        <ul
          className={items.some((item) => item.task) ? "markdown-task-list" : ""}
          key={`ul-${index}`}
        >
          {items.map((item, itemIndex) => (
            <li key={itemIndex}>
              {item.task && (
                <span
                  className={`markdown-checkbox ${item.checked ? "checked" : ""}`}
                  aria-hidden="true"
                >
                  {item.checked && <Check size={10} />}
                </span>
              )}
              <span>{renderMarkdownInline(item.text, onEvidenceClick)}</span>
            </li>
          ))}
        </ul>,
      );
      continue;
    }

    if (/^\s*\d+\.\s+/.test(line)) {
      const items: string[] = [];
      while (index < lines.length) {
        const match = lines[index].match(/^\s*\d+\.\s+(.+)$/);
        if (!match) break;
        items.push(match[1]);
        index += 1;
      }
      blocks.push(
        <ol key={`ol-${index}`}>
          {items.map((item, itemIndex) => (
            <li key={itemIndex}>{renderMarkdownInline(item, onEvidenceClick)}</li>
          ))}
        </ol>,
      );
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      const quote: string[] = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
        quote.push(lines[index].replace(/^\s*>\s?/, ""));
        index += 1;
      }
      blocks.push(
        <blockquote key={`quote-${index}`}>
          {renderMarkdownInline(quote.join(" "), onEvidenceClick)}
        </blockquote>,
      );
      continue;
    }

    const paragraph = [line.trim()];
    index += 1;
    while (
      index < lines.length &&
      lines[index].trim() &&
      !isBlockStart(lines[index], lines[index + 1] || "")
    ) {
      paragraph.push(lines[index].trim());
      index += 1;
    }
    blocks.push(
      <p key={`p-${index}`}>{renderMarkdownInline(paragraph.join(" "), onEvidenceClick)}</p>,
    );
  }

  return <div className="markdown-document">{blocks}</div>;
}

function HistoryDetailScreen({
  record,
  onBack,
  onCopy,
  onExport,
  onDelete,
  onNotify,
  onPersist,
  llmProvider,
  llmModel,
  backgroundTasks,
  onGenerateMinutes,
  onDiarizeMeeting,
  onGenerateReview,
}: {
  record: MeetingRecord;
  onBack: () => void;
  onCopy: (value: string) => void;
  onExport: (format: "md" | "txt") => void;
  onDelete: () => Promise<boolean>;
  onNotify: (value: string) => void;
  /** 历史里改说话人名后落库（连带更新该说话人的全部发言） */
  onPersist: (record: MeetingRecord) => void;
  llmProvider?: string;
  llmModel?: string;
  backgroundTasks?: Record<string, BackgroundTaskInfo>;
  onGenerateMinutes?: (recordId: string, recordTitle: string) => void;
  onDiarizeMeeting?: (
    recordId: string,
    recordTitle: string,
    opts: {
      splitChars?: number;
      enrollMode?: string;
      forceReextract?: boolean;
      cleanup?: boolean;
      meThreshold?: number;
      clusterThreshold?: number;
      provider?: string;
      model?: string;
      cleanTranscript?: boolean;
    },
  ) => void;
  onGenerateReview?: (
    recordId: string,
    recordTitle: string,
    enhance?: boolean,
  ) => void;
}) {
  const stats = meetingStats(record);
  const activeVersion =
    record.transcriptVersion === "offline" ? "offline" : "realtime";
  const cleanupMeta = record.transcriptVersions?.[activeVersion]?.cleanup;
  const audioFileName = record.audioPath
    ? record.audioPath.split(/[\\/]/).at(-1)
    : null;
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [audioError, setAudioError] = useState("");
  const [diarizing, setDiarizing] = useState(false);
  const [diarizeMsg, setDiarizeMsg] = useState("");
  const [reviewTab, setReviewTab] = useState<"suggestions" | "minutes" | "review">(
    record.minutes ? "minutes" : "suggestions",
  );
  const [generatingMinutes, setGeneratingMinutes] = useState(false);
  const [minutesMsg, setMinutesMsg] = useState("");
  const [reviewGenerating, setReviewGenerating] = useState(false);
  const [reviewMsg, setReviewMsg] = useState("");
  const [promotingGlossary, setPromotingGlossary] = useState(false);

  const minutesTask = backgroundTasks?.[`minutes:${record.id}`];
  const diarizeTask = backgroundTasks?.[`diarize:${record.id}`];
  const reviewTask = backgroundTasks?.[`review:${record.id}`];

  const isGeneratingMinutes =
    minutesTask?.status === "running" || generatingMinutes;
  const isDiarizing = diarizeTask?.status === "running" || diarizing;
  const isReviewGenerating =
    reviewTask?.status === "running" || reviewGenerating;
  const effectiveMinutesMsg =
    (minutesTask?.status === "running" ? minutesTask.message : "") || minutesMsg;
  const effectiveDiarizeMsg =
    (diarizeTask?.status === "running" ? diarizeTask.message : "") || diarizeMsg;
  const effectiveReviewMsg =
    (reviewTask?.status === "running" ? reviewTask.message : "") || reviewMsg;
  const [reviewItems, setReviewItems] = useState<MeetingMemoryItem[]>(
    record.memoryItems || record.review?.memoryItems || [],
  );
  const [reviewCandidates, setReviewCandidates] = useState<GlossaryCandidate[]>(
    record.glossaryCandidates || record.review?.glossaryCandidates || [],
  );
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState(record.title);
  const [playbackActiveId, setPlaybackActiveId] = useState<string | null>(null);
  const playbackActiveIdRef = useRef<string | null>(null);
  const [playbackPlaying, setPlaybackPlaying] = useState(false);
  const [followPlayback, setFollowPlayback] = useState(true);
  const [seekRequest, setSeekRequest] = useState<{
    seconds: number;
    token: number;
  } | null>(null);
  const transcriptBodyRef = useRef<HTMLDivElement | null>(null);
  const transcriptLineRefs = useRef(new Map<string, HTMLElement>());
  const suggestionBodyRef = useRef<HTMLDivElement | null>(null);
  const suggestionBatchRefs = useRef(new Map<string, HTMLElement>());
  const [locatedTranscriptIds, setLocatedTranscriptIds] = useState<string[]>([]);
  const [locatedBatchId, setLocatedBatchId] = useState<string | null>(null);
  const [locatedApproximate, setLocatedApproximate] = useState(false);
  const [speakerProfileMenu, setSpeakerProfileMenu] = useState<{
    speakerId: string;
    x: number;
    y: number;
    openAbove: boolean;
  } | null>(null);

  // 说话人名的本地编辑态：seed 自 record.speakers，无则由转写的 speakerId 派生
  const [speakerNames, setSpeakerNames] = useState<Record<string, string>>(() => {
    const map: Record<string, string> = {};
    for (const sp of record.speakers || []) map[sp.id] = sp.name;
    for (const item of record.transcript) {
      if (item.speakerId && !(item.speakerId in map)) {
        map[item.speakerId] = item.speaker;
      }
    }
    return map;
  });
  const [editing, setEditing] = useState<{ id: string; value: string } | null>(
    null,
  );
  const [editingTranscript, setEditingTranscript] = useState<{
    ids: string[];
    value: string;
    original: string;
  } | null>(null);
  // 逐段改派的浮动菜单（点某一行发言时打开）
  const [segmentMenu, setSegmentMenu] = useState<{
    ids: string[];
    speakerId: string | null;
    x: number;
    y: number;
    openAbove: boolean;
  } | null>(null);

  useEffect(() => {
    return () => {
      if (audioUrl?.startsWith("blob:")) URL.revokeObjectURL(audioUrl);
    };
  }, [audioUrl]);

  useEffect(() => {
    setReviewTab(record.minutes ? "minutes" : "suggestions");
    setMinutesMsg("");
  }, [
    record.id,
    Boolean(record.minutes),
    record.minutes?.generatedAt,
    record.minutes?.sourceVersion,
  ]);

  useEffect(() => {
    setReviewMsg(
      record.review?.status === "failed"
        ? `模型增强失败，已保留本地结果：${String(
            record.review.message || "未返回具体原因",
          )
            .replace(/\s+/g, " ")
            .trim()
            .slice(0, 240)}`
        : "",
    );
    setReviewItems(record.memoryItems || record.review?.memoryItems || []);
    setReviewCandidates(record.glossaryCandidates || record.review?.glossaryCandidates || []);
  }, [
    record.id,
    record.review?.status,
    record.review?.message,
    record.review?.generatedAt,
    record.memoryItems,
    record.glossaryCandidates,
  ]);

  // 换会或离线分离回写后，同步名字表
  useEffect(() => {
    const map: Record<string, string> = {};
    for (const sp of record.speakers || []) map[sp.id] = sp.name;
    for (const item of record.transcript) {
      if (item.speakerId && !(item.speakerId in map)) {
        map[item.speakerId] = item.speaker;
      }
    }
    setSpeakerNames(map);
    setTitleDraft(record.title);
    setEditingTitle(false);
    setSpeakerProfileMenu(null);
    setEditing(null);
    setEditingTranscript(null);
    setSegmentMenu(null);
    setLocatedTranscriptIds([]);
    setLocatedBatchId(null);
    setLocatedApproximate(false);
    suggestionBatchRefs.current.clear();
    setDiarizeMsg("");
    setPlaybackActiveId(null);
    playbackActiveIdRef.current = null;
    setPlaybackPlaying(false);
    setFollowPlayback(true);
    setSeekRequest(null);
  }, [record.id, record.transcriptVersion]);

  function persistActiveVersion(
    updated: MeetingRecord,
    options: { transcriptEdited?: boolean } = {},
  ) {
    const kind = record.transcriptVersion === "offline" ? "offline" : "realtime";
    const existing = record.transcriptVersions;
    const currentVersion = existing?.[kind];
    const shouldKeepVersions = Boolean(existing || options.transcriptEdited);
    onPersist({
      ...updated,
      transcriptVersion: kind,
      transcriptVersions: shouldKeepVersions
        ? {
            ...(existing || {}),
            [kind]: {
              transcript: updated.transcript,
              speakers: updated.speakers || [],
              generatedAt: currentVersion?.generatedAt || Date.now(),
              editedAt: options.transcriptEdited
                ? Date.now()
                : currentVersion?.editedAt,
              cleanup: currentVersion?.cleanup,
            },
          }
        : undefined,
    });
  }

  function switchTranscriptVersion(kind: "realtime" | "offline") {
    const version = record.transcriptVersions?.[kind];
    if (!version || record.transcriptVersion === kind) return;
    onPersist({
      ...record,
      transcriptVersion: kind,
      transcript: version.transcript,
      speakers: version.speakers,
    });
    setDiarizeMsg(
      kind === "offline"
        ? "已切换到会后整理版本；改名和改派只影响这个版本。"
        : "已切换回实时转写版本；会后整理结果仍会保留。",
    );
  }

  /**
   * ⚠️ 标了 isMe 的说话人一律显示「我」，与会中一致。
   *    只看 speakers[].name 的话，自己的发言在历史里会显示成「说话人2」
   *    且不走 mine 配色 —— 同一场会在两个页面上敌我配色不一样，很误导。
   */
  const meSpeakerIds = useMemo(
    () => new Set((record.speakers || []).filter((s) => s.isMe).map((s) => s.id)),
    [record.speakers],
  );
  const displayTranscript = useMemo(
    () =>
      mergeConsecutiveTranscript(record.transcript, (item) => {
        const id = item.speakerId;
        if (id && speakerNames[id]) return `id:${id}`;
        return item.speakerId || `name:${item.speaker}`;
      }),
    [record.transcript, speakerNames],
  );
  const playbackCalibrationTranscript =
    record.transcriptVersions?.[
      activeVersion === "realtime" ? "offline" : "realtime"
    ]?.transcript;
  const playbackRanges = useMemo(
    () =>
      buildPlaybackRanges(
        record.transcript,
        record.startedAt,
        record.endedAt,
        record.status,
        record.audioSeconds,
        playbackCalibrationTranscript,
      ),
    [
      record.transcript,
      record.transcriptVersion,
      record.transcriptVersions,
      record.startedAt,
      record.endedAt,
      record.status,
      record.audioSeconds,
      playbackCalibrationTranscript,
    ],
  );
  const playbackRangeById = useMemo(
    () => new Map(playbackRanges.map((range) => [range.id, range])),
    [playbackRanges],
  );

  function locateSuggestionBatch(batch: SuggestionBatch) {
    const location = findTranscriptIdsForContext(
      record.transcript,
      getSuggestionContext(batch),
      playbackRangeById,
    );
    if (!location.ids.length) return;
    setLocatedTranscriptIds(location.ids);
    setLocatedBatchId(batch.id);
    setLocatedApproximate(location.approximate);
    window.requestAnimationFrame(() => {
      const target =
        transcriptLineRefs.current.get(location.targetId || location.ids.at(-1) || "");
      target?.scrollIntoView({ block: "center", behavior: "smooth" });
    });
  }

  function locateSuggestionFromTranscript(ids: string[]) {
    const match = findNearestSuggestionBatchForTranscript(
      record.transcript,
      record.batches,
      ids,
      playbackRangeById,
    );
    if (!match) return;
    const contextSelection = findTranscriptIdsForContext(
      record.transcript,
      match.context,
      playbackRangeById,
    );
    setLocatedTranscriptIds(contextSelection.ids.length ? contextSelection.ids : ids);
    setLocatedBatchId(match.batch.id);
    setLocatedApproximate(match.approximate || contextSelection.approximate);
    setReviewTab("suggestions");
    window.requestAnimationFrame(() => {
      suggestionBatchRefs.current
        .get(match.batch.id)
        ?.scrollIntoView({ block: "center", behavior: "smooth" });
    });
  }

  function canLocateSuggestionForTranscript(ids: string[]) {
    return Boolean(
      findNearestSuggestionBatchForTranscript(
        record.transcript,
        record.batches,
        ids,
        playbackRangeById,
      ),
    );
  }
  const speakerSegmentCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const item of record.transcript) {
      if (!item.speakerId) continue;
      counts[item.speakerId] = (counts[item.speakerId] || 0) + 1;
    }
    return counts;
  }, [record.transcript]);
  const activeSpeakerIds = useMemo(
    () =>
      Object.keys(speakerNames)
        .filter((id) => (speakerSegmentCounts[id] || 0) > 0)
        .sort(
          (a, b) =>
            (speakerSegmentCounts[b] || 0) - (speakerSegmentCounts[a] || 0),
        ),
    [speakerNames, speakerSegmentCounts],
  );

  const speakerTimingRanges = useMemo(
    () =>
      new Map(
        playbackRanges
          .filter((range) => range.endMs > range.startMs)
          .map((range) => [range.id, range]),
      ),
    [playbackRanges, record.audioSeconds],
  );
  const speakerDistribution = useMemo(
    () =>
      buildSpeakerDistribution(
        record.transcript,
        speakerTimingRanges,
        (item) => item.speakerId || `name:${item.speaker}`,
        nameOf,
        record.audioSeconds ? record.audioSeconds * 1000 : null,
      ),
    [record.transcript, record.audioSeconds, speakerTimingRanges, speakerNames, meSpeakerIds],
  );

  useEffect(() => {
    if (!playbackPlaying || !followPlayback || !playbackActiveId) return;
    const node = transcriptLineRefs.current.get(playbackActiveId);
    const container = transcriptBodyRef.current;
    if (!node || !container) return;
    const nodeRect = node.getBoundingClientRect();
    const containerRect = container.getBoundingClientRect();
    const comfortablyVisible =
      nodeRect.top >= containerRect.top + 42 &&
      nodeRect.bottom <= containerRect.bottom - 42;
    if (comfortablyVisible) return;
    const targetTop =
      container.scrollTop +
      nodeRect.top -
      containerRect.top -
      container.clientHeight * 0.34;
    container.scrollTo({ top: Math.max(0, targetTop), behavior: "smooth" });
  }, [playbackActiveId, playbackPlaying, followPlayback]);

  function nameOf(item: TranscriptItem) {
    if (item.speakerId) {
      if (meSpeakerIds.has(item.speakerId)) return "我";
      if (speakerNames[item.speakerId]) return speakerNames[item.speakerId];
      if (item.speakerId === "other") return "对方";
    }
    // speakerId 存在时优先相信 ID；避免改派后残留的旧 speaker="我" 把
    // 已经改到对方的内容再次渲染成「我」。无 ID 的旧记录才回退显示名。
    return item.speaker;
  }

  /**
   * 会后逐段改派。
   *
   * ⚠️ 重命名是「这个说话人的全部发言」，改派是「这一小段」，两件事不能互相替代。
   *    分离再准也会有个别段落归错，而错的往往只是一大段里的一两句 ——
   *    没有逐段改派，用户只能眼睁睁看着错的留在档案里（真机反馈）。
   */
  function persistSpeakerChange(
    ids: string[],
    targetId: string,
    names: Record<string, string> = speakerNames,
  ) {
    const idSet = new Set(ids);
    const targetIsMe =
      targetId !== "other" &&
      (targetId === "me" || meSpeakerIds.has(targetId));
    const targetName = targetIsMe
      ? "我"
      : targetId === "other"
        ? "对方"
        : names[targetId] || targetId;
    const nextNames = { ...names, [targetId]: targetName };
    const meIds = new Set(
      (record.speakers || [])
        .filter((s) => s.isMe && s.id !== targetId)
        .map((s) => s.id),
    );
    if (targetIsMe) meIds.add(targetId);
    const updated: MeetingRecord = {
      ...record,
      transcript: record.transcript.map((item) =>
        idSet.has(item.id)
          ? {
              ...item,
              speakerId: targetId,
              speaker: targetName,
            }
          : item,
      ),
      speakers: Object.entries(nextNames).map(([id, n]) => ({
        id,
        name: n,
        isMe: meIds.has(id),
        mergedInto: null,
      })),
    };
    persistActiveVersion(updated);
  }

  function reassignSegments(ids: string[], targetId: string) {
    if (!ids.length) return;
    persistSpeakerChange(ids, targetId);
    setDiarizeMsg(
      `已把 ${ids.length} 段发言改派给「${
        targetId !== "other" &&
        (targetId === "me" || meSpeakerIds.has(targetId))
          ? "我"
          : targetId === "other"
            ? "对方"
            : speakerNames[targetId] || targetId
      }」。`,
    );
  }

  /** 这段其实是第三个人：新建一个只存在于本地的说话人 */
  function splitSegmentsToNewSpeaker(ids: string[]) {
    if (!ids.length) return;
    const newId = `local-${Date.now().toString(36)}`;
    const ordinal = Object.keys(speakerNames).length + 1;
    const name = `说话人${ordinal}`;
    const nextNames = { ...speakerNames, [newId]: name };
    setSpeakerNames(nextNames);
    persistSpeakerChange(ids, newId, nextNames);
    setDiarizeMsg(`已把这段拆给「${name}」，点名字可以改名。`);
  }

  /**
   * 气泡误标时优先归到已有「对方」；没有对方档案时创建 other，
   * 不要直接「新建说话人 N」（两人会最常见诉求是挪到对方）。
   */
  function reassignSegmentsToOther(ids: string[]) {
    if (!ids.length) return;
    const preferred = preferredOtherSpeakerId(
      Object.keys(speakerNames),
      speakerNames,
      meSpeakerIds,
      segmentMenu?.speakerId || null,
    );
    if (preferred) {
      reassignSegments(ids, preferred);
      return;
    }
    const nextNames = { ...speakerNames, other: speakerNames.other || "对方" };
    setSpeakerNames(nextNames);
    persistSpeakerChange(ids, "other", nextNames);
    setDiarizeMsg(`已把 ${ids.length} 段发言改派给「对方」。`);
  }

  function commitTranscriptCorrection() {
    if (!editingTranscript) return;
    const text = editingTranscript.value.trim();
    if (!text) {
      setDiarizeMsg("转写正文不能为空；如需删除整段，请先取消编辑。");
      return;
    }
    if (text === editingTranscript.original.trim()) {
      setEditingTranscript(null);
      return;
    }
    const updatedTranscript = replaceTranscriptLineText(
      record.transcript,
      editingTranscript.ids,
      text,
    );
    persistActiveVersion(
      { ...record, transcript: updatedTranscript },
      { transcriptEdited: true },
    );
    setEditingTranscript(null);
    setSegmentMenu(null);
    setDiarizeMsg(
      `已修正当前${
        activeVersion === "offline" ? "会后整理" : "实时转写"
      }版本正文；另一版本不受影响。`,
    );
    if (
      record.minutes?.content &&
      record.minutes.sourceVersion === activeVersion
    ) {
      setMinutesMsg("转写正文已更新，现有纪要基于旧内容，请重新生成。");
    }
  }

  function commitTitleRename() {
    const title = titleDraft.trim();
    setEditingTitle(false);
    if (!title) {
      setTitleDraft(record.title);
      return;
    }
    if (title === record.title) return;
    persistActiveVersion({ ...record, title });
    setDiarizeMsg(`会议已改名为「${title}」。`);
  }

  function reassignSpeakerAll(
    sourceId: string,
    targetId: string,
    createTargetName?: string,
  ) {
    if (sourceId === targetId) return;
    const ids = record.transcript
      .filter((item) => item.speakerId === sourceId)
      .map((item) => item.id);
    if (!ids.length) return;
    const sourceName = meSpeakerIds.has(sourceId)
      ? "我"
      : speakerNames[sourceId] || sourceId;
    const targetIsMe =
      targetId !== "other" &&
      (targetId === "me" || meSpeakerIds.has(targetId));
    const targetName = targetIsMe
      ? "我"
      : createTargetName || (targetId === "other" ? "对方" : speakerNames[targetId] || targetId);
    const nextNames: Record<string, string> = {
      ...Object.fromEntries(
        Object.entries(speakerNames).filter(([id]) => id !== sourceId),
      ),
    };
    nextNames[targetId] = targetName;
    const meIds = new Set(
      (record.speakers || [])
        .filter(
          (speaker) =>
            speaker.isMe && speaker.id !== sourceId && speaker.id !== targetId,
        )
        .map((speaker) => speaker.id),
    );
    if (targetIsMe) meIds.add(targetId);
    const updated: MeetingRecord = {
      ...record,
      transcript: record.transcript.map((item) =>
        item.speakerId === sourceId
          ? {
              ...item,
              speakerId: targetId,
              speaker: targetName,
            }
          : item,
      ),
      speakers: Object.entries(nextNames).map(([id, name]) => ({
        id,
        name,
        isMe: meIds.has(id),
        mergedInto: null,
      })),
    };
    setSpeakerNames(nextNames);
    persistActiveVersion(updated);
    setDiarizeMsg(
      `已将「${sourceName}」的 ${ids.length} 段发言全部归到「${targetName}」。`,
    );
    setSpeakerProfileMenu(null);
    setEditing(null);
  }

  function commitRename() {
    if (!editing) return;
    const name = editing.value.trim();
    if (!name) {
      setEditing(null);
      setSpeakerProfileMenu(null);
      return;
    }
    const nextNames = { ...speakerNames, [editing.id]: name };
    setSpeakerNames(nextNames);
    // 组装更新后的 record：说话人的全部发言同步改名 + speakers 表更新
    const meIds = new Set((record.speakers || []).filter((s) => s.isMe).map((s) => s.id));
    const updated: MeetingRecord = {
      ...record,
      transcript: record.transcript.map((item) =>
        item.speakerId === editing.id ? { ...item, speaker: name } : item,
      ),
      speakers: Object.entries(nextNames).map(([id, n]) => ({
        id,
        name: n,
        isMe: meIds.has(id),
        mergedInto: null,
      })),
    };
    persistActiveVersion(updated);
    setEditing(null);
    setSpeakerProfileMenu(null);
  }

  function handlePlaybackTime(seconds: number) {
    const active =
      seconds < 0
        ? null
        : playbackRangeAt(
              playbackRanges,
              seconds * 1000,
              playbackActiveIdRef.current,
            )?.id ?? null;
    if (active === playbackActiveIdRef.current) return;
    playbackActiveIdRef.current = active;
    setPlaybackActiveId(active);
  }

  async function loadAudio() {
    if (!window.meetingCopilot) {
      setAudioUrl(createDemoMeetingAudioUrl());
      setAudioError("");
      return true;
    }
    const result = await window.meetingCopilot.loadMeetingAudio(record.id);
    if (result.ok && result.dataUrl) {
      setAudioUrl(result.dataUrl);
      setAudioError("");
      return true;
    }
    setAudioError(result.message || "无法读取录音");
    return false;
  }

  async function jumpToTranscriptLine(ids: string[]) {
    const ranges = ids
      .map((id) => playbackRangeById.get(id))
      .filter((range): range is PlaybackRange => Boolean(range));
    if (!ranges.length) return;
    const first = ranges.reduce((best, range) =>
      range.startMs < best.startMs ? range : best,
    );
    setFollowPlayback(true);
    // 这里会故意提前 1.5 秒开始播放上下文；在 onSeeked/onTimeUpdate 回报
    // 真正的录音位置前，不要先把目标行标成 active，否则上一句声音还在播时
    // 下一句已经高亮，用户会把预播放误认为时间轴偏移。
    playbackActiveIdRef.current = null;
    setPlaybackActiveId(null);
    setSeekRequest({
      // 听归属时需要一点上下文；留 1.5 秒但不越过录音起点。
      seconds: Math.max(0, first.startMs / 1000 - 1.5),
      token: Date.now(),
    });
    if (!audioUrl) await loadAudio();
  }

  function resumePlaybackFollow() {
    setFollowPlayback(true);
    const active = playbackActiveIdRef.current;
    if (!active) return;
    const node = transcriptLineRefs.current.get(active);
    node?.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  async function runOfflineDiarize() {
    if (onDiarizeMeeting) {
      onDiarizeMeeting(record.id, record.title, {
        meThreshold: 0.65,
        clusterThreshold: 0.6,
        provider: llmProvider,
        model: llmModel,
        cleanTranscript: true,
      });
      return;
    }
    if (!window.meetingCopilot?.diarizeMeeting) {
      setDiarizeMsg("当前环境不支持离线分离");
      return;
    }
    if (!record.audioPath) {
      setDiarizeMsg("本场没有录音");
      return;
    }
    if (record.meetingMode === "online" && !record.systemAudioPath) {
      setDiarizeMsg(
        "这是一条只有历史混音的线上记录，无法安全分离远端；实时转写仍可正常查看。",
      );
      return;
    }
    setDiarizing(true);
    setDiarizeMsg(
      record.meetingMode === "online"
        ? "正在只对系统回环音轨分离远端说话人（麦克风固定为我）…"
        : "正在会后分离说话人并整理转写文字…",
    );
    try {
      const result = await window.meetingCopilot.diarizeMeeting(record.id, {
        meThreshold: 0.65,
        clusterThreshold: 0.6,
        provider: llmProvider,
        model: llmModel,
        cleanTranscript: true,
      });
      if (!result.ok || !result.record) {
        setDiarizeMsg(result.message || "分离失败");
        return;
      }
      const s = result.summary;
      const confidenceLabel =
        s?.confidence === "high"
          ? "高置信"
          : s?.confidence === "not_recommended"
            ? "不建议使用"
            : s?.confidence === "coarse"
              ? "粗分"
              : "";
      setDiarizeMsg(
        `完成：${s?.speakerCount ?? "?"} 人 · ${s?.segmentCount ?? "?"} 段语音` +
          (s?.splitItems
            ? ` · ${s.splitItems} 条过长转写已按说话人切开（共 ${s.transcriptItems ?? "?"} 段）`
            : "") +
          (s?.enrollUsed
            ? s?.meDecision === "threshold"
              ? " · 声纹按固定阈值认「我」（簇判据不成立）"
              : " · 已用声纹认「我」"
            : " · 未用声纹") +
          (s?.systemAudioOnly
            ? ` · 仅系统音轨${confidenceLabel ? ` · ${confidenceLabel}` : ""}`
            : "") +
          (!s?.systemAudioOnly && confidenceLabel ? ` · ${confidenceLabel}` : "") +
          (s?.cleanupStatus === "ok"
            ? ` · 已整理 ${s.cleanupChanged ?? 0} 段文字`
            : s?.cleanupStatus === "failed"
              ? ` · 文字整理失败，已保留原文${
                  s.cleanupReason ? `（${s.cleanupReason.slice(0, 80)}）` : ""
                }`
              : "") +
          (s?.qualityReasons?.length ? `（${s.qualityReasons.join("；")}）` : "") +
          (s?.elapsedSec != null ? ` · ${s.elapsedSec}s` : "") +
          "。点说话人名字改名，点某一段可以单独改派。",
      );
      // 刷新本地名字表
      const map: Record<string, string> = {};
      for (const sp of result.record.speakers || []) map[sp.id] = sp.name;
      for (const item of result.record.transcript) {
        if (item.speakerId && !(item.speakerId in map)) {
          map[item.speakerId] = item.speaker;
        }
      }
      setSpeakerNames(map);
      onPersist(result.record);
    } catch (error) {
      setDiarizeMsg(error instanceof Error ? error.message : "分离失败");
    } finally {
      setDiarizing(false);
    }
  }

  async function generateMinutes() {
    if (onGenerateMinutes) {
      setReviewTab("minutes");
      onGenerateMinutes(record.id, record.title);
      return;
    }
    if (!window.meetingCopilot?.generateMeetingMinutes) {
      setMinutesMsg("当前环境不支持自动生成会议纪要");
      return;
    }
    setReviewTab("minutes");
    setGeneratingMinutes(true);
    setMinutesMsg("正在梳理结论、待办和风险…");
    try {
      const result = await window.meetingCopilot.generateMeetingMinutes(
        record.id,
        { provider: llmProvider, model: llmModel },
      );
      if (!result.ok || !result.record) {
        if (result.record) onPersist(result.record);
        setMinutesMsg(result.message || "会议纪要生成失败");
        return;
      }
      onPersist(result.record);
      const elapsed = result.summary?.elapsedSec;
      const evidenceMarkerCount = result.summary?.evidenceMarkerCount;
      const pendingEvidenceCount = result.summary?.pendingEvidenceCount;
      setMinutesMsg(
        `已基于${
          result.record.minutes?.sourceVersion === "offline"
            ? "会后整理"
            : "实时转写"
        }生成${elapsed != null ? ` · ${elapsed}s` : ""}${
          evidenceMarkerCount != null
            ? ` · 证据 ${evidenceMarkerCount} 条${
                pendingEvidenceCount ? ` · 待确认 ${pendingEvidenceCount}` : ""
              }`
            : ""
        }`,
      );
    } catch (error) {
      setMinutesMsg(error instanceof Error ? error.message : "会议纪要生成失败");
    } finally {
      setGeneratingMinutes(false);
    }
  }

  async function generateReview(enhance = false) {
    if (onGenerateReview) {
      onGenerateReview(record.id, record.title, enhance);
      return;
    }
    if (!window.meetingCopilot?.generateMeetingReview) {
      setReviewMsg("当前环境不支持会后复盘");
      return;
    }
    setReviewGenerating(true);
    setReviewMsg(enhance ? "正在尝试用模型补全责任人和截止时间…" : "正在生成本地复盘…");
    try {
      const result = await window.meetingCopilot.generateMeetingReview(record.id, {
        enhance,
        provider: llmProvider,
        model: llmModel,
      });
      if (!result.ok || !result.record) {
        const reason = String(result.message || "未返回具体原因")
          .replace(/\s+/g, " ")
          .trim()
          .slice(0, 240);
        const message = enhance
          ? `模型增强失败，已保留本地结果：${reason}`
          : `复盘生成失败：${reason}`;
        setReviewMsg(message);
        if (enhance) onNotify(message);
        return;
      }
      const nextItems = result.record.memoryItems || result.record.review?.memoryItems || [];
      const nextCandidates = result.record.glossaryCandidates || result.record.review?.glossaryCandidates || [];
      setReviewItems(nextItems);
      setReviewCandidates(nextCandidates);
      onPersist(result.record);
      if (result.record.review?.status === "failed") {
        const reason = String(
          result.message || result.record.review.message || "未返回具体原因",
        )
          .replace(/\s+/g, " ")
          .trim()
          .slice(0, 240);
        const message = `模型增强失败，已保留本地结果：${reason}`;
        setReviewMsg(message);
        onNotify(message);
      } else {
        setReviewMsg("复盘已更新");
      }
    } catch (error) {
      const reason = String(
        error instanceof Error ? error.message : "未返回具体原因",
      )
        .replace(/\s+/g, " ")
        .trim()
        .slice(0, 240);
      const message = enhance
        ? `模型增强失败，已保留本地结果：${reason}`
        : `复盘生成失败：${reason}`;
      setReviewMsg(message);
      if (enhance) onNotify(message);
    } finally {
      setReviewGenerating(false);
    }
  }

  function updateReviewItem(id: string, patch: Partial<MeetingMemoryItem>) {
    setReviewItems((current) => {
      const next = current.map((item) =>
        item.id === id ? { ...item, ...patch, source: "user" as const, updatedAt: Date.now() } : item,
      );
      onPersist({ ...record, memoryItems: next, review: record.review ? { ...record.review, memoryItems: next } : record.review });
      return next;
    });
  }

  async function promoteReviewCandidates() {
    const selected = reviewCandidates.filter((item) => item.selected).map((item) => item.id);
    if (!selected.length || !window.meetingCopilot?.promoteGlossaryCandidates) {
      setReviewMsg("先勾选要加入全局词库的候选词");
      return;
    }
    setPromotingGlossary(true);
    setReviewMsg("正在加入全局词库…");
    try {
      const result = await window.meetingCopilot.promoteGlossaryCandidates(record.id, selected);
      if (!result.ok) {
        throw new Error("词库服务没有确认本次操作");
      }
      setReviewCandidates((current) => current.map((item) => (selected.includes(item.id) ? { ...item, selected: true } : item)));
      const message = result.terms.length
        ? `已加入全局词库 ${result.terms.length} 个词`
        : "没有新增词条（可能已存在于全局词库）";
      setReviewMsg(message);
      onNotify(message);
    } catch (error) {
      const message = error instanceof Error ? error.message : "词表保存失败";
      setReviewMsg(`加入全局词库失败：${message}`);
      onNotify(`加入全局词库失败：${message}`);
    } finally {
      setPromotingGlossary(false);
    }
  }

  const minutesSourceEditedAt = record.minutes
    ? record.transcriptVersions?.[record.minutes.sourceVersion]?.editedAt || 0
    : 0;
  const minutesStale = Boolean(
    record.minutes?.content &&
      minutesSourceEditedAt > record.minutes.generatedAt,
  );

  return (
    <div className="page history-detail-page">
      <header className="history-detail-header">
        <div className="history-detail-nav">
          <button className="text-button history-back" onClick={onBack}>
            <ArrowLeft size={15} /> 返回会议历史
          </button>
          <span
            className={`record-status large ${
              record.status === "active" ? "active" : ""
            } ${record.status === "interrupted" ? "interrupted" : ""}`}
          >
            {record.status === "active"
              ? "记录中"
              : record.status === "interrupted"
                ? "异常中断"
                : "已结束"}
          </span>
        </div>

        <div className="history-detail-mainline">
          <div className="history-heading-block">
            <div className="eyebrow">
              会议档案 · 本机保存
              {` · ${SCENE_META[record.scene || "general"].label}`}
              {record.projectName ? ` · ${record.projectName}` : ""}
            </div>
            <div className="history-title-row">
              {editingTitle ? (
                <form
                  className="history-title-form"
                  onSubmit={(event) => {
                    event.preventDefault();
                    commitTitleRename();
                  }}
                >
                  <input
                    autoFocus
                    value={titleDraft}
                    maxLength={80}
                    aria-label="会议名称"
                    onChange={(event) => setTitleDraft(event.target.value)}
                    onBlur={commitTitleRename}
                    onKeyDown={(event) => {
                      if (event.key === "Escape") {
                        setTitleDraft(record.title);
                        setEditingTitle(false);
                      }
                    }}
                  />
                </form>
              ) : (
                <>
                  <h1>{record.title}</h1>
                  <button
                    className="icon-button history-title-edit"
                    onClick={() => setEditingTitle(true)}
                    title="修改会议名称"
                    aria-label="修改会议名称"
                  >
                    <Pencil size={15} />
                  </button>
                </>
              )}
            </div>
            <p className="history-date-line">
              {formatRecordDate(record.startedAt)} · {formatDuration(record)}
            </p>
          </div>

          <div className="detail-actions">
            <button
              className="button primary"
              onClick={() => void runOfflineDiarize()}
              disabled={
                isDiarizing ||
                !record.audioPath ||
                (record.meetingMode === "online" && !record.systemAudioPath)
              }
              title={
                !record.audioPath
                  ? "无录音不可用"
                  : record.meetingMode === "online" && !record.systemAudioPath
                    ? "历史线上记录只有混音，无法安全分离"
                    : record.meetingMode === "online"
                      ? "只对系统回环音轨分离远端说话人，麦克风固定为我"
                      : "基于本场录音分离说话人，并整理明显错词、断句和口语重复"
              }
            >
              <Split size={15} />
              {isDiarizing
                ? "会后处理中…"
                : record.meetingMode === "online"
                  ? "分离远端说话人"
                  : "会后整理转写"}
            </button>
            <div className="export-actions">
              <button className="button secondary" onClick={() => onExport("md")}>
                <Download size={15} /> 导出 Markdown
              </button>
              <button className="button secondary" onClick={() => onExport("txt")}>
                TXT
              </button>
            </div>
            <button
              className="button danger"
              onClick={() => void onDelete()}
              title="永久删除会议记录及其本地录音"
            >
              <Trash2 size={15} /> 删除
            </button>
          </div>
        </div>

        <div className="history-context-row">
          {record.audioPath && (
            <div className="history-context-item recording-context">
              <Headphones size={14} />
              <span>
                录音
                {record.audioSeconds
                  ? ` · ${Math.floor(record.audioSeconds / 60)}:${String(
                      Math.round(record.audioSeconds % 60),
                    ).padStart(2, "0")}`
                  : ""}
              </span>
              <small title={record.audioPath}>{audioFileName}</small>
              {record.meetingMode === "online" && (
                <small>
                  {record.micAudioPath && record.systemAudioPath
                    ? "线上三音轨"
                    : "线上仅混音，无法安全会后分离"}
                </small>
              )}
              {audioUrl ? (
                <BoostedMeetingAudio
                  src={audioUrl}
                  seekRequest={seekRequest}
                  onTimeChange={handlePlaybackTime}
                  onPlayingChange={setPlaybackPlaying}
                />
              ) : (
                <button
                  className="text-button context-action"
                  onClick={() => void loadAudio()}
                >
                  <Play size={12} /> 播放
                </button>
              )}
            </div>
          )}
          <div className="history-context-meta">
            {record.hotwords && (
              <div className="history-context-item">
                <span>热词</span>
                <small>
                  {record.hotwords.status === "loaded"
                    ? `已加载 ${record.hotwords.count} 个`
                    : record.hotwords.status === "empty"
                      ? "未配置"
                      : record.hotwords.status === "unsupported"
                        ? `当前服务不支持（${record.hotwords.count} 个）`
                        : record.hotwords.status === "pending"
                          ? "同步中"
                          : `已降级${record.hotwords.count ? `（${record.hotwords.count} 个）` : ""}`}
                  {getHotwordReasonLabel(record.hotwords.reason)
                    ? ` · ${getHotwordReasonLabel(record.hotwords.reason)}`
                    : ""}
                </small>
              </div>
            )}
            {record.lastError && (
              <div className="history-context-item history-error-context">
                <span>最近错误</span>
                <small title={record.lastError.message}>
                  {record.lastError.stage} · {record.lastError.message}
                </small>
              </div>
            )}
            <div className="history-context-item knowledge-context">
              <BookOpen size={14} />
              <span>知识范围</span>
              <small>
                {record.documents && record.documents.length > 0
                  ? record.documents.map((doc) => doc.name).join("、")
                  : "未使用知识文档，建议均为经验判断"}
              </small>
            </div>
            {audioError && <span className="warn-text">{audioError}</span>}
          </div>
        </div>

        {(effectiveDiarizeMsg || effectiveMinutesMsg) && (
          <div className="history-operation-note">
            {effectiveDiarizeMsg || effectiveMinutesMsg}
          </div>
        )}

        <div className="record-summary-strip">
          <div>
            <strong>{stats.transcript}</strong>
            <span>段发言</span>
          </div>
          <SpeakerDistributionPopover
            count={stats.speakers}
            distribution={speakerDistribution}
          />
          <div>
            <strong>{record.batches.length}</strong>
            <span>批建议</span>
          </div>
          <div>
            <strong>{stats.suggestions}</strong>
            <span>条话术</span>
          </div>
        </div>
      </header>

      <div className="history-review-grid">
        <section className="history-record-panel">
          <header>
            <div>
              <span className="channel-dot cyan" />
              <strong>完整转写</strong>
            </div>
            <div className="history-panel-actions">
              <div
                className="transcript-version-switch"
                aria-label="转写版本"
              >
                <button
                  className={activeVersion === "realtime" ? "active" : ""}
                  onClick={() => switchTranscriptVersion("realtime")}
                  disabled={!record.transcriptVersions?.realtime}
                >
                  实时转写
                </button>
                {record.transcriptVersions?.offline && (
                  <button
                    className={activeVersion === "offline" ? "active" : ""}
                    onClick={() => switchTranscriptVersion("offline")}
                  >
                    会后整理
                  </button>
                )}
              </div>
              {record.transcriptVersions?.[activeVersion]?.editedAt && (
                <span className="transcript-edited-status">
                  <Check size={11} /> 正文已校对
                </span>
              )}
              {cleanupMeta && (
                <span
                  className={
                    cleanupMeta.status === "failed"
                      ? "warn-text"
                      : "transcript-edited-status"
                  }
                  title={cleanupMeta.reason || undefined}
                >
                  {cleanupMeta.status === "ok"
                    ? `会后整理 ${cleanupMeta.changed} 段${
                        cleanupMeta.fallbackChunks.length
                          ? `，${cleanupMeta.fallbackChunks.length} 个分块保留原文`
                          : ""
                      }`
                    : cleanupMeta.status === "failed"
                      ? "会后整理失败，保留原文"
                      : "未执行会后整理"}
                </span>
              )}
              {audioUrl && playbackPlaying ? (
                followPlayback ? (
                  <span className="playback-follow-status">
                    <Radio size={11} /> 正在跟随
                  </span>
                ) : (
                  <button
                    className="text-button playback-follow-button"
                    onClick={resumePlaybackFollow}
                  >
                    <Radio size={11} /> 恢复跟随
                  </button>
                )
              ) : (
                <span>按会议时间排列</span>
              )}
              {locatedBatchId && locatedApproximate && (
                <span className="location-approximate">近似定位</span>
              )}
            </div>
          </header>
          <div
            className="history-record-body"
            ref={transcriptBodyRef}
            onWheel={() => {
              if (playbackPlaying) setFollowPlayback(false);
            }}
            onTouchStart={() => {
              if (playbackPlaying) setFollowPlayback(false);
            }}
          >
            {record.transcript.length === 0 ? (
              <EmptyState
                icon={Volume2}
                title="没有保存到转写"
                detail="会议可能在产生第一段最终结果前结束。"
              />
            ) : (
              displayTranscript.map((item) => {
                const shown = nameOf(item);
                const mine = shown === "我";
                return (
                  <article
                    className={`utterance ${mine ? "mine" : ""}`}
                    key={item.segmentIds.join("-") || item.id}
                    style={
                      {
                        "--speaker-color": speakerColor(
                          item.speakerId || `name:${item.speaker}`,
                          mine,
                        ),
                      } as React.CSSProperties
                    }
                    >
                      <div className="utterance-meta">
                      {item.speakerId ? (
                        <button
                          className="speaker-name actionable"
                          onClick={(event) => {
                            const rect = event.currentTarget.getBoundingClientRect();
                            setSegmentMenu(null);
                            setEditing(null);
                            const pos = placeFixedMenu(rect, {
                              width: 260,
                              estimatedHeight: 280,
                            });
                            setSpeakerProfileMenu({
                              speakerId: item.speakerId!,
                              x: pos.x,
                              y: pos.y,
                              openAbove: pos.openAbove,
                            });
                          }}
                          title="点击：调整这位说话人（全部发言）"
                        >
                          {shown}
                          <ChevronDown size={12} />
                        </button>
                      ) : (
                        <span className="speaker-name">{shown}</span>
                      )}
                      <time>{formatTime(item.at)}</time>
                    </div>
                    <div className="utterance-body">
                      {groupSegmentsByPause(item.segments).map((line) => {
                        const lineIsPlaying =
                          playbackActiveId != null &&
                          line.ids.includes(playbackActiveId);
                        const lineIsLocated = line.ids.some((id) =>
                          locatedTranscriptIds.includes(id),
                        );
                        const lineHasApproximateTime = line.ids.some(
                          (id) => playbackRangeById.get(id)?.approximate,
                        );
                        const hasRelatedSuggestion = canLocateSuggestionForTranscript(
                          line.ids,
                        );
                        return (
                          <div
                            className={`utterance-line-row ${
                              record.audioPath ? "has-playback" : "no-playback"
                            } ${
                              lineIsPlaying ? "is-playing" : ""
                            } ${
                              lineIsLocated ? "is-located" : ""
                            } ${
                              editingTranscript?.ids[0] === line.id
                                ? "is-editing"
                                : ""
                            }`}
                            key={line.id}
                            ref={(node) => {
                              for (const id of line.ids) {
                                if (node) transcriptLineRefs.current.set(id, node);
                                else transcriptLineRefs.current.delete(id);
                              }
                            }}
                          >
                            {record.audioPath && (
                              <button
                                className="playback-jump-button"
                                aria-label={`从这里播放${
                                  lineHasApproximateTime ? "（旧记录，时间为近似值）" : ""
                                }`}
                                title={`从这里播放${
                                  lineHasApproximateTime ? "（旧记录约提前 3 秒校准）" : ""
                                }`}
                                onClick={() => void jumpToTranscriptLine(line.ids)}
                              >
                                <Play size={11} />
                              </button>
                            )}
                            {editingTranscript?.ids[0] === line.id ? (
                              <form
                                className="transcript-correction-form"
                                onSubmit={(event) => {
                                  event.preventDefault();
                                  commitTranscriptCorrection();
                                }}
                              >
                                <textarea
                                  autoFocus
                                  aria-label="修正转写正文"
                                  value={editingTranscript.value}
                                  rows={Math.min(
                                    6,
                                    Math.max(
                                      2,
                                      Math.ceil(editingTranscript.value.length / 42),
                                    ),
                                  )}
                                  onChange={(event) =>
                                    setEditingTranscript({
                                      ...editingTranscript,
                                      value: event.target.value,
                                    })
                                  }
                                  onKeyDown={(event) => {
                                    if (event.key === "Escape") {
                                      event.preventDefault();
                                      setEditingTranscript(null);
                                    }
                                    if (
                                      event.key === "Enter" &&
                                      !event.shiftKey &&
                                      !event.nativeEvent.isComposing
                                    ) {
                                      event.preventDefault();
                                      commitTranscriptCorrection();
                                    }
                                  }}
                                />
                                <div className="transcript-correction-actions">
                                  <span>Enter 保存 · Shift+Enter 换行</span>
                                  <button
                                    className="icon-button"
                                    type="button"
                                    title="取消"
                                    aria-label="取消正文修改"
                                    onClick={() => setEditingTranscript(null)}
                                  >
                                    <X size={13} />
                                  </button>
                                  <button
                                    className="icon-button save"
                                    type="submit"
                                    title="保存"
                                    aria-label="保存正文修改"
                                  >
                                    <Check size={13} />
                                  </button>
                                </div>
                              </form>
                            ) : (
                              <>
                                <p
                                  className="utterance-text actionable-line"
                                  title="点击：只改这一段的归属；右侧铅笔修正正文"
                                  onClick={(event) => {
                                    const rect = (
                                      event.currentTarget as HTMLElement
                                    ).getBoundingClientRect();
                                    setSpeakerProfileMenu(null);
                                    const pos = placeFixedMenu(rect, {
                                      width: 240,
                                      estimatedHeight: 220,
                                    });
                                    setSegmentMenu({
                                      ids: line.ids,
                                      speakerId: item.speakerId ?? null,
                                      x: pos.x,
                                      y: pos.y,
                                      openAbove: pos.openAbove,
                                    });
                                  }}
                                >
                                  {line.text}
                                </p>
                                <button
                                  className="locate-suggestion-line-button"
                                  type="button"
                                  disabled={!hasRelatedSuggestion}
                                  title={
                                    hasRelatedSuggestion
                                      ? "定位相关话术"
                                      : "这段转写没有关联话术建议"
                                  }
                                  aria-label={
                                    hasRelatedSuggestion
                                      ? "定位相关话术"
                                      : "没有关联话术建议"
                                  }
                                  onClick={() =>
                                    locateSuggestionFromTranscript(line.ids)
                                  }
                                >
                                  <MessageSquareText size={12} />
                                </button>
                                <button
                                  className="transcript-edit-button"
                                  type="button"
                                  title="修正这段转写正文"
                                  aria-label="修正这段转写正文"
                                  onClick={() => {
                                    setSegmentMenu(null);
                                    setEditingTranscript({
                                      ids: line.ids,
                                      value: line.text,
                                      original: line.text,
                                    });
                                  }}
                                >
                                  <Pencil size={12} />
                                </button>
                              </>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  </article>
                );
              })
            )}
          </div>
        </section>

        <section className="history-record-panel suggestion-history-panel">
          <header>
            <div className="review-tabs" role="tablist">
              <button
                className={reviewTab === "suggestions" ? "active" : ""}
                onClick={() => setReviewTab("suggestions")}
                role="tab"
                aria-selected={reviewTab === "suggestions"}
              >
                话术记录
              </button>
              <button
                className={reviewTab === "minutes" ? "active" : ""}
                onClick={() => setReviewTab("minutes")}
                role="tab"
                aria-selected={reviewTab === "minutes"}
              >
                会议纪要
              </button>
              <button
                className={reviewTab === "review" ? "active" : ""}
                onClick={() => setReviewTab("review")}
                role="tab"
                aria-selected={reviewTab === "review"}
              >
                会议复盘
              </button>
            </div>
            {reviewTab === "suggestions" ? (
              <span>生成依据可回溯</span>
            ) : reviewTab === "minutes" ? (
              <div className="minutes-actions">
                {record.minutes?.content && (
                  <button
                    className="text-button"
                    onClick={() => onCopy(stripMinutesEvidenceMarkers(record.minutes!.content))}
                  >
                    <Copy size={12} /> 复制
                  </button>
                )}
                <button
                  className="button primary small"
                  disabled={isGeneratingMinutes}
                  onClick={() => void generateMinutes()}
                >
                  {isGeneratingMinutes ? (
                    <>
                      <Loader2 size={13} className="spin" /> 生成中…
                    </>
                  ) : record.minutes?.content ? (
                    "重新生成"
                  ) : (
                    "生成纪要"
                  )}
                </button>
              </div>
            ) : (
              <div className="minutes-actions">
                <button
                  className="button ghost small"
                  disabled={isReviewGenerating}
                  onClick={() => void generateReview(false)}
                >
                  {isReviewGenerating ? "生成中…" : "刷新本地复盘"}
                </button>
                <button
                  className="button primary small"
                  disabled={isReviewGenerating || reviewItems.length === 0}
                  onClick={() => void generateReview(true)}
                >
                  {isReviewGenerating ? "增强中…" : "模型增强"}
                </button>
              </div>
            )}
          </header>
          <div
            className="history-record-body suggestion-history-body"
            ref={suggestionBodyRef}
          >
            {reviewTab === "suggestions" ? (
              record.batches.length === 0 ? (
                <EmptyState
                  icon={Sparkles}
                  title="这场会议没有建议"
                  detail="转写仍已保留，可继续用于会后整理。"
                />
              ) : (
                record.batches.map((batch, index) => (
                  <SuggestionBatchGroup
                    key={batch.id}
                    batch={batch}
                    isLatest={index === 0}
                    latestLabel="最后一轮"
                    onCopy={onCopy}
                    onLocateTranscript={locateSuggestionBatch}
                    canLocateTranscript={record.transcript.length > 0}
                    isLocated={locatedBatchId === batch.id}
                    batchRef={(node) => {
                      if (node) suggestionBatchRefs.current.set(batch.id, node);
                      else suggestionBatchRefs.current.delete(batch.id);
                    }}
                  />
                ))
              )
            ) : reviewTab === "review" ? (
              <HistoryReviewPanel
                items={reviewItems}
                candidates={reviewCandidates}
                message={effectiveReviewMsg}
                promoting={promotingGlossary}
                onUpdateItem={updateReviewItem}
                onToggleCandidate={(id) =>
                  setReviewCandidates((current) =>
                    current.map((item) =>
                      item.id === id ? { ...item, selected: !item.selected } : item,
                    ),
                  )
                }
                onPromote={promoteReviewCandidates}
              />
            ) : record.minutes?.content ? (
              <article className="minutes-document">
                {minutesStale && (
                  <div className="minutes-stale-note">
                    <ShieldAlert size={14} />
                    <span>转写正文后来有修改，这份纪要可能已过期，请重新生成。</span>
                  </div>
                )}
                <div className="minutes-source">
                  <FileText size={14} />
                  <span>
                    基于
                    {record.minutes.sourceVersion === "offline"
                      ? "会后整理"
                      : "实时转写"}
                    生成 · {formatRecordDate(record.minutes.generatedAt)}
                  </span>
                </div>
                <MarkdownDocument
                  source={record.minutes.content}
                  onEvidenceClick={(transcriptId) => {
                    void jumpToTranscriptLine([transcriptId]);
                  }}
                />
              </article>
            ) : (
              <EmptyState
                icon={FileText}
                title={isGeneratingMinutes ? "正在生成会议纪要" : "还没有会议纪要"}
                detail={
                  effectiveMinutesMsg ||
                  `将基于当前的${
                    activeVersion === "offline" ? "会后整理" : "实时转写"
                  }，整理结论、待办、约束与风险。`
                }
              />
            )}
          </div>
        </section>
      </div>

      {segmentMenu && (() => {
        const targets = Object.entries(speakerNames).filter(
          ([id]) => id !== segmentMenu.speakerId,
        );
        const hasOtherTarget = targets.some(
          ([id, name]) =>
            id === "other" || name === "对方" || !meSpeakerIds.has(id),
        );
        return (
          <>
            <div className="menu-backdrop" onClick={() => setSegmentMenu(null)} />
            <div
              className="speaker-menu"
              style={fixedMenuStyle(segmentMenu)}
            >
              <div className="menu-label">只改这一段的归属</div>
              {targets.map(([id, name]) => (
                <button
                  key={`reassign-${id}`}
                  onClick={() => {
                    reassignSegments(segmentMenu.ids, id);
                    setSegmentMenu(null);
                  }}
                >
                  {meSpeakerIds.has(id) ? "我" : name}
                </button>
              ))}
              {!hasOtherTarget && (
                <button
                  onClick={() => {
                    reassignSegmentsToOther(segmentMenu.ids);
                    setSegmentMenu(null);
                  }}
                >
                  对方
                </button>
              )}
              <div className="menu-divider" />
              <button
                onClick={() => {
                  splitSegmentsToNewSpeaker(segmentMenu.ids);
                  setSegmentMenu(null);
                }}
              >
                <Split size={13} /> 新建说话人（第三人）
              </button>
            </div>
          </>
        );
      })()}
      {speakerProfileMenu && (
        <>
          <div
            className="menu-backdrop"
            onClick={() => {
              setSpeakerProfileMenu(null);
              setEditing(null);
            }}
          />
          <div
            className="speaker-menu history-speaker-menu"
            style={fixedMenuStyle(speakerProfileMenu)}
          >
            {editing?.id === speakerProfileMenu.speakerId ? (
              <form
                className="rename-form"
                onSubmit={(event) => {
                  event.preventDefault();
                  commitRename();
                }}
              >
                <input
                  autoFocus
                  aria-label="说话人名称"
                  value={editing.value}
                  onChange={(event) =>
                    setEditing({ ...editing, value: event.target.value })
                  }
                  onKeyDown={(event) => {
                    if (event.key === "Escape") {
                      setEditing(null);
                      setSpeakerProfileMenu(null);
                    }
                  }}
                />
                <button className="button primary tiny" type="submit">
                  保存
                </button>
              </form>
            ) : (
              <>
                <div className="menu-label">调整这位说话人的全部发言</div>
                <button
                  onClick={() =>
                    setEditing({
                      id: speakerProfileMenu.speakerId,
                      value:
                        speakerNames[speakerProfileMenu.speakerId] ||
                        speakerProfileMenu.speakerId,
                    })
                  }
                >
                  <Pencil size={13} /> 修改说话人名称
                </button>
                <div className="menu-divider" />
                <div className="menu-label">
                  将该说话人的全部发言改派给
                </div>
                {activeSpeakerIds
                  .filter((id) => id !== speakerProfileMenu.speakerId)
                  .map((id) => (
                    <button
                      key={`merge-speaker-${id}`}
                      onClick={() =>
                        reassignSpeakerAll(speakerProfileMenu.speakerId, id)
                      }
                    >
                      <span>{meSpeakerIds.has(id) ? "我" : speakerNames[id]}</span>
                      <small>{speakerSegmentCounts[id] || 0} 段</small>
                    </button>
                  ))}
                {activeSpeakerIds.length <= 1 && (
                  <button
                    onClick={() =>
                      reassignSpeakerAll(
                        speakerProfileMenu.speakerId,
                        "other",
                        "对方",
                      )
                    }
                  >
                    全部改派给「对方」
                  </button>
                )}
              </>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function HistoryReviewPanel({
  items,
  candidates,
  message,
  promoting,
  onUpdateItem,
  onToggleCandidate,
  onPromote,
}: {
  items: MeetingMemoryItem[];
  candidates: GlossaryCandidate[];
  message: string;
  promoting: boolean;
  onUpdateItem: (id: string, patch: Partial<MeetingMemoryItem>) => void;
  onToggleCandidate: (id: string) => void;
  onPromote: () => void;
}) {
  const [editing, setEditing] = useState<{ id: string; value: string } | null>(
    null,
  );
  const [view, setView] = useState<"pending" | "processed" | "glossary">("pending");
  const pendingItems = items.filter((item) => item.status === "candidate");
  const confirmedItems = items.filter((item) => item.status === "confirmed");
  const rejectedItems = items.filter((item) => item.status === "rejected");
  const processedItems = [...confirmedItems, ...rejectedItems];
  const pendingDecisions = pendingItems.filter((item) => item.kind === "decision");
  const pendingActions = pendingItems.filter((item) => item.kind === "action_item");
  const selectedCandidates = candidates.filter((item) => item.selected);

  function renderMemoryItem(item: MeetingMemoryItem) {
    const processed = item.status !== "candidate";
    return (
      <article
        className={`review-memory-item ${item.status} ${processed ? "processed" : ""}`}
        key={item.id}
      >
        <div className="review-memory-main">
          <div className="review-memory-labels">
            <span className={`memory-kind ${item.kind}`}>
              {item.kind === "decision" ? "决策" : "待办"}
            </span>
            <span className={`review-item-status ${item.status}`}>
              {item.status === "confirmed"
                ? "已确认"
                : item.status === "rejected"
                  ? "已驳回"
                  : "待处理"}
            </span>
          </div>
          {editing?.id === item.id ? (
            <input
              autoFocus
              value={editing.value}
              onChange={(event) =>
                setEditing({ id: item.id, value: event.target.value })
              }
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  onUpdateItem(item.id, {
                    content: editing.value.trim() || item.content,
                    status: "candidate",
                  });
                  setEditing(null);
                }
                if (event.key === "Escape") setEditing(null);
              }}
            />
          ) : (
            <p>{item.content}</p>
          )}
        </div>
        <div className="review-memory-meta">
          {(item.owner || item.dueAt) && (
            <small>
              {[item.owner && `负责人 ${item.owner}`, item.dueAt && `截止 ${item.dueAt}`]
                .filter(Boolean)
                .join(" · ")}
            </small>
          )}
          {item.evidenceText && <small>依据：{item.evidenceText}</small>}
        </div>
        <div className="review-memory-actions">
          {processed ? (
            <>
              <button
                type="button"
                onClick={() => setEditing({ id: item.id, value: item.content })}
              >
                <Pencil size={12} /> 编辑
              </button>
              <button
                type="button"
                className="restore"
                onClick={() => onUpdateItem(item.id, { status: "candidate" })}
              >
                <RotateCcw size={12} /> 恢复待处理
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                className={item.status === "confirmed" ? "active" : ""}
                onClick={() => onUpdateItem(item.id, { status: "confirmed" })}
              >
                <Check size={12} /> 确认
              </button>
              <button
                type="button"
                onClick={() => setEditing({ id: item.id, value: item.content })}
              >
                <Pencil size={12} /> 编辑
              </button>
              <button
                type="button"
                onClick={() => onUpdateItem(item.id, { status: "rejected" })}
              >
                <X size={12} /> 驳回
              </button>
            </>
          )}
        </div>
      </article>
    );
  }

  function renderMemoryBucket(title: string, bucket: MeetingMemoryItem[]) {
    if (!bucket.length) return null;
    return (
      <div className="review-memory-bucket">
        <div className="review-memory-bucket-head">
          <strong>{title}</strong>
          <span>{bucket.length} 条</span>
        </div>
        <div className="review-memory-list">{bucket.map(renderMemoryItem)}</div>
      </div>
    );
  }

  return (
    <div className="history-review-panel">
      <div className="review-panel-intro">
        <div>
          <strong>把这场会议沉淀成可复用信息</strong>
          <p>
            先保留规则提取结果，再由你确认。确认后的决策和待办会优先进入纪要，词候选可批量加入全局词库。
          </p>
        </div>
        <span className="review-status-mark">
          {pendingItems.length
            ? `${pendingItems.length} 条待处理`
            : processedItems.length
              ? "本场项目已处理"
              : candidates.length
                ? `${candidates.length} 个领域词候选`
                : "暂无候选"}
        </span>
      </div>
      {message && (
        <div
          className={`review-panel-message ${
            message.includes("失败")
              ? "error"
              : message.includes("没有新增")
                ? "notice"
                : "success"
          }`}
        >
          {message.includes("失败") ? (
            <ShieldAlert size={14} />
          ) : (
            <Check size={14} />
          )}
          <span>{message}</span>
        </div>
      )}

      <div className="review-view-tabs" role="tablist" aria-label="复盘分组">
        <button
          type="button"
          className={view === "pending" ? "active" : ""}
          onClick={() => setView("pending")}
          role="tab"
          aria-selected={view === "pending"}
        >
          <span>待处理 <em>{pendingItems.length}</em></span>
          <small>决策 {pendingDecisions.length} · 待办 {pendingActions.length}</small>
        </button>
        <button
          type="button"
          className={view === "processed" ? "active" : ""}
          onClick={() => setView("processed")}
          role="tab"
          aria-selected={view === "processed"}
        >
          <span>已处理 <em>{processedItems.length}</em></span>
          <small>确认 {confirmedItems.length} · 驳回 {rejectedItems.length}</small>
        </button>
        <button
          type="button"
          className={view === "glossary" ? "active" : ""}
          onClick={() => setView("glossary")}
          role="tab"
          aria-selected={view === "glossary"}
        >
          <span>领域词候选 <em>{candidates.length}</em></span>
          <small>已选 {selectedCandidates.length} · 可加入全局词库</small>
        </button>
      </div>

      {view === "pending" && (
        <section className="review-panel-section">
          <header>
            <div>
              <h3>待处理的决策与待办</h3>
              <small>先确认事实，再决定是否进入正式纪要。</small>
            </div>
            <span>{pendingItems.length} 条</span>
          </header>
          {pendingItems.length === 0 ? (
            <div className="review-panel-empty">没有待处理项；已处理内容请到“已处理”查看。</div>
          ) : (
            <>
              {renderMemoryBucket("需要确认的决策", pendingDecisions)}
              {renderMemoryBucket("需要跟进的待办", pendingActions)}
            </>
          )}
        </section>
      )}

      {view === "processed" && (
        <section className="review-panel-section processed-review-section">
          <header>
            <div>
              <h3>已处理的决策与待办</h3>
              <small>确认和驳回都会保留；需要重新处理时可以恢复。</small>
            </div>
            <span>{processedItems.length} 条</span>
          </header>
          {processedItems.length === 0 ? (
            <div className="review-panel-empty">还没有确认或驳回的项目。</div>
          ) : (
            <>
              {renderMemoryBucket("已确认", confirmedItems)}
              {renderMemoryBucket("已驳回", rejectedItems)}
            </>
          )}
        </section>
      )}

      {view === "glossary" && (
        <section className="review-panel-section glossary-candidate-review">
          <header>
            <div>
              <h3>领域词候选</h3>
              <small>与决策/待办分开管理；勾选后可批量加入全局词库。</small>
            </div>
            <span>{candidates.length} 个候选词</span>
          </header>
          <div className="review-glossary-actions">
            <small>已选择 {selectedCandidates.length} 个</small>
            <button
              type="button"
              className="button secondary small"
              disabled={!selectedCandidates.length || promoting}
              onClick={onPromote}
            >
              {promoting ? (
                <>
                  <Loader2 size={13} className="spin" /> 加入中…
                </>
              ) : (
                <>
                  <Tags size={13} /> 加入全局词库
                </>
              )}
            </button>
          </div>
          {candidates.length === 0 ? (
            <div className="review-panel-empty">暂未发现需要维护的领域词。</div>
          ) : (
            <div className="review-glossary-list">
              {candidates.map((candidate) => (
                <label className={`review-glossary-item ${candidate.selected ? "selected" : ""}`} key={candidate.id}>
                  <input
                    type="checkbox"
                    checked={Boolean(candidate.selected)}
                    onChange={() => onToggleCandidate(candidate.id)}
                  />
                  <span className="review-glossary-term">
                    <strong>{candidate.term}</strong>
                    <small>
                      出现 {candidate.frequency} 次 · 权重 {candidate.weight} · {candidate.reason}
                    </small>
                    {candidate.sampleContext && <em>“{candidate.sampleContext}”</em>}
                  </span>
                </label>
              ))}
            </div>
          )}
        </section>
      )}
    </div>
  );
}

function formatBytes(size: number) {
  if (!size) return "—";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

/**
 * 专有名词维护面板。
 * scope=general → 通用词库；scope=projectId → 项目词库。
 * 始终可编辑；是否被当前 ASR 读取由设置页提示，不在此拦截。
 */
function GlossaryPanel({
  scope,
  title,
  description,
  onNotify,
  onTermsChanged,
}: {
  scope: "general" | string;
  title: string;
  description: string;
  onNotify: (value: string) => void;
  onTermsChanged?: () => void;
}) {
  const bridge = window.meetingCopilot;
  const [terms, setTerms] = useState<GlossaryTerm[]>([]);
  const [draft, setDraft] = useState("");
  const [weight, setWeight] = useState(4);
  const [busy, setBusy] = useState(false);
  const [termSearch, setTermSearch] = useState("");
  const isGeneral = scope === "general";

  const reload = useCallback(async () => {
    if (!bridge?.listGlossaryTerms) {
      setTerms([]);
      return;
    }
    try {
      const list = await bridge.listGlossaryTerms(
        isGeneral ? "general" : scope,
      );
      setTerms(list);
    } catch {
      setTerms([]);
    }
  }, [bridge, isGeneral, scope]);

  useEffect(() => {
    void reload();
  }, [reload]);

  async function addTerm() {
    const term = draft.trim();
    if (!term || !bridge?.saveGlossaryTerm) return;
    setBusy(true);
    try {
      await bridge.saveGlossaryTerm({
        term,
        weight,
        projectId: isGeneral ? null : scope,
      });
      setDraft("");
      await reload();
      onTermsChanged?.();
      onNotify(`已添加专有名词「${term}」`);
    } catch (error) {
      onNotify(error instanceof Error ? error.message : "添加失败");
    } finally {
      setBusy(false);
    }
  }

  async function removeTerm(item: GlossaryTerm) {
    if (!bridge?.deleteGlossaryTerm) return;
    await bridge.deleteGlossaryTerm(item.id);
    await reload();
    onTermsChanged?.();
    onNotify(`已删除「${item.term}」`);
  }

  const filteredTerms = terms.filter((t) => {
    if (!termSearch.trim()) return true;
    return t.term.toLowerCase().includes(termSearch.trim().toLowerCase());
  });

  return (
    <div className="glossary-detail-card">
      <div className="glossary-card-header">
        <div className="glossary-title-area">
          <div className="glossary-title-row">
            <h2>{title}</h2>
            <span className="meta-badge">共 {terms.length} 个专词</span>
          </div>
          <p className="glossary-desc-text">{description}</p>
        </div>
      </div>

      <div className="glossary-body-section">
        {/* 添加专有名词录入卡片 */}
        <div className="glossary-add-bar">
          <div className="glossary-input-wrap">
            <input
              value={draft}
              placeholder="输入专有名词 / 黑话 / 缩写，例如：三快、CAM++、SaaS网关"
              maxLength={20}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") void addTerm();
              }}
              disabled={!bridge || busy}
            />
          </div>
          <div className="glossary-weight-picker">
            <span>权重</span>
            <select
              value={weight}
              onChange={(event) => setWeight(Number(event.target.value) || 4)}
              disabled={!bridge || busy}
              title="权重越高，实时语音识别时越优先偏向匹配此词"
            >
              <option value={5}>5 (最高)</option>
              <option value={4}>4 (推荐)</option>
              <option value={3}>3 (普通)</option>
              <option value={2}>2 (较低)</option>
              <option value={1}>1 (最低)</option>
            </select>
          </div>
          <button
            className="button primary small"
            onClick={() => void addTerm()}
            disabled={!bridge || busy || !draft.trim()}
          >
            <Plus size={14} /> 添加专词
          </button>
        </div>

        {/* 专有名词过滤与列表区域 */}
        <div className="glossary-terms-container">
          {terms.length > 6 && (
            <div className="glossary-search-filter">
              <Search size={14} />
              <input
                placeholder="搜索当前列表中的专有名词..."
                value={termSearch}
                onChange={(e) => setTermSearch(e.target.value)}
              />
              {termSearch && (
                <button
                  className="icon-button small"
                  onClick={() => setTermSearch("")}
                >
                  <X size={12} />
                </button>
              )}
            </div>
          )}

          {terms.length === 0 ? (
            <div className="empty-hint glossary-empty-box">
              <Type size={36} />
              <p>当前词库还没有专有名词。</p>
              <small>
                建议录入业务专有名词、业务简称或容易被 ASR 误识别的黑话。
              </small>
            </div>
          ) : filteredTerms.length === 0 ? (
            <div className="empty-hint" style={{ padding: "32px 16px" }}>
              未找到匹配「{termSearch}」的专有名词
            </div>
          ) : (
            <div className="glossary-term-grid">
              {filteredTerms.map((item) => (
                <div className="glossary-term-chip" key={item.id}>
                  <strong className="term-text">{item.term}</strong>
                  <span
                    className={`term-weight-pill weight-${item.weight}`}
                    title={`识别加权: ${item.weight}`}
                  >
                    w{item.weight}
                  </span>
                  <button
                    type="button"
                    className="term-del-btn"
                    title={`删除「${item.term}」`}
                    aria-label={`删除 ${item.term}`}
                    onClick={() => void removeTerm(item)}
                  >
                    <X size={13} />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// 知识库页：纯文档库维护（导入 / 搜索过滤 / 重命名 / 预览 / 删除）。
function KnowledgeScreen({
  documents,
  onRefresh,
  onNotify,
}: {
  documents: KnowledgeDocument[];
  onRefresh: () => Promise<unknown>;
  onNotify: (value: string) => void;
}) {
  const bridge = window.meetingCopilot;
  const [preview, setPreview] = useState<{ name: string; text: string } | null>(
    null,
  );
  const [renamingDoc, setRenamingDoc] = useState<{ id: string; value: string } | null>(
    null,
  );
  const [isDragging, setIsDragging] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [filterTab, setFilterTab] = useState<"all" | "ready" | "missing">("all");

  const missingCount = documents.filter((doc) => !doc.exists).length;
  const readyCount = documents.length - missingCount;

  const filteredDocs = documents.filter((doc) => {
    if (filterTab === "ready" && !doc.exists) return false;
    if (filterTab === "missing" && doc.exists) return false;
    if (searchQuery.trim()) {
      const q = searchQuery.trim().toLowerCase();
      return doc.name.toLowerCase().includes(q) || (doc.path && doc.path.toLowerCase().includes(q));
    }
    return true;
  });

  async function commitRename() {
    if (!renamingDoc || !bridge) return;
    const name = renamingDoc.value.trim();
    if (!name) return setRenamingDoc(null);
    await bridge.renameDocument(renamingDoc.id, name);
    setRenamingDoc(null);
    await onRefresh();
    onNotify("已重命名（磁盘上的原文件未改动）");
  }

  async function importDocuments() {
    if (!bridge) return onNotify("桌面服务未连接，无法导入");
    const result = await bridge.pickDocuments();
    await onRefresh();
    if (result.errors?.length) {
      const first = result.errors[0];
      onNotify(
        `已加入 ${result.added} 份，${result.errors.length} 份未导入：${first.name}（${first.message}）`,
      );
      return;
    }
    onNotify(
      result.added > 0
        ? `已加入 ${result.added} 份文档`
        : "没有新增文档（可能已存在或格式不支持）",
    );
  }

  async function importFolder() {
    if (!bridge) return onNotify("桌面服务未连接，无法导入");
    const result = await bridge.pickDocumentFolder();
    await onRefresh();
    if (result.errors?.length) {
      const first = result.errors[0];
      onNotify(
        `扫描发现 ${result.discoveredCount || 0} 份文档，成功加入 ${result.added} 份，${result.errors.length} 份未导入：${first.name}（${first.message}）`,
      );
      return;
    }
    onNotify(
      result.added > 0
        ? `扫描发现 ${result.discoveredCount || result.added} 份文档，已成功导入 ${result.added} 份`
        : "所选文件夹内未发现支持的文档（支持 .md/.txt/.docx/.pdf）",
    );
  }

  async function handleDrop(event: React.DragEvent) {
    event.preventDefault();
    event.stopPropagation();
    setIsDragging(false);
    if (!bridge) return onNotify("桌面服务未连接，无法导入");
    const files = Array.from(event.dataTransfer.files || []);
    if (!files.length) return;
    const filePaths = files
      .map((f) => {
        if (bridge?.getPathForFile) {
          const p = bridge.getPathForFile(f);
          if (p) return p;
        }
        return (f as unknown as { path?: string }).path || "";
      })
      .filter(Boolean);
    if (!filePaths.length) {
      onNotify("未能获取拖拽文件路径，请点击右上角「导入文件」选择");
      return;
    }
    const result = await bridge.addDocumentPaths(filePaths);
    await onRefresh();
    if (result.errors?.length) {
      const first = result.errors[0];
      onNotify(
        `拖拽导入：成功加入 ${result.added} 份，${result.errors.length} 份未导入：${first.name}（${first.message}）`,
      );
      return;
    }
    onNotify(
      result.added > 0
        ? `拖拽导入：已成功加入 ${result.added} 份文档（扫描了 ${result.discoveredCount || result.added} 个文件）`
        : "未导入新文档（可能已存在或文件夹内无支持格式）",
    );
  }

  function handleDragOver(event: React.DragEvent) {
    event.preventDefault();
    event.stopPropagation();
    if (!isDragging) setIsDragging(true);
  }

  function handleDragLeave(event: React.DragEvent) {
    event.preventDefault();
    event.stopPropagation();
    setIsDragging(false);
  }

  async function removeDoc(doc: KnowledgeDocument) {
    if (!bridge) return;
    await bridge.removeDocument(doc.id);
    await onRefresh();
    onNotify(`已从库中移除「${doc.name}」（原文件保留在磁盘上）`);
  }

  async function previewDoc(doc: KnowledgeDocument) {
    if (!bridge) return;
    const result = await bridge.previewDocument(doc.path);
    if (result.ok) setPreview({ name: doc.name, text: result.text || "" });
    else onNotify(result.message || "无法预览");
  }

  return (
    <div
      className={`page knowledge-page ${isDragging ? "dragging-over" : ""}`}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      {isDragging && (
        <div className="knowledge-drag-overlay">
          <UploadCloud size={44} />
          <strong>释放以导入文档或整个文件夹</strong>
          <span>自动递归扫描所有 .md、.txt、.docx、.pdf 文档并登记到全局库</span>
        </div>
      )}
      <header className="page-heading">
        <div>
          <div className="eyebrow">证据来源</div>
          <h1>知识库</h1>
          <p>
            全局文档中枢。开会与评审时检索建议的原文证据来自这里。支持直接拖拽文件或文件夹到本页面导入。
          </p>
        </div>
        <div className="page-heading-actions">
          <button
            className="button secondary"
            onClick={() => void importFolder()}
            disabled={!bridge}
            title="选择文件夹，递归扫描并导入其中的所有知识文档"
          >
            <FolderPlus size={16} /> 导入文件夹
          </button>
          <button
            className="button primary"
            onClick={() => void importDocuments()}
            disabled={!bridge}
            title="选择单个或多个文档文件导入"
          >
            <Plus size={16} /> 导入文件
          </button>
        </div>
      </header>

      {/* 顶部统计卡片 */}
      <div className="knowledge-summary">
        <div className="summary-stat-box">
          <Library size={22} />
          <span>
            <strong>{documents.length} 份文档</strong>
            <small>已登记的全局证据文档</small>
          </span>
        </div>
        <div className="summary-stat-box">
          <Gauge size={22} />
          <span>
            <strong>本地关键词与向量检索</strong>
            <small>BM25 + 中文切词 · 毫秒级匹配</small>
          </span>
        </div>
        {missingCount > 0 ? (
          <div className="summary-stat-box warn-tile">
            <ShieldAlert size={22} />
            <span>
              <strong>{missingCount} 份已失效</strong>
              <small>源文件已被移走或删除</small>
            </span>
          </div>
        ) : (
          <div className="summary-stat-box success-tile">
            <Check size={22} />
            <span>
              <strong>全部文档可用</strong>
              <small>所有路径校验正常</small>
            </span>
          </div>
        )}
      </div>

      {/* 知识库主卡片 */}
      <div className="knowledge-main-card">
        {/* 工具栏：搜索 + 状态过滤 */}
        <div className="knowledge-toolbar">
          <div className="toolbar-search-group">
            <div className="knowledge-search-input">
              <Search size={14} />
              <input
                placeholder="搜索文档名称或路径..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
              />
              {searchQuery && (
                <button
                  className="icon-button small"
                  onClick={() => setSearchQuery("")}
                >
                  <X size={12} />
                </button>
              )}
            </div>
            <div className="knowledge-filter-pills">
              <button
                type="button"
                className={`filter-pill ${filterTab === "all" ? "active" : ""}`}
                onClick={() => setFilterTab("all")}
              >
                全部 ({documents.length})
              </button>
              <button
                type="button"
                className={`filter-pill ${filterTab === "ready" ? "active" : ""}`}
                onClick={() => setFilterTab("ready")}
              >
                可检索 ({readyCount})
              </button>
              {missingCount > 0 && (
                <button
                  type="button"
                  className={`filter-pill warn ${filterTab === "missing" ? "active" : ""}`}
                  onClick={() => setFilterTab("missing")}
                >
                  已失效 ({missingCount})
                </button>
              )}
            </div>
          </div>
        </div>

        {/* 文档列表区 */}
        <div className="knowledge-doc-container">
          {documents.length === 0 ? (
            <div className="empty-hint knowledge-empty-dropzone">
              <UploadCloud size={40} />
              <p>知识库还没有文档。点击上方<strong>「导入文件 / 文件夹」</strong>或<strong>直接拖拽文件到这里</strong>。</p>
              <small>支持 .md / .txt / .docx / .pdf 格式，按原路径快速索引，不占用冗余磁盘空间。</small>
            </div>
          ) : filteredDocs.length === 0 ? (
            <div className="empty-hint" style={{ padding: "48px 20px" }}>
              <p>未找到符合条件的文档</p>
              {searchQuery && (
                <button
                  className="button secondary small"
                  style={{ marginTop: 10 }}
                  onClick={() => setSearchQuery("")}
                >
                  清空搜索
                </button>
              )}
            </div>
          ) : (
            <div className="knowledge-doc-rows">
              {filteredDocs.map((doc) => (
                <div
                  className={`knowledge-doc-row ${doc.exists ? "" : "missing"}`}
                  key={doc.id}
                >
                  <div className="doc-row-icon">
                    <FileText size={18} />
                  </div>
                  <div className="doc-row-content">
                    {renamingDoc?.id === doc.id ? (
                      <form
                        className="rename-form"
                        onSubmit={(event) => {
                          event.preventDefault();
                          void commitRename();
                        }}
                      >
                        <input
                          autoFocus
                          value={renamingDoc.value}
                          onChange={(event) =>
                            setRenamingDoc({ ...renamingDoc, value: event.target.value })
                          }
                          onBlur={() => void commitRename()}
                          onKeyDown={(e) => {
                            if (e.key === "Escape") setRenamingDoc(null);
                          }}
                        />
                      </form>
                    ) : (
                      <div className="doc-name-line">
                        <strong
                          className="doc-name"
                          onDoubleClick={() =>
                            setRenamingDoc({ id: doc.id, value: doc.name })
                          }
                          title="双击可重命名"
                        >
                          {doc.name}
                        </strong>
                      </div>
                    )}
                    <div className="doc-row-meta">
                      <span>{doc.exists ? formatBytes(doc.size) : "大小未知"}</span>
                      <span className="dot-divider">·</span>
                      <span className="doc-path-text" title={doc.path}>
                        {doc.path}
                      </span>
                    </div>
                  </div>

                  <div className="doc-row-status">
                    {doc.exists ? (
                      <span className="ready-badge">
                        <Check size={12} /> 可检索
                      </span>
                    ) : (
                      <span className="missing-badge">
                        <ShieldAlert size={12} /> 已失效
                      </span>
                    )}
                  </div>

                  <div className="doc-row-actions">
                    <button
                      type="button"
                      className="button ghost small"
                      onClick={() => void previewDoc(doc)}
                      title="预览文档内容"
                    >
                      预览
                    </button>
                    <button
                      type="button"
                      className="button ghost small"
                      onClick={() => setRenamingDoc({ id: doc.id, value: doc.name })}
                      title="重命名显示名称"
                    >
                      <Pencil size={12} /> 改名
                    </button>
                    <button
                      type="button"
                      className="button ghost small danger"
                      aria-label="从库中移除"
                      onClick={() => void removeDoc(doc)}
                      title="从知识库移除（原文件仍保留在磁盘上）"
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* 文档内容预览弹窗 */}
      {preview && (
        <div className="preview-overlay" onClick={() => setPreview(null)}>
          <div className="preview-panel" onClick={(e) => e.stopPropagation()}>
            <header>
              <strong>{preview.name}</strong>
              <button className="icon-button" onClick={() => setPreview(null)}>
                <X size={16} />
              </button>
            </header>
            <pre>{preview.text}</pre>
          </div>
        </div>
      )}
    </div>
  );
}

// 项目页：项目增删改 + 维护每个项目的"可用资料"（Master-Detail 两栏布局）。
function ProjectsScreen({
  projects,
  documents,
  activeProjectId,
  onSelectProject,
  onRefresh,
  onNotify,
}: {
  projects: Project[];
  documents: KnowledgeDocument[];
  activeProjectId: string | null;
  onSelectProject: (id: string | null) => void;
  onRefresh: () => Promise<unknown>;
  onNotify: (value: string) => void;
}) {
  const bridge = window.meetingCopilot;
  const [isCreateModalOpen, setIsCreateModalOpen] = useState(false);
  const [newProjectName, setNewProjectName] = useState("");
  const [memberIds, setMemberIds] = useState<string[]>([]);
  const [editingName, setEditingName] = useState<{ id: string; value: string } | null>(
    null,
  );
  const [isDragging, setIsDragging] = useState(false);

  // 「从知识库挑选」模态框状态
  const [isPickModalOpen, setIsPickModalOpen] = useState(false);
  const [pickerSelectedIds, setPickerSelectedIds] = useState<string[]>([]);
  const [pickerSearch, setPickerSearch] = useState("");

  // 默认激活项守护：如果有项目但当前未选中或选中的不存在，自动选中第一个
  useEffect(() => {
    if (projects.length > 0) {
      if (!activeProjectId || !projects.some((p) => p.id === activeProjectId)) {
        onSelectProject(projects[0].id);
      }
    } else if (activeProjectId) {
      onSelectProject(null);
    }
  }, [projects, activeProjectId, onSelectProject]);

  const active = projects.find((p) => p.id === activeProjectId) || null;

  // 载入选中项目的可用资料勾选
  useEffect(() => {
    if (!bridge || !activeProjectId) {
      setMemberIds([]);
      return;
    }
    void bridge.getProjectDocuments(activeProjectId).then(setMemberIds).catch(() => {});
  }, [activeProjectId, projects]);

  // 当前项目关联的文档列表
  const projectDocs = documents.filter((doc) => memberIds.includes(doc.id));

  async function createProject() {
    const name = newProjectName.trim();
    if (!name || !bridge) return;
    const project = await bridge.saveProject({ name });
    setNewProjectName("");
    setIsCreateModalOpen(false);
    await onRefresh();
    onSelectProject(project.id);
    onNotify(`已创建项目「${name}」`);
  }

  async function renameProject() {
    if (!editingName || !bridge) return;
    const name = editingName.value.trim();
    if (!name) return setEditingName(null);
    await bridge.saveProject({ id: editingName.id, name });
    setEditingName(null);
    await onRefresh();
    onNotify("项目已重命名");
  }

  async function removeProject(project: Project) {
    if (!bridge) return;
    await bridge.deleteProject(project.id);
    if (activeProjectId === project.id) onSelectProject(null);
    await onRefresh();
    onNotify(`已删除项目「${project.name}」（知识库文档与历史会议均已保留）`);
  }

  // 从本项目移除某份文档关联
  async function removeMember(doc: KnowledgeDocument) {
    if (!bridge || !activeProjectId) return;
    const next = memberIds.filter((id) => id !== doc.id);
    setMemberIds(next);
    await bridge.setProjectDocuments(activeProjectId, next);
    await onRefresh();
    onNotify(`已从本项目移除「${doc.name}」（原文档保留在知识库中）`);
  }

  // 打开从知识库挑选弹窗
  function openPickModal() {
    setPickerSelectedIds([...memberIds]);
    setPickerSearch("");
    setIsPickModalOpen(true);
  }

  // 弹窗中切换单选
  function togglePickerDoc(docId: string) {
    setPickerSelectedIds((current) =>
      current.includes(docId)
        ? current.filter((id) => id !== docId)
        : [...current, docId],
    );
  }

  // 保存弹窗中的勾选结果
  async function savePickedDocs() {
    if (!bridge || !activeProjectId) return;
    await bridge.setProjectDocuments(activeProjectId, pickerSelectedIds);
    setMemberIds(pickerSelectedIds);
    setIsPickModalOpen(false);
    await onRefresh();
    onNotify(`已更新项目可用资料（共 ${pickerSelectedIds.length} 份）`);
  }

  async function importForProject() {
    if (!bridge || !activeProjectId) return;
    const result = await bridge.pickDocuments(activeProjectId);
    await onRefresh();
    const updatedMemberIds = await bridge.getProjectDocuments(activeProjectId);
    setMemberIds(updatedMemberIds);
    if (result.errors?.length) {
      const first = result.errors[0];
      onNotify(
        `已加入 ${result.added} 份，${result.errors.length} 份未导入：${first.name}（${first.message}）`,
      );
      return;
    }
    onNotify(
      result.added > 0
        ? `已导入 ${result.added} 份文档并关联到本项目`
        : "没有新增文档（可能已存在或格式不支持）",
    );
  }

  async function importFolderForProject() {
    if (!bridge || !activeProjectId) return;
    const result = await bridge.pickDocumentFolder(activeProjectId);
    await onRefresh();
    const updatedMemberIds = await bridge.getProjectDocuments(activeProjectId);
    setMemberIds(updatedMemberIds);
    if (result.errors?.length) {
      const first = result.errors[0];
      onNotify(
        `扫描发现 ${result.discoveredCount || 0} 份，加入 ${result.added} 份，${result.errors.length} 份未导入：${first.name}（${first.message}）`,
      );
      return;
    }
    onNotify(
      result.added > 0
        ? `已导入 ${result.added} 份文档并关联到本项目（扫描了 ${result.discoveredCount || result.added} 个文件）`
        : "文件夹内未发现支持的文档（支持 .md/.txt/.docx/.pdf）",
    );
  }

  async function handleDrop(event: React.DragEvent) {
    event.preventDefault();
    event.stopPropagation();
    setIsDragging(false);
    if (!bridge || !activeProjectId) return;
    const files = Array.from(event.dataTransfer.files || []);
    if (!files.length) return;
    const filePaths = files
      .map((f) => {
        if (bridge?.getPathForFile) {
          const p = bridge.getPathForFile(f);
          if (p) return p;
        }
        return (f as unknown as { path?: string }).path || "";
      })
      .filter(Boolean);
    if (!filePaths.length) {
      onNotify("未能获取拖拽文件路径，请点击上方「导入文件」选择");
      return;
    }
    const result = await bridge.addDocumentPaths(filePaths, activeProjectId);
    await onRefresh();
    const updatedMemberIds = await bridge.getProjectDocuments(activeProjectId);
    setMemberIds(updatedMemberIds);
    if (result.errors?.length) {
      const first = result.errors[0];
      onNotify(
        `拖拽导入：成功加入 ${result.added} 份，${result.errors.length} 份未导入：${first.name}（${first.message}）`,
      );
      return;
    }
    onNotify(
      result.added > 0
        ? `拖拽导入：已成功加入 ${result.added} 份文档并关联到本项目`
        : "未导入新文档（可能已存在或文件夹内无支持格式）",
    );
  }

  function handleDragOver(event: React.DragEvent) {
    event.preventDefault();
    event.stopPropagation();
    if (activeProjectId && !isDragging) setIsDragging(true);
  }

  function handleDragLeave(event: React.DragEvent) {
    event.preventDefault();
    event.stopPropagation();
    setIsDragging(false);
  }

  // 弹窗搜索过滤
  const filteredPickerDocs = documents.filter((doc) => {
    if (!pickerSearch.trim()) return true;
    return doc.name.toLowerCase().includes(pickerSearch.trim().toLowerCase());
  });

  return (
    <div
      className={`page projects-page ${isDragging ? "dragging-over" : ""}`}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      {isDragging && active && (
        <div className="knowledge-drag-overlay">
          <UploadCloud size={44} />
          <strong>释放以导入文档到项目「{active.name}」</strong>
          <span>自动登记知识库并关联到此项目</span>
        </div>
      )}

      <header className="page-heading">
        <div>
          <div className="eyebrow">业务空间</div>
          <h1>项目</h1>
          <p>
            为项目归集专属可用资料。会议绑定项目后仅检索本项目选中的文档，互不干扰。
          </p>
        </div>
        <div className="page-heading-actions">
          <button
            className="button primary"
            onClick={() => {
              setNewProjectName("");
              setIsCreateModalOpen(true);
            }}
          >
            <Plus size={16} /> 新建项目
          </button>
        </div>
      </header>

      <div className="projects-layout">
        {/* 左侧：项目导航列表 */}
        <aside className="projects-sidebar">
          <div className="projects-sidebar-header">
            <div className="sidebar-title">
              <FolderKanban size={15} />
              <span>所有项目</span>
              <span className="count-pill">{projects.length}</span>
            </div>
          </div>

          <div className="projects-nav-list">
            {projects.length === 0 ? (
              <div className="projects-sidebar-empty">
                <p>还没有项目</p>
                <button
                  className="button secondary small"
                  onClick={() => {
                    setNewProjectName("");
                    setIsCreateModalOpen(true);
                  }}
                >
                  <Plus size={13} /> 新建第一个项目
                </button>
              </div>
            ) : (
              projects.map((project) => {
                const isActive = project.id === activeProjectId;
                return (
                  <button
                    key={project.id}
                    type="button"
                    className={`project-nav-item ${isActive ? "active" : ""}`}
                    onClick={() => onSelectProject(project.id)}
                  >
                    <div className="project-nav-main">
                      <strong className="project-nav-name">{project.name}</strong>
                    </div>
                    <div className="project-nav-meta">
                      <span>{project.documentCount} 份资料</span>
                      <span className="dot-divider">·</span>
                      <span>{project.meetingCount} 场会议</span>
                    </div>
                  </button>
                );
              })
            )}
          </div>
        </aside>

        {/* 右侧：项目工作区 */}
        <main className="project-main-panel">
          {!active ? (
            <div className="empty-hint projects-empty-panel">
              <FolderKanban size={40} />
              <h3>{projects.length === 0 ? "开始创建你的第一个项目" : "请选择一个项目"}</h3>
              <p>
                {projects.length === 0
                  ? "新建项目后，可从知识库中勾选该项目的专属资料，让 AI 在评审时只引用该业务背景。"
                  : "在左侧列表中点击选择项目，维护其名称和关联的可用资料。"}
              </p>
              {projects.length === 0 && (
                <button
                  className="button primary"
                  onClick={() => {
                    setNewProjectName("");
                    setIsCreateModalOpen(true);
                  }}
                >
                  <Plus size={15} /> 新建项目
                </button>
              )}
            </div>
          ) : (
            <div className="project-detail-card">
              {/* 项目卡片头部：名称编辑与删除 */}
              <div className="project-card-header">
                <div className="project-title-area">
                  {editingName?.id === active.id ? (
                    <form
                      className="rename-form"
                      onSubmit={(e) => {
                        e.preventDefault();
                        void renameProject();
                      }}
                    >
                      <input
                        autoFocus
                        value={editingName.value}
                        onChange={(e) =>
                          setEditingName({ ...editingName, value: e.target.value })
                        }
                        onBlur={() => void renameProject()}
                        onKeyDown={(e) => {
                          if (e.key === "Escape") setEditingName(null);
                        }}
                      />
                    </form>
                  ) : (
                    <div className="project-title-row">
                      <h2>{active.name}</h2>
                      <button
                        className="icon-button"
                        onClick={() =>
                          setEditingName({ id: active.id, value: active.name })
                        }
                        title="重命名项目"
                      >
                        <Pencil size={14} /> 改名
                      </button>
                    </div>
                  )}
                  <div className="project-subtitle-meta">
                    <span className="meta-badge">
                      {projectDocs.length} 份可用资料
                    </span>
                    <span className="meta-badge">
                      {active.meetingCount} 场关联会议
                    </span>
                  </div>
                </div>

                <div className="project-card-actions">
                  <button
                    className="button ghost small danger"
                    onClick={() => void removeProject(active)}
                    title="删除此项目（知识库实际文档与会议记录仍会保留）"
                  >
                    <Trash2 size={13} /> 删除项目
                  </button>
                </div>
              </div>

              {/* 可用资料管理区：仅展示本项目关联的文档 */}
              <div className="project-docs-card">
                <div className="project-docs-toolbar">
                  <div className="toolbar-left">
                    <h3 className="section-title">
                      可用资料
                      <span className="sub-count">
                        （本项目共 {projectDocs.length} 份）
                      </span>
                    </h3>
                  </div>

                  <div className="toolbar-right">
                    <button
                      className="button secondary small"
                      onClick={openPickModal}
                      title="从全局知识库中勾选已有文档关联到此项目"
                    >
                      <Library size={14} /> 从知识库挑选
                    </button>
                    <button
                      className="button secondary small"
                      onClick={() => void importFolderForProject()}
                      disabled={!bridge}
                      title="选择文件夹导入并自动关联到此项目"
                    >
                      <FolderPlus size={14} /> 导入文件夹
                    </button>
                    <button
                      className="button primary small"
                      onClick={() => void importForProject()}
                      disabled={!bridge}
                      title="选择文件导入并自动关联到此项目"
                    >
                      <Plus size={14} /> 导入文件
                    </button>
                  </div>
                </div>

                <div className="project-docs-container">
                  {projectDocs.length === 0 ? (
                    <div className="empty-hint project-docs-empty">
                      <BookOpen size={36} />
                      <p>
                        本项目暂未关联任何参考资料。可点击上方<strong>「从知识库挑选」</strong>已有文档，或直接<strong>「导入文件 / 文件夹」</strong>。
                      </p>
                      <div className="empty-actions" style={{ marginTop: 12, display: "flex", gap: 10 }}>
                        <button
                          className="button secondary small"
                          onClick={openPickModal}
                        >
                          <Library size={14} /> 从知识库挑选
                        </button>
                        <button
                          className="button primary small"
                          onClick={() => void importForProject()}
                          disabled={!bridge}
                        >
                          <Plus size={14} /> 导入本地文件
                        </button>
                      </div>
                    </div>
                  ) : (
                    <div className="project-doc-rows">
                      {projectDocs.map((doc) => (
                        <div
                          className={`project-doc-row ${doc.exists ? "" : "missing"}`}
                          key={doc.id}
                        >
                          <div className="project-doc-main">
                            <FileText size={17} />
                            <div className="project-doc-texts">
                              <strong className="doc-name">{doc.name}</strong>
                              <small className="doc-meta">
                                {doc.exists
                                  ? `${formatBytes(doc.size)}`
                                  : "⚠️ 原文件已移动或删除，无法检索"}
                              </small>
                            </div>
                          </div>
                          <div className="project-doc-row-actions">
                            <button
                              type="button"
                              className="button ghost small danger"
                              onClick={() => void removeMember(doc)}
                              title="从本项目中移除（原文件仍完好保留在知识库中）"
                            >
                              <Trash2 size={13} /> 移除
                            </button>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </div>
          )}
        </main>
      </div>

      {/* 「从知识库挑选文档」Modal 模态框 */}
      {isPickModalOpen && (
        <div
          className="modal-backdrop"
          onClick={() => setIsPickModalOpen(false)}
        >
          <div
            className="modal-card doc-picker-modal"
            onClick={(e) => e.stopPropagation()}
            role="dialog"
            aria-labelledby="pick-docs-title"
          >
            <div className="modal-header">
              <h3 id="pick-docs-title">从知识库挑选可用资料</h3>
              <button
                className="icon-button"
                onClick={() => setIsPickModalOpen(false)}
                title="关闭"
              >
                <X size={16} />
              </button>
            </div>
            <div className="modal-body doc-picker-body">
              <div className="picker-search-bar">
                <Search size={15} />
                <input
                  autoFocus
                  placeholder="搜索知识库文档名称..."
                  value={pickerSearch}
                  onChange={(e) => setPickerSearch(e.target.value)}
                />
                {pickerSearch && (
                  <button
                    className="icon-button small"
                    onClick={() => setPickerSearch("")}
                  >
                    <X size={13} />
                  </button>
                )}
              </div>

              <div className="picker-toolbar-sub">
                <span>
                  已选择 <strong>{pickerSelectedIds.length}</strong> / 知识库共 {documents.length} 份
                </span>
                <div className="picker-quick-ops">
                  <button
                    type="button"
                    className="text-button"
                    onClick={() => {
                      const allIds = documents.filter((d) => d.exists).map((d) => d.id);
                      setPickerSelectedIds(allIds);
                    }}
                    disabled={documents.filter((d) => d.exists).length === 0}
                  >
                    全选
                  </button>
                  <span className="action-sep">|</span>
                  <button
                    type="button"
                    className="text-button"
                    onClick={() => setPickerSelectedIds([])}
                    disabled={pickerSelectedIds.length === 0}
                  >
                    全部取消
                  </button>
                </div>
              </div>

              <div className="picker-doc-scroll">
                {documents.length === 0 ? (
                  <div className="empty-hint" style={{ padding: "30px 16px" }}>
                    知识库目前为空。可先去「知识库」页导入，或直接在项目内导入文件。
                  </div>
                ) : filteredPickerDocs.length === 0 ? (
                  <div className="empty-hint" style={{ padding: "30px 16px" }}>
                    未找到匹配「{pickerSearch}」的文档
                  </div>
                ) : (
                  filteredPickerDocs.map((doc) => {
                    const isChecked = pickerSelectedIds.includes(doc.id);
                    return (
                      <label
                        key={doc.id}
                        className={`document-check ${doc.exists ? "" : "missing"} ${
                          isChecked ? "selected" : ""
                        }`}
                      >
                        <input
                          type="checkbox"
                          checked={isChecked && doc.exists}
                          disabled={!doc.exists}
                          onChange={() => togglePickerDoc(doc.id)}
                        />
                        <span className="custom-check">
                          <Check size={13} />
                        </span>
                        <FileText size={17} />
                        <span>
                          <strong>{doc.name}</strong>
                          <small>
                            {doc.exists ? formatBytes(doc.size) : "⚠️ 原文件已失效"}
                          </small>
                        </span>
                      </label>
                    );
                  })
                )}
              </div>
            </div>
            <div className="modal-footer">
              <button
                type="button"
                className="button ghost"
                onClick={() => setIsPickModalOpen(false)}
              >
                取消
              </button>
              <button
                type="button"
                className="button primary"
                onClick={() => void savePickedDocs()}
              >
                确定保存（已选 {pickerSelectedIds.length} 份）
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 新建项目 Modal 模态框 */}
      {isCreateModalOpen && (
        <div
          className="modal-backdrop"
          onClick={() => setIsCreateModalOpen(false)}
        >
          <div
            className="modal-card"
            onClick={(e) => e.stopPropagation()}
            role="dialog"
            aria-labelledby="new-project-title"
          >
            <div className="modal-header">
              <h3 id="new-project-title">新建项目</h3>
              <button
                className="icon-button"
                onClick={() => setIsCreateModalOpen(false)}
                title="关闭"
              >
                <X size={16} />
              </button>
            </div>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void createProject();
              }}
            >
              <div className="modal-body">
                <label className="field">
                  <span>项目名称</span>
                  <input
                    autoFocus
                    value={newProjectName}
                    placeholder="例如：电商后台重构、支付网关对接"
                    onChange={(e) => setNewProjectName(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Escape") setIsCreateModalOpen(false);
                    }}
                  />
                  <small className="field-hint">
                    创建后可为项目勾选专属资料，开会时选定该项目即可精准检索。
                  </small>
                </label>
              </div>
              <div className="modal-footer">
                <button
                  type="button"
                  className="button ghost"
                  onClick={() => setIsCreateModalOpen(false)}
                >
                  取消
                </button>
                <button
                  type="submit"
                  className="button primary"
                  disabled={!newProjectName.trim() || !bridge}
                >
                  创建项目
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

/** 专有名词独立页：通用词 + 按项目维护（Master-Detail 两栏布局）。 */
function GlossaryScreen({
  projects,
  activeProjectId,
  onSelectProject,
  onNotify,
}: {
  projects: Project[];
  activeProjectId: string | null;
  onSelectProject: (id: string | null) => void;
  onNotify: (value: string) => void;
}) {
  const [selectedScope, setSelectedScope] = useState<"general" | string>(
    activeProjectId && projects.some((p) => p.id === activeProjectId)
      ? activeProjectId
      : "general",
  );

  const activeProject =
    selectedScope !== "general"
      ? projects.find((p) => p.id === selectedScope) || null
      : null;

  function selectGeneral() {
    setSelectedScope("general");
  }

  function selectProject(projId: string) {
    setSelectedScope(projId);
    onSelectProject(projId);
  }

  return (
    <div className="page glossary-page">
      <header className="page-heading">
        <div>
          <div className="eyebrow">ASR 热词</div>
          <h1>专有名词</h1>
          <p>
            维护产品名、业务黑话、客户简称等。开会时自动合并「通用 +
            本场项目」（同名以项目词为准），大幅提高语音识别准确率。
          </p>
        </div>
      </header>

      <div className="glossary-layout">
        {/* 左侧侧边栏：词库范围选择 */}
        <aside className="glossary-sidebar">
          <div className="glossary-sidebar-header">
            <div className="sidebar-title">
              <Tags size={15} />
              <span>词库范围</span>
            </div>
          </div>

          <div className="glossary-nav-list">
            {/* 通用词库导航项 */}
            <button
              type="button"
              className={`glossary-nav-item ${
                selectedScope === "general" ? "active" : ""
              }`}
              onClick={selectGeneral}
            >
              <div className="glossary-nav-main">
                <strong className="glossary-nav-name">🌐 全局通用名词</strong>
              </div>
              <div className="glossary-nav-meta">
                <span>全部会议均会自动生效</span>
              </div>
            </button>

            {/* 分隔区 */}
            <div className="glossary-nav-section-title">
              <span>项目专属词库</span>
              <span className="count-pill">{projects.length}</span>
            </div>

            {projects.length === 0 ? (
              <div className="glossary-sidebar-empty">
                <p>暂无项目</p>
                <small>请先在「项目」页创建项目</small>
              </div>
            ) : (
              projects.map((project) => {
                const isActive = selectedScope === project.id;
                return (
                  <button
                    key={project.id}
                    type="button"
                    className={`glossary-nav-item ${isActive ? "active" : ""}`}
                    onClick={() => selectProject(project.id)}
                  >
                    <div className="glossary-nav-main">
                      <strong className="glossary-nav-name">{project.name}</strong>
                    </div>
                    <div className="glossary-nav-meta">
                      <span>{project.meetingCount} 场关联会议</span>
                    </div>
                  </button>
                );
              })
            )}
          </div>
        </aside>

        {/* 右侧工作区：专有名词面板 */}
        <main className="glossary-main-panel">
          {selectedScope === "general" ? (
            <GlossaryPanel
              scope="general"
              title="🌐 全局通用专有名词"
              description="所有会议都会自动叠加；若与项目词同名，则以项目词为准"
              onNotify={onNotify}
            />
          ) : activeProject ? (
            <GlossaryPanel
              scope={activeProject.id}
              title={`📁 ${activeProject.name} · 专属专有名词`}
              description={`仅在「${activeProject.name}」相关会议时叠加；同名时优先覆盖全局通用词`}
              onNotify={onNotify}
            />
          ) : (
            <div className="empty-hint glossary-empty-panel">
              <Tags size={40} />
              <h3>请在左侧选择一个词库范围</h3>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}

function SettingsScreen({
  persisted,
  onChange,
  onNotify,
}: {
  persisted: PersistedState;
  onChange: (state: PersistedState) => void;
  onNotify: (value: string) => void;
}) {
  const bridge = window.meetingCopilot;
  const [status, setStatus] = useState<ServiceStatus | null>(null);
  const [llmTest, setLlmTest] = useState<LlmTestResult | null>(null);
  const [bench, setBench] = useState<BenchResult | null>(null);
  const [testing, setTesting] = useState(false);
  const [benching, setBenching] = useState(false);
  const [dataInfo, setDataInfo] = useState<DataInfo | null>(null);
  const [probing, setProbing] = useState(false);
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const [asrTesting, setAsrTesting] = useState(false);
  const [asrTest, setAsrTest] = useState<AsrTestResult | null>(null);
  // 直接取 bridge 的返回类型，避免这里的内联类型和 types.ts 各写各的而脱节
  const [vpStatus, setVpStatus] = useState<Awaited<
    ReturnType<DesktopBridge["voiceprintStatus"]>
  > | null>(null);
  const [vpBusy, setVpBusy] = useState(false);
  const [vpMsg, setVpMsg] = useState("");
  const [secrets, setSecrets] = useState<SecretsStatus | null>(null);
  const [secretDrafts, setSecretDrafts] = useState<Record<string, string>>({});
  const [savingSecrets, setSavingSecrets] = useState(false);
  const [importingSecrets, setImportingSecrets] = useState(false);
  const [asrCredOpen, setAsrCredOpen] = useState(false);
  const [llmCredOpen, setLlmCredOpen] = useState(false);
  const [userToggledAsrCred, setUserToggledAsrCred] = useState(false);
  const [userToggledLlmCred, setUserToggledLlmCred] = useState(false);

  const asrProvider = persisted.asrProvider || status?.asr.provider || "";
  const llmProvider = persisted.llmProvider || status?.llm.provider || "";
  const asrOptions = status?.asr.options || [];
  const llmOptions = status?.providers || [];
  const ASR_LABELS: Record<string, string> = {
    xfyun: "讯飞 RTASR（标准版）",
    "xfyun-llm": "讯飞实时转写大模型",
    aliyun: "阿里云",
    volcano: "火山引擎",
    tencent: "腾讯云",
    mimo: "小米 MiMo",
  };
  const asrFields = ASR_CREDENTIAL_FIELDS[asrProvider] || [];
  const llmFields = LLM_CREDENTIAL_FIELDS[llmProvider] || [];
  const currentModel =
    persisted.llmModel ||
    llmOptions.find((p) => p.id === llmProvider)?.model ||
    "";
  const retrieval = status?.retrieval;

  function fieldsReady(
    fields: Array<{ key: string }>,
  ): { ready: number; total: number; allOk: boolean } {
    const total = fields.length;
    if (!total) return { ready: 0, total: 0, allOk: true };
    const ready = fields.filter(
      (f) => secrets?.fields?.[f.key]?.configured,
    ).length;
    // 大模型 ASR：App ID 可回退 XFYUN_APP_ID
    let ok = ready === total;
    if (
      fields.some((f) => f.key === "XFYUN_LLM_ASR_APP_ID") &&
      !secrets?.fields?.XFYUN_LLM_ASR_APP_ID?.configured &&
      secrets?.fields?.XFYUN_APP_ID?.configured
    ) {
      const rest = fields.filter((f) => f.key !== "XFYUN_LLM_ASR_APP_ID");
      ok =
        rest.every((f) => secrets?.fields?.[f.key]?.configured) &&
        Boolean(secrets?.fields?.XFYUN_APP_ID?.configured);
    }
    return { ready, total, allOk: ok };
  }

  const asrCred = fieldsReady(asrFields);
  const llmCred = fieldsReady(llmFields);

  // 缺凭证时自动展开；用户手动折叠后不再抢控制
  useEffect(() => {
    if (!userToggledAsrCred && asrFields.length) {
      setAsrCredOpen(!asrCred.allOk);
    }
  }, [asrProvider, asrCred.allOk, asrFields.length, userToggledAsrCred]);
  useEffect(() => {
    if (!userToggledLlmCred && llmFields.length) {
      setLlmCredOpen(!llmCred.allOk);
    }
  }, [llmProvider, llmCred.allOk, llmFields.length, userToggledLlmCred]);

  async function refreshSecrets() {
    if (!bridge?.secretsStatus) return;
    try {
      setSecrets(await bridge.secretsStatus());
    } catch {
      /* ignore */
    }
  }

  function chooseAsr(provider: string) {
    onChange({ ...persisted, asrProvider: provider });
    setAsrTest(null);
    setSecretDrafts({});
    setUserToggledAsrCred(false);
  }

  async function refreshVoiceprint() {
    if (!bridge?.voiceprintStatus) return;
    try {
      setVpStatus(await bridge.voiceprintStatus());
    } catch {
      setVpStatus({ ok: false });
    }
  }

  async function runEnrollVoiceprint() {
    if (!bridge?.enrollVoiceprint) return;
    setVpBusy(true);
    setVpMsg("正在录制声纹，请对着开会用的麦克风连续说话，中途不要停…");
    try {
      // 默认追加。多次短录（不同时间/状态）比一次长录更能覆盖音色变化
      await bridge.enrollVoiceprint({
        seconds: 20,
        device: persisted.selectedDevice,
        append: true,
      });
      const next = await bridge.voiceprintStatus();
      setVpStatus(next);
      const n = next.sampleCount ?? 1;
      setVpMsg(
        n < 3
          ? `已录 ${n} 段。建议再追加 ${3 - n} 段（不同时间/坐姿各录一次），认「我」会更稳。`
          : `已录 ${n} 段，合计 ${next.totalSeconds ?? 0} 秒。样本量足够了。`,
      );
      onChange({
        ...persisted,
        voiceprintEnabled: true,
        meThreshold: persisted.meThreshold ?? 0.65,
        asrProvider: persisted.asrProvider || "aliyun",
      });
      onNotify(`声纹已追加第 ${n} 段`);
    } catch (error) {
      setVpMsg(error instanceof Error ? error.message : "声纹录制失败");
    } finally {
      setVpBusy(false);
    }
  }

  async function clearVoiceprint() {
    if (!bridge?.clearVoiceprint) return;
    setVpBusy(true);
    try {
      await bridge.clearVoiceprint();
      setVpMsg("已清除全部声纹样本");
      await refreshVoiceprint();
    } catch (error) {
      setVpMsg(error instanceof Error ? error.message : "清除失败");
    } finally {
      setVpBusy(false);
    }
  }

  async function removeLastVoiceprintSample() {
    if (!bridge?.removeLastVoiceprintSample) return;
    setVpBusy(true);
    try {
      const res = await bridge.removeLastVoiceprintSample();
      setVpMsg(
        res.ok ? `已删除最后一段，剩余 ${res.remaining ?? 0} 段` : res.message || "删除失败",
      );
      await refreshVoiceprint();
    } catch (error) {
      setVpMsg(error instanceof Error ? error.message : "删除失败");
    } finally {
      setVpBusy(false);
    }
  }
  function chooseLlm(provider: string) {
    onChange({ ...persisted, llmProvider: provider, llmModel: undefined });
    setProbe(null);
    setLlmTest(null);
    setBench(null);
    setSecretDrafts({});
    setUserToggledLlmCred(false);
  }
  function chooseModel(model: string) {
    onChange({ ...persisted, llmModel: model });
    setProbe(null);
    onNotify(`已选用 ${model}，下场会议生效`);
  }

  async function saveCredentialKeys(keys: string[]) {
    if (!bridge?.saveSecrets) return;
    const patch: Record<string, string> = {};
    let touched = 0;
    for (const key of keys) {
      if (secretDrafts[key] === undefined) continue;
      patch[key] = secretDrafts[key];
      touched += 1;
    }
    if (!touched) {
      onNotify("没有需要保存的改动");
      return;
    }
    setSavingSecrets(true);
    try {
      await bridge.saveSecrets(patch);
      setSecretDrafts((current) => {
        const next = { ...current };
        for (const key of keys) delete next[key];
        return next;
      });
      await refreshSecrets();
      setStatus(
        await bridge.serviceStatus({
          provider: persisted.llmProvider,
          asrProvider: persisted.asrProvider,
        }),
      );
      onNotify("凭证已保存");
    } catch (error) {
      onNotify(error instanceof Error ? error.message : "保存失败");
    } finally {
      setSavingSecrets(false);
    }
  }

  async function clearCredentialKey(key: string) {
    if (!bridge?.saveSecrets) return;
    try {
      await bridge.saveSecrets({ [key]: "" });
      setSecretDrafts((current) => {
        const next = { ...current };
        delete next[key];
        return next;
      });
      await refreshSecrets();
      onNotify("已清除该项");
    } catch (error) {
      onNotify(error instanceof Error ? error.message : "清除失败");
    }
  }

  async function importFromConfig() {
    if (!bridge?.importSecretsFromConfig) return;
    setImportingSecrets(true);
    try {
      const result = await bridge.importSecretsFromConfig();
      await refreshSecrets();
      onNotify(
        result.imported > 0
          ? `已导入 ${result.imported} 项密钥`
          : "没有新的密钥可导入",
      );
    } catch (error) {
      onNotify(error instanceof Error ? error.message : "导入失败");
    } finally {
      setImportingSecrets(false);
    }
  }

  function fieldStatusLabel(key: string) {
    const info = secrets?.fields?.[key];
    if (
      key === "XFYUN_LLM_ASR_APP_ID" &&
      !info?.configured &&
      secrets?.fields?.XFYUN_APP_ID?.configured
    ) {
      return {
        text: `沿用 ${secrets.fields.XFYUN_APP_ID.preview}`,
        ok: true,
      };
    }
    if (!info?.configured) return { text: "未配置", ok: false };
    if (info.source === "app") return { text: info.preview, ok: true };
    if (info.source === "config") return { text: `文件 · ${info.preview}`, ok: true };
    return { text: "已配置", ok: true };
  }

  async function openConsole(url: string) {
    if (!bridge?.openExternal) return;
    try {
      await bridge.openExternal(url);
    } catch (error) {
      onNotify(error instanceof Error ? error.message : "无法打开链接");
    }
  }

  function renderCredentials(
    fields: Array<{ key: string; label: string; hint?: string }>,
    open: boolean,
    setOpen: (v: boolean) => void,
    onUserToggle: () => void,
    cred: { ready: number; total: number; allOk: boolean },
    site?: ProviderConsole,
  ) {
    if (!fields.length) return null;
    return (
      <div className={`settings-cred ${open ? "open" : ""}`}>
        <button
          type="button"
          className="settings-cred-toggle"
          onClick={() => {
            onUserToggle();
            setOpen(!open);
          }}
        >
          <span className="settings-cred-toggle-left">
            <ChevronDown size={14} className={open ? "rot" : ""} />
            API 凭证
          </span>
          <span className={cred.allOk ? "ok-text" : "warn-text"}>
            {cred.allOk
              ? "已配置"
              : cred.total
                ? `${cred.ready}/${cred.total} 项`
                : "—"}
          </span>
        </button>
        {open && (
          <div className="settings-cred-body">
            <div className="settings-fields">
              {fields.map((field) => {
                const st = fieldStatusLabel(field.key);
                return (
                  <label className="field" key={field.key}>
                    <span>
                      {field.label}
                      <em className={st.ok ? "ok-text" : "warn-text"}>
                        {st.text}
                      </em>
                    </span>
                    <div className="inline-row">
                      <input
                        type="password"
                        autoComplete="off"
                        spellCheck={false}
                        placeholder={
                          st.ok ? "留空保留 · 输入则覆盖" : "粘贴密钥"
                        }
                        value={secretDrafts[field.key] ?? ""}
                        onChange={(event) =>
                          setSecretDrafts((current) => ({
                            ...current,
                            [field.key]: event.target.value,
                          }))
                        }
                        disabled={!bridge}
                      />
                      {secrets?.fields?.[field.key]?.source === "app" && (
                        <button
                          type="button"
                          className="button ghost small"
                          onClick={() => void clearCredentialKey(field.key)}
                        >
                          清除
                        </button>
                      )}
                    </div>
                    {field.hint && (
                      <small className="field-hint">{field.hint}</small>
                    )}
                  </label>
                );
              })}
            </div>
            <div className="settings-cred-actions">
              {site && bridge?.openExternal && (
                <button
                  type="button"
                  className="button ghost small"
                  title={site.url}
                  onClick={() => void openConsole(site.url)}
                >
                  <ExternalLink size={13} />
                  去申请 · {site.label}
                </button>
              )}
              <button
                type="button"
                className="button primary small"
                disabled={savingSecrets || !bridge}
                onClick={() =>
                  void saveCredentialKeys(fields.map((f) => f.key))
                }
              >
                {savingSecrets ? "保存中…" : "保存凭证"}
              </button>
            </div>
          </div>
        )}
      </div>
    );
  }

  async function runProbe() {
    if (!bridge) return;
    setProbing(true);
    setProbe(null);
    try {
      setProbe(await bridge.probeLlm(llmProvider));
    } catch (error) {
      onNotify(error instanceof Error ? error.message : "探测失败");
    } finally {
      setProbing(false);
    }
  }

  async function runAsrTest() {
    if (!bridge) return;
    setAsrTesting(true);
    setAsrTest(null);
    try {
      const result = await bridge.testAsr({
        asrProvider,
        asrModel:
          asrProvider === "aliyun"
            ? persisted.asrModel || "qwen-audio-3.0-asr-flash-streaming"
            : undefined,
        asrLang: persisted.asrLang || "zh_en",
      });
      setAsrTest(result);
      if (result.ok) {
        onNotify(
          `语音转写「${asrProvider}」测试成功（耗时 ${result.elapsed ?? 0}s）`,
        );
      } else {
        onNotify(`语音转写测试失败：${result.message || "连接失败"}`);
      }
    } catch (error) {
      const errorMsg = error instanceof Error ? error.message : "测试失败";
      setAsrTest({
        provider: asrProvider,
        ok: false,
        message: errorMsg,
      });
      onNotify(`语音转写测试失败：${errorMsg}`);
    } finally {
      setAsrTesting(false);
    }
  }

  async function refreshDataInfo() {
    if (!bridge) return;
    try {
      setDataInfo(await bridge.dataInfo());
    } catch {
      /* ignore */
    }
  }

  async function clearData() {
    if (!bridge) return;
    try {
      const result = await bridge.clearAllData();
      if (result.canceled) return;
      onNotify("已清空全部本地数据");
      await refreshDataInfo();
    } catch (error) {
      onNotify(error instanceof Error ? error.message : "清空失败");
    }
  }

  useEffect(() => {
    if (!bridge) return;
    void refreshDataInfo();
    void refreshSecrets();
    // 必须一起读：vpStatus 初值是 null，不在挂载时拉一次的话，
    // 重启应用后即便 enroll_me.wav 还在，设置页也会显示「未注册」
    void refreshVoiceprint();
  }, []);

  useEffect(() => {
    if (!bridge) return;
    void bridge
      .serviceStatus({
        provider: persisted.llmProvider,
        asrProvider: persisted.asrProvider,
      })
      .then(setStatus)
      .catch(() => onNotify("读取服务状态失败"));
  }, [persisted.llmProvider, persisted.asrProvider]);

  async function runTest() {
    if (!bridge) return;
    setTesting(true);
    setLlmTest(null);
    try {
      const result = await bridge.testLlm({
        provider: llmProvider,
        model: currentModel || undefined,
        scene: "general",
      });
      setLlmTest(result);
      if (result.ok) {
        onNotify(
          `模型「${currentModel || llmProvider}」连接成功（耗时 ${result.elapsed ?? 0}s）`,
        );
      } else {
        onNotify(
          `模型「${currentModel || llmProvider}」连接失败：${result.message || "请求失败"}`,
        );
      }
    } catch (error) {
      const errorMsg = error instanceof Error ? error.message : "测试失败";
      setLlmTest({
        ok: false,
        message: errorMsg,
      });
      onNotify(`模型连接失败：${errorMsg}`);
    } finally {
      setTesting(false);
    }
  }

  async function runBench(all: boolean) {
    if (!bridge) return;
    setBenching(true);
    setBench(null);
    onNotify(
      all
        ? "正在对比各家供应商，约需一到两分钟…"
        : `正在测 ${currentModel || llmProvider}…`,
    );
    try {
      setBench(
        await bridge.benchmarkProviders(
          all
            ? { all: true }
            : { provider: llmProvider, model: currentModel || undefined },
        ),
      );
    } catch (error) {
      onNotify(error instanceof Error ? error.message : "测速失败");
    } finally {
      setBenching(false);
    }
  }

  return (
    <div className="page settings-page">
      <header className="page-heading compact settings-hero">
        <div>
          <div className="eyebrow">本机配置</div>
          <h1>设置</h1>
          <p>选择服务商、填写密钥、验证连通。密钥仅保存在本机。</p>
        </div>
        <button
          className="button ghost"
          disabled={importingSecrets || !bridge}
          onClick={() => void importFromConfig()}
        >
          {importingSecrets ? "导入中…" : "从 config.py 导入"}
        </button>
      </header>

      <div className="settings-layout">
        <aside className="settings-index" aria-label="设置分组">
          <span>信号链</span>
          {[
            ["settings-asr", "实时转写"],
            ["settings-voiceprint", "说话人识别"],
            ["settings-llm", "话术模型"],
            ["settings-retrieval", "知识与触发"],
            ["settings-data", "数据与隐私"],
          ].map(([id, label], index) => (
            <button
              key={id}
              onClick={() =>
                document.getElementById(id)?.scrollIntoView({
                  behavior: "smooth",
                  block: "start",
                })
              }
            >
              <em>{String(index + 1).padStart(2, "0")}</em>
              {label}
            </button>
          ))}
          <p>设置会自动保存；服务与模型调整从下一场会议开始生效。</p>
        </aside>

        <div className="settings-stack">
        {/* ── ASR ── */}
        <section className="settings-card" id="settings-asr">
          <header className="settings-card-head">
            <div>
              <span className="settings-card-kicker">语音识别</span>
              <h2>实时转写</h2>
            </div>
            <span
              className={
                status?.asr.ok || asrCred.allOk
                  ? "verified-tag"
                  : "missing-badge"
              }
            >
              {status?.asr.ok || asrCred.allOk ? "就绪" : "待配置"}
            </span>
          </header>

          <div className="settings-row">
            <label className="field grow">
              <span>供应商</span>
              <select
                value={asrProvider}
                onChange={(e) => chooseAsr(e.target.value)}
                disabled={!bridge}
              >
                {asrOptions.map((id) => (
                  <option key={id} value={id}>
                    {ASR_LABELS[id] || id}
                  </option>
                ))}
              </select>
            </label>
            {asrProvider === "aliyun" && (
              <label className="field grow">
                <span>转写模型</span>
                <select
                  value={
                    persisted.asrModel ||
                    status?.asr.model ||
                    "qwen-audio-3.0-asr-flash-streaming"
                  }
                  onChange={(e) =>
                    onChange({
                      ...persisted,
                      asrModel: e.target.value,
                    })
                  }
                  disabled={!bridge}
                  title="选择阿里云实时语音转写模型"
                >
                  {ALIYUN_ASR_MODELS.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.label}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <label className="field grow">
              <span>识别语种</span>
              <select
                value={persisted.asrLang || "zh_en"}
                onChange={(e) =>
                  onChange({
                    ...persisted,
                    asrLang: e.target.value as "zh" | "en" | "zh_en",
                  })
                }
                disabled={!bridge}
                title="限制自动识语种，避免串出日文等"
              >
                <option value="zh">中文</option>
                <option value="en">英文</option>
                <option value="zh_en">中英混用</option>
              </select>
            </label>
            <div className="field settings-action-field">
              <span>连通性</span>
              <div className="inline-row">
                <button
                  className="button secondary small"
                  onClick={() => void runAsrTest()}
                  disabled={asrTesting || !bridge}
                >
                  {asrTesting ? "测试中…" : "测试连接"}
                </button>
                {asrTest && (
                  <span className={asrTest.ok ? "ok-text" : "warn-text"}>
                    {asrTest.ok
                      ? `✓ ${asrTest.elapsed}s`
                      : asrTest.message?.slice(0, 48)}
                  </span>
                )}
              </div>
            </div>
          </div>
          {(() => {
            const glossary = ASR_GLOSSARY_SUPPORT[asrProvider] || {
              readsLibrary: false,
              note: "本版不会读取本地专有名词库",
            };
            return (
              <p
                className={`glossary-asr-note ${
                  glossary.readsLibrary ? "is-supported" : "is-unsupported"
                }`}
              >
                <strong>
                  {glossary.readsLibrary
                    ? "当前转写会使用专有名词库"
                    : "当前转写不会使用专有名词库"}
                </strong>
                <span>{glossary.note}</span>
                <span>词库在左侧「专有名词」页维护；不使用时词仍保留，仅本场不生效。</span>
              </p>
            );
          })()}
          <details className="settings-explanation">
            <summary>识别语种如何映射到不同供应商？</summary>
            <p>
              默认中英混用。不设语种时部分模型会自动识别，实测可能串出日文等。
              映射关系：阿里 language_hints · 讯飞 lang · 火山 language · 腾讯引擎 ·
              MiMo asr_options.language（混说仅 auto）。
            </p>
          </details>

          {renderCredentials(
            asrFields,
            asrCredOpen,
            setAsrCredOpen,
            () => setUserToggledAsrCred(true),
            asrCred,
            ASR_CONSOLE[asrProvider],
          )}
        </section>

        {/* ── 声纹认我 D1 ── */}
        <section className="settings-card" id="settings-voiceprint">
          <header className="settings-card-head">
            <div>
              <span className="settings-card-kicker">方案 A + D1</span>
              <h2>本地声纹认「我」</h2>
            </div>
            <span className={vpStatus?.ok ? "verified-tag" : "missing-badge"}>
              {vpStatus?.ok
                ? `${vpStatus.sampleCount ?? 1} 段 · ${vpStatus.totalSeconds ?? vpStatus.seconds ?? "?"}s`
                : "未注册"}
            </span>
          </header>
          <p className="muted" style={{ marginTop: 0 }}>
            会中用 CAM++ 判断每句是不是你。建议使用相同的麦克风与降噪设置，
            在不同时间或坐姿下累计录制至少 3 段。
          </p>
          <div className="settings-row">
            <label className="field grow">
              <span>开会时启用</span>
              <select
                value={persisted.voiceprintEnabled === false ? "off" : "on"}
                onChange={(e) =>
                  onChange({
                    ...persisted,
                    voiceprintEnabled: e.target.value === "on",
                  })
                }
                disabled={!bridge}
              >
                <option value="on">启用（有注册文件时自动加载）</option>
                <option value="off">关闭</option>
              </select>
            </label>
            <label className="field">
              <span>阈值</span>
              <input
                type="number"
                min={0.4}
                max={0.85}
                step={0.01}
                value={persisted.meThreshold ?? 0.65}
                onChange={(e) =>
                  onChange({
                    ...persisted,
                    meThreshold: Number(e.target.value) || 0.65,
                  })
                }
                disabled={!bridge}
              />
            </label>
          </div>
          <div className="settings-primary-action">
            <div>
              <strong>补充声纹样本</strong>
              <span>每次录制 20 秒，并追加到现有样本。</span>
            </div>
            <button
              className="button primary"
              onClick={() => void runEnrollVoiceprint()}
              disabled={vpBusy || !bridge}
            >
              {vpBusy
                ? "处理中…"
                : vpStatus?.ok
                  ? "再追加一段 20 秒"
                  : "录制声纹 20 秒"}
            </button>
          </div>
          {vpStatus?.ok && (
            <details className="settings-maintenance">
              <summary>
                查看和管理声纹样本
              </summary>
              <div>
                {!!vpStatus.samples?.length && (
                  <div className="voiceprint-sample-list">
                    {vpStatus.samples.map((sample) => (
                      <span key={sample.path} title={sample.path}>
                        第 {sample.index} 段
                        <em>{sample.seconds}s</em>
                      </span>
                    ))}
                  </div>
                )}
                <div className="settings-maintenance-actions">
                <button
                  className="button ghost small"
                  onClick={() => void removeLastVoiceprintSample()}
                  disabled={vpBusy || !bridge}
                  title="某段录砸了（音量低/被打断）不必全部重来"
                >
                  删除最后一段
                </button>
                <button
                  className="button ghost small danger"
                  onClick={() => void clearVoiceprint()}
                  disabled={vpBusy || !bridge}
                >
                  全部清除
                </button>
                </div>
              </div>
            </details>
          )}
          {asrProvider !== "aliyun" && (
          <div className="settings-recommendation">
            <div>
              <strong>推荐搭配：阿里云 ASR</strong>
              <span>声纹链路当前以阿里云实时转写作为主要验证底座。</span>
            </div>
            <button
              className="text-button"
              onClick={() => {
                onChange({ ...persisted, asrProvider: "aliyun" });
                onNotify("已切换 ASR 为阿里云");
              }}
              disabled={!bridge}
            >
              切换为阿里云
            </button>
          </div>
          )}
          {vpMsg && (
            <p className="muted" style={{ marginBottom: 0 }}>
              {vpMsg}
            </p>
          )}
        </section>

        {/* ── LLM ── */}
        <section className="settings-card" id="settings-llm">
          <header className="settings-card-head">
            <div>
              <span className="settings-card-kicker">话术建议</span>
              <h2>生成模型</h2>
            </div>
            <span
              className={
                status?.llm.ok || llmCred.allOk
                  ? "verified-tag"
                  : "missing-badge"
              }
            >
              {status?.llm.ok || llmCred.allOk ? "就绪" : "待配置"}
            </span>
          </header>

          <div className="settings-row">
            <label className="field grow">
              <span>供应商</span>
              <select
                value={llmProvider}
                onChange={(e) => chooseLlm(e.target.value)}
                disabled={!bridge}
              >
                {llmOptions.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="field grow">
              <span>模型</span>
              <div className="inline-row model-row">
                <span className="mono model-chip" title={currentModel}>
                  {currentModel || "默认"}
                </span>
                <button
                  className="button ghost small"
                  onClick={() => void runProbe()}
                  disabled={probing || !bridge}
                >
                  {probing ? "探测中…" : "探测可用模型"}
                </button>
              </div>
            </label>
          </div>

          {renderCredentials(
            llmFields,
            llmCredOpen,
            setLlmCredOpen,
            () => setUserToggledLlmCred(true),
            llmCred,
            LLM_CONSOLE[llmProvider],
          )}

          {probe && (
            <div className="settings-model-picker">
              <header>
                <div>
                  <strong>{probe.source === "catalog" ? "供应商模型目录" : "选择可用模型"}</strong>
                  <span>
                    {probe.source === "catalog"
                      ? "以下来自供应商当前返回的目录；当前模型已验证，其余模型可按需测试。"
                      : "已完成最短请求验证；点击一行即可切换。"}
                  </span>
                </div>
                <small>{(probe.results || []).filter((item) => item.ok).length} 个模型</small>
              </header>
              <div className="model-choice-list" role="radiogroup" aria-label="可用模型">
                {(probe.results || []).map((result) => {
                  const selected = currentModel === result.model;
                  return (
                    <button
                      className={`model-choice ${selected ? "selected" : ""}`}
                      key={result.model}
                      disabled={!result.ok}
                      onClick={() => chooseModel(result.model)}
                      role="radio"
                      aria-checked={selected}
                    >
                      <span className="mono">{result.model}</span>
                      <small>
                        {!result.ok
                          ? "当前不可用"
                          : result.verified
                            ? `已验证 ${result.elapsed}s`
                            : "目录可用，未试跑"}
                      </small>
                      <em>
                        {selected ? (
                          <>
                            <Check size={12} /> 当前模型
                          </>
                        ) : result.ok ? (
                          "选用"
                        ) : (
                          "不可用"
                        )}
                      </em>
                    </button>
                  );
                })}
              </div>
              {probe.ok ? (
                <details className="model-probe-note">
                  <summary>
                    {probe.source === "catalog"
                      ? "目录模型为什么还需要单独测试？"
                      : "为什么不能按这里的耗时选择最快模型？"}
                  </summary>
                  <p>
                    {probe.source === "catalog"
                      ? "目录只说明供应商把模型提供给当前密钥，不保证该模型支持本应用使用的对话接口。选中后可点击下方“测试连接”验证。"
                      : "这里只发送一句最短测试语，用来确认模型名和密钥可用；不代表真实建议速度。真实建议还需读取会议上下文和知识库，实测可能相差 10 倍以上。"}
                  </p>
                </details>
              ) : (
                <p className="bench-note">
                  {probe.message || "没有可用模型，请先配置密钥。"}
                </p>
              )}
              {probe.source === "fallback" && probe.discoveryError && (
                <p className="bench-note">
                  未能读取供应商动态目录，已回退到内置候选：{probe.discoveryError}
                </p>
              )}
            </div>
          )}

          <div className="settings-tool-groups">
            <section>
              <div>
                <strong>连接验证</strong>
                <span>检查当前供应商、密钥和模型能否正常返回内容。</span>
              </div>
              <button
                className="button secondary small"
                onClick={() => void runTest()}
                disabled={testing || !bridge}
              >
                {testing ? "测试中…" : "测试连接"}
              </button>
              {llmTest && (
                <small
                  className={
                    llmTest.ok && llmTest.verdict === "pass"
                      ? "ok-text"
                      : "warn-text"
                  }
                >
                  {llmTest.ok
                    ? `${
                        llmTest.verdict === "pass"
                          ? "8 秒目标达标"
                          : llmTest.verdict === "warning"
                            ? "可用但延迟偏高"
                            : "高风险：超过 12 秒"
                      } · ${llmTest.elapsed}s${
                        " · 已返回"
                      }`
                    : llmTest.hint || llmTest.message?.slice(0, 48)}
                </small>
              )}
            </section>
            <section>
              <div>
                <strong>真实建议测速</strong>
                <span>使用完整提示词和上下文，结果才可用于模型选型。</span>
              </div>
              <div className="settings-tool-actions">
                <button
                  className="button secondary small"
                  onClick={() => void runBench(false)}
                  disabled={benching || !bridge}
                >
                  <Gauge size={14} />
                  {benching ? "测速中…" : "测当前模型"}
                </button>
                <button
                  className="text-button"
                  onClick={() => void runBench(true)}
                  disabled={benching || !bridge}
                  title="遍历所有已配置密钥的供应商，用于选型对比"
                >
                  对比全部供应商
                </button>
              </div>
            </section>
          </div>

          {status?.llm.message && !status.llm.ok && (
            <div className="inline-error tight">{status.llm.message}</div>
          )}

          {bench && (
            <div className="settings-panel">
              <div className="settings-panel-title">
                {bench.scope === "all" ? "各家对比" : "当前模型测速"}
                {bench.fastest && bench.scope === "all" && (
                  <em>
                    最快 {bench.fastest.label} · {bench.fastest.avg}s
                  </em>
                )}
              </div>
              <div className="bench-table compact">
                <div className="bench-row head">
                  <span>供应商</span>
                  <span>模型</span>
                  <span>均值</span>
                  <span>最慢</span>
                </div>
                {bench.results.map((row) => (
                  <div
                    className={`bench-row ${
                      row.ok &&
                      row.max !== null &&
                      row.max < bench.targetSeconds
                        ? "good"
                        : ""
                    }`}
                    key={row.provider}
                  >
                    <span>{row.label}</span>
                    <span className="mono">{row.model}</span>
                    <span className="mono">
                      {row.ok ? `${row.avg}s` : "—"}
                    </span>
                    <span className="mono">
                      {row.ok
                        ? `${row.max}s`
                        : row.error?.slice(0, 28) || "失败"}
                    </span>
                  </div>
                ))}
              </div>
              <p className="bench-note">
                看<strong>最慢一次</strong>；目标 &lt; {bench.targetSeconds}s。
                {bench.scope === "current"
                  ? " 这是当前选中模型的完整建议生成耗时。"
                  : " 仅包含已配置密钥的供应商。"}
              </p>
            </div>
          )}
        </section>

        {/* ── 检索 / 触发 ── */}
        <div className="settings-two-col" id="settings-retrieval">
          <section className="settings-card">
            {/* 诊断进程尚未返回时不能把空状态误报成云端检索。 */}
            <header className="settings-card-head">
              <div>
                <span className="settings-card-kicker">知识库</span>
                <h2>检索</h2>
              </div>
              <span className="local-tag">
                {!retrieval
                  ? "状态未读取"
                  : retrieval.backend === "local"
                    ? "本地"
                    : "云端"}
              </span>
            </header>
            <p className="settings-static">
              <strong>{retrieval?.label || "正在读取知识库状态…"}</strong>
              <span>{retrieval?.note || "请稍候，或检查本机服务状态"}</span>
            </p>
          </section>

          <section className="settings-card">
            <header className="settings-card-head">
              <div>
                <span className="settings-card-kicker">会议中</span>
                <h2>建议触发</h2>
              </div>
            </header>
            <div className="settings-row">
              <label className="field grow">
                <span>对方停顿多久后给建议</span>
                <select
                  value={persisted.silenceSeconds}
                  onChange={(event) =>
                    onChange({
                      ...persisted,
                      silenceSeconds: Number(event.target.value),
                    })
                  }
                >
                  <option value={1.5}>1.5 秒（最快）</option>
                  <option value={2}>2 秒</option>
                  <option value={3}>3 秒（推荐）</option>
                  <option value={5}>5 秒（最稳）</option>
                </select>
              </label>
              <label className="field grow">
                <span>每次条数</span>
                <select
                  value={persisted.suggestionCount}
                  onChange={(event) =>
                    onChange({
                      ...persisted,
                      suggestionCount: Number(event.target.value),
                    })
                  }
                >
                  <option value={2}>2 条</option>
                  <option value={3}>3 条</option>
                </select>
              </label>
            </div>
            {/*
              这两项此前只存在于界面、从没传到桥接层，用户改了没有任何效果。
              现在真的生效了，就得把"它到底控制什么"说清楚——
              等待时间不等于这个值，前面还有转写送达延迟，后面还有 20 秒冷却。
            */}
            <p className="hint-text trigger-hint">
              等的是<strong>转写出现后</strong>的静默时长，不是你听到对方停下的那一刻——
              转写本身还有几秒延迟。两批自动建议之间另有 20 秒冷却，
              调小只让单批更快出现，不会让建议变多。
              <br />
              下一场会议开始时生效；等不及可随时点「现在给建议」。
            </p>
          </section>
        </div>

        {/* ── 数据 ── */}
        <section className="settings-card" id="settings-data">
          <header className="settings-card-head">
            <div>
              <span className="settings-card-kicker">隐私</span>
              <h2>本机数据</h2>
            </div>
            <span className="local-tag">不上传</span>
          </header>
          <div className="settings-row">
            <label className="field grow">
              <span>目录</span>
              <input value={dataInfo?.root || "…"} readOnly />
            </label>
            <div className="field">
              <span>占用</span>
              <span className="mono settings-stat">
                {dataInfo
                  ? `${formatBytes(dataInfo.dbBytes)} · 录音 ${dataInfo.audioCount}`
                  : "—"}
              </span>
            </div>
          </div>
          <div className="settings-toolbar">
            <button
              className="button secondary small"
              onClick={() => void bridge?.revealDataFolder()}
              disabled={!bridge}
            >
              打开目录
            </button>
            <button
              className="button ghost small danger"
              onClick={() => void clearData()}
              disabled={!bridge}
            >
              清空数据
            </button>
          </div>
          <p className="settings-footnote">
            转写与建议会调用云服务；知识库文档为路径引用，清空不会删除原文件。
          </p>
        </section>
        </div>
      </div>
    </div>
  );
}
