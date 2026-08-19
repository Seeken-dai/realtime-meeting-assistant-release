# 系统架构设计 (Architecture)

## 整体设计原则

1. **音频留在本地，文本上云**：原始音频数据不上传任何第三方分析平台，在本地完成分流、VAD 裁剪与声纹特征提取。
2. **解耦说话人区分与 ASR**：避免捆绑高昂的云端实时声纹分离服务，采用“本地声纹认我 + 云端低成本转写 + 会后本地离线聚类”的组合拳。
3. **轻量化 Windows 原生体验**：前端基于 Electron + React 构建，后台常驻轻量 Python 进程进行异步音频流泵送。

---

## 数据流向示意

```text
麦克风 PCM (16kHz 16bit Mono)
      │
      ├───→ Desktop Bridge (计算实时电平, 驱动 UI 波动)
      │
      ├───→ Local Sherpa-ONNX (Silero-VAD 活性切分 + CAM++ 声纹验证) ──┐
      │                                                                ├──→ 判定 speakerId: "me" vs "other"
      └───→ Cloud Stream ASR (WebSocket 实时转写, 接收 Final Text) ────┘
                                     │
                                     ▼
                        话术建议引擎 (SuggestEngine)
                                     │
                        ┌────────────┴────────────┐
                        ▼                         ▼
                  关键词与意图规则匹配       本地 SQLite 知识库语义匹配
                        └────────────┬────────────┘
                                     ▼
                        Prompt 构建并调用 LLM API
                                     │
                                     ▼
                        IPC 通信 (主进程 → 悬浮窗渲染进程)
```
