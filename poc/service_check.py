"""桌面端服务诊断与测速（对应 PRD SET-1 / SET-5 / SET-7）。

只输出一行 JSON 到 stdout，诊断信息走 stderr，协议与 desktop_bridge 一致。

用法：
    python service_check.py --status          # 三类服务的配置与凭证来源
    python service_check.py --test-llm        # 建议模型连通性
    python service_check.py --bench           # 跨供应商测速排名
"""

import argparse
import contextlib
import json
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import providers
from model_catalog import FALLBACK_MODEL_CANDIDATES
from suggest import PROVIDERS

_BENCH_PROMPT = """客户说："你们这个模块能不能支持自定义审批流？就是简单改一下，
按金额分几档走不同的人。"请站在我方立场，给出 2 条应对话术建议，
每条包含意图提示和建议话术。用 JSON 输出。"""

_ASR_LABEL = {
    "xfyun": "讯飞 RTASR（标准版）", "xfyun-llm": "讯飞实时转写大模型",
    "aliyun": "阿里云", "volcano": "火山引擎",
    "tencent": "腾讯云", "mimo": "小米 MiMo",
}


class _EmptyKB:
    forbidden_terms = set()
    internal_numbers = set()

    def search(self, _query, top_k=4):
        return []


def out(payload):
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def status(provider=None, asr_provider=None):
    """三类服务各自的配置状态。provider/asr_provider 非空时按指定的报。

    ⚠️ SET-7：必须报告【实际使用的是哪个凭证变量】。讯飞每个模型的
    APIPassword 相互独立，静默回退会让用户以为自己填的密钥在生效。
    """
    asr = asr_provider or providers._cfg("ASR_PROVIDER", default="xfyun")
    llm = provider or providers._cfg("LLM_PROVIDER", default="xfyun")
    retrieval = providers._cfg("RETRIEVAL_BACKEND", default="local")

    # 探测 LLM 凭证来源而不真正发请求
    llm_key_var, llm_key = None, None
    try:
        with contextlib.redirect_stdout(sys.stderr):
            engine = providers.build_llm(kb=None, provider=provider)
        llm_key_var, llm_key = providers.LAST_KEY_SOURCE.get("llm", (None, None))
        llm_info = {
            "ok": True,
            "label": engine.label,
            "model": engine.model,
            "baseUrl": engine.base_url,
        }
    except SystemExit as exc:
        llm_info = {"ok": False, "message": str(exc)}
    except Exception as exc:
        llm_info = {"ok": False, "message": str(exc)}

    # 各 ASR 供应商所需的 config 变量，用于判断是否已配置
    _ASR_KEYS = {
        "xfyun": ["XFYUN_APP_ID", "XFYUN_API_KEY"],
        "xfyun-llm": [
            "XFYUN_LLM_ASR_APP_ID",  # 可回退 XFYUN_APP_ID，见下方 ready 判断
            "XFYUN_LLM_ASR_KEY_ID",
            "XFYUN_LLM_ASR_KEY_SECRET",
        ],
        # 兼容旧配置：实时 ASR 可复用 DashScope 的 ALIYUN_API_KEY。
        "aliyun": ["ALIYUN_ASR_KEY", "ALIYUN_API_KEY"],
        "volcano": ["VOLC_APP_KEY", "VOLC_ACCESS_KEY"],
        "tencent": ["TENCENT_APP_ID", "TENCENT_SECRET_ID", "TENCENT_SECRET_KEY"],
        "mimo": ["MIMO_API_KEY"],
    }
    asr_key_vars = _ASR_KEYS.get(asr, [])
    if asr == "xfyun-llm":
        asr_ready = bool(
            (providers._cfg("XFYUN_LLM_ASR_APP_ID") or providers._cfg("XFYUN_APP_ID"))
            and providers._cfg("XFYUN_LLM_ASR_KEY_ID")
            and providers._cfg("XFYUN_LLM_ASR_KEY_SECRET")
        )
    elif asr == "aliyun":
        asr_ready = bool(providers._cfg("ALIYUN_ASR_KEY", "ALIYUN_API_KEY"))
    else:
        asr_ready = all(providers._cfg(v) for v in asr_key_vars) if asr_key_vars else True

    out({
        "type": "status",
        "asr": {
            "provider": asr,
            "label": _ASR_LABEL.get(asr, asr),
            "model": (
                providers._cfg("ALIYUN_ASR_MODEL", default="qwen-audio-3.0-asr-flash-streaming")
                if asr == "aliyun"
                else None
            ),
            "ok": asr_ready,
            "keyVar": (
                "XFYUN_APP_ID + XFYUN_LLM_ASR_KEY_ID/SECRET"
                if asr == "xfyun-llm"
                else (
                    "ALIYUN_ASR_KEY 或 ALIYUN_API_KEY"
                    if asr == "aliyun"
                    else (" / ".join(asr_key_vars) if asr_key_vars else None)
                )
            ),
            "options": list(_ASR_KEYS.keys()),
        },
        "llm": {
            "provider": llm,
            "keyVar": llm_key_var,
            "keyPreview": providers.mask(llm_key) if llm_key else None,
            **llm_info,
        },
        "retrieval": {
            "backend": retrieval,
            "label": "本地关键词检索" if retrieval == "local" else "云端向量检索",
            "ok": True,
            "note": "不依赖外部账号" if retrieval == "local" else "需 Embedding 密钥",
        },
        "providers": [
            {"id": key, "label": value["label"], "model": value["model"]}
            for key, value in PROVIDERS.items()
        ],
    })


