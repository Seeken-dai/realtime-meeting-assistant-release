"""
ASR 热词 / 专有名词同步（当前优先阿里 Paraformer）。

设计：
  - 本地 SQLite 是词表源；本模块只负责「开会前」把合并后的词同步到云端。
  - 阿里账号热词列表上限约 10 个：固定复用 prefix=mchot，避免每次 create 占坑。
  - 同步失败不阻断开会，只返回 None 并打日志。
"""

from __future__ import annotations

import json
import re
from pathlib import Path


LAST_SYNC_DIAGNOSTIC: str | None = None


def _safe_diagnostic(value: object) -> str:
    """保留排障信息，但不要把凭证或整段异常响应带到会议记录里。"""
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    text = re.sub(
        r"(?i)(api[_ -]?key|access[_ -]?key|secret|token|password)\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        text,
    )
    return re.sub(r"\s+", " ", text).strip()[:300]


def _is_allocation_quota_error(value: object) -> bool:
    """识别阿里热词接口的额度/限流错误，避免继续创建新词表。"""
    text = str(value or "").lower()
    return any(
        marker in text
        for marker in (
            "allocationquota",
            "free allocated quota exceeded",
            "throttling",
        )
    )

def load_hotwords_file(path: str | None) -> list[dict]:
    if not path:
        return []
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[热词] 读取失败：{exc}", flush=True)
        return []
    items = raw.get("terms") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    out = []
    seen = set()
    for item in items:
        if isinstance(item, str):
            text, weight = item, 4
        elif isinstance(item, dict):
            text = str(item.get("text") or item.get("term") or "").strip()
            weight = int(item.get("weight") or 4)
        else:
            continue
        text = re.sub(r"\s+", " ", text).strip()
        if not text or text in seen:
            continue
        # Paraformer：含非 ASCII 合计 ≤15；纯英文按空格切分后片段数也有限
        if len(text) > 15:
            text = text[:15]
        weight = max(1, min(5, weight))
        seen.add(text)
        out.append({"text": text, "weight": weight})
        if len(out) >= 500:
            break
    return out


def ensure_aliyun_vocabulary_id(
    terms: list[dict],
    api_key: str,
    target_model: str = "paraformer-realtime-v2",
    prefix: str = "mchot",
) -> str | None:
    """创建或更新固定列表，返回 vocabulary_id；失败返回 None。"""
    global LAST_SYNC_DIAGNOSTIC
    LAST_SYNC_DIAGNOSTIC = None
    if not terms or not api_key:
        if terms and not api_key:
            LAST_SYNC_DIAGNOSTIC = "阿里 ASR 凭证为空"
        return None
    try:
        import dashscope
        from dashscope.audio.asr import VocabularyService
    except Exception as exc:
        LAST_SYNC_DIAGNOSTIC = _safe_diagnostic(f"SDK 不可用：{exc}")
        print(f"[热词] dashscope 不可用：{exc}", flush=True)
        return None

    # DashScope 只接受英文字母和数字。兼容旧调用方传入的连字符、空格等，
    # 避免热词失败后整场会议悄悄降级为无热词转写。
    prefix = re.sub(r"[^A-Za-z0-9]", "", str(prefix or "")) or "mchot"

    dashscope.api_key = api_key
    service = VocabularyService()
    vocabulary = [
        {"text": t["text"], "weight": int(t.get("weight") or 4)}
        for t in terms
        if t.get("text")
    ]
    if not vocabulary:
        return None

    try:
        # 复用本应用固定 prefix 的列表，避免占满 10 个配额
        existing_id = None
        try:
            listed = service.list_vocabularies() or []
            # SDK 可能返回 list / dict
            rows = listed if isinstance(listed, list) else (
                listed.get("vocabularies")
                or listed.get("data")
                or listed.get("output")
                or []
            )
            if isinstance(rows, dict):
                rows = rows.get("vocabularies") or rows.get("list") or []
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                vid = row.get("vocabulary_id") or row.get("vocabularyId") or row.get("id")
                pfx = str(row.get("prefix") or row.get("name") or "")
                model = str(row.get("target_model") or row.get("targetModel") or "")
                if vid and (prefix in pfx or pfx.startswith(prefix)):
                    if not model or model == target_model:
                        existing_id = str(vid)
                        break
        except Exception as exc:
            diagnostic = _safe_diagnostic(f"{type(exc).__name__}: {exc}")
            if _is_allocation_quota_error(exc):
                LAST_SYNC_DIAGNOSTIC = f"阿里热词额度受限：{diagnostic}"
                print(
                    f"[热词] 列举词表受限，停止新建以免继续触发配额：{diagnostic}",
                    flush=True,
                )
                return None
            print(f"[热词] 列举词表失败，将尝试新建：{diagnostic}", flush=True)

        if existing_id:
            try:
                try:
                    service.update_vocabulary(
                        vocabulary_id=existing_id,
                        vocabulary=vocabulary,
                    )
                except TypeError:
                    # 部分 SDK 签名为 (vocabulary_id, vocabulary)
                    service.update_vocabulary(existing_id, vocabulary)
                print(
                    f"[热词] 已更新阿里词表 {existing_id}（{len(vocabulary)} 词）",
                    flush=True,
                )
                return existing_id
            except Exception as exc:
                # 更新失败时不能删除已有词表再重建：删除/创建本身会消耗配额，
                # 在 429 场景下还会把一个本来可用的旧词表变成彻底无词表。
                # 保留已有 ID 继续开会，并把原因交给界面显示。
                diagnostic = _safe_diagnostic(f"{type(exc).__name__}: {exc}")
                LAST_SYNC_DIAGNOSTIC = (
                    f"阿里词表更新失败，继续复用已有词表：{diagnostic}"
                )
                print(f"[热词] {LAST_SYNC_DIAGNOSTIC}", flush=True)
                return existing_id

        result = service.create_vocabulary(
            prefix=prefix,
            target_model=target_model,
            vocabulary=vocabulary,
        )
        # 返回可能是 str 或对象
        if isinstance(result, str):
            vocabulary_id = result
        elif isinstance(result, dict):
            vocabulary_id = (
                result.get("vocabulary_id")
                or result.get("vocabularyId")
                or result.get("output", {}).get("vocabulary_id")
            )
        else:
            vocabulary_id = (
                getattr(result, "vocabulary_id", None)
                or getattr(result, "vocabularyId", None)
            )
        if not vocabulary_id:
            print(f"[热词] 创建成功但未解析到 id：{result!r}", flush=True)
            return None
        print(
            f"[热词] 已创建阿里词表 {vocabulary_id}（{len(vocabulary)} 词）",
            flush=True,
        )
        return str(vocabulary_id)
    except Exception as exc:
        LAST_SYNC_DIAGNOSTIC = _safe_diagnostic(f"{type(exc).__name__}: {exc}")
        print(
            f"[热词] 同步阿里失败（将不带热词继续）：{LAST_SYNC_DIAGNOSTIC}",
            flush=True,
        )
        return None
