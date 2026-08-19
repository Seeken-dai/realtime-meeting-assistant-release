"""Conservatively clean a saved meeting transcript with the configured LLM.

The realtime ASR text remains the immutable-ish baseline.  This pass is a
post-meeting editor: it keeps one output item per input item, preserves ids and
ordering, and only rewrites the text so the offline transcript is readable.
It is deliberately separate from diarization because speaker labels and text
cleanup have different failure modes.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import re
import sys
import time
from typing import Any, Dict, Iterable, List, Sequence, Tuple

import providers
from suggest import (
    format_llm_error,
    llm_error_details,
    normalize_scene,
)


CLEANUP_LLM_TIMEOUT_SECONDS = 30.0
CLEANUP_LLM_RETRY_ATTEMPTS = 2
# 一次输出过多行时，模型更容易截断 JSON，随后只能整块回退原文。
# 约 25–40 行/块能兼顾上下文和结构化输出的完整性。
MAX_CHUNK_CHARS = 3000


CLEANUP_SYSTEM = """你是中文会议转写校对员。输入是同一场多人会议的自动语音识别初稿。
你的任务是逐句校对，让文字连贯、自然、可读；不是总结会议，也不是补写纪要。

必须遵守：
1. 输入中的每一行必须对应输出中的一行；保持 id 和顺序完全不变。
2. 只修改 text。保留原意、语气和信息顺序，不要删掉实质内容，不要合并或拆分行。
3. 纠正常见同音字、明显的识别乱码、断句和标点；可以删除明显的连续口吃和无意义重复。
4. 结合上下文修正项目名、产品名、技术名词、缩写、版本号、人名和日期，但不确定时保留原文，不能凭空创造新事实。
5. 英文单词只在原句已有，或得到本场词表、候选词、资料名支持时保留；不要把中文语气词或乱码猜成英文。数字、金额、日期、版本号没有充分依据时不要改成另一个数。
6. 不要把听不清的内容改成看似合理但没有依据的内容；不确定处保留原文，不要用“……”代替原文；不要总结、解释或添加括号说明。
7. 只输出 JSON 数组，格式严格为：[{"id":"原始id","text":"校对后的文字"}]。
不要输出 Markdown、代码围栏或任何 JSON 以外的文字。"""


class TranscriptCleanupError(RuntimeError):
    """让 Electron 收到可展示的清理失败诊断。"""

    def __init__(self, message: str, diagnostic: Dict[str, Any]):
        self.diagnostic = diagnostic
        super().__init__(message)


def format_time(at: Any, started_at: float) -> str:
    try:
        seconds = max(0.0, (float(at or started_at) - started_at) / 1000.0)
    except (TypeError, ValueError):
        seconds = 0.0
    whole = int(seconds)
    return f"{whole // 60:02d}:{whole % 60:02d}"


def _text(item: Dict[str, Any]) -> str:
    return re.sub(r"\s+", " ", str(item.get("text") or "")).strip()


def _bounded_text(value: Any, limit: int = 120) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _candidate_terms(payload: Dict[str, Any]) -> List[str]:
    terms: List[str] = []
    raw_candidates = payload.get("glossaryCandidates") or []
    if not isinstance(raw_candidates, list):
        return terms
    for candidate in raw_candidates:
        value = candidate.get("term") if isinstance(candidate, dict) else candidate
        term = _bounded_text(value, 80)
        if term and term not in terms:
            terms.append(term)
    return terms[:120]


def _document_names(payload: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    raw_documents = payload.get("documentNames") or payload.get("documents") or []
    if not isinstance(raw_documents, list):
        return names
    for document in raw_documents:
        value = document.get("name") if isinstance(document, dict) else document
        name = _bounded_text(value, 120)
        if name and name not in names:
            names.append(name)
    return names[:40]


def _numbers(text: str) -> List[str]:
    return re.findall(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?%?", text)


def _latin_tokens(text: str) -> List[str]:
    return re.findall(r"[A-Za-z][A-Za-z0-9_-]*", text)


def _semantic_drift_reason(
    source: Dict[str, Any],
    text: str,
    trusted_terms: Sequence[str] = (),
) -> str | None:
    """拒绝模型凭空改数字、造缩写或加省略号；宁可保留该小块原文。"""
    source_text = _text(source)
    if Counter(_numbers(source_text)) != Counter(_numbers(text)):
        return "模型改动了数字、日期、金额或版本号"
    if ("..." in text or "…" in text) and "..." not in source_text and "…" not in source_text:
        return "模型凭空添加了省略号"

    source_latin = {token.casefold() for token in _latin_tokens(source_text)}
    trusted_latin = {
        token.casefold()
        for term in trusted_terms
        for token in _latin_tokens(str(term))
    }
    unknown_latin = [
        token
        for token in _latin_tokens(text)
        if token.casefold() not in source_latin and token.casefold() not in trusted_latin
    ]
    if unknown_latin:
        return f"模型新增了未经词库支持的英文/缩写：{', '.join(unknown_latin[:4])}"
    return None


def format_item(item: Dict[str, Any], started_at: float) -> str:
    transcript_id = str(item.get("id") or "unknown")
    speaker = str(item.get("speaker") or "未知说话人")
    stamp = format_time(item.get("at"), started_at)
    return f"id={transcript_id} t={stamp} speaker={speaker} text={_text(item)}"


def chunk_transcript(
    transcript: Sequence[Dict[str, Any]],
    started_at: float,
    max_chars: int = MAX_CHUNK_CHARS,
) -> List[List[Dict[str, Any]]]:
    """Split only between complete transcript items."""
    chunks: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    size = 0
    for item in transcript:
        if not isinstance(item, dict) or not _text(item):
            continue
        line_size = len(format_item(item, started_at)) + 1
        if current and size + line_size > max_chars:
            chunks.append(current)
            current = []
            size = 0
        current.append(item)
        size += line_size
    if current:
        chunks.append(current)
    return chunks


def _json_candidates(raw: str) -> Iterable[str]:
    text = str(raw or "").strip()
    fenced = re.findall(r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", text, re.I)
    yield from fenced
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        yield text[start : end + 1]
    yield text


def parse_cleaned_items(
    raw: str,
    source_items: Sequence[Dict[str, Any]],
    trusted_terms: Sequence[str] = (),
) -> List[Dict[str, str]]:
    """Parse and validate a one-to-one model response."""
    expected = [str(item.get("id") or "") for item in source_items]
    expected_set = set(expected)
    last_error: Exception | None = None
    for candidate in _json_candidates(raw):
        try:
            payload = json.loads(candidate)
            if not isinstance(payload, list):
                raise ValueError("模型输出不是数组")
            result: List[Dict[str, str]] = []
            seen = set()
            for item in payload:
                if not isinstance(item, dict):
                    raise ValueError("数组中存在非对象项")
                transcript_id = str(item.get("id") or "")
                text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
                if transcript_id not in expected_set or transcript_id in seen or not text:
                    raise ValueError("模型改写破坏了原始 id 或产生空文本")
                source = next(source for source in source_items if str(source.get("id") or "") == transcript_id)
                # 防止模型把一小段 ASR 变成一大段新内容；正常中文校对不会膨胀到这个程度。
                if len(text) > max(240, len(_text(source)) * 3 + 80):
                    raise ValueError("模型输出异常膨胀")
                drift_reason = _semantic_drift_reason(source, text, trusted_terms)
                if drift_reason:
                    raise ValueError(drift_reason)
                seen.add(transcript_id)
                result.append({"id": transcript_id, "text": text})
            if [item["id"] for item in result] != expected:
                raise ValueError("模型输出缺少、重复或重排了转写行")
            return result
        except Exception as exc:  # noqa: BLE001 - try the next JSON candidate
            last_error = exc
    raise ValueError(f"模型输出无法安全应用：{last_error}")


def apply_cleaned_items(
    transcript: Sequence[Dict[str, Any]],
    cleaned_items: Sequence[Dict[str, str]],
) -> List[Dict[str, Any]]:
    by_id = {str(item["id"]): str(item["text"]) for item in cleaned_items}
    output: List[Dict[str, Any]] = []
    for item in transcript:
        row = dict(item)
        transcript_id = str(row.get("id") or "")
        if transcript_id in by_id:
            row["text"] = by_id[transcript_id]
        output.append(row)
    return output


def clean_transcript(
    payload: Dict[str, Any],
    *,
    provider: str | None = None,
    model: str | None = None,
    timeout_seconds: float = CLEANUP_LLM_TIMEOUT_SECONDS,
    retry_attempts: int = CLEANUP_LLM_RETRY_ATTEMPTS,
) -> Dict[str, Any]:
    transcript = [
        item
        for item in (payload.get("transcript") or [])
        if isinstance(item, dict) and item.get("isFinal", True) and _text(item)
    ]
    if not transcript:
        raise ValueError("会议没有可用于会后整理的最终转写")

    started_at = float(payload.get("startedAt") or 0)
    title = str(payload.get("title") or "未命名会议")
    scene = normalize_scene(payload.get("scene"))
    terms = [str(term).strip() for term in (payload.get("glossaryTerms") or []) if str(term).strip()]
    candidate_terms = _candidate_terms(payload)
    document_names = _document_names(payload)
    chunks = chunk_transcript(transcript, started_at)
    if not chunks:
        raise ValueError("会议没有可用于会后整理的有效转写")

    started = time.time()
    kb = providers.build_kb(verbose=False, doc_paths=[])
    engine = providers.build_llm(
        kb,
        me_name="我",
        provider=provider,
        model=model,
        scene=scene,
        timeout_seconds=timeout_seconds,
        retry_attempts=retry_attempts,
    )
    cleaned: List[Dict[str, str]] = []
    fallback_chunks: set[int] = set()

    def clean_chunk(
        chunk: Sequence[Dict[str, Any]],
        chunk_index: int,
        depth: int = 0,
    ) -> List[Dict[str, str]]:
        lines = "\n".join(format_item(item, started_at) for item in chunk)
        reference_hints = []
        if terms:
            reference_hints.append(
                "本场已登记的专有名词（只在语境支持时使用）：" + "、".join(terms[:120])
            )
        if candidate_terms:
            reference_hints.append(
                "本场历史复盘候选词（仅作参考，不要直接替换）："
                + "、".join(candidate_terms)
            )
        if document_names:
            reference_hints.append(
                "本场参考资料名称（可帮助识别项目名，但不要据此补写句子）："
                + "、".join(document_names)
            )
        glossary_hint = "\n" + "\n".join(reference_hints) if reference_hints else ""

        neighboring_lines = []
        if chunk_index > 1:
            neighboring_lines.extend(
                f"前文：{_text(item)}" for item in chunks[chunk_index - 2][-2:]
            )
        if chunk_index < len(chunks):
            neighboring_lines.extend(
                f"后文：{_text(item)}" for item in chunks[chunk_index][:2]
            )
        neighboring_hint = (
            "\n相邻段落仅用于理解上下文，不要输出或改写这些段落：\n"
            + "\n".join(neighboring_lines)
            if neighboring_lines
            else ""
        )
        user = (
            f"会议标题：{title}\n"
            f"这是第 {chunk_index}/{len(chunks)} 部分。\n"
            f"{glossary_hint}{neighboring_hint}\n\n"
            "请逐行校对下面的转写，输出与输入行数完全相同的 JSON 数组：\n"
            f"{lines}"
        )
        try:
            raw = engine._call(CLEANUP_SYSTEM, user)
            return parse_cleaned_items(
                raw,
                chunk,
                trusted_terms=[title, *terms, *candidate_terms, *document_names],
            )
        except ValueError:
            # 大块 JSON 被截断或漏行时，拆小后重试，避免整块几十行一起回退。
            if depth < 2 and len(chunk) > 16:
                middle = len(chunk) // 2
                return clean_chunk(chunk[:middle], chunk_index, depth + 1) + clean_chunk(
                    chunk[middle:], chunk_index, depth + 1
                )
        except Exception:
            # 网络、鉴权或服务故障不重复放大请求；保留这一块原文。
            pass
        fallback_chunks.add(chunk_index)
        return [
            {"id": str(item.get("id") or ""), "text": _text(item)}
            for item in chunk
        ]

    for index, chunk in enumerate(chunks, 1):
        cleaned.extend(clean_chunk(chunk, index))

    original_by_id = {str(item.get("id") or ""): _text(item) for item in transcript}
    changed = sum(
        1
        for item in cleaned
        if item.get("text") != original_by_id.get(str(item.get("id") or ""))
    )
    return {
        "ok": True,
        "transcript": apply_cleaned_items(payload.get("transcript") or [], cleaned),
        "chunks": len(chunks),
        "changed": changed,
        "fallbackChunks": sorted(fallback_chunks),
        "elapsedSec": round(time.time() - started, 1),
        "provider": getattr(engine, "provider", provider),
        "model": getattr(engine, "model", model),
        "timeoutSeconds": float(timeout_seconds),
        "retryAttempts": max(1, int(retry_attempts)),
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--timeout-seconds", type=float, default=CLEANUP_LLM_TIMEOUT_SECONDS)
    parser.add_argument("--retry-attempts", type=int, default=CLEANUP_LLM_RETRY_ATTEMPTS)
    args = parser.parse_args()
    try:
        with open(args.input, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        result = clean_transcript(
            payload,
            provider=args.provider,
            model=args.model,
            timeout_seconds=args.timeout_seconds,
            retry_attempts=args.retry_attempts,
        )
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False)
        print(json.dumps({k: result[k] for k in result if k != "transcript"}, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        diagnostic = llm_error_details(
            exc,
            provider=args.provider,
            model=args.model,
            timeout_seconds=args.timeout_seconds,
            stage="transcript_cleanup",
        )
        result = {
            "ok": False,
            "error": format_llm_error(diagnostic, "会后转写整理服务"),
            "diagnostic": diagnostic,
        }
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False)
        print(json.dumps(result, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
