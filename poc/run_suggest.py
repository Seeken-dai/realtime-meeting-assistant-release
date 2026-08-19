"""
话术建议 POC —— 离线验证入口（M1 第二部分）。

用预设的会议片段验证建议质量，无需开麦克风，便于快速迭代提示词。

用法：
    python run_suggest.py                 # 跑全部预设场景
    python run_suggest.py --case 2        # 只跑第 2 个场景
    python run_suggest.py --ask "问题"     # 手动提问模式
    python run_suggest.py --interactive   # 交互式提问

验收关注点：
    ① 建议是否站在"我"的立场（而不是中立复述）
    ② 是否识别出对方话里的风险/陷阱
    ③ 引用的依据是否真实存在于知识库（有没有编造）
    ④ 知识库没有的内容，是否如实说明而非杜撰
    ⑤ 延迟是否可接受（PRD 目标 < 5s）
"""

import argparse
import sys
import time

import providers

ME = "我"

# ── 预设验证场景（贴近真实需求评审/客户澄清会）──────────────
CASES = [
    {
        "name": "客户试探定制边界",
        "transcript": [
            {"speaker": ME, "text": "张总，这次主要想跟您确认一下审批这块的需求。"},
            {"speaker": "客户-张总", "text": "嗯，我们这边流程比较复杂。你们这个模块能不能支持自定义审批流？就是简单改一下，按金额分几档走不同的人。"},
        ],
    },
    {
        "name": "客户要求免费做定制",
        "transcript": [
            {"speaker": "客户-张总", "text": "我们现在用的是泛微的 OA，你们这个系统上线以后，审批的数据要能同步到我们 OA 的待办里去。"},
            {"speaker": ME, "text": "这个是需要做系统对接的。"},
            {"speaker": "客户-张总", "text": "对接应该不难吧？就调个接口的事。这个你们顺便就给做了呗，别单独收费了，我们采购那边不好走流程。"},
        ],
    },
    {
        "name": "客户追问工期和报价",
        "transcript": [
            {"speaker": "客户-张总", "text": "行，那这些需求你们大概要做多久？能给个报价吗？我们下周要上会。"},
        ],
    },
    {
        "name": "知识库无依据的问题（测试是否编造）",
        "transcript": [
            {"speaker": "客户-张总", "text": "你们这个系统支持区块链存证吗？我们审计部门要求所有审批记录上链。"},
        ],
    },
]


_BADGES = {
    "grounded": "\033[92m[✓ 有依据]\033[0m",
    "advisory": "\033[94m[◆ 经验建议·非知识库]\033[0m",
    "clarify": "\033[93m[⚠️ 无依据·仅澄清]\033[0m",
}


def _cited_preview(refs, hits, width=64):
    """展示被引用片段的实际内容摘要。

    ⚠️ 为什么必须显示原文而不只是文件名：模型会【引用真实文档、但内容是编的】。
    实测：知识库里只有"金额分级审批"案例，模型却说成"审计存证需求"案例，
    并引用了真实的《历史项目案例.md》—— 引用校验放行，内容却是假的。
    只显示文件名时用户无从判断；显示原文摘要，一眼就能看出对不上。
    """
    if not refs:
        return []
    lines = []
    for r in refs:
        # 同一文档可能命中多个片段，挑【有实质内容】的那个：
        # 仅含标题的片段对核对毫无帮助（实测出现过只显示文档名的情况）
        same = [h for h in hits if h["source"] == r]
        if not same:
            continue
        def _substance(h):
            head = (h.get("heading") or "").strip()
            body = " ".join(h["text"].split())
            return len(body) - len(head)
        chunk = max(same, key=_substance)
        head = (chunk.get("heading") or "").strip()
        body = " ".join(chunk["text"].split())
        if head and body.startswith("#"):
            body = body.lstrip("#").strip()
        lines.append(f"      \033[90m└ {r}"
                     + (f"·{head}" if head else "")
                     + f"：{body[:width]}…\033[0m")
    return lines


def print_suggestions(result, elapsed):
    for i, s in enumerate(result.get("suggestions", []), 1):
        # 兼容旧字段 grounded
        stype = s.get("type") or ("grounded" if s.get("grounded", True) else "clarify")
        badge = _BADGES.get(stype, stype)
        print(f"\n  \033[96m建议 {i}\033[0m {badge}")
        if s.get("_sensitive"):
            print(f"    \033[93m👁 含敏感内容，说之前请确认：{s['_sensitive']}\033[0m")
        if s.get("_downgraded"):
            print(f"    \033[91m⬇ 已自动降级：{s['_downgraded']}\033[0m")
        if s.get("_reclassified"):
            print(f"    \033[90m↔ {s['_reclassified']}\033[0m")
        print(f"    💡 意图/风险：{s.get('intent', '')}")
        print(f"    🗣️  建议话术：{s.get('script', '')}")
        refs = s.get("references") or []
        print(f"    📎 依据来源：{'、'.join(refs) if refs else '（无知识库依据）'}")
        # 显示引用片段的实际内容，便于一眼核对"引用是真的、但内容是不是编的"
        for line in _cited_preview(refs, result.get("hits", [])):
            print(line)
    print(f"\n  \033[90m耗时 {elapsed:.1f}s | 检索命中："
          f"{'、'.join(h['source'] for h in result['hits'])}\033[0m")


def main():
    parser = argparse.ArgumentParser(description="话术建议 POC - 离线验证")
    parser.add_argument("--case", type=int, help="只跑指定场景（1 开始）")
    parser.add_argument("--ask", help="手动提问模式")
    parser.add_argument("--interactive", action="store_true", help="交互式提问")
    parser.add_argument("--provider", help="临时指定建议模型供应商"
                                           "（覆盖 config.py 的 LLM_PROVIDER）")
    parser.add_argument("--model", help="临时指定模型名（如 gemini-3.5-flash-lite）；"
                                        "换模型前请用本脚本的 4 个场景过一遍防编造")
    args = parser.parse_args()

    kb = providers.build_kb()
    engine = providers.build_llm(kb, me_name=ME, provider=args.provider,
                                 model=args.model)
    print(f"  建议模型：{engine.label} / {engine.model}"
          f"　|　检索：{'本地关键词' if kb._local else '云端向量'}")

    # ── 手动提问模式 ──
    if args.ask or args.interactive:
        transcript = CASES[0]["transcript"]
        questions = [args.ask] if args.ask else []
        if args.interactive:
            print("\n交互式提问（输入 q 退出）。已加载场景1的会议上下文。")
        while True:
            if questions:
                q = questions.pop(0)
            elif args.interactive:
                q = input("\n你的提问 > ").strip()
                if q.lower() in ("q", "quit", "exit") or not q:
                    break
            else:
                break
            t0 = time.time()
            res = engine.answer(q, transcript)
            print(f"\n🤖 {res['answer']}")
            print(f"\033[90m   耗时 {time.time()-t0:.1f}s | 检索命中："
                  f"{'、'.join(h['source'] for h in res['hits'])}\033[0m")
        return

    # ── 场景验证模式 ──
    cases = [CASES[args.case - 1]] if args.case else CASES
    for idx, case in enumerate(cases, 1):
        num = args.case if args.case else idx
        print(f"\n{'='*66}")
        print(f"场景 {num}：{case['name']}")
        print("=" * 66)
        for seg in case["transcript"]:
            tag = "【我】" if seg["speaker"] == ME else f"【{seg['speaker']}】"
            print(f"  {tag} {seg['text']}")
        t0 = time.time()
        result = engine.suggest(case["transcript"])
        print_suggestions(result, time.time() - t0)


if __name__ == "__main__":
    main()
