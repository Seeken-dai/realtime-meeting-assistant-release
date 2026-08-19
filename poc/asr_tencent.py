"""
腾讯云 实时语音识别适配器（基于官方语音 SDK）。

鉴权：AppID + SecretId + SecretKey（SDK 内部完成 WebSocket 签名）。
说话人分离：set_speaker_diarization(1) + set_enable_speaker_context(1)。
引擎：16k_zh（中文普通话 16kHz）。

⚠️ 依赖官方语音 SDK（未在 PyPI 稳定发布），需从源码安装：
   pip install git+https://github.com/TencentCloud/tencentcloud-speech-sdk-python.git
   若上面失败，可 git clone 后把仓库根目录加入 PYTHONPATH。

⚠️ 需 POC 实测确认：
   - 说话人编号在回调结果里的字段（下方按 speaker_id / speaker 兜底提取）；
   - 实时模式下分离的稳定性。
   官方示例：examples/asr/realtimev2example.py
"""

from base import ASRBase


class TencentASR(ASRBase):
    name = "腾讯云 实时 ASR"

    def __init__(self, app_id, secret_id, secret_key,
                 engine_model_type="16k_zh", diarization=True, **_ignored):
        # engine_model_type 即语种/引擎：
        #   16k_zh 中文 · 16k_en 英文 · 16k_zh_en 中英大模型（混用推荐）
        # 延迟导入，未安装 SDK 时给出清晰提示
        try:
            from common import credential
            from asr import realtime_recognizer_v2
        except ImportError as e:
            raise ImportError(
                "未找到腾讯云语音 SDK。请执行：\n"
                "  pip install git+https://github.com/TencentCloud/"
                "tencentcloud-speech-sdk-python.git"
            ) from e

        self._credential_mod = credential
        self._recognizer_mod = realtime_recognizer_v2
        self.app_id = app_id
        self.secret_id = secret_id
        self.secret_key = secret_key
        self.engine_model_type = engine_model_type or "16k_zh"
        self.diarization = diarization
        self._recognizer = None

    def start(self, on_result):
        realtime = self._recognizer_mod
        cred = self._credential_mod.Credential(self.secret_id, self.secret_key)

        class _Listener(realtime.RealtimeRecognizerV2Listener
                        if hasattr(realtime, "RealtimeRecognizerV2Listener")
                        else realtime.SpeechRecognitionListener):
            def on_recognition_start(self, response):
                print("[腾讯云] 识别开始")

            def on_recognition_sentences(self, response):  # 中间结果
                self._emit(response, is_final=False)

            def on_sentence_end(self, response):           # 最终结果
                self._emit(response, is_final=True)

            def on_fail(self, response):
                print(f"[腾讯云] 失败: {response}")

            def _emit(self, response, is_final):
                result = getattr(response, "result", None) or {}
                if isinstance(result, dict):
                    text = result.get("voice_text_str", "")
                    speaker = result.get("speaker_id", result.get("speaker"))
                else:
                    text = getattr(result, "voice_text_str", "")
                    speaker = getattr(result, "speaker_id", None)
                if text:
                    on_result(text=text, speaker=speaker, is_final=is_final)

        self._recognizer = realtime.RealtimeRecognizerV2(
            self.app_id, cred, self.engine_model_type, _Listener())
        self._recognizer.set_voice_format(1)   # 1 = PCM
        self._recognizer.set_need_vad(1)
        if self.diarization:
            self._recognizer.set_speaker_diarization(1)
            if hasattr(self._recognizer, "set_enable_speaker_context"):
                self._recognizer.set_enable_speaker_context(1)
        self._recognizer.start()

    def send(self, pcm_bytes):
        self._recognizer.write(pcm_bytes)

    def stop(self):
        if self._recognizer is not None:
            self._recognizer.stop()
