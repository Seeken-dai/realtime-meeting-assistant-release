"""
知识文档库（RAG）—— 加载 md/txt/docx/pdf、切片、向量化、语义检索。

POC 阶段用「内存向量 + 本地缓存」实现，不引入向量数据库依赖：
  - 切片：按 Markdown 标题分块，过长的块再按段落二次切分
  - 向量化：阿里云百炼 text-embedding
  - 检索：余弦相似度 Top-K

产品化时可替换为 sqlite-vec / LanceDB（见 PRD §6.2），检索接口保持不变。
"""

import hashlib
import json
import os
import re

import dashscope
import numpy as np
from document_extract import DocumentExtractionError, extract_document

_EMBED_MODEL = "text-embedding-v3"
_CACHE_FILE = ".kb_cache.json"   # 缓存向量，避免每次启动重复计费
_MAX_CHUNK_CHARS = 500
_BATCH = 10                      # embedding 接口单次批量上限

# 相关性门槛
#
# ⚠️ 实测结论：本地关键词检索【无法】用阈值可靠区分"相关/不相关"。
#    实测数据（覆盖率）：场景3报价(真相关)=0.038，场景4区块链(不相关)=0.039，
#    两者几乎相同 —— 口语化短句中大量填充词产生噪声，信号被淹没。
#    因此本地检索只过滤零分片段，把【相关性判断交给 LLM】（见 suggest.py 提示词：
#    明确告知片段来自关键词检索、可能无关，要求模型先判断再引用）。
#    向量检索的余弦相似度是可靠信号，保留阈值。
MIN_COVERAGE = 0.0     # 本地检索：不设门槛（阈值不可靠，理由见上）
MIN_COSINE = 0.35      # 向量检索：余弦相似度下限（可靠）


