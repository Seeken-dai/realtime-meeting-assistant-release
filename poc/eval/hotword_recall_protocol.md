# G6 热词真实召回 — 真会操作协议

> 工程代码与模拟失败测试已完成；本协议用于**真实阿里短会**对照专名是否进转写。  
> 评分工具：`poc/eval_hotword_recall.py`（本地、不上传音频）。

## 1. 会前（约 3 分钟）

1. 转写服务选 **阿里 Paraformer**（仅此链路会同步本地专有名词库）。
2. 在应用「专有名词」中确认至少包含：`三快`、`蓝凌`、`EKP`、`MK`（权重建议 5）。
3. 复制 `eval/hotword_targets.example.json` 为本场文件，例如：  
   `eval/hotword_targets.local.json`（**勿提交**含客户名的版本）。
4. 若有额外客户专名，写入 `terms`，并设 `required: true` / `expected_mentions`。

## 2. 会中（3～5 分钟短会即可）

1. 开始录音后，确认状态里热词为 **已加载**（`hotwords.status=loaded` 且有 `vocabularyId`）。  
   - 若是 `degraded` / `empty` / `unsupported`：本场**不算** G6 召回样本，先修同步再测。
2. 按脚本自然口播（可直接读示例 `spoken_script_zh`），每个 required 词至少说 2 次。
3. 故意夹一句易混音：如「MK 合同 / EKP 基座 / 蓝凌三快」。

## 3. 会后评分

1. 导出或复制本场转写全文（JSON 会议记录 / Markdown / 纯文本均可）。
2. 在 `poc/` 下执行：

```powershell
.\.venv\Scripts\python.exe eval_hotword_recall.py `
  --terms eval/hotword_targets.local.json `
  --transcript path\to\transcript.json `
  --label aliyun-short-YYYYMMDD
```

3. 报告默认写到 `eval/reports/`（已 gitignore）。只保留统计与短上下文，勿把完整敏感转写入库。

## 4. 通过口径

| 项 | 默认门槛 |
|---|---|
| 热词同步状态 | `loaded` 且有 vocabularyId |
| required 专名 | 每个 ≥1 次子串命中 |
| term_recall | 100%（可用 `--min-term-recall 0.75` 做摸底，不关 Goal） |
| 对照样本 | 至少 1 场阿里短会；有条件再加 1 场「无热词/清空词表」负对照 |

**负对照（推荐）**：同一脚本、同一环境，临时清空专有名词再开 1 次，对比 miss 是否增加。用于证明「有词表」而非「本来就好认」。

## 5. 失败时怎么调

1. 先看会中状态是否真的 `loaded`（凭证、quota、prefix）。
2. 未命中词：检查是否被截断到 15 字、是否有 aliases、权重是否过低。
3. 仍不中：把 ASR 常见错写加入 `aliases` 再评一次（只作诊断；产品侧是否自动纠错另议）。
4. 调整后重跑短会，不改门槛口头宣称完成。

## 6. 本地无会冒烟

不调用云端，只验证评分器：

```powershell
.\.venv\Scripts\python.exe -m tests.test_hotword_recall
.\.venv\Scripts\python.exe eval_hotword_recall.py `
  --terms eval/hotword_targets.example.json `
  --text "蓝凌三快里 EKP 和 MK 合同都要对齐" `
  --no-write
```
