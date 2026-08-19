"""Generate a conservative, evidence-linked meeting review.

The local rules in meeting-store.cjs run first. This optional pass only enriches
those candidates; the Electron layer keeps the local result when this process
times out or returns unusable JSON.
"""

from __future__ import annotations

import argparse
import json
import re
import time

import providers
from suggest import (
    format_llm_error,
    llm_error_details,
    normalize_scene,
    scene_config,
)


# 会后复盘是低频任务，不能复用会中建议的 12 秒截止时间。
# 单次请求给足模型生成结构化 JSON 的时间，仍保留一次有限重试。
REVIEW_LLM_TIMEOUT_SECONDS = 30.0
REVIEW_LLM_RETRY_ATTEMPTS = 2


class EmptyKnowledgeBase:
    """Review must not invent product facts from an unrelated knowledge base."""

    forbidden_terms = set()
    internal_numbers = set()

    def search(self, _query, top_k=4):
        return []


class ReviewGenerationError(RuntimeError):
    """保留可安全展示的复盘增强诊断，不把密钥或完整响应带回桌面端。"""

    def __init__(self, message, diagnostic=None):
        self.diagnostic = diagnostic or {}
        super().__init__(message)


REVIEW_SYSTEM = """你是严谨的会议复盘助手。你只能根据给定的会议转写提取“候选”，不能补造会议没有明确说过的结论。

只输出 JSON，不要 Markdown，不要解释。结构必须是：
{
  "memoryItems": [{
    "kind": "decision" 或 "action_item",
    "content": "完整、可执行的候选内容",
    "owner": "明确提到的负责人，没有就 null",
    "dueAt": "明确提到的期限，没有就 null",
    "evidenceTranscriptId": "原文段落 id",
    "evidenceText": "支持该候选的原文短句"
  }],
  "glossaryCandidates": [{
    "term": "领域词",
    "frequency": 1,
    "weight": 1 到 5,
    "sampleContext": "原文中的短上下文",
    "reason": "为什么需要加入词库"
  }]
}

规则：
1. 决策必须出现确定、决定、确认、同意、采用、定为等明确表达；待办必须出现负责、跟进、提交、整理、截止、下周等明确动作或期限。
2. 每个候选都必须带原文 id 和证据短句；无法定位就不要输出。
3. 领域词只保留产品名、接口名、项目黑话、缩写或明显的 ASR 易错词；不要输出普通虚词、人名、泛化词。长度 2 到 20 个字符。
4. 候选不是最终结论，宁可少提取，不要猜测。
"""


def _json_candidates(text):
    raw = str(text or "").strip()
    for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S | re.I):
        yield match.group(1)
    depth = 0
    start = -1
    for index, char in enumerate(raw):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                yield raw[start : index + 1]
    yield raw


def parse_review_json(raw):
    last_error = None
    for candidate in _json_candidates(raw):
        try:
            payload = json.loads(candidate)
        except Exception as exc:  # noqa: BLE001 - try the next candidate
            last_error = exc
            continue
        if isinstance(payload, dict):
            return {
                "memoryItems": payload.get("memoryItems")
                if isinstance(payload.get("memoryItems"), list)
                else [],
                "glossaryCandidates": payload.get("glossaryCandidates")
                if isinstance(payload.get("glossaryCandidates"), list)
                else [],
            }
    raise ValueError(f"模型输出不是合法复盘 JSON：{last_error}")


def format_transcript(transcript, started_at):
    lines = []
    for item in transcript:
        if item.get("isFinal", True) is False:
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        try:
            seconds = max(0, (float(item.get("at") or started_at) - started_at) / 1000)
        except (TypeError, ValueError):
            seconds = 0
        stamp = f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"
        lines.append(
            f"[证据 id={item.get('id') or 'unknown'} t={stamp}] "
            f"[{item.get('speaker') or '未知说话人'}] {text}"
        )
    return "\n".join(lines)


def generate_review(
    payload,
    *,
    provider=None,
    model=None,
    timeout_seconds=REVIEW_LLM_TIMEOUT_SECONDS,
    retry_attempts=REVIEW_LLM_RETRY_ATTEMPTS,
):
    scene = normalize_scene(payload.get("scene"))
    config = scene_config(scene)
    title = str(payload.get("title") or "未命名会议")
    transcript = payload.get("transcript") or []
    started_at = float(payload.get("startedAt") or 0)
    evidence = format_transcript(transcript, started_at)
    if not evidence:
        raise ValueError("会议没有可用于复盘的最终转写")
    evidence = evidence[-12000:]

    started = time.time()
    engine = None
    prompt = (
        f"会议标题：{title}\n"
        f"会议场景：{config['label']}。重点关注：{'、'.join(config['minutes'])}\n\n"
        f"【会议转写证据】\n{evidence}"
    )
    try:
        engine = providers.build_llm(
            EmptyKnowledgeBase(),
            me_name="我",
            provider=provider,
            model=model,
            scene=scene,
            timeout_seconds=timeout_seconds,
            retry_attempts=retry_attempts,
        )
        parsed = parse_review_json(engine._call(REVIEW_SYSTEM, prompt))
    except Exception as exc:  # noqa: BLE001 - 转成结构化、安全的桌面端诊断
        diagnostic = llm_error_details(
            exc,
            provider=getattr(engine, "provider", provider),
            model=getattr(engine, "model", model),
            timeout_seconds=timeout_seconds,
            stage="review",
        )
        raise ReviewGenerationError(
            format_llm_error(diagnostic, "会后复盘服务"),
            diagnostic,
        ) from exc

    return {
        "ok": True,
        "memoryItems": parsed["memoryItems"][:80],
        "glossaryCandidates": parsed["glossaryCandidates"][:80],
        "elapsedSec": round(time.time() - started, 1),
        "provider": getattr(engine, "provider", provider),
        "model": getattr(engine, "model", model),
        "timeoutSeconds": float(timeout_seconds),
        "retryAttempts": max(1, int(retry_attempts)),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=REVIEW_LLM_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--retry-attempts",
        type=int,
        default=REVIEW_LLM_RETRY_ATTEMPTS,
    )
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    result = generate_review(
        payload,
        provider=args.provider,
        model=args.model,
        timeout_seconds=args.timeout_seconds,
        retry_attempts=args.retry_attempts,
    )
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except ReviewGenerationError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    "diagnostic": exc.diagnostic,
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(1)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
