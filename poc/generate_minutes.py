"""基于已保存转写生成结构化会议纪要。

输入和输出都走文件，避免长会议文本超过 Windows 命令行长度；stdout 只输出
一行 JSON，便于 Electron 主进程解析。纪要只总结转写中明确出现的事实，
不使用知识库补写未在会上达成的结论。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import providers
from suggest import (
    format_llm_error,
    llm_error_details,
    normalize_scene,
    scene_config,
)


# 纪要是会后低频任务，不应复用会中建议的 12 秒截止时间。
# 讯飞 X2-Flash 对 8 分钟左右会议的最终汇总偶尔超过 30 秒；
# 纪要请求允许更长的单次响应时间，避免“已有旧纪要但重新生成失败”。
MINUTES_LLM_TIMEOUT_SECONDS = 90.0
MINUTES_LLM_RETRY_ATTEMPTS = 2
EVIDENCE_CATALOG_MAX_CHARS = 48_000


PART_SYSTEM = """你是严谨的会议记录员。请把这一部分会议转写压缩成事实笔记。
只记录明确出现的内容，不推断负责人、期限、数字或结论；不确定就写“未明确”。
保留：讨论主题、已确认结论、待办、需求/约束、风险和待确认问题。使用简洁中文。
每条事实后必须保留输入中对应的完整证据标记，原样复制
`[证据 id=转写ID t=MM:SS]`，不得改写 id 或时间；合并多条事实时可以附多个证据标记。
没有明确支持材料时才写 `[证据 待确认]`，不要为了省字删掉证据标记。"""

FINAL_SYSTEM = """你是严谨的产品需求会议记录员。请根据提供的会议事实笔记生成 Markdown 纪要。
必须使用以下结构：
# 会议纪要
## 一句话摘要
## 已确认结论
## 待办事项
## 需求与约束
## 风险与待确认

规则：
1. 只能写材料中明确出现的事实，不补造负责人、期限、数字、客户立场或最终决定。
2. 待办事项尽量写成“- [ ] 事项｜负责人：…｜期限：…”，未明确就如实标注。
3. 合并重复内容，保留分歧与未决问题。
4. 每条已确认结论、待办、风险或待确认事项末尾附上材料中的证据标记，格式为
   `[证据 id=转写ID t=MM:SS]`。证据标记必须从输入材料中逐字复制，尤其是 id 和 t
   不得改写、翻译或替换成“待确认”；只有找不到明确证据时才使用 `[证据 待确认]`。
5. 长会议的分块事实笔记可能遗漏证据标记；如果输入末尾提供了“完整证据目录”，
   必须优先从目录中选择与事实对应的原始标记，不要凭空编造 id 或时间。
6. 不要输出代码块，不要解释生成过程。"""

REQUIRED_HEADINGS = (
    "## 一句话摘要",
    "## 已确认结论",
    "## 待办事项",
    "## 需求与约束",
    "## 风险与待确认",
)


class MinutesGenerationError(RuntimeError):
    """让 Electron 收到可展示、可落库的纪要失败诊断。"""
MINUTES_LLM_RETRY_ATTEMPTS = 2
EVIDENCE_CATALOG_MAX_CHARS = 48_000


PART_SYSTEM = """你是严谨的会议记录员。请把这一部分会议转写压缩成事实笔记。
只记录明确出现的内容，不推断负责人、期限、数字或结论；不确定就写“未明确”。
保留：讨论主题、已确认结论、待办、需求/约束、风险和待确认问题。使用简洁中文。
每条事实后必须保留输入中对应的完整证据标记，原样复制
`[证据 id=转写ID t=MM:SS]`，不得改写 id 或时间；合并多条事实时可以附多个证据标记。
没有明确支持材料时才写 `[证据 待确认]`，不要为了省字删掉证据标记。"""

FINAL_SYSTEM = """你是严谨的产品需求会议记录员。请根据提供的会议事实笔记生成 Markdown 纪要。
必须使用以下结构：
# 会议纪要
## 一句话摘要
## 已确认结论
## 待办事项
## 需求与约束
## 风险与待确认

规则：
1. 只能写材料中明确出现的事实，不补造负责人、期限、数字、客户立场或最终决定。
2. 待办事项尽量写成“- [ ] 事项｜负责人：…｜期限：…”，未明确就如实标注。
3. 合并重复内容，保留分歧与未决问题。
4. 每条已确认结论、待办、风险或待确认事项末尾附上材料中的证据标记，格式为
   `[证据 id=转写ID t=MM:SS]`。证据标记必须从输入材料中逐字复制，尤其是 id 和 t
   不得改写、翻译或替换成“待确认”；只有找不到明确证据时才使用 `[证据 待确认]`。
5. 长会议的分块事实笔记可能遗漏证据标记；如果输入末尾提供了“完整证据目录”，
   必须优先从目录中选择与事实对应的原始标记，不要凭空编造 id 或时间。
