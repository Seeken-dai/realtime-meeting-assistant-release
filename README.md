# 实时会议话术助手 (Meeting Copilot) 🎙️🤖

<p align="center">
  <img src="https://img.shields.io/badge/Release-v0.1.4-blue.svg" alt="Release v0.1.4">
  <img src="https://img.shields.io/badge/Platform-Windows%2010%2F11%20x64-green.svg" alt="Platform">
  <img src="https://img.shields.io/badge/Electron-43.2.0-indigo.svg" alt="Electron">
  <img src="https://img.shields.io/badge/Python-3.10%2B-yellow.svg" alt="Python">
  <img src="https://img.shields.io/badge/License-MIT-orange.svg" alt="License">
</p>

**实时会议话术助手** 是一款专为技术评审、对客沟通、售前谈判及方案答辩打造的高性能桌面 AI 助手。

在会议进行时，它通过系统/麦克风拾音，结合**本地轻量声纹识别（Sherpa-ONNX）**与**高性价比流式语音转写（ASR）**，实时将对话投影为双轨时间轴；当检测到对方提出技术难点、定制边界、商务报价或架构约束时，毫秒级召回本地项目知识库与预设规则，在**桌面悬浮窗**上实时呈现针对性应对策略与标准话术。

---

## ✨ 核心特性

### 1. 🪟 智能桌面悬浮窗（Hover-Freeze 交互）
* **双模迷你胶囊**：支持一键折叠为 38px 迷你状态，自动收缩物理窗口，不阻碍背景点击操作。
* **防冲刷阅读**：鼠标移入时自动锁定当前策略建议，新建议在顶栏静默累积，移开后平滑刷新。
* **信任链溯源**：每条建议均标注来源规则或知识库切片，点击可展开查看原文依据。
* **多风格外观**：支持浅灰、半透明毛玻璃、暗黑沉浸风格无缝切换。

### 2. ⚡ 本地声纹区分 + 低成本流式 ASR
* **本地声纹识别「我/对方」**：无需向云厂商购买昂贵的实时声纹分离服务，通过本地 `sherpa-onnx` 提取声纹特征，1:1 高精度识别说话人身份，区分「本人发言」与「对方提问」。
* **极速就绪预热**：会前静默预热 Python 运行时与专有名词 Vocabulary 缓存，实现秒级点击开会。
* **多云 ASR 适配**：支持阿里云通义百炼实时识别（0.288 元/小时）、腾讯云、火山引擎豆包、讯飞实时转写等多渠道自由配置。

### 3. 🧠 实时智能话术与 RAG 知识检索
* **毫秒级意图判断**：实时分析对方提问中的需求陷阱（如“免费改”、“要求实时同步”、“私有化接口对接”）。
* **知识库批量导入**：支持直接从 Windows 资源管理器批量拖拽或递归扫描文件夹（`.md` / `.txt` / `.docx` / `.pdf`），自动建立检索索引。
* **专有名词库同步**：支持行业热词与项目专属词典，自动同步至云端 ASR 热词表，大幅消除生僻词识别错误。

### 4. 📝 异步长任务与会后全景复盘
* **切页不中断**：会议纪要生成、全量离线说话人分离、复盘提取等长耗时任务提升至后台并发执行，完成后发送全局 Toast 提示。
* **双轨说话人改派**：会后完整时间轴支持可视化按句/按说话人单独微调与改派。
* **本地持久化隐私保障**：所有原始录音、声纹向量、SQLite 数据库均持久化存储在本机 `%APPDATA%`，音频数据绝不回传任何第三方服务端。

---

## 🏗️ 架构概览

```text
麦克风 / 系统音频 ─┬─→ 本地声纹模块 (Sherpa-ONNX VAD + Embedding) ──┐
                  │                                                    ├─→ 时间戳对齐与发言归属
                  └─→ 云端流式 ASR (阿里云 / 腾讯 / 讯飞 / 火山) ──────┘
                                          │
                                          ▼
                                触发规则引擎与 RAG 检索
                                          │
                                          ▼
                                大模型 (Qwen / Gemini / DeepSeek)
                                          │
                                          ▼
                            Electron 桌面交互悬浮窗 (React + TS)
```

---

## 🚀 快速开始

### 方式一：下载开箱即用的安装包 (Windows)

访问 [Releases 页面](../../releases) 下载最新版本的安装程序：
* `实时会议话术助手-V0.1.4-x64.exe`

下载后直接双击安装，并在应用的【设置】页面填入你的大模型与 ASR API Key 即可使用。

---

### 方式二：从源码构建

#### 环境要求
1. **Node.js**: >= 18.0.0
2. **Python**: 3.10 或 3.11 (Windows 64-bit)

#### 1. 克隆代码
```powershell
git clone https://github.com/Seeken-dai/realtime-meeting-assistant.git
cd realtime-meeting-assistant
```

#### 2. 配置 Python 依赖与本地模型
```powershell
cd poc
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 下载本地声纹与 VAD 模型（约 50MB）
# 具体步骤详见 poc/models/README.md
```

#### 3. 运行桌面客户端
```powershell
cd ../app
npm install
npm run dev
```

#### 4. 打包分发安装包
```powershell
# 在根目录下执行打包脚本
cd ..
.\package.ps1
```
打包产物将自动输出至 `release/V0.1.4/` 目录。

---

## 📖 详细文档

* 📘 [用户使用手册](docs/USER_GUIDE.md)：ASR/LLM 密钥配置、声纹注册指南、悬浮窗操作秘籍。
* 📐 [系统架构设计](docs/ARCHITECTURE.md)：双轨音频采集、桥接协议、说话人区分算法与状态机。
* ❓ [常见问题解答 (FAQ)](docs/FAQ.md)：收音调优、云厂商计费比较、快捷键冲突。
* 🔒 [隐私与数据安全说明](docs/PRIVACY.md)：本地安全存储机制与传输策略。
* 📜 [版本更新日志](CHANGELOG.md)：从 v0.1.0 到 v0.1.4 的完整演进历程。

---

## 🤝 参与贡献

欢迎提交 Issue 和 Pull Request！请查阅 [贡献指南](.github/CONTRIBUTING.md) 了解开发规范。

---

## 📄 开源协议

本项目基于 [MIT 协议](LICENSE) 开源。
