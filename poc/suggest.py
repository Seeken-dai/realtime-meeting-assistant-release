"""
实时话术建议引擎（对应 PRD §3.3 / §6.4）。

两种能力：
  1. suggest()  —— 对方说完一段话后，站在"我"的立场自动给 2-3 条建议
  2. answer()   —— 我主动提问，结合会议上下文 + 知识库回答

关键设计：
  - 系统提示词明确"站在我方立场"，这是产品的核心差异点
  - 知识库检索不到时必须如实说明，不允许编造（PRD §3.3.1 质量边界）
  - 输出结构化 JSON，前端渲染成卡片（意图提示 / 建议话术 / 依据来源）
"""

import json
import re
import time

# 会中话术是提词器，不是阅读材料。此前实测偶发 113～460 字的长文，
# 用户根本来不及在会议中读完。提示词负责把大多数输出控制在理想区间，
# 程序化上限负责兜住不听指令的模型。
SUGGESTION_SCRIPT_TARGET_CHARS = 60
SUGGESTION_SCRIPT_MAX_CHARS = 80

# LLM 请求的默认可靠性策略。实时建议仍保留较短截止时间，避免会议中长时间
# 卡住；仅对连接/超时/限流/服务端错误做一次有限重试，鉴权和模型名错误不重试。
DEFAULT_LLM_TIMEOUT_SECONDS = 12.0
DEFAULT_LLM_RETRY_ATTEMPTS = 2
DEFAULT_LLM_RETRY_BACKOFF_SECONDS = 0.35


def classify_llm_error(error):
    """把 OpenAI 兼容 SDK 的异常归类为可重试或不可重试。"""
    current = getattr(error, "cause", error)
    name = type(current).__name__.lower()
    message = str(current or "").strip()
    low = message.lower()
    status = getattr(current, "status_code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None

    if (
        "timeout" in name
        or "timed out" in low
        or "timeout" in low
        or status == 408
    ):
        return "timeout", True, "llm"
    if status in {429} or "rate limit" in low or "too many requests" in low:
        return "rate_limit", True, None
    if (
        "connection" in name
        or "connection error" in low
        or "connection reset" in low
        or "temporarily unavailable" in low
        or "server error" in low
        or status in {409, 425, 500, 502, 503, 504}
    ):
        return "connection", True, None
    return "request", False, None


class LLMRequestError(RuntimeError):
    """保留底层异常，同时携带重试次数和服务诊断信息。"""

    def __init__(self, cause, *, attempts, timeout_seconds):
        self.cause = cause
        self.attempts = max(1, int(attempts))
        self.timeout_seconds = float(timeout_seconds)
        self.kind, self.retryable, self.timeout_stage = classify_llm_error(cause)
        super().__init__(str(cause) or type(cause).__name__)


def llm_error_details(
    error,
    *,
    provider=None,
    model=None,
    timeout_seconds=None,
    stage=None,
):
    """返回可安全落库/回传前端的 LLM 错误摘要，不包含密钥。"""
    kind, retryable, timeout_stage = classify_llm_error(error)
    attempts = max(1, int(getattr(error, "attempts", 1)))
    effective_timeout = timeout_seconds
    if effective_timeout is None:
        effective_timeout = getattr(error, "timeout_seconds", None)
    try:
        effective_timeout = float(effective_timeout) if effective_timeout is not None else None
    except (TypeError, ValueError):
        effective_timeout = None
    cause = getattr(error, "cause", error)
    cause_message = str(cause or type(cause).__name__).strip().replace("\n", " ")[:300]
    return {
        "kind": kind,
        "retryable": bool(retryable),
        "timeoutStage": timeout_stage,
        "attempts": attempts,
        "timeoutSeconds": effective_timeout,
        "provider": str(provider or ""),
        "model": str(model or ""),
        "stage": str(stage or "llm"),
        "cause": cause_message,
    }


def format_llm_error(details, label="LLM 服务"):
    """把结构化错误变成面向用户的简短提示。"""
    provider = details.get("provider") or "未知供应商"
    model = details.get("model") or "默认模型"
    kind = details.get("kind")
    if kind == "timeout":
        timeout = details.get("timeoutSeconds")
        reason = f"请求超时{f'（{timeout:g} 秒）' if timeout else ''}"
    elif kind == "connection":
        reason = "连接失败"
    elif kind == "rate_limit":
        reason = "服务限流"
    else:
        reason = f"请求失败：{details.get('cause') or '未知错误'}"
    attempts = max(1, int(details.get("attempts") or 1))
    suffix = f"，已尝试 {attempts} 次" if attempts > 1 else ""
    return f"{label}（{provider} / {model}：{reason}{suffix}）"

# 这类内容不能直接交给用户照读：它意味着模型没有把模板填完。
_SPOKEN_PLACEHOLDER_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z])x{2,}(?![A-Za-z])|"
    r"(?<![A-Za-z])(?:tbd|todo)(?![A-Za-z])|"
    r"[（(【\[]\s*(?:待填|占位|填写|项目名|客户名|负责人|期限|时间|姓名|"
    r"name|owner|date|xxx|xx)[^）)】\]]*[）)】\]]"
)

SAFE_CLARIFY_SCRIPT = "我先确认一下具体边界和验收标准，核实后给您准确答复。"

SCENE_CONFIGS = {
    "general": {
        "label": "通用会议",
        "categories": ("澄清", "总结", "风险", "下一步"),
        "instruction": "优先帮助我澄清事实、总结共识、指出风险，并给出下一步动作。",
        "minutes": ("关键结论", "风险与待确认", "下一步行动"),
    },
    "sales": {
        "label": "售前沟通",
        "categories": ("客户目标", "客户痛点", "异议", "能力边界", "商务承诺", "推进动作"),
        "instruction": "优先识别客户目标与痛点，回应异议，守住能力和商务承诺边界，并推动下一步。不得在没有资料时承诺报价、工期或交付。",
        "minutes": ("客户目标与痛点", "异议与能力边界", "商务承诺", "推进动作"),
    },
    "requirements": {
        "label": "需求评审",
        "categories": ("范围", "业务规则", "数据与接口", "异常分支", "验收标准", "责任人与待确认项"),
        "instruction": "优先追踪范围、业务规则、数据与接口、异常分支、验收标准，以及责任人与待确认项。发现“简单改一下”时主动追问完整条件。",
        "minutes": ("范围与业务目标", "业务规则与异常分支", "数据与接口", "验收标准", "责任人与待确认项"),
    },
}


def normalize_scene(scene=None):
    value = str(scene or "general").strip().lower()
    return value if value in SCENE_CONFIGS else "general"


def scene_config(scene=None):
    return SCENE_CONFIGS[normalize_scene(scene)]


def _contains_spoken_placeholder(text):
    return bool(_SPOKEN_PLACEHOLDER_PATTERN.search(str(text or "")))