6. 不要输出代码块，不要解释生成过程。"""

REQUIRED_HEADINGS = (
    "## 一句话摘要",
    "## 已确认结论",
    "## 待办事项",
    "## 需求与约束",
    "## 风险与待确认",
)


class MinutesGenerationError(RuntimeError):
    """让 Electron 收到可展示、可落库的纪要失败诊断。"""

    def __init__(self, message, diagnostic):
        self.diagnostic = diagnostic
        super().__init__(message)


def normalize_evidence_tags(text: str) -> str:
    """把模型产出的各种多层嵌套、逗号数组格式的证据标记规范化为标准单层空格分隔格式。"""
    s = str(text or "").replace("［", "[").replace("］", "]").replace("【", "[").replace("】", "]")

    def _clean_evidence_block(m):
        block = m.group(0)
        items = re.findall(r"\[证据\s+[^\]]+\]", block)
        if items:
            return " " + " ".join(items)
        return block

    # 匹配外层包裹了多个证据或带逗号的证据组，例如 [[证据 ...], [证据 ...]]
    s = re.sub(r"\[\s*(?:\[证据\s+[^\]]+\]\s*[,，、]?\s*)+\]", _clean_evidence_block, s)
    # 单独的 [[证据 ...]] 双层括号
    s = re.sub(r"\[\s*(\[证据\s+[^\]]+\])\s*\]", r"\1", s)
    return s


def ensure_minutes_sections(content):
    """Keep the promised structure even when a model omits an empty section."""
    text = str(content or "").strip()
    # 讯飞部分模型会把 Markdown 换行写成字面量 <ret>，否则正文会整段
    # 粘在一起，且下面的章节检查也无法识别「## 已确认结论」等标题。
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)<ret\s*/?>", "\n", text)
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n")
    text = re.sub(r"\r\n?", "\n", text)
    if text.startswith("```") and text.endswith("```"):
        text = text.strip("`").removeprefix("markdown").strip()
    if re.match(r"^会议纪要\s*\n", text):
        text = re.sub(r"^会议纪要\s*\n", "# 会议纪要\n", text, count=1)
    elif not text.startswith("# 会议纪要"):
        text = "# 会议纪要\n\n" + text
    size = 0
    for line in lines:
        if current and size + len(line) + 1 > limit:
            chunks.append("\n".join(current))
            current = []
            size = 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def build_evidence_catalog(lines, max_chars=EVIDENCE_CATALOG_MAX_CHARS):
    """为长会议最终汇总提供一份可复制的原始证据目录。

    正常情况下保留完整转写行；极长输入则缩短每条证据正文，但始终保留
    ``id + t``，避免最终模型只能看到没有时间锚点的分块摘要。
    """
    source = [str(line).strip() for line in lines if str(line).strip()]
    full = "\n".join(source)
    if len(full) <= max_chars:
        return full

    compact = []
    for line in source:
        marker, separator, text = line.partition("] ")
        if not separator:
            compact.append(line)
            continue
        snippet = text.strip()
        if len(snippet) > 72:
            snippet = snippet[:71].rstrip() + "…"
        compact.append(f"{marker}] {snippet}")
    compact_text = "\n".join(compact)
    if len(compact_text) <= max_chars:
        return compact_text

    # 最后的保底仍保留每条证据的 canonical id/t，不能让模型自行猜时间。
    marker_lines = []
    for line in source:
        marker, separator, _ = line.partition("] ")
        marker_lines.append(marker + ("]" if separator else ""))
    return "\n".join(marker_lines)


def format_evidence_time(at, started_at):
    try:
        total_seconds = max(0.0, (float(at or started_at) - started_at) / 1000.0)
    except (TypeError, ValueError):
        total_seconds = 0.0
    whole_seconds = int(total_seconds)
    return f"{whole_seconds // 60:02d}:{whole_seconds % 60:02d}"


EVIDENCE_MARKER_RE = re.compile(
    r"\[证据\s+id=(?P<id>[^\]\s]+)(?:\s+t=[^\]]*)?\]"
)
CANONICAL_EVIDENCE_MARKER_RE = re.compile(
    r"\[证据\s+id=[^\]\s]+\s+t=\d{2}:\d{2}\]"
)


def normalize_evidence_markers(content, transcript, started_at):
    """Restore canonical evidence times after a model has rewritten Markdown.

    The model may keep a valid transcript id but replace its time with
    ``待确认`` (or omit the time altogether). The transcript is the source of
    truth, so known ids are rewritten deterministically and unknown ids are
    downgraded to an explicit pending marker.
    """
    evidence_by_id = {}
    for item in transcript or []:
        if not isinstance(item, dict) or not item.get("isFinal", True):
            continue
        transcript_id = str(item.get("id") or "").strip()
        if not transcript_id or not str(item.get("text") or "").strip():
            continue
        evidence_by_id[transcript_id] = item

    def replace(match):
        transcript_id = match.group("id")
        item = evidence_by_id.get(transcript_id)
        if not item:
            return "[证据 待确认]"
        timestamp = format_evidence_time(item.get("at"), started_at)
        return f"[证据 id={transcript_id} t={timestamp}]"

    return EVIDENCE_MARKER_RE.sub(replace, str(content or ""))


def evidence_marker_stats(content):
    text = str(content or "")
    return {
        "evidenceMarkerCount": len(CANONICAL_EVIDENCE_MARKER_RE.findall(text)),
        "pendingEvidenceCount": text.count("[证据 待确认]"),
    }


def build_final_prompt(title, memory_lines, facts, evidence_catalog=""):
    """构造最终汇总输入，确保长会的完整证据目录不会被漏接。"""
    return (
        f"会议标题：{title}\n\n"
        f"【已确认会议记忆】\n{memory_lines}\n\n"
        f"【会议事实材料】\n{facts}"
        f"{evidence_catalog}\n\n"
        "请确保每条结论、待办、风险和待确认事项都带有可回溯证据标记。"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--provider")
    parser.add_argument("--model")
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    transcript = payload.get("transcript") or []
    title = str(payload.get("title") or "未命名会议")
    scene = normalize_scene(payload.get("scene"))
    scene_meta = scene_config(scene)
    confirmed_memory = [
        item
        for item in (payload.get("memoryItems") or [])
        if isinstance(item, dict) and item.get("status") == "confirmed"
    ]
    started_at = float(payload.get("startedAt") or 0)
    lines = [
        (
            f"[证据 id={item.get('id') or 'unknown'} "
            f"t={format_evidence_time(item.get('at'), started_at)}] "
            f"[{item.get('speaker') or '未知说话人'}] "
            f"{str(item.get('text') or '').strip()}"
        )
        for item in transcript
        if item.get("isFinal", True) and str(item.get("text") or "").strip()
    ]
    if not lines:
        raise ValueError("会议没有可用于生成纪要的最终转写")

    started = time.time()
    kb = providers.build_kb(verbose=False, doc_paths=[])
    engine = providers.build_llm(
        kb,
        me_name="我",
        provider=args.provider,
        model=args.model,
        scene=scene,
        timeout_seconds=MINUTES_LLM_TIMEOUT_SECONDS,
        retry_attempts=MINUTES_LLM_RETRY_ATTEMPTS,
    )
    chunks = chunk_lines(lines)
    evidence_catalog = ""
    if len(chunks) > 1:
        evidence_catalog = (
            "\n\n【完整证据目录】\n"
            "以下内容直接来自原始最终转写，只用于选择证据标记；请复制其中的 id 和 t：\n"
            f"{build_evidence_catalog(lines)}"
        )
    generation_stage = "facts"
    try:
        if len(chunks) == 1:
            facts = chunks[0]
        else:
            partials = []
            for index, chunk in enumerate(chunks, 1):
                partials.append(
                    engine._call(
                        PART_SYSTEM,
                        f"会议：{title}\n这是第 {index}/{len(chunks)} 部分：\n\n{chunk}",
                    )
                )
            facts = "\n\n".join(
                f"【部分 {index}】\n{text}"
                for index, text in enumerate(partials, 1)
            )
        memory_lines = "\n".join(
            f"- {item.get('content', '').strip()}"
            f" [证据 id={item.get('evidenceTranscriptId') or 'unknown'}]"
            for item in confirmed_memory
            if str(item.get("content") or "").strip()
        ) or "- 暂无已确认记忆"
        scene_guidance = (
            f"\n\n本场场景：{scene_meta['label']}。纪要重点关注："
            f"{'、'.join(scene_meta['minutes'])}。"
            "已确认记忆优先落入对应章节，但仍必须以事实材料中的证据为准。"
        )
        generation_stage = "final"
        content = engine._call(
            FINAL_SYSTEM + scene_guidance,
            build_final_prompt(title, memory_lines, facts, evidence_catalog),
        ).strip()
    except Exception as exc:
        diagnostic = llm_error_details(
            exc,
            provider=getattr(engine, "provider", args.provider),
            model=getattr(engine, "model", args.model),
            timeout_seconds=MINUTES_LLM_TIMEOUT_SECONDS,
            stage=generation_stage,
        )
        raise MinutesGenerationError(
            format_llm_error(diagnostic, "会议纪要服务"),
            diagnostic,
        ) from exc
    content = normalize_evidence_markers(
        ensure_minutes_sections(content), transcript, started_at
    )
    evidence_stats = evidence_marker_stats(content)
    result = {
        "ok": True,
        "content": content,
        "elapsedSec": round(time.time() - started, 1),
        "chunks": len(chunks),
        "provider": getattr(engine, "provider", args.provider),
        "model": getattr(engine, "model", args.model),
        "timeoutSeconds": MINUTES_LLM_TIMEOUT_SECONDS,
        "retryAttempts": MINUTES_LLM_RETRY_ATTEMPTS,
        **evidence_stats,
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except MinutesGenerationError as exc:
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
        sys.exit(1)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        sys.exit(1)
