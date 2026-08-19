# 本地模型

本目录存放会议运行时需要的 ONNX 模型。模型文件体积较大，不入 Git；换机器或重新克隆后按本文重新下载。

下载源为 [sherpa-onnx 官方 Release](https://github.com/k2-fsa/sherpa-onnx/releases)。当前桌面端打包至少需要 CAM++ 和 Silero VAD 两个文件。

## 必需模型

| 文件 | 用途 | 体积约 | 说明 |
|---|---|---:|---|
| `3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx` | 会中声纹识别「我」 | 27 MB | 默认模型，体积较小，CPU 运行较快 |
| `silero_vad_v5.onnx` | 语音端点检测 | 2.2 MB | 辅助切出有人说话的片段 |

可选的更大声纹模型：

| 文件 | 体积约 | 说明 |
|---|---:|---|
| `3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common.onnx` | 68 MB | ERes2NetV2 中文模型，精度倾向更高，运行开销也更大 |

## Windows PowerShell 下载

在 `poc/models/` 目录执行：

```powershell
$speakerBase = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models"
$asrBase = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"

Invoke-WebRequest "$speakerBase/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx" `
  -OutFile "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"

Invoke-WebRequest "$speakerBase/3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common.onnx" `
  -OutFile "3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common.onnx"

Invoke-WebRequest "$asrBase/silero_vad_v5.onnx" `
  -OutFile "silero_vad_v5.onnx"
```

只做默认配置时，下载 CAM++ 和 Silero VAD 即可。模型文件名必须保持不变，否则桌面端和打包脚本找不到它们。

## 运行条件

- 输入音频为 16 kHz、单声道，与 `poc/config.py` 中的 `SAMPLE_RATE` 和 `CHANNELS` 一致。
- Python 依赖由 `poc/requirements.txt` 安装，核心包是 `sherpa-onnx`。
- 不需要安装 PyTorch。

## 相关用法

- 会中识别「我」：桌面设置页录制声纹，底层使用 `speaker_me.py`。
- 会后分离说话人：历史详情页触发，底层使用 `diarize_offline.py`。
- 离线验证：`verify_speaker.py`、`eval_labeled_enroll.py`。

模型由本地 Python 运行时读取。不要把 `.onnx` 文件复制到仓库以外的临时路径后再修改配置，打包脚本只会检查 `poc/models/` 下的默认文件。