# LLM 供应商配置：全部走 OpenAI 兼容接口，一套代码切换
PROVIDERS = {
    # ⚠️ 讯飞星火的 X 系列与经典系列【端点不同、模型名也不同】：
    #    经典系列走 /v1，模型名是 4.0Ultra / max-32k 等；
    #    X 系列走 /x2 或 /v2，模型名统一为 spark-x —— 靠端点区分版本。
    #    凭证都是控制台的 APIPassword（形如 AK:SK），非 RTASR 的 Key。
    "xfyun": {
        "base_url": "https://spark-api-open.xf-yun.com/v1",
        "model": "4.0Ultra",
        "label": "讯飞星火（经典系列）",
    },
    "xfyun-x2-flash": {
        # 官方文档 https://www.xfyun.cn/doc/spark/X2-Flash.html
        # ⚠️ 端点是 /agent/v1，与 X2 的 /x2 完全不同，别混
        "base_url": "https://spark-api-open.xf-yun.com/agent/v1",
        "model": "spark-x",
        "label": "讯飞星火 X2-Flash",
    },
    "xfyun-x2": {
        "base_url": "https://spark-api-open.xf-yun.com/x2",
        "model": "spark-x",
        "label": "讯飞星火 X2",
    },
    "xfyun-x1.5": {
        "base_url": "https://spark-api-open.xf-yun.com/v2",
        "model": "spark-x",
        "label": "讯飞星火 X1.5",
    },
    "aliyun": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "label": "阿里云通义千问",
    },
    "mimo": {
        "base_url": "https://api.xiaomimimo.com/v1",
        "model": "mimo-v2-flash",     # 或 mimo-v2.5-pro
        "label": "小米 MiMo",
        # MiMo 文档示例用 api-key 头；同时带 Bearer 与 api-key 以兼容两种写法
        "auth_header": "api-key",
    },
    # ── 以下为「快速档」模型，实时会议场景优先考虑 ──
    # 建议延迟是本产品的 P0 指标（PRD < 5s），大模型旗舰档普遍偏慢，
    # flash/turbo 这类为速度优化的型号更契合场景。
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        # 实测 2.4s，比 gemini-2.5-flash（12.5s）快一个数量级，见 HANDOFF §4.8。
        # ⚠️ 别"顺手升级"成非 lite 的新版本：3.5-flash 中位 24.2s、出现过 70s。
        "model": "gemini-3.5-flash-lite",
        "label": "Google Gemini Flash",
    },
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",       # 智谱有长期免费的 flash 档
        "label": "智谱 GLM Flash",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "label": "DeepSeek",
    },
    "moonshot": {
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "label": "月之暗面 Kimi",
    },
    "grok": {
        "base_url": "https://api.x.ai/v1",
        # fast 档更契合实时会议的延迟要求；旧模型名会重定向到新版本
        "model": "grok-4-fast-non-reasoning",
        "label": "xAI Grok",
    },
    "custom": {
        "base_url": None,             # 由 config.py 提供
        "model": None,
        "label": "自定义 OpenAI 兼容服务",
        # 可指向 Ollama / vLLM / one-api / 任意 OpenAI 兼容网关
    },
}

TONE_CONFIGS = {
    "direct": {
        "label": "直率务实（产研内推）",
        "description": "极简干货、直指技术与业务逻辑、直说方案漏洞与执行动作，不带客套寒暄。",
        "persona": "你是一名精炼、务实、直切要害的会议助手（尤其适合产研内部评审、技术过会与敏捷站会）。",
        "instruction": (
            "- 语气风格：平铺直叙、极简精炼、直指技术与业务逻辑要害，严禁一切公关辞令、客套寒暄和外交太极。\n"
            "- 禁止输出“您方便详细说说”、“我先确认一下再答复您”等客服/商务套话。\n"
            "- 发现问题直接说解决方案、技术约束或异常分支（例如：“该接口需加幂等，前端传 request_id 即可”）。\n"
            "- 知识库无依据时，直接指出“资料中无此定义，需对齐确认”或直接反问核心参数，不兜圈子。"
        ),
    },
    "business": {
        "label": "商务稳健（对外客户）",
        "description": "得体客气、留有余地、严控承诺边界、积极引导对方需求。",
        "persona": "你是一名资深售前顾问/产品经理的会议助手，协助应对外部客户或商务沟通。",
        "instruction": (
            "- 语气风格：得体专业、温和客气、严谨稳健。\n"
            "- 严控我方承诺边界，未明确事项积极引导对方提供更多业务背景。\n"
            "- 知识库无依据时，委婉表达“这块需要核实后给您准确答复”。"
        ),
    },
    "challenger": {
        "label": "敏锐质询（把关挑刺）",
        "description": "以质疑、挑刺、找逻辑漏洞与异常边界为主，充当会议的风险守门人。",
        "persona": "你是一名极其敏锐、严苛的技术专家/架构评审顾问，负责在会议中挑刺把关、排查风险。",
        "instruction": (
            "- 语气风格：敏锐犀利、注重风险把控、直击潜在漏洞。\n"
            "- 重点关注高并发、异常容灾、边界条件、工期风险和不合理诉求，给出具有穿透力的质询话术。\n"
            "- 知识库有依据时用依据反问，无依据时直接质询实现可行性与兜底方案。"
        ),
    },
    "collaborative": {
        "label": "温和协调（推进共识）",
        "description": "善于总结分歧、化解冲突、提出折中方案并明确下一步行动。",
        "persona": "你是一名擅长跨部门协同的项目推进顾问，负责化解分歧、拉齐认知并推动共识。",
        "instruction": (
            "- 语气风格：包容建设性、积极推动共识、聚焦可落地的折中方案。\n"
            "- 识别多方分歧点并提炼共同目标，话术侧重于拆分期次、明确责任人与推进下一步动作。"
        ),
    },
    "custom": {
        "label": "自定义风格",
        "description": "使用用户在后台填写的个性化角色与风格提示词。",
        "persona": "你是用户专属定制的会议助手。",
        "instruction": "- 语气风格：请严格遵循下方【用户自定义风格补充要求】进行话术生成。",
    },
}


def normalize_tone(tone=None):
    value = str(tone or "direct").strip().lower()
    return value if value in TONE_CONFIGS else "direct"


def tone_config(tone=None):
    return TONE_CONFIGS[normalize_tone(tone)]


