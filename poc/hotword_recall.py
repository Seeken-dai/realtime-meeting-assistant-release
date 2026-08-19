"""
G6 热词真实召回：纯本地评分（不调用 ASR / 云端）。

用途：
  - 会后把转写全文与「本场应说出的专名」对照，算每词命中与整体召回。
  - 支持英文大小写折叠、中文连续子串、以及可选 aliases（如 MK / mk 合同）。
  - 报告只写统计与命中片段前后若干字，避免整场敏感正文入库。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


_WS_RE = re.compile(r"\s+")
_MD_SPEAKER_LINE_RE = re.compile(
    r"^(?:\[[^\]]+\]\s*)?(?P<speaker>[^:：]{1,40})[:：]\s*(?P<text>.+)$"
)


def normalize_surface(text: str) -> str:
    """折叠空白；英文保留原字符以便做大小写不敏感匹配。"""
    return _WS_RE.sub(" ", str(text or "")).strip()


def fold_for_match(text: str) -> str:
    """匹配用标准化：空白折叠 + 英文小写。中文保持原样。"""
    return normalize_surface(text).casefold()


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_terms(source: str | Path | list | dict) -> list[dict]:
    """
    接受：
      - 路径：JSON 文件（list 或 {"terms":[...]} 或 hotword targets 清单）
      - list[str|dict]
      - dict 含 terms / targets
    每项输出：{id, text, weight, aliases[], expected_mentions}
    """
    raw: Any
    if isinstance(source, (str, Path)):
        raw = load_json(source)
    else:
        raw = source

    if isinstance(raw, dict):
        items = (
            raw.get("terms")
            or raw.get("targets")
            or raw.get("hotwords")
            or []
        )
    elif isinstance(raw, list):
        items = raw
    else:
        items = []

    out: list[dict] = []
    seen = set()
    for index, item in enumerate(items):
        if isinstance(item, str):
            text = normalize_surface(item)
            aliases: list[str] = []
            weight = 4
            expected = 1
            term_id = text
        elif isinstance(item, dict):
            text = normalize_surface(
                item.get("text") or item.get("term") or item.get("name") or ""
            )
            aliases = [
                normalize_surface(a)
                for a in (item.get("aliases") or [])
                if normalize_surface(a)
            ]
            weight = int(item.get("weight") or 4)
            expected = max(0, int(item.get("expected_mentions") or item.get("expected") or 1))
            term_id = str(item.get("id") or text or f"term-{index}")
        else:
            continue
        if not text:
            continue
        key = fold_for_match(text)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "id": term_id,
                "text": text,
                "weight": max(1, min(5, weight)),
                "aliases": aliases,
                "expected_mentions": expected,
            }
        )
    return out


def extract_transcript_text(source: str | Path | dict | list) -> str:
    """
    从多种产物中抽出纯文本：
      - 纯文本 / Markdown 转写
      - 会议 record JSON（transcript / transcriptVersions）
      - 行列表 [{text}] 或字符串列表
    """
    if isinstance(source, dict):
        return _text_from_record(source)
    if isinstance(source, list):
        return _text_from_lines(source)

    path = Path(source)
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(raw)
        if isinstance(data, (dict, list)):
            return extract_transcript_text(data)
        return normalize_surface(str(data))
    return _text_from_markdown(raw)


def _text_from_markdown(raw: str) -> str:
    parts: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _MD_SPEAKER_LINE_RE.match(line)
        if match:
            parts.append(match.group("text").strip())
        else:
            # 去掉常见时间戳前缀 [00:12]
            parts.append(re.sub(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s*", "", line))
    return normalize_surface("\n".join(parts))


def _text_from_lines(lines: Iterable[Any]) -> str:
    parts: list[str] = []
    for item in lines:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text") or item.get("content") or ""
            if text:
                parts.append(str(text))
    return normalize_surface("\n".join(parts))


def _text_from_record(record: dict) -> str:
    if isinstance(record.get("transcript"), list):
        text = _text_from_lines(record["transcript"])
        if text:
            return text
    versions = record.get("transcriptVersions") or record.get("transcript_versions")
    if isinstance(versions, dict):
        preferred = (
            record.get("transcriptVersion")
            or record.get("transcript_version")
            or "offline"
        )
        for key in (preferred, "offline", "realtime", "live"):
            version = versions.get(key)
            if isinstance(version, dict) and isinstance(version.get("transcript"), list):
                text = _text_from_lines(version["transcript"])
                if text:
                    return text
            if isinstance(version, list):
                text = _text_from_lines(version)
                if text:
                    return text
    # 兼容 { "lines": [...] } / { "text": "..." }
    if isinstance(record.get("lines"), list):
        return _text_from_lines(record["lines"])
    if isinstance(record.get("text"), str):
        return normalize_surface(record["text"])
    if isinstance(record.get("content"), str):
        return normalize_surface(record["content"])
    return ""


def _surface_patterns(term: str, aliases: list[str]) -> list[str]:
    surfaces = [term, *aliases]
    # 去重且保序
    seen = set()
    out = []
    for surface in surfaces:
        key = fold_for_match(surface)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(surface)
    return out


def count_mentions(transcript: str, term: str, aliases: list[str] | None = None) -> dict:
    """在全文中统计 term + aliases 的出现次数，并截取最多 3 个短上下文。"""
    haystack = fold_for_match(transcript)
    original = normalize_surface(transcript)
    hits = 0
    contexts: list[str] = []
    for surface in _surface_patterns(term, aliases or []):
        needle = fold_for_match(surface)
        if not needle:
            continue
        start = 0
        while True:
            index = haystack.find(needle, start)
            if index < 0:
                break
            hits += 1
            if len(contexts) < 3:
                # 用原始串大致同位置截取（casefold 不改变多数中文长度）
                left = max(0, index - 12)
                right = min(len(original), index + len(surface) + 12)
                snippet = original[left:right]
                contexts.append(snippet)
            start = index + max(1, len(needle))
    return {"count": hits, "contexts": contexts}


@dataclass
class TermScore:
    id: str
    text: str
    expected_mentions: int
    found_mentions: int
    hit: bool
    aliases: list[str] = field(default_factory=list)
    contexts: list[str] = field(default_factory=list)
    weight: int = 4

    @property
    def coverage(self) -> float:
        if self.expected_mentions <= 0:
            return 1.0 if self.found_mentions > 0 else 0.0
        return min(1.0, self.found_mentions / self.expected_mentions)


@dataclass
class RecallReport:
    term_count: int
    hit_count: int
    miss_count: int
    term_recall: float
    mention_recall: float
    expected_mentions_total: int
    found_mentions_total: int
    terms: list[TermScore]
    misses: list[str]
    transcript_chars: int
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["terms"] = [asdict(t) for t in self.terms]
        return payload


def score_hotword_recall(
    transcript: str,
    terms: list[dict],
    *,
    min_found_for_hit: int = 1,
) -> RecallReport:
    """
    term_recall：至少命中一次的专名占比。
    mention_recall：实际命中次数 / 期望说出次数（封顶 1.0 按词累计）。
    """
    text = normalize_surface(transcript)
    scores: list[TermScore] = []
    expected_total = 0
    found_capped_total = 0.0
    notes: list[str] = []

    if not text:
        notes.append("转写正文为空，无法计算召回")
    if not terms:
        notes.append("目标专名为空")

    for term in terms:
        expected = max(0, int(term.get("expected_mentions") or 1))
        mention = count_mentions(text, term["text"], term.get("aliases") or [])
        found = int(mention["count"])
        hit = found >= max(1, min_found_for_hit)
        scores.append(
            TermScore(
                id=str(term.get("id") or term["text"]),
                text=term["text"],
                expected_mentions=expected,
                found_mentions=found,
                hit=hit,
                aliases=list(term.get("aliases") or []),
                contexts=list(mention["contexts"]),
                weight=int(term.get("weight") or 4),
            )
        )
        expected_total += expected
        if expected > 0:
            found_capped_total += min(found, expected)
        elif found > 0:
            found_capped_total += 1.0

    hit_count = sum(1 for s in scores if s.hit)
    term_count = len(scores)
    term_recall = (hit_count / term_count) if term_count else 0.0
    mention_recall = (
        (found_capped_total / expected_total) if expected_total else term_recall
    )
    misses = [s.text for s in scores if not s.hit]

    return RecallReport(
        term_count=term_count,
        hit_count=hit_count,
        miss_count=term_count - hit_count,
        term_recall=round(term_recall, 4),
        mention_recall=round(float(mention_recall), 4),
        expected_mentions_total=expected_total,
        found_mentions_total=sum(s.found_mentions for s in scores),
        terms=scores,
        misses=misses,
        transcript_chars=len(text),
        notes=notes,
    )


def pass_criteria(
    report: RecallReport,
    *,
    min_term_recall: float = 1.0,
    required_terms: list[str] | None = None,
) -> tuple[bool, list[str]]:
    """默认要求核心专名全中；可放宽 min_term_recall。"""
    reasons: list[str] = []
    if report.term_count == 0:
        return False, ["无目标专名"]
    if report.term_recall + 1e-9 < min_term_recall:
        reasons.append(
            f"term_recall={report.term_recall:.0%} < 阈值 {min_term_recall:.0%}"
        )
    if required_terms:
        hit_set = {fold_for_match(t.text) for t in report.terms if t.hit}
        for name in required_terms:
            if fold_for_match(name) not in hit_set:
                reasons.append(f"必中专名未命中：{name}")
    return (len(reasons) == 0), reasons


def render_markdown_report(
    report: RecallReport,
    *,
    title: str = "G6 热词召回对照",
    meta: dict | None = None,
) -> str:
    lines = [f"# {title}", ""]
    if meta:
        lines.append("## 元信息")
        for key, value in meta.items():
            if value is None or value == "":
                continue
            lines.append(f"- **{key}**：{value}")
        lines.append("")
    lines.extend(
        [
            "## 汇总",
            f"- 专名数：{report.term_count}",
            f"- 命中：{report.hit_count} / 未命中：{report.miss_count}",
            f"- 词级召回 term_recall：{report.term_recall:.0%}",
            f"- 提及召回 mention_recall：{report.mention_recall:.0%}",
            f"- 转写字符数：{report.transcript_chars}",
            "",
            "## 分词明细",
            "| 专名 | 期望 | 命中次数 | 结果 | 上下文摘录 |",
            "|---|---:|---:|---|---|",
        ]
    )
    for term in report.terms:
        ctx = " / ".join(term.contexts) if term.contexts else "—"
        ctx = ctx.replace("|", "\\|")
        lines.append(
            f"| {term.text} | {term.expected_mentions} | {term.found_mentions} | "
            f"{'✓' if term.hit else '✗'} | {ctx} |"
        )
    if report.misses:
        lines.extend(["", "## 未命中", ", ".join(report.misses)])
    if report.notes:
        lines.extend(["", "## 备注"])
        lines.extend(f"- {n}" for n in report.notes)
    lines.append("")
    return "\n".join(lines)
