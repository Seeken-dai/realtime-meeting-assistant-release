"""
POC 配置模板。

    cp config.example.py config.py    # config.py 已在 .gitignore 中忽略

═══════════════════════════════════════════════════════════
架构原则：三类服务【完全解耦】，可任意组合、独立更换
    ① 语音识别（ASR）    —— 把声音变成带说话人的文字
    ② 建议模型（LLM）    —— 生成话术建议
    ③ 知识库检索          —— 从文档中找相关片段
例如可以：讯飞 ASR + 阿里云 LLM + 本地检索
       或：讯飞 ASR + 讯飞星火 + 本地检索（只需一个平台的账号）
任一家欠费或故障，只影响它负责的那一环，不会连累其它环节。
═══════════════════════════════════════════════════════════
"""

# ╔══════════════════════════════════════════════════════╗
# ║  ① 语音识别服务（ASR）                                ║
# ╚══════════════════════════════════════════════════════╝
ASR_PROVIDER = "aliyun"     # 方案A推荐 aliyun；也可 xfyun / xfyun-llm / volcano / tencent / mimo

# 识别语种（限制自动识语种串出日文等）。桌面设置页可覆盖；也可设环境变量 ASR_LANG
#   zh     = 中文
#   en     = 英文
#   zh_en  = 中英混用（默认）
# 各家映射（providers._asr_lang_params）：
#   阿里      language_hints = ['zh'] / ['en'] / ['zh','en']
#   讯飞标准  lang = cn / en / cn
#   讯飞大模型 lang = autodialect / en / autodialect
#   火山      request.language = zh-CN / en-US / （不传=中英+方言）
#   腾讯      engine_model_type = 16k_zh / 16k_en / 16k_zh_en
#   小米 MiMo asr_options.language = zh / en / auto（混说只能 auto）
ASR_LANG = "zh_en"

# 讯飞 RTASR（实时语音转写·标准版）—— POC 已验证可用，推荐
# 控制台 https://console.xfyun.cn/ → 实时语音转写 → 我的应用
XFYUN_APP_ID = ""
XFYUN_API_KEY = ""      # 注意：必须是「实时语音转写」服务下的 APIKey

# 讯飞 实时语音转写【大模型】版（基于星火大模型，非标准版）
# 控制台 https://console.xfyun.cn/services/new_rta
#   · App ID：开放平台应用 ID（可与上方 XFYUN_APP_ID 相同，也可单独填）
#   · accessKeyId / accessKeySecret：大模型服务页的接口认证信息
# ⚠️ appId 与 accessKeyId 不是同一个；混用会握手 37000 parameter is wrong
XFYUN_LLM_ASR_APP_ID = ""   # 可留空，回退使用 XFYUN_APP_ID
XFYUN_LLM_ASR_KEY_ID = ""
XFYUN_LLM_ASR_KEY_SECRET = ""

# 阿里云百炼实时语音识别（无实时说话人分离，靠本地声纹认「我」）
# 控制台 https://bailian.console.aliyun.com/ → API-KEY
ALIYUN_ASR_KEY = ""
# 可选模型：
#   - "qwen-audio-3.0-asr-flash-streaming"（推荐：Qwen-Audio 3.0 大模型流式转写）
#   - "fun-asr-realtime"（FunASR 实时语音识别，中文与方言）
#   - "paraformer-realtime-v2"（经典 Paraformer 实时模型）
ALIYUN_ASR_MODEL = "qwen-audio-3.0-asr-flash-streaming"

# 火山引擎豆包
# 控制台 https://console.volcengine.com/speech/app
VOLC_APP_KEY = ""
VOLC_ACCESS_KEY = ""

# 腾讯云
# 控制台 https://console.cloud.tencent.com/asr
TENCENT_APP_ID = ""
TENCENT_SECRET_ID = ""
TENCENT_SECRET_KEY = ""

# 小米 MiMo（整段转写，无分离，仅作准确率对照）
# https://mimo.mi.com/
MIMO_API_KEY = ""


# ╔══════════════════════════════════════════════════════╗
# ║  ② 建议模型服务（LLM）—— 与上面的 ASR 完全独立         ║
# ╚══════════════════════════════════════════════════════╝
# xfyun（经典系列）/ xfyun-x2-flash / xfyun-x2 / xfyun-x1.5 / aliyun / mimo
# / gemini / zhipu / deepseek / moonshot / grok / custom
LLM_PROVIDER = "xfyun-x2-flash"

