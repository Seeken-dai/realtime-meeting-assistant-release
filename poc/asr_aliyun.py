"""
阿里云百炼（DashScope）实时语音识别适配器。

模型：paraformer-realtime-v2（推荐）或 fun-asr-realtime（新一代）
鉴权：API Key（SDK 内部处理）

✅ 已确认（官方文档 + 本项目实测）：
   paraformer-realtime / fun-asr-realtime **不支持** 实时说话人分离。
   分离仅出现在 Fun-ASR 系列的**非实时**模型上。
   因此本适配器不再传 diarization_enabled / speaker_count（死参数已移除）。
   方案 B 正是「实时不带分离 + 会后/本地区分说话人」的刻意选择。

官方：https://help.aliyun.com/zh/model-studio/real-time-speech-recognition-user-guide
"""

import threading
import time

import dashscope
from dashscope.audio.asr import Recognition, RecognitionCallback, RecognitionResult

from base import ASRBase


class _Callback(RecognitionCallback):
    def __init__(self, on_result, owner, debug=False):
        self._on_result = on_result
        self._owner = owner
        self._debug = debug

    def on_open(self):
        self._owner._connected = True
        self._owner._needs_reconnect = False
        print("[阿里云] 连接已建立")

    def on_close(self):
        self._owner._connected = False
        if self._owner._running:
            self._owner._needs_reconnect = True
            print("[阿里云] 连接已断开（将自动平滑重连）")
        else:
            print("[阿里云] 连接已关闭")

    def on_error(self, result):
        msg = (
            getattr(result, "message", None)
            or getattr(result, "status_message", None)
            or repr(result)
        )
        print(f"[阿里云] 错误: {msg}")
        if self._owner._running:
            self._owner._needs_reconnect = True

    def on_event(self, result: RecognitionResult):
        sentence = result.get_sentence()
        if not sentence:
            return
        if self._debug:
            import json as _json

            print(
                f"\n\033[90m[原始返回] {_json.dumps(sentence, ensure_ascii=False)}\033[0m"
            )
        text = sentence.get("text", "")
        if not text:
            return
        # 实时模型不返回说话人；字段保留兜底，正常应为 None
        speaker = sentence.get("speaker_id")
        if speaker is None:
            words = sentence.get("words") or []
            if words and isinstance(words[0], dict):
                speaker = words[0].get("speaker_id")
        is_final = RecognitionResult.is_sentence_end(sentence)
        end_ms = int(sentence.get("end_time") or 0)
        # begin_time / words 用于把一条长句按说话人切开（见 turn_split.py）：
        # 实时模型只在静音处断句，一条 final 能覆盖几十秒、跨好几次换人。
        # words 不是所有模型都给，缺了就退回按语音时长比例分配。
        begin_ms = sentence.get("begin_time")
        self._on_result(
            text=text,
            speaker=speaker,
            is_final=is_final,
            end_ms=end_ms,
            begin_ms=None if begin_ms is None else int(begin_ms),
            words=sentence.get("words") or None,
        )


class AliyunASR(ASRBase):
    name = "阿里云实时语音识别"

    # 单个 WebSocket 连接安全轮换上限（阿里云服务端约 20 分钟断开，提前在 18 分钟安全续接）
    MAX_SESSION_SECONDS = 18 * 60

    def __init__(
        self,
        api_key,
        sample_rate=16000,
        model="qwen-audio-3.0-asr-flash-streaming",
        language_hints=None,
        vocabulary_id=None,
        debug=False,
        **_ignored,
    ):
        # **_ignored：兼容旧调用方传入的 diarization / speaker_count，静默丢弃
        # language_hints：仅 paraformer-realtime-v2 等模型生效；不设则自动识语种，
        # 实测易串出日文等。默认中英，评审会场景用 zh / en / zh_en 显式限定。
        # vocabulary_id：预编译热词列表 ID（专有名词库同步后传入）。
        dashscope.api_key = api_key
        self.sample_rate = sample_rate
        self.model = model or "qwen-audio-3.0-asr-flash-streaming"
        self.name = f"阿里云 {self.model}"
        self.language_hints = list(language_hints) if language_hints else ["zh", "en"]
        self.vocabulary_id = vocabulary_id or None
        self.debug = debug
        self._recognition = None
        self._on_result = None
        self._running = False
        self._connected = False
        self._needs_reconnect = False
        self._session_start_time = 0.0
        self._lock = threading.Lock()

    def _build_recognition(self):
        kw = dict(
            model=self.model,
            format="pcm",
            sample_rate=self.sample_rate,
            callback=_Callback(self._on_result, owner=self, debug=self.debug),
        )
        # language_hints 仅在 paraformer 系列生效，避免向 qwen-audio / fun-asr 传入不支持参数
        if self.language_hints and self.model.startswith("paraformer"):
            kw["language_hints"] = self.language_hints
        # 官方实时接口支持 vocabulary_id；部分 SDK 版本也认 phrase_id
        if self.vocabulary_id:
            kw["vocabulary_id"] = self.vocabulary_id
            kw["phrase_id"] = self.vocabulary_id
        return Recognition(**kw)

    def start(self, on_result):
        with self._lock:
            self._on_result = on_result
            self._running = True
            self._needs_reconnect = False
            self._recognition = self._build_recognition()
            self._session_start_time = time.time()
            self._recognition.start()

    def _reconnect(self):
        old_rec = self._recognition
        self._recognition = None
        if old_rec:
            try:
                old_rec.stop()
            except Exception:
                pass
        if not self._running or not self._on_result:
            return
        print("[阿里云] 正在重建 ASR 流式会话...")
        try:
            self._recognition = self._build_recognition()
            self._session_start_time = time.time()
            self._needs_reconnect = False
            self._recognition.start()
        except Exception as exc:
            print(f"[阿里云] 重建连接失败: {exc}")
            self._needs_reconnect = True

    def send(self, pcm_bytes):
        with self._lock:
            if not self._running:
                return
            now = time.time()
            if self._needs_reconnect or (
                self._connected
                and (now - self._session_start_time > self.MAX_SESSION_SECONDS)
            ):
                self._reconnect()

            if self._recognition is None:
                return

            try:
                self._recognition.send_audio_frame(pcm_bytes)
            except Exception as exc:
                print(f"[阿里云] 发送音频帧异常: {exc}")
                self._needs_reconnect = True

    def stop(self):
        """结束会话。**已经停了再停不算错。**"""
        with self._lock:
            self._running = False
            self._connected = False
            self._needs_reconnect = False
            if self._recognition is None:
                return
            try:
                self._recognition.stop()
            except Exception as exc:
                print(f"[阿里云] 停止时忽略：{exc}")
            finally:
                self._recognition = None
