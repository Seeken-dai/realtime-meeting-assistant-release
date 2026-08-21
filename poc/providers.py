"""
服务工厂 —— 把「语音识别」「建议模型」「知识库检索」三类服务解耦。

设计原则：三者独立配置、独立故障。任一家欠费或宕机，只影响它那一环。
调用方（run_poc / run_suggest / run_live）只管要服务，不关心背后是哪家。

兼容说明：为不破坏已填好的旧版 config.py，读取时对旧字段名做了回退。
优先级：环境变量（桌面端设置页注入）> config.py > 默认值。
"""

import os
import sys
import types

try:
    import config
except ImportError:
    # 桌面端可把密钥存在 userData，不再强制要求 config.py
    config = types.SimpleNamespace(SAMPLE_RATE=16000)

# 记录各服务实际使用的密钥来源变量，供诊断输出（不打印明文）
LAST_KEY_SOURCE = {}


def _read_value(key):
    """单键读取：环境变量优先（Electron 设置页写入后注入），再读 config.py。"""
    env = os.environ.get(key)
    if env is not None and str(env).strip() != "":
        return str(env).strip()
    val = getattr(config, key, None)
    if val is not None and str(val).strip() != "":
        return val
    return None


def _cfg(name, *fallbacks, default=None):
    """读配置，支持旧字段名回退"""
    for key in (name,) + fallbacks:
        val = _read_value(key)
        if val:
            return val
    return default


def _cfg_traced(name, *fallbacks):
    """读配置并返回来源变量名。

    ⚠️ 密钥回退必须【可见】：静默回退到别的变量会让人误以为自己填的密钥在生效，
    实际用的是另一把 —— POC 中就因此多花了一轮排查。
    """
    for key in (name,) + fallbacks:
        val = _read_value(key)
        if val:
            return val, key
    return None, None


def mask(key):
    """密钥打码预览，用于确认"到底用了哪一把"而不泄露完整值"""
    if not key:
        return "(空)"
    k = str(key)
    return f"{k[:4]}…{k[-4:]}（{len(k)}位）" if len(k) > 10 else "****"


# ══════════════ ① 语音识别 ══════════════
# 用户侧统一三种：zh / en / zh_en。各供应商参数名和取值不同，见 _asr_lang_params。
ASR_LANG_CHOICES = ("zh", "en", "zh_en")


def normalize_asr_lang(lang=None):
    """把 UI / 配置 / 别名统一成 zh | en | zh_en。空则读 ASR_LANG，默认 zh_en。"""
    raw = (lang if lang is not None else _cfg("ASR_LANG", default="zh_en")) or "zh_en"
    raw = str(raw).strip().lower().replace("-", "_")
    aliases = {
        "zh": "zh", "cn": "zh", "chinese": "zh", "中文": "zh",
        "en": "en", "english": "en", "英文": "en",
        "zh_en": "zh_en", "zh_cn_en": "zh_en", "cn_en": "zh_en",
        "mixed": "zh_en", "mix": "zh_en", "中英": "zh_en", "中英混用": "zh_en",
        "中英混合": "zh_en",
    }
    return aliases.get(raw, "zh_en")


def _asr_lang_params(provider, lang):
    """按供应商把统一语种映射成适配器 kwargs。

    - 阿里 paraformer-realtime-v2：language_hints=['zh'|'en'|…]
      不设会自动识别语种，实测易串出日文等；评审会场景应显式限定。
    - 讯飞 RTASR：lang=cn（中文+中英混合）/ en
    - 讯飞大模型：lang 默认 autodialect 过宽；中文用 autodialect、英文 en、
      中英混用仍用 autodialect（其「免切」能力覆盖中英，比 autominor 更窄）。
    - 火山豆包流式：request.language = zh-CN / en-US；空=中英+方言（作中英混用）
    - 腾讯实时：engine_model_type = 16k_zh / 16k_en / 16k_zh_en
    - 小米 MiMo：asr_options.language = zh / en / auto（仅 auto 支持混说）
    """
    mode = normalize_asr_lang(lang)
    if provider == "aliyun":
        hints = {"zh": ["zh"], "en": ["en"], "zh_en": ["zh", "en"]}[mode]
        return {"language_hints": hints}
    if provider == "xfyun":
        # 官方：cn = 中文 / 中英混合；en = 英文。无「纯中文不含英」档。
        return {"lang": "en" if mode == "en" else "cn"}
    if provider == "xfyun-llm":
        return {"lang": "en" if mode == "en" else "autodialect"}
    if provider == "volcano":
        # 空字符串 = 不传 language 键，走中英+方言默认
        return {
            "language": {"zh": "zh-CN", "en": "en-US", "zh_en": ""}[mode],
        }
    if provider == "tencent":
        return {
            "engine_model_type": {
                "zh": "16k_zh",
                "en": "16k_en",
                "zh_en": "16k_zh_en",
            }[mode],
        }
    if provider == "mimo":
        # 文档：zh/en 为单语种；中英混说用 auto
        return {
            "language": {"zh": "zh", "en": "en", "zh_en": "auto"}[mode],
        }
    return {}


