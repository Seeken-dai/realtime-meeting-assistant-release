"""
基于真实录音对话切片验证 4 种预设人设（Tone）的建议生成效果。
"""

import json
import sys
from pathlib import Path

# 添加 poc 目录到 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers import build_kb, build_llm
from suggest import TONE_CONFIGS


# 真实切片 1：交接沟通中关于“模块新增上线报价器、接口人与定价流程”
TRANSCRIPT_SNIPPET_1 = [
    {
        "speaker": "我",
        "text": "然后你就要在这个评审会之前先跟开发经理去对齐所有的需求工作量，然后以及这些需求的具体输出都要在这个之前完成？",
    },
    {
        "speaker": "孙丝语",
        "text": "不，你也可以输一个大概，它能评出来工作量，你后面再去做细化也可以。那你前面要让它能评估出来工作量，然后基本上能卡的需求要尽量卡完，不能说列出来的东西超出范围很远，因为他们会评估完成率。",
    },
    {
        "speaker": "孙丝语",
        "text": "然后再一部分，就是你要新增的模块之类你后面要上报价，所以你有模块的新增或者变动之后，要上到报价器上面。目前的报价设计基本上是希望EKD跟MK一致嘛，你可以去参考那个价格对标一下竞品，确认没问题了之后再拉群让刘平上到在线报价器里面，同时product平台找蔡军把模块更新上去。",
    },
]

# 真实切片 2：需求评审中关于“系统集成与接口定制范围”
TRANSCRIPT_SNIPPET_2 = [
    {
        "speaker": "我",
        "text": "关于这次客户提到的第三方系统单点登录和组织架构同步，我们当前的集成组件能直接覆盖吗？",
    },
    {
        "speaker": "对方客户/业务",
        "text": "他们那边的OA是老版本泛微，认证协议不是标准的OAuth2或者SAML，而且用户表里有三万多人，希望每10分钟全量同步一次，如果接口超时你们怎么处理？这个算在标准交付范围里还是需要另外收定制费？",
    },
]


def run_evaluation():
    print("正在构建本地知识库索引...")
    from knowledge_base import KnowledgeBase
    kb = KnowledgeBase(
        docs_dir=str(Path(__file__).resolve().parent.parent / "docs"),
        backend="local",
        verbose=False,
    ).build()

    tones = ["direct", "business", "challenger", "collaborative"]
    results = {}

    cases = [
        {"id": "case_1_handover", "name": "真实会议交接切片（报价器与上线流程）", "transcript": TRANSCRIPT_SNIPPET_1, "scene": "general"},
        {"id": "case_2_requirements", "name": "需求边界与定制范围切片（非标协议与同步）", "transcript": TRANSCRIPT_SNIPPET_2, "scene": "requirements"},
    ]

    for case in cases:
        print(f"\n=======================================================")
        print(f"测试切片: {case['name']} (Scene: {case['scene']})")
        print("-------------------------------------------------------")
        last_speaker = case["transcript"][-1]["speaker"]
        last_text = case["transcript"][-1]["text"]
        print(f"对方最后发言 [{last_speaker}]: {last_text}")
        print("=======================================================")

        case_results = {}

        for tone in tones:
            meta = TONE_CONFIGS[tone]
            print(f"\n>>> 正在测试人设: [{meta['label']}] (Tone: {tone}) ...")
            try:
                engine = build_llm(
                    kb=kb,
                    me_name="我",
                    scene=case["scene"],
                    tone=tone,
                    timeout_seconds=20.0,
                )
                output = engine.suggest(case["transcript"], count=3)
                suggestions = output.get("suggestions", [])
                case_results[tone] = {
                    "label": meta["label"],
                    "suggestions": suggestions,
                    "error": output.get("error"),
                }
                for i, s in enumerate(suggestions, 1):
                    intent = s.get("intent", "无意图")
                    script = s.get("script", "")
                    category = s.get("category", "")
                    stype = s.get("type", "")
                    print(f"  [{i}] 【{category}·{intent}】({stype})")
                    print(f"      话术: \"{script}\"")
            except Exception as e:
                print(f"  [ERROR] 生成失败: {e}")
                case_results[tone] = {"label": meta["label"], "error": str(e)}

        results[case["id"]] = case_results

    out_file = Path(__file__).resolve().parent / "reports" / "tone_eval_results.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n评估完成！结果已写入: {out_file}")


if __name__ == "__main__":
    run_evaluation()
