"""
小米 MiMo-V2.5-ASR 适配器（⚠️ 分段准实时，非真流式；无说话人分离）。

重要限制（务必知悉）：
  - MiMo ASR 是 OpenAI 兼容的【整段文件转写】模型，音频需完整发送后才识别，
    不支持边说边出字的真流式输入（stream=True 只流式返回文字，不流式接收音频）；
  - 【不提供说话人分离】，输出仅为转写文本；
  - 优势是中文/方言/中英混说/术语识别准确率很高，适合做「转写准确率对照组」。

因此本适配器采用【分段缓冲】策略：每累积 SEGMENT_SEC 秒音频，打包成 wav
发一次识别，模拟准实时。speaker 恒为 None。

音频：本模块把 16kHz PCM 直接封装为 wav 上传（MiMo 支持 wav/mp3，≤10MB）。
文档：https://mimo.mi.com/docs/en-US/quick-start/usage-guide/audio/Speech-Recognition
"""

import base64
import io
import threading
import wave

from base import ASRBase

_BASE_URL = "https://api.xiaomimimo.com/v1"
_MODEL = "mimo-v2.5-asr"
_SEGMENT_SEC = 6  # 每段时长，越小越"实时"但请求越频繁、上下文越碎


class MimoASR(ASRBase):
    name = "小米 MiMo-V2.5-ASR（对照组·无分离）"

    def __init__(self, api_key, sample_rate=16000, segment_sec=_SEGMENT_SEC,
                 language="auto", **_ignored):
        # language：zh / en / auto。文档仅支持单语种 + auto；中英混说用 auto。
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ImportError("请先安装 openai 库：pip install openai") from e
        self._client = OpenAI(api_key=api_key, base_url=_BASE_URL)
        self.sample_rate = sample_rate
        self.segment_sec = segment_sec
        self.language = (language or "auto").strip() or "auto"
        self._bytes_per_segment = sample_rate * 2 * segment_sec  # 16bit=2字节
        self._buf = bytearray()
        self._on_result = None
        self._lock = threading.Lock()

    def start(self, on_result):
        self._on_result = on_result
        print(
            f"[MiMo] 就绪（分段准实时，每 {self.segment_sec}s 转写一次，"
            f"language={self.language}，无说话人分离）"
        )

    def _pcm_to_wav_b64(self, pcm):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sample_rate)
            w.writeframes(pcm)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    def _transcribe(self, pcm):
        audio_b64 = self._pcm_to_wav_b64(pcm)
        try:
            completion = self._client.chat.completions.create(
                model=_MODEL,
                messages=[{
                    "role": "user",
                    "content": [{
                        "type": "input_audio",
                        "input_audio": {"data": f"data:audio/wav;base64,{audio_b64}"},
                    }],
                }],
                extra_body={"asr_options": {"language": self.language}},
            )
            text = completion.choices[0].message.content or ""
        except Exception as e:
            print(f"[MiMo] 识别请求失败: {e}")
            return
        if text.strip():
            # speaker 恒为 None：MiMo 不做说话人分离
            self._on_result(text=text.strip(), speaker=None, is_final=True)

    def send(self, pcm_bytes):
        with self._lock:
            self._buf.extend(pcm_bytes)
            if len(self._buf) >= self._bytes_per_segment:
                segment = bytes(self._buf)
                self._buf.clear()
            else:
                segment = None
        if segment:
            # 异步转写，避免阻塞采集
            threading.Thread(target=self._transcribe, args=(segment,),
                             daemon=True).start()

    def stop(self):
        with self._lock:
            tail = bytes(self._buf)
            self._buf.clear()
        if tail:
            self._transcribe(tail)
