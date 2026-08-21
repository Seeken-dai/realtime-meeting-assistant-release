import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from knowledge_base import KnowledgeBase
from providers import build_llm

print("构建本地知识库...", flush=True)
kb = KnowledgeBase(docs_dir=str(Path(__file__).resolve().parent.parent / "docs"), backend="local", verbose=False).build()

transcript = [
    {
        "speaker": "我",
        "text": "关于这次客户提到的第三方系统单点登录和组织架构同步，我们当前的集成组件能直接覆盖吗？",
    },
    {
        "speaker": "对方客户",
        "text": "我们这边的OA是老版本泛微，认证协议不是标准OAuth2或者SAML，而且组织架构有三万多人，希望每10分钟全量同步一次，如果接口超时怎么处理？这个算在标准交付范围里还是需要另外收定制费？",
    },
]

for tone in ["direct", "business", "challenger", "collaborative"]:
    print(f"\n=======================================================", flush=True)
    print(f">>> 测试 Tone: {tone}", flush=True)
    engine = build_llm(kb=kb, me_name="我", scene="requirements", tone=tone, timeout_seconds=12.0)
    res = engine.suggest(transcript, count=3)
    for i, s in enumerate(res.get("suggestions", []), 1):
        print(f"  [{i}] 【{s.get('category')} · {s.get('intent')}】", flush=True)
        print(f"      话术: \"{s.get('script')}\"", flush=True)