def build_asr(provider=None, debug=False, model=None, lang=None, vocabulary_id=None):
    provider = provider or _cfg("ASR_PROVIDER", default="xfyun")
    lang_kw = _asr_lang_params(provider, lang)
    # vocabulary_id 仅阿里链路消费；其它供应商忽略（词库仍可维护，见 UI 提示）

    if provider == "xfyun":
        from asr_xfyun import XfyunASR
        app_id = _cfg("XFYUN_APP_ID")
        api_key = _cfg("XFYUN_API_KEY")
        if not (app_id and api_key):
            sys.exit("请在设置页或 config.py 填入 XFYUN_APP_ID / XFYUN_API_KEY\n"
                     "（须为「实时语音转写」服务下的 Key）")
        return XfyunASR(app_id=app_id, api_key=api_key, debug=debug, **lang_kw)

    if provider == "xfyun-llm":
        from asr_xfyun_llm import XfyunLlmASR
        kid = _cfg("XFYUN_LLM_ASR_KEY_ID")
        ksecret = _cfg("XFYUN_LLM_ASR_KEY_SECRET")
        # appId 是开放平台应用 ID，与 accessKeyId 不是同一个
        # （37000 parameter is wrong 的常见原因就是把二者混用）
        app_id = _cfg("XFYUN_LLM_ASR_APP_ID", "XFYUN_APP_ID")
        if not (kid and ksecret):
            sys.exit("请在设置页或 config.py 填入 XFYUN_LLM_ASR_KEY_ID / "
                     "XFYUN_LLM_ASR_KEY_SECRET\n"
                     "（讯飞「实时语音转写大模型」服务的 accessKeyId/Secret，"
                     "与标准版 RTASR 的 APPID/APIKey 不同）")
        if not app_id:
            sys.exit("请在设置页或 config.py 填入应用 App ID\n"
                     "（XFYUN_LLM_ASR_APP_ID 或 XFYUN_APP_ID，"
                     "开放平台控制台「我的应用」里的 APPID）")
        return XfyunLlmASR(access_key_id=kid, access_key_secret=ksecret,
                           app_id=app_id, debug=debug, **lang_kw)

    if provider == "aliyun":
        from asr_aliyun import AliyunASR
        key = _cfg("ALIYUN_ASR_KEY", "ALIYUN_API_KEY")
        if not key:
            sys.exit("请在设置页或 config.py 填入 ALIYUN_ASR_KEY")
        sample_rate = int(_cfg("SAMPLE_RATE", default=16000) or 16000)
        kw = dict(api_key=key, sample_rate=sample_rate, debug=debug, **lang_kw)
        resolved_model = model or _cfg("ALIYUN_ASR_MODEL")
        if resolved_model:
            kw["model"] = resolved_model
        # 专有名词同步后的阿里 vocabulary_id（见 asr_hotwords.py）
        vocab = vocabulary_id or _cfg("ALIYUN_VOCABULARY_ID")
        if vocab:
            kw["vocabulary_id"] = vocab
        return AliyunASR(**kw)

    if provider == "volcano":
        from asr_volcano import VolcanoASR
        ak, sk = _cfg("VOLC_APP_KEY"), _cfg("VOLC_ACCESS_KEY")
        if not (ak and sk):
            sys.exit("请在设置页或 config.py 填入 VOLC_APP_KEY / VOLC_ACCESS_KEY")
        sample_rate = int(_cfg("SAMPLE_RATE", default=16000) or 16000)
        return VolcanoASR(app_key=ak, access_key=sk, sample_rate=sample_rate,
                          **lang_kw)

    if provider == "tencent":
        from asr_tencent import TencentASR
        aid = _cfg("TENCENT_APP_ID")
        sid, skey = _cfg("TENCENT_SECRET_ID"), _cfg("TENCENT_SECRET_KEY")
        if not (aid and sid and skey):
            sys.exit("请在设置页或 config.py 填入腾讯云 APP_ID / SECRET_ID / SECRET_KEY")
        return TencentASR(app_id=aid, secret_id=sid, secret_key=skey, **lang_kw)

    if provider == "mimo":
        from asr_mimo import MimoASR
        key = _cfg("MIMO_API_KEY")
        if not key:
            sys.exit("请在设置页或 config.py 填入 MIMO_API_KEY")
        print("⚠️ MiMo 为整段转写模型：分段准实时、无说话人分离")
        sample_rate = int(_cfg("SAMPLE_RATE", default=16000) or 16000)
        return MimoASR(api_key=key, sample_rate=sample_rate, **lang_kw)

    sys.exit(f"未知 ASR 供应商: {provider}")


