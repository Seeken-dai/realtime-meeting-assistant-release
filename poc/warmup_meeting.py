"""会议开始前预热：导入重模块 + 可选预同步阿里热词。

输出一行 JSON 到 stdout，供 Electron 解析。不打开麦克风、不写录音。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser(description="Warm up meeting dependencies")
    parser.add_argument("--hotwords-file", default="")
    parser.add_argument("--asr-provider", default="")
    parser.add_argument("--check-enroll", action="append", default=[])
    args = parser.parse_args()

    started = time.time()
    steps: list[dict] = []
    vocabulary_id = None
    term_count = 0
    warning = None

    def step(name: str, ok: bool, message: str = "", **extra):
        item = {"name": name, "ok": ok, "message": message, **extra}
        steps.append(item)
        return item

    try:
        import providers  # noqa: F401

        step("imports", True, "Python 依赖已预热")
    except Exception as exc:
        step("imports", False, str(exc))
        print(json.dumps({"ok": False, "steps": steps, "error": str(exc)}, ensure_ascii=False))
        return 1

    enroll_ok = 0
    for path in args.check_enroll or []:
        if path and os.path.isfile(path):
            enroll_ok += 1
    step(
        "voiceprint",
        True,
        f"声纹样本 {enroll_ok} 段就绪" if enroll_ok else "未注册声纹（本场可不启用）",
        samples=enroll_ok,
    )

    asr = (args.asr_provider or "").strip().lower()
    hotwords_path = (args.hotwords_file or "").strip()
    if hotwords_path and asr in ("aliyun", "ali", "dashscope", ""):
        try:
            from asr_hotwords import ensure_aliyun_vocabulary_id, load_hotwords_file
            import asr_hotwords
            from providers import _cfg

            terms = load_hotwords_file(hotwords_path)
            term_count = len(terms)
            if not terms:
                step("hotwords", True, "无专有名词，跳过云端同步", termCount=0)
            else:
                resolved = asr or str(_cfg("ASR_PROVIDER", default="aliyun") or "aliyun").lower()
                if resolved not in ("aliyun", "ali", "dashscope"):
                    step(
                        "hotwords",
                        True,
                        f"当前 ASR「{resolved}」不读热词，已跳过同步",
                        termCount=term_count,
                    )
                else:
                    api_key = _cfg("ALIYUN_ASR_KEY", "ALIYUN_API_KEY")
                    vocabulary_id = ensure_aliyun_vocabulary_id(
                        terms,
                        api_key=api_key,
                        target_model="paraformer-realtime-v2",
                    )
                    if vocabulary_id:
                        warning = getattr(asr_hotwords, "LAST_SYNC_DIAGNOSTIC", None)
                        step(
                            "hotwords",
                            True,
                            (
                                f"专有名词已预同步 {term_count} 个"
                                + (f"；{warning}" if warning else "")
                            ),
                            termCount=term_count,
                            vocabularyId=str(vocabulary_id),
                            warning=warning,
                        )
                    else:
                        reason = (
                            getattr(asr_hotwords, "LAST_SYNC_DIAGNOSTIC", None)
                            or "供应商未返回具体原因"
                        )
                        step(
                            "hotwords",
                            False,
                            f"热词预同步失败：{reason}",
                            termCount=term_count,
                            reason=reason,
                        )
        except Exception as exc:
            reason = (
                asr_hotwords._safe_diagnostic(exc)
                if "asr_hotwords" in locals()
                else str(exc)
            ) or "供应商未返回具体原因"
            step(
                "hotwords",
                False,
                f"热词预热异常：{reason}",
                termCount=term_count,
                reason=reason,
            )
    else:
        step("hotwords", True, "未请求热词预同步")

    payload = {
        "ok": all(item.get("ok") for item in steps if item["name"] == "imports"),
        "steps": steps,
        "vocabularyId": vocabulary_id,
        "termCount": term_count,
        "warning": warning,
        "elapsedMs": int((time.time() - started) * 1000),
    }
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