def build_suggestion_system_prompt(
    me_name: str = "我",
    scene: str = "general",
    tone: str = "direct",
    custom_tone_prompt: str = "",
    count: int = 3,
    target_chars: int = SUGGESTION_SCRIPT_TARGET_CHARS,
    max_chars: int = SUGGESTION_SCRIPT_MAX_CHARS,
) -> str:
    s_cfg = scene_config(scene)
    t_cfg = tone_config(tone)

    tone_instruction = t_cfg["instruction"]
    if custom_tone_prompt and custom_tone_prompt.strip():
        tone_instruction += f"\n【用户自定义风格补充要求】\n{custom_tone_prompt.strip()}\n"

    system_text = f"""{t_cfg['persona']}

你的立场：你始终站在「{me_name}」这一方，帮 TA 应对当前会议中对方的发言。
你的输出会在会议进行中实时显示给「{me_name}」看，TA 会【参考或直接照着你的话术开口】。

═══ 话术人设与风格要求（{t_cfg['label']}） ═══
{tone_instruction}

═══ 铁律一：区分【事实】与【策略】，前者绝不编造 ═══

把你要说的内容分成两类，它们的规则完全不同：

▸ **事实型内容**：我方产品能力、技术参数、报价数字、交付周期、历史案例、
  第三方产品名、任何形式的承诺。
  → **只能来自【知识库片段】**。知识库没写的，**一个字都不许编**。
  → 不许用"通常""一般来说""应该可以"来填补空白。
  → 知识库无依据时，涉及事实的话术只能是澄清或追问型：
    "这个我需要确认一下再准确答复""能否说明具体参数与边界"
  → **“我们不支持 X / 做不了 X / 属于定制”同样是事实断言**；知识库没写时
    禁止断言归属，只能问需求、说“需核实”，或策略性地说
    “这类能力一般要单独评估范围与工作量”（不要断言“属于定制开发范畴”）。

▸ **策略型内容**：沟通技巧、追问方向、风险提示、话术结构、
  如何把话题拉回来、需求分析的常见陷阱。
  → **鼓励你运用专业经验自由发挥**，这类内容不涉及我方事实断言，是安全且有价值的。
  → 但**不得夹带任何我方产品能力的暗示或承诺**。

  ⚠️ 唯一要小心的是【不要替我方作承诺】：
     反例："我们会出具方案，确保满足审计合规" ← 承诺交付结果，越界了
     正例："这类需求通常要先明确 X 和 Y 才能评估，方便说说吗？"
     正例："建议现在就把边界确认清楚，避免后期返工"
     行业通行做法、技术方向、常见坑，都可以讲 —— 只要不说成"我们能做到"。

【判断口诀】这句话如果说错了，对方会拿它来要求我方兑现吗？
  会 → 事实型，必须有依据；不会 → 策略型，可自由发挥。

═══ 引用纪律（减少“引错文档”） ═══

- 涉及**产品已有能力**（接口、Webhook、审批节点、标准功能）时：
  优先引用标题/正文明确写了该能力的片段（常见如《产品功能清单》），
  **不要**用边界/报价/历史案例文档去支撑“我们有没有某能力”。
- 只有片段原文里**真的出现**了你要说的事实，才能标 grounded 并写 evidence.quote。
- 检索片段和当前问题无关时：宁可用 advisory/clarify，也不要硬套无关引用。

═══ 关于内部资料：默认谨慎，但不必因噎废食 ═══

知识库中有些内容标注为「内部资料」（成本数字、其他客户名称、内部口径）。
**默认**把它转化为得体的说法，而不是原样复述：
  · 成本数字 → "这块需要评估后出正式结论/报价"
  · 其他项目/客户名 → 匿名到行业（"我们有个零售行业案例…"）
  · 对方追问具体名称时：优先用
    「需授权后才能具名」「可先讲行业匿名案例」这类说法，
    话术里应出现「案例 / 授权 / 行业 / 匿名」中的关键信息，避免空话。

但这也只是默认倾向，**不是硬禁令**：用户是这份知识库的主人，最终由他决定
说不说。如果引用具体数字确实能帮他（比如用工作量区间锚定预期），
你可以给出，系统会自动标注提醒他复核。
你真正要避免的是**无意识地泄露** —— 不要在用户没意识到的情况下，
把内部数字或敏感名称混在一大段话里带出去。

═══ 其他要求 ═══
- 话术要口语化、简洁，是【能直接说出口的话】，不是书面报告。
- 每条 script 只保留一个核心动作，建议 35～{target_chars} 字，最多
  {max_chars} 字（含标点）；优先给结论和下一句该怎么说，删掉背景复述、
  原因展开和客套填充。如果信息较多，拆到不同建议中，不要塞成长段。
- 主动识别对方话里的风险和陷阱（模糊需求、免费预期、工期承诺等）。
- 严格输出 JSON，不要任何额外文字。

输出格式：
{{"suggestions": [
  {{"intent": "一句话点破对方意图或风险",
    "script": "建议我方直接说出口的话术",
    "category": "本场场景下的建议分类",
    "references": ["依据的文档名"],
    "evidence": [{{"source": "依据的文档名", "quote": "从候选片段逐字复制的原文短句"}}],
     "type": "grounded"}}
],
"memoryCandidates": [
  {{"kind": "decision", "content": "已明确达成的决策原句", "owner": null, "dueAt": null}},
  {{"kind": "action_item", "content": "已明确提出的待办原句", "owner": null, "dueAt": null}}
]}}

memoryCandidates 只记录当前对话中已经明确说出的决策和待办，中文、英文或中英混写都可以；
不要根据上下文猜测未说出的结论。没有明确决策或待办时返回空数组。owner 和 dueAt
无法确认时必须返回 null。

type 三选一，必须如实标注：
- "grounded"  ：话术中的事实性内容全部有知识库支撑，references 填来源文档名。
                evidence 必须给 1～2 条，quote 只能从对应候选片段逐字复制，
                不得改写、概括、拼接。
- "advisory"  ：**经验建议**。不含任何我方事实断言，只有沟通策略/追问方向/
                风险提示等专业经验。references 和 evidence 都留空数组。
- "clarify"   ：涉及我方产品能力/报价/承诺，但知识库无依据 —— 此时话术
                只做澄清或追问，不臆造我方能力。references 和 evidence 都留空数组。
                “我方不支持/无法提供某能力”同样属于事实断言；没有原文依据时也不能说，
                只能问“是否需要该能力”或说“需要核实”。

生成 {count} 条，按重要性排序。
**知识库没内容时也要给出有价值的建议** —— 你的专业经验（怎么追问、有什么坑、
行业通行做法、如何引导话题）对用户很有价值，标成 advisory 即可，
不要因为谨慎就只会说"我确认一下"，那对开会中的人毫无帮助。
唯一的红线是：不编造我方的产品能力、数据和承诺。

【本场会议场景：{s_cfg['label']}】
{s_cfg['instruction']}
建议 category 必须从本场场景的分类中选择：{"、".join(s_cfg['categories'])}"""
    return system_text


def build_answer_system_prompt(me_name: str = "我", tone: str = "direct", custom_tone_prompt: str = "") -> str:
    t_cfg = tone_config(tone)
    custom_part = f"\n补充风格要求：{custom_tone_prompt.strip()}" if custom_tone_prompt and custom_tone_prompt.strip() else ""
    return f"""{t_cfg['persona']}，站在「{me_name}」这一方。
{me_name} 会在会议中随时向你提问，你要结合【当前会议上下文】和【知识库片段】简洁作答。

风格要求（{t_cfg['label']}）：{t_cfg['instruction']}{custom_part}

严格遵守：
1. 只依据知识库片段中的事实回答，没有依据就明确说"知识库中没有相关信息"，绝不编造。
2. 回答要简短（3 句话以内），因为 {me_name} 正在开会，没时间读长文。
3. 如果引用了知识库，在末尾用「依据：文档名」标注。
4. 注意区分【可对外说】和【内部资料】——内部口径不要直接建议对客户原样复述。"""


def _format_context(transcript, me_name):
    """把转写记录格式化为对话上下文"""
    lines = []
    for seg in transcript:
        speaker = seg["speaker"]
        tag = f"{speaker}（我方）" if speaker == me_name else speaker
        lines.append(f"{tag}：{seg['text']}")
    return "\n".join(lines)


def _format_refs(hits):
    if not hits:
        return "（本次检索没有返回任何片段 —— 知识库中没有相关内容）"
    return "\n\n".join(
        f"【候选片段{i+1}·来自《{h['source']}》】\n{h['text']}"
        for i, h in enumerate(hits)
    )