def _hint_for(message):
    low = message.lower().replace(" ", "")
    if "appidnoauth" in low or "noauth" in low:
        return "模型名正确，但当前密钥没有该模型的权限。讯飞每个模型的 APIPassword 独立。"
    if "invalidparammodel" in low:
        return "模型名不对，请检查该供应商的模型标识符。"
    return ""


def test_llm(provider=None, model=None, scene="general"):
    """用最短固定探针测密钥、模型名和接口连通性；真实建议速度另行测速。"""
    try:
        with contextlib.redirect_stdout(sys.stderr):
            engine = providers.build_llm(
                kb=_EmptyKB(), provider=provider, model=model, scene=scene
            )
            started = time.time()
            reply = engine._call(
                "你是一个服务连通性测试器。",
                "只回复两个字：正常",
            )
            elapsed = time.time() - started
            verdict = "pass" if elapsed <= 8 else "warning" if elapsed <= 12 else "high_risk"
        out({
            "type": "llm_test", "ok": True, "provider": engine.provider,
            "label": engine.label, "model": engine.model,
            "scene": engine.scene, "elapsed": round(elapsed, 2),
            "targetSeconds": 8,
            "verdict": verdict,
            "reply": (reply or "").strip()[:40],
            "warning": None,
        })
    except SystemExit as exc:
        out({"type": "llm_test", "ok": False, "message": str(exc)})
    except Exception as exc:
        message = str(exc)
        out({"type": "llm_test", "ok": False, "message": message[:200],
             "hint": _hint_for(message)})


def _model_field(item, name):
    """从 OpenAI SDK 对象或兼容网关返回的 dict 读取字段。"""
    if isinstance(item, dict):
        return item.get(name)
    value = getattr(item, name, None)
    if value is not None:
        return value
    dumper = getattr(item, "model_dump", None)
    if callable(dumper):
        try:
            return dumper().get(name)
        except Exception:
            return None
    return None


def _is_text_generation_model(item, model_id):
    """过滤明显不是会议文本模型的目录项。

    大多数 OpenAI 兼容的 /models 只返回 id，没有能力字段，因此只能做保守的
    名称过滤；真正的文本模型仍由用户点击“测试连接”验证。
    """
    methods = _model_field(item, "supported_generation_methods")
    if methods:
        normalized = {str(method).lower() for method in methods}
        if not any(method in normalized for method in (
            "generatecontent", "generate_content", "chat.completions",
            "chat-completions", "completion",
        )):
            return False
    lower = str(model_id).lower()
    non_text_markers = (
        "embedding", "moderation", "rerank", "text-to-speech", "tts",
        "transcrib", "whisper", "imagen", "image-generation", "image-",
        "veo", "video-generation", "video-",
    )
    return not any(marker in lower for marker in non_text_markers)