# ══════════════ ② 建议模型 ══════════════
def build_llm(
    kb,
    me_name="我",
    provider=None,
    model=None,
    scene="general",
    tone=None,
    custom_tone_prompt=None,
    timeout_seconds=None,
    retry_attempts=None,
):
    from suggest import SuggestionEngine
    configured = _cfg("LLM_PROVIDER", default="xfyun")
    explicit = provider is not None and provider != configured
    provider = provider or configured
    tone = tone or _cfg("RESPONSE_TONE", default="direct")
    custom_tone_prompt = (
        custom_tone_prompt
        if custom_tone_prompt is not None
        else _cfg("CUSTOM_TONE_PROMPT", default="")
    )

    base_url = None
    # 模型名优先级：显式传入(UI 探测选中) > 显式切换供应商时用默认(None) >
    #   未切换时读 config.LLM_MODEL。
    # ⚠️ 跨家切换不能沿用上一家的 LLM_MODEL —— 各家模型名互不通用
    #    （实测：把 4.0Ultra 带到 X2 端点直接 400 invalid param model）。
    if model is None:
        model = None if explicit else _cfg("LLM_MODEL")

    if provider.startswith("xfyun"):
        # ⚠️ 讯飞【每个模型/服务的 APIPassword 是独立的】，不通用。
        #    实测：用经典系列的 APIPassword 调 X2 的 spark-x，
        #    返回 500 AppIdNoAuthError（模型名对，但该凭证无此模型权限）。
        if provider in ("xfyun-x2-flash", "xfyun-x2"):
            key, src = _cfg_traced("XFYUN_X2_PASSWORD", "XFYUN_SPARK_PASSWORD")
            hint_name, hint_var = "星火 X2 / X2-Flash", "XFYUN_X2_PASSWORD"
        elif provider == "xfyun-x1.5":
            key, src = _cfg_traced("XFYUN_X15_PASSWORD", "XFYUN_SPARK_PASSWORD")
            hint_name, hint_var = "星火 X1.5", "XFYUN_X15_PASSWORD"
        else:
            key, src = _cfg_traced("XFYUN_SPARK_PASSWORD")
            hint_name, hint_var = "星火经典系列", "XFYUN_SPARK_PASSWORD"
        # 回退发生时必须明说，否则用户以为自己填的密钥在生效
        if key and src != hint_var:
            print(f"⚠️ {hint_var} 未填写，已回退使用 {src} 的密钥。")
            print(f"   若该密钥无 {hint_name} 权限，会报 AppIdNoAuthError。")
        LAST_KEY_SOURCE["llm"] = (src, key)
        if not key:
            sys.exit(
                f"缺少讯飞{hint_name}的密钥。请在设置页填写 {hint_var}，或在 config.py 添加：\n"
                f'    {hint_var} = "该模型的APIPassword"\n\n'
                "获取方式：https://console.xfyun.cn/ → 对应模型服务 → APIPassword\n"
                "⚠️ 讯飞每个模型的 APIPassword 是独立的，不同模型不能混用；\n"
                "   也与 RTASR 的 APIKey 不同。")
    elif provider == "aliyun":
        key, src = _cfg_traced("ALIYUN_LLM_KEY", "ALIYUN_API_KEY")
        LAST_KEY_SOURCE["llm"] = (src, key)
        if not key:
            sys.exit("请在设置页或 config.py 填入 ALIYUN_LLM_KEY")
    elif provider == "mimo":
        key, src = _cfg_traced("MIMO_LLM_KEY", "MIMO_API_KEY")
        LAST_KEY_SOURCE["llm"] = (src, key)
        if not key:
            sys.exit("请在设置页或 config.py 填入 MIMO_LLM_KEY\n"
                     "获取方式：https://mimo.mi.com/ → API Key")
    elif provider in ("gemini", "zhipu", "deepseek", "moonshot", "grok"):
        # 快速档模型，统一用 <大写供应商名>_LLM_KEY 配置
        var = f"{provider.upper()}_LLM_KEY"
        key = _cfg(var)
        LAST_KEY_SOURCE["llm"] = (var, key)
        if not key:
            urls = {
                "gemini": "https://aistudio.google.com/apikey",
                "zhipu": "https://open.bigmodel.cn/usercenter/apikeys",
                "deepseek": "https://platform.deepseek.com/api_keys",
                "moonshot": "https://platform.moonshot.cn/console/api-keys",
                "grok": "https://console.x.ai/",
            }
            sys.exit(f"请在设置页或 config.py 填入 {var}\n获取方式：{urls[provider]}")
    elif provider == "custom":
        key = _cfg("CUSTOM_LLM_KEY", default="not-needed")
        base_url = _cfg("CUSTOM_LLM_BASE_URL")
        model = model or _cfg("CUSTOM_LLM_MODEL")
        LAST_KEY_SOURCE["llm"] = ("CUSTOM_LLM_KEY", key)
        if not base_url:
            sys.exit(
                "自定义服务请在设置页填写 Base URL / 模型 / Key，或在 config.py 配置：\n"
                '    CUSTOM_LLM_BASE_URL = "http://localhost:11434/v1"\n'
                '    CUSTOM_LLM_MODEL    = "qwen2.5:14b"\n'
                '    CUSTOM_LLM_KEY      = "ollama"   # 本地服务可填任意值\n\n'
                "支持任何 OpenAI 兼容服务：Ollama / vLLM / one-api / LM Studio 等。")
    else:
        from suggest import PROVIDERS as _P
        sys.exit(f"未知 LLM 供应商: {provider}\n"
                 f"可选：{' / '.join(_P.keys())}")

    return SuggestionEngine(kb, me_name=me_name, provider=provider,
                            api_key=key, model=model, base_url=base_url,
                            scene=scene,
                            tone=tone,
                            custom_tone_prompt=custom_tone_prompt,
                            timeout_seconds=(
                                timeout_seconds
                                if timeout_seconds is not None
                                else 12.0
                            ),
                            retry_attempts=(
                                retry_attempts
                                if retry_attempts is not None
                                else 2
                            ))