_REF_PREAMBLE = """⚠️ 关于下面的【候选片段】，你必须知道：

这些片段是【关键词检索】返回的，检索器并不理解语义，**很可能与当前问题毫无关系**
—— 它可能只是碰巧匹配了"系统""要求""记录"这类通用词。

因此你的第一步是**逐个判断每个片段是否真的回答了当前的问题**：
- 片段中确实包含回答该问题所需的事实 → 可以引用，标 type=grounded
- 片段只是话题相近、或只匹配了通用词，并未真正涉及问题所问的东西
  → **视为知识库没有相关内容**，绝不可拿它当依据

【自检】写完话术后回头看：我说的每一个关于我方产品/报价/案例的事实，
是否能在某个片段里逐字找到对应？grounded 必须把对应原句复制到 evidence；
找不到的，必须删掉或降级为 clarify。"""


def _iter_json_candidates(text):
    """从模型输出里按可信度依次给出可能的 JSON 片段。

    ⚠️ 原实现只试一个候选：先找 ```json 代码块，否则用贪婪的 `\\{.*\\}`。
       两种情况都会失败：模型带思维链再输出代码块时贪婪匹配会把前后杂项
       一起吞进来；输出多个代码块时只取到第一个（可能是示例而非结果）。
       改为多候选逐个尝试 —— 解析成功即采用。
    """
    text = (text or "").strip()
    # 1) 所有 markdown 代码块，按出现顺序
    for m in re.finditer(r"```(?:json)?\s*(.+?)\s*```", text, re.S):
        yield m.group(1)
    # 2) 花括号配对扫描：从每个 { 出发找到与之匹配的 }，比贪婪匹配精确
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                yield text[start:i + 1]
    # 3) 兜底：整段
    yield text


def _extract_json(text):
    """LLM 有时会包 markdown 代码块或夹带说明文字，做容错提取。

    返回的必须是带 suggestions 数组的对象；解析出别的结构（例如模型只回了
    一条建议的裸对象）不算成功，交给调用方重试。
    """
    last_error = None
    for candidate in _iter_json_candidates(text):
        try:
            data = json.loads(candidate)
        except Exception as exc:      # noqa: BLE001 - 逐个候选试错
            last_error = exc
            continue
        if isinstance(data, dict) and isinstance(data.get("suggestions"), list):
            return data
    raise ValueError(f"输出中找不到合法的建议 JSON（{last_error}）")


# 强承诺词：出现在 advisory（经验建议）中即视为越界。
# “保证/确保”不能裸扫：像“为了保证数字准确，先确认范围”是沟通策略，
# 并不是承诺我方交付结果。只有与我方或交付结果搭配时才提示。
_COMMIT_WORDS = ["一定能", "一定可以", "必然", "完全满足",
                 "我们会做到", "肯定可以", "承诺"]
_GUARANTEE_COMMIT_PATTERN = re.compile(
    r"(?:我们|我方).{0,8}(?:确保|保证)"
    r"|(?:确保|保证).{0,10}(?:满足|完成|交付|实现|通过|达到)")
_DELIVERY_COMMIT_PATTERN = re.compile(
    r"(?:我们|我方)(?:会|将在).{0,18}(?:出具|提供|完成|交付|解决|答复)"
    r"|\d+\s*个?工作日内")

# “支持”和“不支持”都是产品事实。模型偶尔会把否定能力包装成 clarify，
# 例如“我们系统不支持区块链存证”，但知识库没写时这仍然是编造。
# ⚠️ 不要用过宽的「可以/提供/无法」：会把正常追问句误杀成核实话术。
_PRODUCT_FACT_ASSERTION_PATTERN = re.compile(
    r"(?:我们|我方|本系统|系统|本产品|产品|当前版本|标准版)"
    r".{0,12}?"
    r"(?:"
    r"暂未集成|尚未集成|未集成|没有集成|"
    r"暂未实现|尚未实现|未实现|"
    r"没有.{0,6}(?:功能|能力)|"
    r"不支持|支持|不具备|具备|"
    r"不提供(?:该|此|这项)?(?:功能|能力)?|"
    r"无法(?:提供|实现|支持)|不能(?:提供|实现|支持)"
    r")"
)


def _is_verification_only_script(script: str) -> bool:
    """是否整句都在核实/追问能力，而非先断言再附一句「是否」。

    ⚠️ 旧实现对整段搜「是否」：会放过
    「目前系统暂未集成区块链……您是否需要评估」这类半断言。
    """
    text = str(script or "").strip()
    if not text:
        return False
    if re.search(
        r"(?:需要确认|需要核实|我(?:再)?核实|待评估|需要评估|需评估|"
        r"不确定|有没有|有无)",
        text,
    ):
        # 若同时出现强断言动词，仍按断言处理
        if _PRODUCT_FACT_ASSERTION_PATTERN.search(text):
            return False
        return True
    # 以澄清问句为主：前面没有「支持/不支持/未集成」等断言
    if re.search(r"(?:是否|能否).{0,12}(?:支持|具备|提供)", text):
        return not re.search(
            r"(?:支持|不支持|暂未集成|未集成|不具备).{0,20}(?:是否|能否)",
            text,
        )
    return False
# 产品能力断言里的停用词：剩下的中文词 / 英文词当作“领域词”，
# 必须至少有一个出现在 evidence 原文里，否则 grounded 也要降级。
_CLAIM_STOPWORDS = {
    "我们", "我方", "本系统", "系统", "本产品", "产品", "当前版本", "标准版",
    "支持", "不支持", "具备", "不具备", "提供", "不提供", "可以", "不能", "无法",
    "已经", "实现", "集成", "功能", "能力", "记录", "全部", "这个", "一下",
    "需要", "确认", "核实", "方便", "说明", "具体", "场景", "要求", "您", "吗",
}


# 能力断言里优先保留的业务词（比盲目 2-gram 干净）
_DOMAIN_LEXICON = (
    "标准版", "三级", "审批", "审批流", "节点", "并签", "或签", "条件分支",
    "定制", "接口", "事件", "订单", "状态", "推送", "回调", "报表", "看板",
    "私有化", "实施", "联调", "报价", "人天", "存证", "上链", "审计", "授权",
    "匿名", "案例", "行业", "Webhook", "REST", "API", "SSO", "OAuth",
)


def _claim_domain_terms(script: str) -> list[str]:
    """从话术中抽出应在证据里出现的领域词。

    优先命中业务词典；再补英文 token 与关键 2～3 字中文片断。
    """
    text = script or ""
    out: list[str] = []
    lower = text.lower()
    for word in _DOMAIN_LEXICON:
        if word.isascii():
            if word.lower() in lower and word not in out:
                out.append(word)
        elif word in text and word not in out:
            out.append(word)
    for eng in re.findall(r"[A-Za-z][A-Za-z0-9_-]{1,}", text):
        if eng not in out:
            out.append(eng)
    # 补充：按标点切开后的 2～6 字短语
    for seg in re.split(r"[，。！？、；：,\s]+", text):
        seg = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", seg)
        if 2 <= len(seg) <= 6 and seg not in _CLAIM_STOPWORDS and seg not in out:
            # 去掉嵌入的支持/不支持字面
            cleaned = re.sub(r"(不)?支持|(不)?具备", "", seg)
            if len(cleaned) >= 2 and cleaned not in out:
                out.append(cleaned)
            elif seg not in out:
                out.append(seg)
    return out[:16]


