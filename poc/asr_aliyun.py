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

import dashscope
from dashscope.audio.asr import Recognition, RecognitionCallback, RecognitionResult

from base import ASRBase


class _Callback(RecognitionCallback):
    def __init__(self, on_result, debug=False):
        self._on_result = on_result
        self._debug = debug

    def on_open(self):
        print("[阿里云] 连接已建立")

    def on_close(self):
        print("[阿里云] 连接已关闭")

    def on_error(self, result):
        msg = getattr(result, "message", None) or getattr(result, "status_message", None) or repr(result)
        print(f"[阿里云] 错误: {msg}")

    def on_event(self, result: RecognitionResult):
        sentence = result.get_sentence()
        if not sentence:
            return
        if self._debug:
            import json as _json
            print(f"\n\033[90m[原始返回] {_json.dumps(sentence, ensure_ascii=False)}\033[0m")
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
        self._on_result(text=text, speaker=speaker, is_final=is_final,
                        end_ms=end_ms,
                        begin_ms=None if begin_ms is None else int(begin_ms),
                        words=sentence.get("words") or None)


class AliyunASR(ASRBase):
    name = "阿里云实时语音识别"

    def __init__(self, api_key, sample_rate=16000, model="qwen-audio-3.0-asr-flash-streaming",
                 language_hints=None, vocabulary_id=None, debug=False, **_ignored):
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

    def start(self, on_result):
        # Recognition 把 **kwargs 原样塞进请求；language_hints 是官方参数名
        kw = dict(
            model=self.model,
            format="pcm",
            sample_rate=self.sample_rate,
            callback=_Callback(on_result, debug=self.debug),
        )
        # language_hints 仅在 paraformer 系列生效，避免向 qwen-audio / fun-asr 传入不支持参数
        if self.language_hints and self.model.startswith("paraformer"):
            kw["language_hints"] = self.language_hints
        # 官方实时接口支持 vocabulary_id；部分 SDK 版本也认 phrase_id
        if self.vocabulary_id:
            kw["vocabulary_id"] = self.vocabulary_id
            kw["phrase_id"] = self.vocabulary_id
        self._recognition = Recognition(**kw)
        self._recognition.start()

    def send(self, pcm_bytes):
        self._recognition.send_audio_frame(pcm_bytes)

    def stop(self):
        """结束会话。**已经停了再停不算错。**

        ⚠️ dashscope 在识别已结束时 `stop()` 会抛
           `InvalidParameter: Speech recognition has stopped.` ——
           服务端先关连接（音频流结束、超时、网络断）时就会走到这里。
           调用方通常在 finally 里收尾，这个异常会把后面的收尾全部掀掉
           （实测：整场会议的 `ended` 事件因此没发出去，录音时长与声纹统计全丢）。
        """
        if self._recognition is None:
            return
        try:
            self._recognition.stop()
        except Exception as exc:
            print(f"[阿里云] 停止时忽略：{exc}")
        finally:
            self._recognition = None