# ══════════════ ③ 知识库检索 ══════════════
def build_kb(docs_dir="docs", verbose=True, doc_paths=None):
    """构建知识库。

    doc_paths 为显式文档路径列表时，只加载这些文件 —— 用于项目/会议级
    知识范围隔离（桌面端会把本场选中的文档路径传进来）。
    """
    from knowledge_base import KnowledgeBase
    backend = _cfg("RETRIEVAL_BACKEND", default="local")
    key = _cfg("EMBEDDING_KEY", "ALIYUN_API_KEY")
    return KnowledgeBase(docs_dir=docs_dir, api_key=key, verbose=verbose,
                         backend=backend, doc_paths=doc_paths).build()


def describe():
    """打印当前的服务组合，让用户一眼看清用的是谁"""
    from suggest import PROVIDERS
    asr = _cfg("ASR_PROVIDER", default="xfyun")
    llm = _cfg("LLM_PROVIDER", default="xfyun")
    ret = _cfg("RETRIEVAL_BACKEND", default="local")
    names = {"xfyun": "讯飞", "aliyun": "阿里云", "volcano": "火山引擎",
             "tencent": "腾讯云", "mimo": "小米 MiMo"}
    print(f"  语音识别：{names.get(asr, asr)}"
          f"　|　建议模型：{PROVIDERS.get(llm, {}).get('label', llm)}"
          f"　|　检索：{'本地关键词' if ret == 'local' else '云端向量'}")