def _claim_supported_by_evidence(script: str, evidence_blob: str) -> bool:
    """能力断言是否被 evidence 原文撑住。

    ⚠️ 仅“quote 存在于候选片段”不够：模型可能引用审批/接口相关原文，
       却在 script 里断言“支持区块链”。领域词必须在证据里出现。
    """
    blob = _norm_evidence(evidence_blob)
    if not blob:
        return False
    # 极性：单侧断言时核对；「支持 X、不支持 Y」混合句只看出域词重叠
    script_pos = bool(re.search(r"(?<!不)支持|(?<!不)具备", script or ""))
    script_neg = bool(
        re.search(r"不支持|不具备|暂未集成|尚未集成|没有.{0,6}(?:功能|能力)", script or "")
    )
    evidence_neg = bool(re.search(r"不支持|不具备|暂未|尚未", blob))
    evidence_pos = bool(re.search(r"(?<!不)支持|(?<!不)具备", blob))
    if not (script_pos and script_neg):
        if script_pos and evidence_neg and not evidence_pos:
            return False
        if script_neg and not evidence_neg:
            return False
    domain = _claim_domain_terms(script)
    if not domain:
        return bool(re.search(r"支持|不支持|具备|提供|接口|webhook|api", blob, re.I))
    hits = sum(1 for term in domain if _norm_evidence(term) in blob)
    return hits >= 2


def _pick_quote_from_text(text: str, domain: list[str]) -> str | None:
    """从候选片段中挑一句可核对的短原文。"""
    parts = re.split(r"(?<=[。！？；\n])", str(text or ""))
    for part in parts:
        quote = part.strip().strip("-•* ")
        normalized = _norm_evidence(quote)
        if not (8 <= len(normalized) <= 160):
            continue
        if any(_norm_evidence(term) in normalized for term in domain):
            return quote
    # 退路：整段里截出包含领域词的窗口
    norm_full = _norm_evidence(text)
    for term in domain:
        t = _norm_evidence(term)
        idx = norm_full.find(t)
        if idx < 0:
            continue
        start = max(0, idx - 20)
        end = min(len(str(text or "")), idx + len(term) + 40)
        window = str(text or "")[start:end].strip()
        if 8 <= len(_norm_evidence(window)) <= 160:
            return window
    return None


def _try_recover_grounded(script: str, hit_texts: dict, available: dict):
    """模型忘了写 evidence 或 quote 改写时，从候选片段自动回填可核原文。

    仅当 script 的领域词确实出现在某个 hit 正文里才恢复，避免无依据硬贴。
    """
    domain = _claim_domain_terms(script)
    for token in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_-]{1,}", script or ""):
        if token in _CLAIM_STOPWORDS or token in domain:
            continue
        domain.append(token)
    domain = domain[:12]
    if not domain:
        return None
    ranked = []
    for source_key, texts in hit_texts.items():
        source_name = available.get(source_key)
        if not source_name:
            continue
        for text in texts:
            score = sum(
                1 for term in domain if _norm_evidence(term) in _norm_evidence(text)
            )
            # 至少两个领域词重合，避免「系统/功能」等泛词误贴无关片段
            if score < 2:
                continue
            quote = _pick_quote_from_text(text, domain)
            if not quote:
                continue
            if not _claim_supported_by_evidence(script, quote):
                continue
            ranked.append((score, source_name, quote))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (-item[0], -len(item[2])))
    _, source_name, quote = ranked[0]
    evidence = [{"source": source_name, "quote": quote}]
    return evidence, [source_name]


def _norm_ref(name):
    """规范化文档名以便比对。

    ⚠️ 模型常给引用加装饰：《产品功能清单.md》、"产品功能清单.md"、
    产品功能清单 等。若按原样精确匹配，会把【真实存在的引用】误判为编造，
    进而错误降级有依据的建议 —— POC 中实测出现过这个误伤。
    """
    if not name:
        return ""
    s = str(name).strip()
    for ch in "《》〈〉「」『』\"'“”‘’ ":
        s = s.replace(ch, "")
    return s.lower().removesuffix(".md").removesuffix(".txt")


