# 评估数据与复现命令

本目录保存可复现评测夹具、协议和历史结果。这里记录怎么重跑，产品结论和工程状态统一看 [`docs/engineering/HANDOFF.md`](../../docs/engineering/HANDOFF.md)。

评测结果要区分工程门禁、与转写对照的一致性和人工听感，不能把某一次录音的结果外推成通用准确率。

## 数据说明

可提交或可审查的内容包括：

| 内容 | 用途 |
|---|---|
| 标注转写 Markdown | CER、声纹注册留出和会后对齐对照 |
| ASR CER JSON | 记录不同 ASR 与对照文本的一致性 |
| 热词目标与召回协议 | 复现专有名词召回检查 |
| JSON 评测夹具 | 运行本地回归和建议质量评分 |

以下内容只保存在本机，不能提交：

```text
*.mp3 / *.wav / _work*       # 原始音频和中间 WAV
../models/*.onnx             # 本地声纹与 VAD 模型
real_meeting_runs/           # 真实录音匿名评测报告
reports/                     # 热词和会后运行报告
```

真实会议资料可能包含姓名、业务内容和声音信息。提交前应脱敏，必要时只保留匿名 ID、时间区间和指标。

## 前置准备

所有命令从 `poc/` 目录执行。长音频先转成 16 kHz 单声道 WAV，只处理标注覆盖的区间即可：

```powershell
cd poc
ffmpeg -y -i "eval/07-21 工作交接与职责梳理.mp3" `
  -t 7500 -ar 16000 -ac 1 -c:a pcm_s16le `
  "eval/_work_16k.wav"
```

需要的模型下载方法见 [`poc/models/README.md`](../models/README.md)。

## 1. ASR 字准对照

窗口为标注文件中的 10 到 490 秒，对照文本约 2408 字。运行：

```powershell
cd poc
.\.venv\Scripts\python.exe eval_asr_accuracy.py
```

结果写入 `poc/eval/asr_cer_<时间戳>.json`。对照 Markdown 本身是转写产物，因此结果表示与对照文本的一致性，不代表人工精标后的绝对 CER。

## 2. 声纹注册留出验证

按标注把「我」的前半段用于注册，后半段用于测试，其他说话人用于观察误纳：

```powershell
cd poc
.\.venv\Scripts\python.exe eval_labeled_enroll.py `
  --wav "eval/_work_16k.wav" `
  --md "eval/07-21 工作交接与职责梳理.md" `
  --me "宸（我）"
```

声纹注册必须使用与正式会议相同的麦克风。该评测的注册段来自会中远场录音，能验证链路，但不能替代使用同一支麦克风录制多段短注册样本的人工验收。

## 3. 声纹可分性

在已有会议 WAV 上估计同源与异源相似度间隔，并对比两种本地声纹模型：

```powershell
cd poc
.\.venv\Scripts\python.exe verify_speaker.py --wav eval/_work_16k.wav
```

## 4. 会后说话人分离

```powershell
cd poc
.\.venv\Scripts\python.exe diarize_offline.py `
  --wav eval/_work_16k.wav `
  --cluster-th 0.60
```

默认需要进行小簇合并，否则一场会议可能被切成过多说话人。会后结果的定位是粗分加人工修正，不能直接视为最终标签。

## 5. 真实会议「我 / 非我」回归

`speaker_regression_20260729_1503.json` 是匿名基准，只包含条目 ID、时间和 `isMe`，不包含标题、正文、姓名或录音。它主要防止同一个「我」被拆成多个说话人，不等同于多人说话人分离准确率。

录音从 `%APPDATA%/meeting-copilot-desktop/recordings/` 读取，运行结果写入已忽略的 `eval/speaker_regression_runs/`：

```powershell
cd poc
.\.venv\Scripts\python.exe eval_speaker_regression.py run
```

如需重新抓取人工确认后的基准：

```powershell
.\.venv\Scripts\python.exe eval_speaker_regression.py capture `
  --meeting-id <meeting-id>
```

## 6. 长会、时间轴和线上三轨

根目录的一键慢门禁会组合执行真实会议匿名评估、线上三轨、时间轴、长会和热词检查：

```powershell
cd ..
.\run-real-regression.ps1

# 要求存在同一场线上会议的 mixed / mic / system 三轨
.\run-real-regression.ps1 -RequireTripleTrack
```

也可以在 `poc/` 下单独运行：

```powershell
cd poc
.\.venv\Scripts\python.exe eval_timeline_axis.py --min-seconds 600 --anchors 15
.\.venv\Scripts\python.exe eval_long_meeting.py --min-seconds 2400 --require-count 2
```

自动指标只检查文件生命周期、时间轴和音轨同步。麦克风与系统音频是否真正隔离，仍需要人工听感复核。

## 7. G6 热词召回

协议全文见 [`hotword_recall_protocol.md`](hotword_recall_protocol.md)。评分器不调用 ASR，只对照会后转写和目标专名：

```powershell
cd poc

# 本地评分器冒烟
.\.venv\Scripts\python.exe -m tests.test_hotword_recall

# 指定会议转写后评估
.\.venv\Scripts\python.exe eval_hotword_recall.py `
  --terms eval/hotword_targets.example.json `
  --transcript <meeting-or-transcript.json> `
  --label aliyun-short-YYYYMMDD
```

当前通过口径是会中热词状态为 `loaded`，并且 required 专名至少各命中一次。连读、口音和 ASR 断句仍可能导致专名出现变体，报告需要结合原始音频复核。
