"""
ASR 适配器基类 —— 统一各厂商接口，便于 POC 横向对比替换。

约定：
- start(on_result): 建立连接。on_result 是回调，签名为
      on_result(text: str, speaker, is_final: bool, end_ms: int = 0,
                begin_ms: int | None = None, words: list | None = None)
  其中 speaker 可为 None（该厂商实时模式不返回说话人时）；
  end_ms 是该句在【当前供应商 ASR 会话】中的结束时刻（毫秒）。
  它不是天然的 WAV 播放器时间：暂停、丢帧、重连都会改变口径；
  桥接层必须通过 RecordingSampleClock 映射后才能持久化。
  begin_ms / words（词级时间戳）是可选的，给得出就传：
  桥接层用它把一条跨了好几个说话人的长 final 切开（见 turn_split.py）。
  消费方一律用关键字接收并给默认值，不给的适配器不受影响。
- send(pcm_bytes): 发送一帧 16-bit PCM 音频。
- stop(): 结束会话，关闭连接。
"""

from abc import ABC, abstractmethod


class ASRBase(ABC):
    name = "未命名 ASR"

    @abstractmethod
    def start(self, on_result):
        ...

    @abstractmethod
    def send(self, pcm_bytes):
        ...

    @abstractmethod
    def stop(self):
        ...