def _norm_evidence(value):
    """只忽略 Markdown 装饰与空白，保留原句里的语义和标点。"""
    text = str(value or "").strip()
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_`~#>]", "", text)
    text = re.sub(r"\s+", "", text)
    return text.strip("“”‘’\"'《》")


def _norm_num(text):
    """去掉数字表述中的空格，便于比对（"10 - 15 人天" → "10-15人天"）"""
    return re.sub(r"\s+", "", text or "")


# 成本类数值的【形态】—— 人天/金额/折扣/百分比
_COST_SHAPE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:-\s*\d+(?:\.\d+)?\s*)?(?:人天|万元|元|％|%|折)")


def _scan_cost_numbers(script, known):
    """扫描话术中的成本类数字，区分【知识库原文】与【模型自创】。

    ⚠️ 为什么不能只做精确匹配：模型会重组数字。实测知识库写的是
    "10-15人天""20-30人天"，模型输出了"15-28人天" —— 精确匹配全部落空，
    而这个数字既泄露了成本量级、本身又是编造的，比原样引用更危险。
    因此改为按【形态】识别：只要出现人天/金额/折扣，一律提示；
    并标出哪些在知识库中查无实据。
    """
    found = {_norm_num(m.group()) for m in _COST_SHAPE.finditer(script or "")}
    if not found:
        return [], []
    known = {_norm_num(k) for k in (known or set())}
    verbatim = sorted(n for n in found if n in known)
    invented = sorted(n for n in found if n not in known)
    return verbatim, invented


def _compact_script(text, max_chars=SUGGESTION_SCRIPT_MAX_CHARS):
    """把极端长话术压到可在会中扫读的长度。

    先尝试在完整句/分句处收口；只有模型输出连标点都没有时才硬截断。
    返回 (结果, 是否缩短, 原始长度)。连续空白会先折叠成一个普通空格。
    """
    # 换行压成普通空格，但不能把英文词间空格一起删掉。
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    original_length = len(normalized)
    if original_length <= max_chars:
        return normalized, False, original_length

    # 给省略号留一个字符。优先完整句，其次分句，避免把数字或词切成两半。
    budget = max_chars - 1
    minimum_useful = min(28, max(1, budget // 2))
    cut = 0
    for punctuation in ("。！？", "；，"):
        positions = [
            index + 1
            for index, char in enumerate(normalized[:budget])
            if char in punctuation and index + 1 >= minimum_useful
        ]
        if positions:
            cut = positions[-1]
            break
    if not cut:
        cut = budget
    compacted = normalized[:cut].rstrip("，、；：,. ")
    return f"{compacted}…", True, original_length


def _apply_length_limits(suggestions, max_chars=SUGGESTION_SCRIPT_MAX_CHARS):
    """对模型输出做确定性的最终长度兜底，并保留诊断元数据。"""
    out = []
    for item in suggestions:
        suggestion = dict(item)
        if _contains_spoken_placeholder(suggestion.get("script")):
            suggestion["_downgraded"] = "话术含未填占位符，已改为安全核实问句"
            suggestion["type"] = "clarify"
            suggestion["references"] = []
            suggestion["evidence"] = []
            suggestion["script"] = SAFE_CLARIFY_SCRIPT
        compacted, shortened, original_length = _compact_script(
            suggestion.get("script", ""), max_chars=max_chars
        )
        suggestion["script"] = compacted
        suggestion["_script_length"] = len(compacted)
        if shortened:
            suggestion["_shortened"] = True
            suggestion["_original_script_length"] = original_length
        out.append(suggestion)
    return out


def _validate(suggestions, hits, forbidden=None, internal_nums=None):
    """程序化校验模型的自评标记。

    ⚠️ 为什么需要：POC 实测证明模型的 type 自评【不可信】——
    「区块链存证」问题下，模型把三条全标成 grounded，其中一条还编造了
    "操作日志留痕功能"（知识库中不存在）。模型对自己有没有依据缺乏可靠判断。

    本函数既核对引用文档，也核对 evidence.quote 是否逐字存在于本次候选片段。
    grounded 未通过原文校验时，不再把原话术交给用户，而是替换成安全核实问句。
    """
    # 按规范化后的名字建索引，同时保留原始文件名用于展示
    available = {_norm_ref(h["source"]): h["source"] for h in hits}
    hit_texts = {}
    for hit in hits:
        key = _norm_ref(hit.get("source"))
        if key:
            hit_texts.setdefault(key, []).append(str(hit.get("text") or ""))
    out = []
    for s in suggestions:
        s = dict(s)
        stype = s.get("type") or ("grounded" if s.get("grounded", True) else "clarify")
        # 把模型给的引用统一还原为真实文件名（去掉书名号等装饰）
        refs = []
        for r in (s.get("references") or []):
            if not r:
                continue
            refs.append(available.get(_norm_ref(r), r))

        script = s.get("script") or ""

        # advisory 的定义是不含我方事实；一旦带引用，就按 grounded 做同等硬校验。
        if stype == "advisory" and refs:
            s["_reclassified"] = "原标为经验建议，因引用了知识库，改按有依据处理"
            stype = "grounded"

        verified_evidence = []
        if stype == "grounded":
            raw_evidence = s.get("evidence") or []
            if not isinstance(raw_evidence, list):
                raw_evidence = [raw_evidence]
            for item in raw_evidence:
                if isinstance(item, dict):
                    quote = str(item.get("quote") or "").strip()
                    source_hint = _norm_ref(item.get("source"))
                else:
                    # 兼容少数模型只返回 quote 字符串的情况。
                    quote = str(item or "").strip()
                    source_hint = ""
                normalized_quote = _norm_evidence(quote)
                if len(normalized_quote) < 8 or len(normalized_quote) > 160:
                    continue
                candidate_sources = (
                    [source_hint] if source_hint in hit_texts else list(hit_texts)
                )
                matched_source = ""
                for source_key in candidate_sources:
                    if any(
                        normalized_quote in _norm_evidence(text)
                        for text in hit_texts[source_key]
                    ):
                        matched_source = available[source_key]
                        break
                if not matched_source:
                    continue
                evidence = {"source": matched_source, "quote": quote}
                if evidence not in verified_evidence:
                    verified_evidence.append(evidence)
                if len(verified_evidence) >= 2:
                    break

            if verified_evidence:
                s["evidence"] = verified_evidence
                # 来源以通过校验的原文为准，不再信任模型单独填写的文件名。
                refs = list(dict.fromkeys(item["source"] for item in verified_evidence))
                # 无明确产品事实、证据也只是弱相关 → 降为 advisory，避免无依据场景冒充 grounded
                blob = "".join(item["quote"] for item in verified_evidence)
                if (
                    not _PRODUCT_FACT_ASSERTION_PATTERN.search(script)
                    and not _claim_supported_by_evidence(script, blob)
                ):
                    s["_reclassified"] = "无明确产品事实或证据弱相关，改按经验建议"
                    stype = "advisory"
                    s["evidence"] = []
                    refs = []
                    verified_evidence = []
            else:
                recovered = _try_recover_grounded(script, hit_texts, available)
                if recovered:
                    s["evidence"], refs = recovered
                    s["_recovered_evidence"] = True
                    verified_evidence = s["evidence"]
                else:
                    s["_original_script"] = script
                    s["_downgraded"] = "有依据建议未提供可核对的原文，已改为核实话术"
                    s["intent"] = "依据未通过原文核验，先确认需求"
                    script = "这个能力点我需要结合资料再核实一下，您方便先说明具体场景和验收要求吗？"
                    s["script"] = script
                    s["evidence"] = []
                    refs = []
                    stype = "clarify"
        else:
            s["evidence"] = []
            refs = []

        # 能力断言（支持/不支持）一律核对：advisory/clarify 直接拦；
        # grounded 也必须让 evidence 原文覆盖领域词，否则仍降级。
        # 带“核实是否”等验证语境的问句不拦截。
        if (
            _PRODUCT_FACT_ASSERTION_PATTERN.search(script)
            and not _is_verification_only_script(script)
        ):
            evidence_blob = "".join(
                str(item.get("quote") or "") for item in (s.get("evidence") or [])
            )
            allowed = (
                stype == "grounded"
                and bool(s.get("evidence"))
                and _claim_supported_by_evidence(script, evidence_blob)
            )
            if not allowed and stype != "grounded":
                # 非 grounded 的能力断言：若候选片段其实写了，升级为 recovered grounded
                recovered = _try_recover_grounded(script, hit_texts, available)
                if recovered and _claim_supported_by_evidence(
                    script, recovered[0][0]["quote"]
                ):
                    s["evidence"], refs = recovered
                    s["_recovered_evidence"] = True
                    stype = "grounded"
                    allowed = True
            if not allowed:
                s["_original_script"] = s.get("_original_script") or script
                s["_downgraded"] = "无依据地断言产品支持或不支持某能力，已改为核实话术"
                s["intent"] = "产品能力尚无原文依据，先确认需求"
                script = "这个能力点我需要结合资料再核实一下，您方便先说明具体场景和验收要求吗？"
                s["script"] = script
                s["evidence"] = []
                refs = []
                stype = "clarify"

        # 【最高优先级】禁提名称扫描：知识库中标注"不可对外提及"的实体
        # 一旦出现在对客话术里，就是泄露其它客户信息的商业事故。
        # POC 实测：X2-Flash 把标注禁提的"西南零售连锁"直接写进了话术。
        # 内部信息【标注而非拦截】。
        #
        # 设计取舍：这是个人工具，知识库是用户自己的，内容他清楚；且每条话术
        # 都要经他判断才出口 —— 是提词器不是自动驾驶。用户完全可能【故意】
        # 想报个工作量区间来锚定预期，那是正当策略，不该被系统剥夺。
        # 因此这里只做提醒：把敏感内容点出来，让他一眼看见、自己决定。
        # （对比：编造类问题必须严管 —— 那个用户没法在两秒内核对。）
        marks = []
        if forbidden:
            hit = sorted(t for t in forbidden if t and t in script)
            if hit:
                marks.append(f"其他客户名称：{'、'.join(hit)}")
        verbatim, invented = _scan_cost_numbers(script, internal_nums)
        if verbatim:
            marks.append(f"内部成本数据：{'、'.join(verbatim)}")
        if invented:
            # 知识库里查不到的成本数字 —— 大概率是模型自己算的/编的，风险更高
            marks.append(f"⚠知识库中查无此数：{'、'.join(invented)}")
        if marks:
            s["_sensitive"] = "；".join(marks)

        # 会议中最容易被客户当作承诺的两类表达：我方将交付某物、明确工作日。
        # 这类事实即使模型挂了一个真实文档引用，也可能是它自行拼出来的
        # （实测凭空生成过“3 个工作日内出方案”）。先标注提醒，由用户决定。
        delivery_commitments = sorted(
            {m.group() for m in _DELIVERY_COMMIT_PATTERN.finditer(script)}
        )
        if delivery_commitments:
            s["_sensitive"] = "；".join(filter(None, [
                s.get("_sensitive"),
                f"含时间/交付承诺：{'、'.join(delivery_commitments)}",
            ]))

        # 承诺性表述扫描：advisory 档不允许出现对"我方将做到什么"的保证。
        # POC 实测中模型曾把"出具方案，确保满足审计合规"标为 advisory ——
        # 这是承诺而非策略，一旦客户当真就是交付风险。提示词已禁止，此处兜底。
        if stype == "advisory":
            hit_word = next((w for w in _COMMIT_WORDS if w in script), None)
            if not hit_word:
                guarantee = _GUARANTEE_COMMIT_PATTERN.search(script)
                hit_word = guarantee.group() if guarantee else None
            if hit_word:
                # 同样改为提醒：要不要作这个承诺是用户的商务判断，不是系统的
                s["_sensitive"] = "；".join(filter(None, [
                    s.get("_sensitive"), f"含承诺性表述「{hit_word}」"]))

        # 引用了本次检索没返回的文档 → 模型在凭记忆编造出处
        bogus = [r for r in refs if _norm_ref(r) not in available]
        if bogus:
            s["_downgraded"] = f"引用了未检索到的文档：{'、'.join(bogus)}"
            refs = [r for r in refs if _norm_ref(r) in available]
            stype = "clarify" if not refs else stype

        # 标称有依据却给不出任何来源 → 自相矛盾，降级
        if stype == "grounded" and not refs:
            s["_downgraded"] = s.get("_downgraded") or "标称有依据但未给出引用来源"
            stype = "clarify"

        # 本次压根没检索到片段，却声称有依据 → 降级
        if stype == "grounded" and not hits:
            s["_downgraded"] = "本次检索无任何片段，不可能有依据"
            stype = "clarify"
            refs = []

        if _contains_spoken_placeholder(script):
            s["_downgraded"] = "话术含未填占位符，已改为安全核实问句"
            s["intent"] = "先确认边界"
            script = SAFE_CLARIFY_SCRIPT
            s["script"] = script
            s["evidence"] = []
            refs = []
            stype = "clarify"

        s["type"] = stype
        s["references"] = refs
        out.append(s)
    return out


def _postprocess_suggestions(suggestions, hits, query):
    """校验后的确定性补救：抑制无原文 grounded，并在有原文时补一条可引用建议。"""
    query = str(query or "")
    hit_blob = "\n".join(str(h.get("text") or "") for h in hits)
    available = {_norm_ref(h["source"]): h["source"] for h in hits}
    hit_texts = {}
    for hit in hits:
        key = _norm_ref(hit.get("source"))
        if key:
            hit_texts.setdefault(key, []).append(str(hit.get("text") or ""))

    # 1) 客户问的冷门能力若知识库根本没写，禁止保留 grounded
    rare_terms = ("区块链", "上链", "存证", "NFT", "元宇宙")
    for term in rare_terms:
        if term in query and term not in hit_blob:
            for item in suggestions:
                if item.get("type") == "grounded":
                    item["type"] = "advisory"
                    item["references"] = []
                    item["evidence"] = []
                    item["_reclassified"] = (
                        f"知识库无「{term}」原文，取消有依据标记"
                    )

    # 2) 已有可核片段但模型全是 clarify/advisory → 补一条 grounded
    #    若问题点名知识库没有的能力（区块链等），禁止注入，否则会假 grounded
    missing_rare = any(
        term in query and term not in hit_blob for term in rare_terms
    )
    has_grounded = any(
        item.get("type") == "grounded" and item.get("evidence")
        for item in suggestions
    )
    if hits and not has_grounded and not missing_rare:
        recovered = _try_recover_grounded(query, hit_texts, available)
        # 对「审批/接口」类问题，用命中片段造一句保守复述
        if not recovered:
            preferred_keys = (
                "条件分支",
                "定制开发",
                "不支持",
                "三级",
                "REST",
                "Webhook",
                "接口",
            )
            ranked_hits = sorted(
                hits,
                key=lambda h: sum(
                    1 for key in preferred_keys if key in str(h.get("text") or "")
                ),
                reverse=True,
            )
            for hit in ranked_hits:
                text = str(hit.get("text") or "")
                if not any(key in text for key in preferred_keys):
                    continue
                domain = _claim_domain_terms(query + " " + text[:120])
                # 优先抽取含边界词的句子
                for prefer in ("条件分支", "定制", "不支持", "Webhook", "REST", "三级"):
                    if prefer in text:
                        domain = [prefer] + [d for d in domain if d != prefer]
                        break
                quote = _pick_quote_from_text(
                    text, domain or ["审批", "接口", "定制", "分支"]
                )
                if quote and 8 <= len(_norm_evidence(quote)) <= 160:
                    recovered = (
                        [{"source": hit["source"], "quote": quote}],
                        [hit["source"]],
                    )
                    break
        if recovered:
            evidence, refs = recovered
            quote = evidence[0]["quote"]
            spoken = re.sub(r"\*+", "", quote).strip()
            spoken = re.sub(r"\s+", " ", spoken)
            if len(spoken) > 48:
                spoken = spoken[:47] + "…"
            # 口语复述，尽量保留「不支持/定制/分支」等边界词
            script = f"资料里写的是：{spoken}"
            if len(script) > 80:
                script = script[:79] + "…"
            injected = {
                "intent": "用资料说明边界",
                "script": script,
                "type": "grounded",
                "references": refs,
                "evidence": evidence,
                "_injected_grounded": True,
            }
            # 放到最前，并最多保留另外 2 条
            rest = [item for item in suggestions if item.get("script")][:2]
            suggestions = [injected] + rest

    return suggestions


class SuggestionEngine:
    """话术建议引擎。

    ⚠️ 供应商与语音识别【完全解耦】：ASR 用哪家、LLM 用哪家互不影响，
    可以讯飞 ASR + 阿里云 LLM，也可以讯飞 ASR + 讯飞星火，任意组合。
    """

    def __init__(
        self,
        kb,
        me_name="我",
        provider="xfyun",
        api_key=None,
        model=None,
        base_url=None,
        scene="general",
        tone="direct",
        custom_tone_prompt="",
        timeout_seconds=DEFAULT_LLM_TIMEOUT_SECONDS,
        retry_attempts=DEFAULT_LLM_RETRY_ATTEMPTS,
    ):
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ImportError("请先安装：pip install openai") from e
        if provider not in PROVIDERS:
            raise ValueError(f"未知 LLM 供应商: {provider}（可选 {list(PROVIDERS)}）")
        cfg = PROVIDERS[provider]
        if not api_key:
            raise ValueError(f"缺少 {cfg['label']} 的密钥")

        url = base_url or cfg["base_url"]
        self.model = model or cfg["model"]
        if not url:
            raise ValueError("自定义服务需在 config.py 提供 CUSTOM_LLM_BASE_URL")
        if not self.model:
            raise ValueError("自定义服务需在 config.py 提供 CUSTOM_LLM_MODEL")

        # 个别厂商（如 MiMo）文档用非标准鉴权头，一并带上以兼容
        headers = {}
        if cfg.get("auth_header"):
            headers[cfg["auth_header"]] = api_key

        self.provider = provider
        self.label = cfg["label"]
        self.base_url = url
        self.scene = normalize_scene(scene)
        self.scene_label = scene_config(self.scene)["label"]
        self.tone = normalize_tone(tone)
        self.tone_label = tone_config(self.tone)["label"]
        self.custom_tone_prompt = str(custom_tone_prompt or "").strip()
        try:
            self.timeout_seconds = max(1.0, float(timeout_seconds))
        except (TypeError, ValueError):
            self.timeout_seconds = DEFAULT_LLM_TIMEOUT_SECONDS
        try:
            self.retry_attempts = max(1, int(retry_attempts))
        except (TypeError, ValueError):
            self.retry_attempts = DEFAULT_LLM_RETRY_ATTEMPTS
        self.retry_backoff_seconds = DEFAULT_LLM_RETRY_BACKOFF_SECONDS
        # 禁用 SDK 自动重试，由本层只对明确的瞬时错误重试，避免鉴权/模型错误
        # 被无意义地重复请求。
        self._client = OpenAI(
            api_key=api_key,
            base_url=url,
            default_headers=headers or None,
            max_retries=0,
        )
        self.kb = kb
        self.me_name = me_name

    def _run_with_retry(self, operation):
        """运行一次 LLM 操作；瞬时错误按配置做有限重试。"""
        last_error = None
        for attempt in range(1, self.retry_attempts + 1):
            try:
                return operation()
            except Exception as error:  # noqa: BLE001 - 统一转换为诊断错误
                last_error = error
                _kind, retryable, _stage = classify_llm_error(error)
                if not retryable or attempt >= self.retry_attempts:
                    raise LLMRequestError(
                        error,
                        attempts=attempt,
                        timeout_seconds=self.timeout_seconds,
                    ) from error
                time.sleep(self.retry_backoff_seconds * attempt)
        raise LLMRequestError(
            last_error or RuntimeError("LLM 请求失败"),
            attempts=self.retry_attempts,
            timeout_seconds=self.timeout_seconds,
        )

    def _call_once(self, system, user):
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=0.3,   # 降低随机性，减少事实性内容的漂移
            timeout=self.timeout_seconds,
        )
        return resp.choices[0].message.content or ""

    def _call(self, system, user):
        return self._run_with_retry(lambda: self._call_once(system, user))

    def _call_stream(self, system, user, on_delta):
        """流式生成，每收到一段文本就回调 on_delta（ASK-3：首字尽快出）。

        ⚠️ 提问回答适合流式：它是自由文本，先出字能大幅改善"开会中干等"的
        体验。建议卡片是结构化 JSON，逐 token 出反而会露出半截 JSON，
        因此建议仍走整体返回，只有问答走这里。
        """
        full = []
        stream = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=0.3,
            stream=True,
            timeout=self.timeout_seconds,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            piece = chunk.choices[0].delta.content or ""
            if piece:
                full.append(piece)
                on_delta(piece)
        return "".join(full)

    def suggest(self, transcript, top_k=4, count=3):
        """对方发言结束 → 生成 count 条话术建议

        transcript: [{speaker, text}, ...] 最近若干轮对话
        count     : 设置页「每次条数」，桥接层透传下来
        返回: {"suggestions": [...], "hits": [...]}
        """
        # 用对方最后一段发言作为检索 query（最能代表当前话题）
        last_other = next(
            (s["text"] for s in reversed(transcript)
             if s["speaker"] != self.me_name), "")
        query = last_other or transcript[-1]["text"]
        hits = self.kb.search(query, top_k=top_k)

        user = f"""{_REF_PREAMBLE}

{_format_refs(hits)}

【当前会议对话】
{_format_context(transcript, self.me_name)}

对方刚说完上面最后一段话，请给「{self.me_name}」{count} 条应对建议。"""

        system = build_suggestion_system_prompt(
            me_name=self.me_name,
            scene=self.scene,
            tone=self.tone,
            custom_tone_prompt=self.custom_tone_prompt,
            count=count,
            target_chars=SUGGESTION_SCRIPT_TARGET_CHARS,
            max_chars=SUGGESTION_SCRIPT_MAX_CHARS,
        )
        try:
            raw = self._call(system, user)
        except Exception as first_error:
            error = llm_error_details(
                first_error,
                provider=self.provider,
                model=self.model,
                timeout_seconds=self.timeout_seconds,
                stage="suggestion",
            )
            return {
                "suggestions": [
                    {
                        "intent": "先确认边界",
                        "script": SAFE_CLARIFY_SCRIPT,
                        "type": "clarify",
                        "references": [],
                        "evidence": [],
                    }
                ],
                "hits": hits,
                "error": {
                    "message": (
                        f"建议服务暂时不可用：{format_llm_error(error, '建议服务')}；"
                        "已返回核实话术。"
                    ),
                    **error,
                },
            }
        try:
            data = _extract_json(raw)
        except Exception as first_error:
            # 不再自动二次请求：一次解析失败就返回安全澄清卡，让用户自行重试。
            return {
                "suggestions": [
                    {
                        "intent": "先确认边界",
                        "script": SAFE_CLARIFY_SCRIPT,
                        "type": "clarify",
                        "category": "澄清",
                        "references": [],
                        "evidence": [],
                    }
                ],
                "hits": hits,
                "error": {
                    "message": f"模型输出不是合法 JSON：{first_error}",
                    "retryable": True,
                    "raw": (raw or "")[:2000],
                },
            }
        data["suggestions"] = _validate(
            data.get("suggestions", []),
            hits,
            getattr(self.kb, "forbidden_terms", None),
            getattr(self.kb, "internal_numbers", None),
        )
        data["suggestions"] = _postprocess_suggestions(
            data["suggestions"], hits, query
        )
        data["suggestions"] = _apply_length_limits(data["suggestions"])
        data["hits"] = hits
        return data

    def answer(self, question, transcript, top_k=4, on_delta=None):
        """我主动提问 → 结合会议上下文 + 知识库回答。

        on_delta 非空时走流式，每段文本回调一次（ASK-3 首字尽快出）。
        """
        hits = self.kb.search(question, top_k=top_k)
        user = f"""【知识库片段】
{_format_refs(hits)}

【当前会议对话】
{_format_context(transcript, self.me_name)}

【{self.me_name} 的提问】{question}"""
        system = build_answer_system_prompt(
            me_name=self.me_name,
            tone=self.tone,
            custom_tone_prompt=self.custom_tone_prompt,
        )
        if on_delta is not None:
            text = self._call_stream(system, user, on_delta)
        else:
            text = self._call(system, user)
        return {"answer": text, "hits": hits}
