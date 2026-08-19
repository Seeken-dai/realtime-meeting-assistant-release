# Python 测试

本目录存放 Python 侧的单元测试、自检和回归测试，不属于 Electron 桌面端运行时。命令都从 `poc/` 目录执行，并使用项目虚拟环境中的 Python。

## 单个测试

```powershell
cd poc
.\.venv\Scripts\python.exe -m tests.test_turn_split
```

常用测试模块包括：

- `tests.test_turn_split`：转写段落切分。
- `tests.test_adaptive_cut`：会中「我 / 对方」门槛。
- `tests.test_align_transcript`：会后转写对齐与回写。
- `tests.test_diarize_decision`：会后说话人簇判定。
- `tests.test_audio_clock`、`tests.test_bridge_timing`：录音时钟和时间戳映射。
- `tests.test_suggestion_quality`：建议长度和质量规则，不调用网络。
- `tests.test_document_extract`：DOCX、文本 PDF 和扫描 PDF 边界。

## 完整回归

从仓库根目录执行：

```powershell
.\run-m4-regression.cmd
```

该套件会串行运行 Python、SQLite、Electron 时间轴和 renderer 构建检查。它不调用 ASR/LLM，也不读取真实会议录音。

需要单独验证真实会议归档、线上三轨、时间轴或长会稳定性时，使用根目录的：

```powershell
.\run-real-regression.ps1
```

报告写入 `poc/eval/real_meeting_runs/`，该目录已忽略。测试过程中不要提交真实录音、完整转写或含敏感上下文的报告。