def _discover_model_ids(engine):
    """读取供应商的 OpenAI 兼容模型目录，失败时返回空列表和原因。"""
    try:
        client = engine._client
        # 目录请求不能沿用 SDK 默认的长超时，否则供应商不支持 /models 时会卡住。
        with_options = getattr(client, "with_options", None)
        if callable(with_options):
            client = with_options(timeout=8.0)
        page = client.models.list()
        items = _model_field(page, "data")
        if items is None and isinstance(page, dict):
            items = page.get("data")
        if items is None:
            items = list(page)
    except Exception as exc:
        return [], str(exc).replace("\n", " ")[:180]

    ids = []
    seen = set()
    for item in items or []:
        model_id = _model_field(item, "id")
        if not model_id:
            continue
        model_id = str(model_id).strip()
        if not model_id or model_id in seen:
            continue
        if not _is_text_generation_model(item, model_id):
            continue
        seen.add(model_id)
        ids.append(model_id)
    return ids, None


def _probe_one_model(engine, model):
    """用最短请求验证一个模型，返回前端可直接展示的结果行。"""
    original = engine.model
    engine.model = model
    try:
        with contextlib.redirect_stdout(sys.stderr):
            started = time.time()
            engine._call("你是一个助手。", "回复两个字：正常")
        return {"model": model, "ok": True,
                "verified": True, "source": "probe",
                "elapsed": round(time.time() - started, 2)}
    except Exception as exc:
        message = str(exc).replace("\n", " ")
        return {"model": model, "ok": False, "verified": True,
                "source": "probe", "error": message[:80],
                "hint": _hint_for(message)}
    finally:
        engine.model = original


def probe_llm(provider):
    """优先读取供应商模型目录，再验证当前模型；目录不可用时试探回退候选。

    这样新模型只要已出现在供应商的 /models 目录中，桌面端无需更新即可展示。
    不提供模型目录的供应商仍保留旧的候选名探测能力。
    """
    try:
        with contextlib.redirect_stdout(sys.stderr):
            engine = providers.build_llm(kb=None, provider=provider)
    except SystemExit as exc:
        out({"type": "probe_llm", "provider": provider, "ok": False,
             "message": str(exc)})
        return
    except Exception as exc:
        out({"type": "probe_llm", "provider": provider, "ok": False,
             "message": str(exc)[:200]})
        return

    catalog_models, catalog_error = _discover_model_ids(engine)
    if catalog_models:
        # 目录是账号当前可见模型的动态来源，不再逐个发送昂贵的生成请求。
        # 仅验证当前模型，其他模型由设置页的“测试连接”按需验证。
        ordered = [engine.model] + [model for model in catalog_models
                                    if model != engine.model]
        results = [{"model": model, "ok": True, "verified": False,
                    "source": "catalog"} for model in ordered]
        current = _probe_one_model(engine, engine.model)
        by_model = {row["model"]: row for row in results}
        by_model[engine.model] = current
        results = [by_model[model] for model in ordered]
        working = [row for row in results if row["ok"] and row.get("verified")]
        working.sort(key=lambda row: row.get("elapsed", float("inf")))
        out({"type": "probe_llm", "provider": provider, "ok": True,
             "label": engine.label, "source": "catalog",
             "catalogCount": len(catalog_models), "results": results,
             "fastest": working[0] if working else None})
        return

    # 供应商未实现 /models、网关屏蔽了目录或目录请求失败时，继续使用静态回退。
    cands = [engine.model] + [model for model in FALLBACK_MODEL_CANDIDATES.get(provider, [])
                              if model != engine.model]
    results = []
    for model in cands:
        result = _probe_one_model(engine, model)
        result["source"] = "fallback"
        results.append(result)
    working = [r for r in results if r["ok"]]
    working.sort(key=lambda r: r.get("elapsed", float("inf")))
    out({"type": "probe_llm", "provider": provider, "ok": bool(working),
         "label": engine.label, "source": "fallback",
         "discoveryError": catalog_error or "目录返回空模型列表",
         "results": results,
         "fastest": working[0] if working else None})


def test_asr(asr_provider, asr_lang=None, asr_model=None):
    """测某个 ASR 供应商能否连通（建连+握手，随即断开）。"""
    try:
        with contextlib.redirect_stdout(sys.stderr):
            asr = providers.build_asr(
                provider=asr_provider,
                lang=asr_lang,
                model=asr_model,
            )
            started = time.time()
            asr.start(lambda *a, **k: None)   # 建连+握手；no-op 回调
            elapsed = round(time.time() - started, 2)
            try:
                asr.stop()
            except Exception:
                pass
        out({"type": "asr_test", "provider": asr_provider, "ok": True,
             "label": getattr(asr, "name", asr_provider), "elapsed": elapsed})
    except SystemExit as exc:
        out({"type": "asr_test", "provider": asr_provider, "ok": False,
             "message": str(exc)})
    except Exception as exc:
        out({"type": "asr_test", "provider": asr_provider, "ok": False,
             "message": str(exc)[:200]})


