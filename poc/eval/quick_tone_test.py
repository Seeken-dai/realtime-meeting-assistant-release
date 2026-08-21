import sys
from pathlib import Path

# 添加 poc 目录到 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from knowledge_base import KnowledgeBase
from providers import build_llm

print("1. 构建知识库...", flush=True)
kb = KnowledgeBase(docs_dir=str(Path(__file__).resolve().parent.parent / "docs"), backend="local", verbose=True).build()
print(f"   知识库就绪，共 {len(kb.chunks)} 个片段", flush=True)

transcript = [
    {"speaker": "我", "text": "新模块上线报价器流程是怎样的？"},
    {"speaker": "孙丝语", "text": "跟刘平确认价格描述后拉群上报价器，product平台找蔡军更新上去。"}
]

for tone in ["direct", "business", "challenger", "collaborative"]:
    print(f"\n2. 测试 Tone: {tone} ...", flush=True)
    engine = build_llm(kb=kb, me_name="我", scene="general", tone=tone, timeout_seconds=12.0)
    print("   调用 LLM suggest ...", flush=True)
    res = engine.suggest(transcript, count=2)
    print(f"   返回结果: {len(res.get('suggestions', []))} 条", flush=True)
    for s in res.get("suggestions", []):
        print(f"   - 【{s.get('category')}·{s.get('intent')}】: {s.get('script')}", flush=True)
