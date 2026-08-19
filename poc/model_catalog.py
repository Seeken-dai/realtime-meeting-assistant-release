"""LLM 模型目录的静态回退项。

供应商如果提供 ``/models`` 目录，运行时应优先使用远端目录；这里的列表只在
目录接口不存在、被网关禁用或网络失败时兜底，不能当作供应商的完整模型清单。
"""


# 各供应商的候选模型名（仅用于动态目录不可用时的连通性回退）。
FALLBACK_MODEL_CANDIDATES = {
    "xfyun": ["4.0Ultra", "max-32k", "generalv3.5", "pro-128k",
              "ultra-32k", "Ultra", "generalv3", "lite"],
    # X 系列模型名统一为 spark-x，靠端点区分版本。
    "xfyun-x2-flash": ["spark-x"],
    "xfyun-x2": ["spark-x"],
    "xfyun-x1.5": ["spark-x"],
    "aliyun": ["qwen-turbo", "qwen-flash", "qwen-plus", "qwen-max",
               "deepseek-v4-flash"],
    "mimo": ["mimo-v2-flash", "mimo-v2.5-pro"],
    "gemini": ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
               "gemini-3.6-flash", "gemini-2.5-flash", "gemini-3.5-flash"],
    "zhipu": ["glm-4-flash", "glm-4.7-flash", "glm-4-flashx", "glm-4-air"],
    "deepseek": ["deepseek-chat", "deepseek-reasoner"],
    "moonshot": ["moonshot-v1-8k", "moonshot-v1-32k"],
    "grok": ["grok-4-fast-non-reasoning", "grok-4.3", "grok-4.5", "grok-3",
             "grok-2-latest"],
    "custom": [],
}