def bench(runs=2, provider=None, model=None):
    """测速。

    - provider 为空：遍历所有已配置密钥的供应商（对比选型）
    - provider 指定：只测当前这一家（可带 model 覆盖）
    ⚠️ 报告最慢一次：会议现场偶尔卡顿与一直慢同样毁体验。
    """
    if provider:
        names = [provider]
        scope = "current"
    else:
        names = [n for n in PROVIDERS if n != "custom"]
        scope = "all"

    results = []
    for name in names:
        try:
            with contextlib.redirect_stdout(sys.stderr):
                # 仅在「测当前」时应用 UI 选中的模型名；全量对比用各家默认
                kw = {"kb": None, "provider": name}
                if scope == "current" and model:
                    kw["model"] = model
                engine = providers.build_llm(**kw)
        except (SystemExit, Exception) as exc:
            if scope == "current":
                out({
                    "type": "bench",
                    "scope": scope,
                    "results": [{
                        "provider": name,
                        "label": name,
                        "model": model or "",
                        "ok": False,
                        "avg": None,
                        "max": None,
                        "error": str(exc)[:80],
                    }],
                    "fastest": None,
                    "targetSeconds": 5,
                })
                return
            continue
        times, error = [], None
        for _ in range(runs):
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    started = time.time()
                    engine._call("你是资深售前顾问的会议助手。", _BENCH_PROMPT)
                times.append(time.time() - started)
            except Exception as exc:
                error = str(exc)[:80]
                break
        results.append({
            "provider": name,
            "label": engine.label,
            "model": engine.model,
            "ok": bool(times),
            "avg": round(sum(times) / len(times), 2) if times else None,
            "max": round(max(times), 2) if times else None,
            "error": error,
        })
    ok_rows = [r for r in results if r["ok"]]
    ok_rows.sort(key=lambda r: r["avg"])
    out({
        "type": "bench",
        "scope": scope,
        "results": results,
        "fastest": ok_rows[0] if ok_rows else None,
        "targetSeconds": 5,
    })


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--test-llm", action="store_true")
    parser.add_argument("--bench", action="store_true")
    parser.add_argument("--probe-llm", action="store_true",
                        help="探测某 LLM 供应商可用的模型名")
    parser.add_argument("--test-asr", action="store_true",
                        help="测某 ASR 供应商连通性")
    parser.add_argument("--provider", help="临时指定 LLM 供应商")
    parser.add_argument("--llm-model", help="测速时覆盖模型名（仅配合 --provider）")
    parser.add_argument("--asr-provider", help="临时指定 ASR 供应商")
    parser.add_argument("--asr-model", help="测速/连通性时覆盖 ASR 模型名")
    parser.add_argument("--asr-lang", help="识别语种：zh / en / zh_en")
    parser.add_argument("--scene", choices=("general", "sales", "requirements"), default="general")
    parser.add_argument(
        "--bench-all", action="store_true",
        help="与 --bench 合用：对比全部已配置供应商（忽略当前选择）",
    )
    args = parser.parse_args()
    try:
        if args.probe_llm:
            probe_llm(args.provider or providers._cfg("LLM_PROVIDER",
                                                      default="xfyun"))
        elif args.test_asr:
            test_asr(
                args.asr_provider or providers._cfg("ASR_PROVIDER",
                                                    default="xfyun"),
                asr_lang=args.asr_lang,
                asr_model=args.asr_model,
            )
        elif args.bench:
            # 默认测当前供应商；--bench-all 才横比各家
            if args.bench_all:
                bench()
            else:
                bench(
                    provider=args.provider or providers._cfg(
                    "LLM_PROVIDER", default="xfyun"),
                    model=args.llm_model,
                )
        elif args.test_llm:
            test_llm(args.provider, args.llm_model, args.scene)
        else:
            status(args.provider, args.asr_provider)
    except Exception as exc:
        out({"type": "error", "message": str(exc)[:200]})


if __name__ == "__main__":
    main()