# ── 讯飞星火 ─────────────────────────────────────────
# ⚠️ 讯飞的坑（POC 实测踩过）：
#   1. 每个模型的 APIPassword 是【独立的】，X2 和经典系列各有各的，不能混用
#      （用错会返回 500 AppIdNoAuthError —— 模型名对但无权限）
#   2. 不同系列走【完全不同的端点】，这是最大的坑：
#        经典系列  /v1          模型名 4.0Ultra / max-32k …
#        X2-Flash  /agent/v1    模型名 spark-x     ← 注意不是 /x2
#        X2        /x2          模型名 spark-x
#        X1.5      /v2          模型名 spark-x
#      端点填错会报 AppIdNoAuthError，看起来像密钥问题，实际是地址问题
#   3. X 系列模型名统一为 spark-x，靠端点区分版本
#   4. 以上都与 RTASR 的 APIKey 无关，那是第四个凭证
# 控制台 https://console.xfyun.cn/ → 对应模型服务 → APIPassword

XFYUN_SPARK_PASSWORD = ""   # 经典系列（4.0Ultra / max-32k 等）
XFYUN_X2_PASSWORD = ""      # 星火 X2（含 X2-Flash）
XFYUN_X15_PASSWORD = ""     # 星火 X1.5

# 阿里云通义千问（与 ALIYUN_ASR_KEY 同一个百炼控制台，但建议分开两个 Key）
# 控制台 https://bailian.console.aliyun.com/ → API-KEY
ALIYUN_LLM_KEY = ""

# 小米 MiMo（https://mimo.mi.com/ 取 API Key）
# 默认模型 mimo-v2-flash，也可用 mimo-v2.5-pro
MIMO_LLM_KEY = ""

# ── 快速档模型（实时会议场景优先）────────────────────────
# 建议延迟是 P0 指标，flash/turbo 这类为速度优化的型号更契合。
# 填了哪家，python -m tests.test_llm --bench-all 就会自动把它纳入测速排名。
GEMINI_LLM_KEY = ""     # https://aistudio.google.com/apikey
ZHIPU_LLM_KEY = ""      # https://open.bigmodel.cn/  glm-4-flash 长期免费
DEEPSEEK_LLM_KEY = ""   # https://platform.deepseek.com/api_keys
MOONSHOT_LLM_KEY = ""   # https://platform.moonshot.cn/console/api-keys
GROK_LLM_KEY = ""       # https://console.x.ai/  xAI Grok

# 自定义 OpenAI 兼容服务 —— 可指向本地模型或任意网关
# 例：Ollama 本地部署（数据完全不出内网，适合强隐私要求场景）
#   CUSTOM_LLM_BASE_URL = "http://localhost:11434/v1"
#   CUSTOM_LLM_MODEL    = "qwen2.5:14b"
#   CUSTOM_LLM_KEY      = "ollama"
# 也支持 vLLM / LM Studio / one-api / 各类中转网关
CUSTOM_LLM_BASE_URL = ""
CUSTOM_LLM_MODEL = ""
CUSTOM_LLM_KEY = ""

LLM_MODEL = None            # 覆盖默认模型名；None = 用供应商默认


# ╔══════════════════════════════════════════════════════╗
# ║  ③ 知识库检索                                         ║
# ╚══════════════════════════════════════════════════════╝
# local     —— 本地关键词检索，零 API 依赖（POC 推荐，不受欠费影响）
# embedding —— 云端向量检索，效果更好，需阿里云百炼 Key
# auto      —— 优先向量，失败自动降级到本地
RETRIEVAL_BACKEND = "local"
EMBEDDING_KEY = ""          # 仅 embedding/auto 模式需要（阿里云百炼 Key）


# ╔══════════════════════════════════════════════════════╗
# ║  音频参数（一般无需修改）                              ║
# ╚══════════════════════════════════════════════════════╝
SAMPLE_RATE = 16000   # 采样率 16kHz
CHANNELS = 1          # 单声道（说话人分离要求单声道）
FRAME_MS = 40         # 每帧时长（毫秒）