def _split_markdown(text, source):
    """按标题切块；过长的块按段落二次切分。返回 [{text, source, heading}]"""
    chunks = []
    # 按 ## / ### 标题切分，保留标题作为块的上下文
    parts = re.split(r"\n(?=#{1,6}\s)", text)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(#{1,6})\s*(.+)", part)
        heading = m.group(2).strip() if m else ""
        if len(part) <= _MAX_CHUNK_CHARS:
            chunks.append({"text": part, "source": source, "heading": heading})
            continue
        # 过长：按空行分段累积
        buf = []
        size = 0
        for para in part.split("\n\n"):
            if size + len(para) > _MAX_CHUNK_CHARS and buf:
                chunks.append({"text": "\n\n".join(buf), "source": source,
                               "heading": heading})
                buf, size = [], 0
            buf.append(para)
            size += len(para)
        if buf:
            chunks.append({"text": "\n\n".join(buf), "source": source,
                           "heading": heading})
    return chunks


def _embed(texts):
    """批量向量化，返回 np.array [n, dim]"""
    vectors = []
    for i in range(0, len(texts), _BATCH):
        batch = texts[i:i + _BATCH]
        resp = dashscope.TextEmbedding.call(model=_EMBED_MODEL, input=batch)
        if resp.status_code != 200:
            raise RuntimeError(f"向量化失败: {resp.code} {resp.message}")
        # 按 text_index 排序，确保顺序与输入一致
        items = sorted(resp.output["embeddings"], key=lambda x: x["text_index"])
        vectors.extend([it["embedding"] for it in items])
    return np.array(vectors, dtype=np.float32)


_FORBID_MARKERS = ["不可对外", "不得对外", "禁止对外", "不对外", "仅供内部",
                   "不可对客", "内部资料"]


def extract_forbidden_terms(text):
    """从知识文档中抽取【禁止对外提及的实体】。

    识别形如：
        以下案例不可对外提及客户名称，只能匿名描述行业：
        - 西南零售连锁、北方能源集团

    ⚠️ 为什么需要这个：POC 实测中，模型把知识库里明确标注"不可对外提及"的
    客户名直接写进了对客话术。仅靠提示词约束不可靠 —— 必须有程序化拦截。
    """
    terms = set()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not any(m in line for m in _FORBID_MARKERS):
            continue
        # 只从标记行【之后的列表项】中提取实体名。
        # 不解析标记行自身冒号后的内容 —— 那里通常是说明性散文，
        # 误抓会产生无意义的禁提词（实测曾抓出"本文档中的实际工作量"）。
        candidates = []
        for nxt in lines[i + 1:]:
            s = nxt.strip()
            if not s:
                continue
            if s.startswith(("-", "*", "•")):
                candidates.append(s.lstrip("-*• ").strip())
            else:
                break
        for c in candidates:
            # 去掉 markdown 强调符，按顿号/逗号切分
            c = c.replace("**", "").replace("`", "")
            for part in re.split(r"[、,，;；]", c):
                part = part.strip().strip("。.")
                # 过滤掉说明性文字，只保留像实体名的短词
                if 2 <= len(part) <= 20 and not any(
                        m in part for m in _FORBID_MARKERS):
                    terms.add(part)
    return terms


_INTERNAL_MARKERS = ["内部资料", "仅供内部", "内部参考", "不要在会议现场直接报价",
                     "对客话术需谨慎", "内部口径"]
# 内部敏感数值：人天、金额、折扣、百分比等成本类数据
_NUM_PATTERN = re.compile(
    r"\d+(?:\.\d+)?\s*(?:-\s*\d+(?:\.\d+)?\s*)?(?:人天|万元|元|％|%|折)")


def extract_internal_numbers(text):
    """从标记为【内部资料】的文档中抽取敏感数值（人天/金额/折扣）。

    ⚠️ 为什么需要：POC 实测中，模型把内部的参考工作量
    "单个外部系统对接 10-15 人天""22 人天"和单价"3000 元/人天"
    直接写进了对客话术 —— 而同一份文档明确写着"不要在会议现场直接报价"。
    这类数字是确定性事实，能精确匹配，不该靠模型自觉。

    仅当文档整体被标记为内部资料时才启用，避免误伤可对外的数字
    （如"标准实施周期 6-8 周"这类已获准的对外口径）。
    """
    if not any(m in text for m in _INTERNAL_MARKERS):
        return set()
    return {m.group().replace(" ", "") for m in _NUM_PATTERN.finditer(text)}


def _bigrams(text):
    """中文按字符二元组切分（无需分词库，对中文检索效果足够好）"""
    t = re.sub(r"\s+", "", text.lower())
    grams = [t[i:i + 2] for i in range(len(t) - 1)]
    # 英文/数字单词整体保留
    grams += re.findall(r"[a-z0-9]{2,}", text.lower())
    return grams


class LocalRetriever:
    """本地关键词检索（BM25 简化版）—— 零 API 依赖，零外部模型。

    用途：当 embedding 服务不可用（欠费/未开通）时的检索后备方案。
    对本 POC 这种小知识库（十几个片段），效果足以支撑建议质量验证。
    """

    def __init__(self, chunks):
        self.chunks = chunks
        self.docs = [_bigrams(c["text"]) for c in chunks]
        self.df = {}
        for d in self.docs:
            for g in set(d):
                self.df[g] = self.df.get(g, 0) + 1
        self.avg_len = sum(len(d) for d in self.docs) / max(len(self.docs), 1)
        self.N = len(self.docs)

    def _idf(self, gram):
        import math
        return math.log(1 + (self.N - self.df.get(gram, 0) + 0.5)
                        / (self.df.get(gram, 0) + 0.5))

    def search(self, query, top_k=4, min_coverage=MIN_COVERAGE):
        """检索。

        ⚠️ 关键：带【相关性门槛】。若最佳片段对查询中"有区分度的词"覆盖不足，
        返回空列表 —— 表示知识库确实没有相关内容。

        为什么必须有这个门槛：BM25 总会返回分数最高的若干片段，哪怕它们只是
        碰巧匹配了"系统""要求""记录"这类通用词。把这些无关片段喂给 LLM，
        等于在暗示"你有依据"，模型就会据此编造。POC 实测中，「区块链存证」
        这类知识库完全没有的问题，正是因此被误判为"有依据"。
        """
        q_terms = set(_bigrams(query))
        if not q_terms:
            return []
        # 用 IDF 加权：通用词（各片段都有）权重低，区分度高的词权重高
        q_idf = {g: self._idf(g) for g in q_terms}
        total_idf = sum(q_idf.values()) or 1e-9

        k1, b = 1.5, 0.75
        scored = []
        for i, doc in enumerate(self.docs):
            if not doc:
                continue
            tf = {}
            for g in doc:
                tf[g] = tf.get(g, 0) + 1
            score = 0.0
            matched_idf = 0.0
            for g in q_terms:
                if g not in tf:
                    continue
                f = tf[g]
                score += q_idf[g] * (f * (k1 + 1)) / (
                    f + k1 * (1 - b + b * len(doc) / self.avg_len))
                matched_idf += q_idf[g]
            if score > 0:
                scored.append((score, matched_idf / total_idf, i))

        if not scored:
            return []
        scored.sort(reverse=True)
        # 最佳片段的区分度覆盖不足 → 视为知识库无相关内容
        best_coverage = max(c for _, c, _ in scored)
        if best_coverage < min_coverage:
            return []
        return [{**self.chunks[i], "score": float(s), "coverage": float(c)}
                for s, c, i in scored[:top_k]]


class KnowledgeBase:
    def __init__(self, docs_dir="docs", api_key=None, verbose=True, backend="auto",
                 doc_paths=None):
        """
        backend: 'embedding' 用云端向量检索 / 'local' 用本地关键词检索 /
                 'auto' 优先向量，失败自动降级到本地

        doc_paths: 显式指定本次要加载的文档路径列表。
            ⚠️ 这是【知识范围隔离】的关键：给定后只加载这些文件，
            忽略 docs_dir。会议级/项目级作用域依赖它——检索绝不能
            访问本场未选中的资料，否则会串用其它项目的内容。
            为 None 时才回退到扫描 docs_dir（POC 命令行的默认行为）。
        """
        if api_key:
            dashscope.api_key = api_key
        self.docs_dir = docs_dir
        self.doc_paths = list(doc_paths) if doc_paths is not None else None
        self.verbose = verbose
        self.backend = backend
        self.chunks = []
        self.vectors = None
        self._local = None
        self.forbidden_terms = set()
        self.internal_numbers = set()
        self.missing_paths = []      # 路径引用失效的文档，供上层提示用户
        self.parse_errors = []       # 文件存在但无法解析；不应拖垮整场会议

    def _fingerprint(self):
        """所有文档内容的指纹，用于判断缓存是否失效"""
        h = hashlib.md5()
        for path in sorted(self._doc_paths()):
            h.update(path.encode("utf-8"))
            with open(path, "rb") as f:
                h.update(f.read())
        return h.hexdigest()

    def _doc_paths(self):
        # 显式指定时只用这些文件 —— 知识范围隔离的落点
        if self.doc_paths is not None:
            existing, missing = [], []
            for p in self.doc_paths:
                (existing if os.path.isfile(p) else missing).append(p)
            self.missing_paths = missing
            if missing and self.verbose:
                print(f"[知识库] ⚠️ {len(missing)} 份文档路径已失效："
                      f"{'、'.join(os.path.basename(m) for m in missing)}")
            return existing
        paths = []
        for root, _, files in os.walk(self.docs_dir):
            for fn in files:
                if fn.lower().endswith((".md", ".txt", ".docx", ".pdf")):
                    paths.append(os.path.join(root, fn))
        return paths

    def build(self):
        """加载并向量化全部文档（命中缓存时直接复用）"""
        paths = self._doc_paths()
        if not paths:
            if self.doc_paths is not None:
                # 本场会议未选任何文档（或选中的都失效）是合法状态：
                # 检索恒为空，建议将只能走 advisory/clarify，不会串用其它资料。
                self._local = LocalRetriever([])
                if self.verbose:
                    print("[知识库] 本场未加载任何文档，检索将始终为空")
                return self
            raise RuntimeError(
                f"知识库目录 {self.docs_dir} 下没有 md/txt/docx/pdf 文档"
            )

        fp = self._fingerprint()
        cache_path = os.path.join(self.docs_dir, _CACHE_FILE)

        # 先切片（本地检索也需要），同时抽取禁止对外提及的实体
        self.chunks = []
        self.forbidden_terms = set()
        self.internal_numbers = set()
        self.parse_errors = []
        for path in paths:
            try:
                content = extract_document(path)["text"]
            except DocumentExtractionError as exc:
                self.parse_errors.append({"path": path, "message": str(exc)})
                if self.verbose:
                    print(
                        f"[知识库] ⚠️ 跳过无法解析的文档 "
                        f"{os.path.basename(path)}：{exc}"
                    )
                continue
            source = os.path.basename(path)
            self.chunks.extend(_split_markdown(content, source))
            self.forbidden_terms |= extract_forbidden_terms(content)
            self.internal_numbers |= extract_internal_numbers(content)
        if not self.chunks:
            self._local = LocalRetriever([])
            if self.verbose:
                print("[知识库] 没有成功解析的文本片段，检索将始终为空")
            return self
        if self.verbose and (self.forbidden_terms or self.internal_numbers):
            print(f"[知识库] 已识别 {len(self.forbidden_terms)} 个禁提名称、"
                  f"{len(self.internal_numbers)} 项内部数值，将自动拦截")

        if self.backend == "local":
            self._local = LocalRetriever(self.chunks)
            if self.verbose:
                print(f"[知识库] 本地关键词检索，{len(self.chunks)} 个片段（无需 API）")
            return self

        # 尝试复用缓存的向量
        if os.path.exists(cache_path):
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
            if cache.get("fingerprint") == fp:
                self.chunks = cache["chunks"]
                self.vectors = np.array(cache["vectors"], dtype=np.float32)
                if self.verbose:
                    print(f"[知识库] 命中缓存，{len(self.chunks)} 个片段")
                return self

        if self.verbose:
            print(f"[知识库] {len(paths)} 个文档 → {len(self.chunks)} 个片段，向量化中…")
        try:
            self.vectors = _embed([c["text"] for c in self.chunks])
        except Exception as e:
            if self.backend == "embedding":
                raise
            # auto 模式：向量化失败（欠费/未开通）时降级到本地检索
            print(f"[知识库] ⚠️ 向量化不可用（{str(e)[:60]}…）")
            print(f"[知识库] → 自动降级为本地关键词检索")
            self._local = LocalRetriever(self.chunks)
            return self

        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"fingerprint": fp, "chunks": self.chunks,
                       "vectors": self.vectors.tolist()}, f, ensure_ascii=False)
        if self.verbose:
            print(f"[知识库] 就绪（已缓存，下次启动免重复计费）")
        return self

    def search(self, query, top_k=4):
        """检索，返回 [{text, source, heading, score}]"""
        if self._local is not None:
            return self._local.search(query, top_k=top_k)
        if self.vectors is None:
            raise RuntimeError("请先调用 build()")
        try:
            qv = _embed([query])[0]
        except Exception as e:
            # 查询时才欠费：就地降级，不中断会议
            print(f"[知识库] ⚠️ 查询向量化失败，降级为本地检索")
            self._local = LocalRetriever(self.chunks)
            return self._local.search(query, top_k=top_k)
        norms = np.linalg.norm(self.vectors, axis=1) * np.linalg.norm(qv)
        scores = (self.vectors @ qv) / np.maximum(norms, 1e-8)
        idx = np.argsort(-scores)[:top_k]
        # 同样施加相关性门槛（理由见 LocalRetriever.search）
        if len(idx) == 0 or scores[idx[0]] < MIN_COSINE:
            return []
        return [{**self.chunks[i], "score": float(scores[i])} for i in idx
                if scores[i] >= MIN_COSINE * 0.8]
