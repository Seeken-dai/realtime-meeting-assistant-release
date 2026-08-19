"""
火山引擎 豆包 大模型流式语音识别适配器。

鉴权：Header 方式（X-Api-App-Key / X-Api-Access-Key / X-Api-Resource-Id）。
说话人分离：请求参数 result_type/enable_speaker_info 开启，返回中带 speaker 字段。
协议：WebSocket + 自定义二进制帧（4 字节头 + 可选 gzip 压缩的 JSON/音频负载）。

⚠️ 二进制协议较复杂，且各账号资源标识（resource_id）、鉴权字段名可能随版本调整，
   此适配器为「结构就绪、需真机联调」状态。拿到密钥后按官方文档核对：
   - WS 地址与 X-Api-Resource-Id 取值（流式全量/流式识别）
   - 请求 JSON 中 speaker 相关开关的确切字段名（enable_speaker_info / show_utterances）
   - 响应中说话人字段路径
   官方文档：https://www.volcengine.com/docs/6561/1354869（大模型流式语音识别 API）

音频要求：16kHz / 16bit / 单声道 PCM。
"""

import gzip
import json
import ssl
import threading
import uuid

import websocket  # websocket-client

from base import ASRBase

_WS_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"
_RESOURCE_ID = "volc.bigasr.sauc.duration"  # 按需改为 volc.bigasr.sauc.concurrent

# ── 二进制协议常量 ─────────────────────────────────────
_PROTOCOL_VERSION = 0b0001
_DEFAULT_HEADER_SIZE = 0b0001
# message type
_FULL_CLIENT_REQUEST = 0b0001
_AUDIO_ONLY_REQUEST = 0b0010
_FULL_SERVER_RESPONSE = 0b1001
_SERVER_ERROR = 0b1111
# message flags
_NO_SEQ = 0b0000
_NEG_SEQ_LAST = 0b0011  # 最后一包
# serialization / compression
_JSON = 0b0001
_RAW = 0b0000
_GZIP = 0b0001


def _make_header(msg_type, flags, serialization, compression):
    b0 = (_PROTOCOL_VERSION << 4) | _DEFAULT_HEADER_SIZE
    b1 = (msg_type << 4) | flags
    b2 = (serialization << 4) | compression
    b3 = 0x00
    return bytes([b0, b1, b2, b3])


class VolcanoASR(ASRBase):
    name = "火山引擎 豆包 ASR"

    def __init__(self, app_key, access_key, sample_rate=16000, diarization=True,
                 language=None, **_ignored):
        # language：zh-CN / en-US；空或 None 表示不传，走中英+方言默认（作中英混用）
        self.app_key = app_key
        self.access_key = access_key
        self.sample_rate = sample_rate
        self.diarization = diarization
        self.language = (language or "").strip() or None
        self._ws = None
        self._on_result = None
        self._recv_thread = None
        self._running = False

    def _build_config(self):
        req = {
            "model_name": "bigmodel",
            "enable_punc": True,
            "show_utterances": True,
        }
        if self.language:
            # 官方：空=中英+方言；zh-CN / en-US / ja-JP … 限定单语种
            req["language"] = self.language
        if self.diarization:
            # ⚠️ 说话人开关字段名需按当前文档核对
            req["enable_speaker_info"] = True
        return {
            "user": {"uid": "poc-user"},
            "audio": {
                "format": "pcm",
                "rate": self.sample_rate,
                "bits": 16,
                "channel": 1,
            },
            "request": req,
        }

    def start(self, on_result):
        self._on_result = on_result
        headers = [
            f"X-Api-App-Key: {self.app_key}",
            f"X-Api-Access-Key: {self.access_key}",
            f"X-Api-Resource-Id: {_RESOURCE_ID}",
            f"X-Api-Request-Id: {uuid.uuid4()}",
        ]
        self._ws = websocket.create_connection(
            _WS_URL, header=headers, sslopt={"cert_reqs": ssl.CERT_NONE})

        # 发送 full client request（配置帧，JSON + gzip）
        payload = gzip.compress(json.dumps(self._build_config()).encode("utf-8"))
        frame = _make_header(_FULL_CLIENT_REQUEST, _NO_SEQ, _JSON, _GZIP)
        frame += len(payload).to_bytes(4, "big") + payload
        self._ws.send_binary(frame)

        print("[火山] 连接已建立")
        self._running = True
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()

    def _recv_loop(self):
        while self._running:
            try:
                raw = self._ws.recv()
            except Exception:
                break
            if not raw or not isinstance(raw, (bytes, bytearray)):
                continue
            self._parse_frame(raw)

    def _parse_frame(self, raw):
        if len(raw) < 4:
            return
        msg_type = (raw[1] >> 4) & 0x0F
        compression = raw[2] & 0x0F
        body = raw[4:]
        if msg_type == _SERVER_ERROR:
            # 错误帧：前 4 字节错误码 + 消息
            print(f"[火山] 服务端错误: {body[4:].decode('utf-8', 'ignore')}")
            return
        if msg_type != _FULL_SERVER_RESPONSE:
            return
        # 跳过 4 字节 payload size，取负载
        payload = body[4:] if len(body) > 4 else body
        if compression == _GZIP:
            try:
                payload = gzip.decompress(payload)
            except Exception:
                return
        try:
            data = json.loads(payload.decode("utf-8"))
        except Exception:
            return
        self._emit(data)

    def _emit(self, data):
        result = data.get("result") or {}
        text = result.get("text", "")
        # 说话人字段：可能在 utterances[].speaker
        speaker = None
        for utt in result.get("utterances", []) or []:
            if utt.get("speaker") is not None:
                speaker = utt.get("speaker")
        # 是否最终结果：utterances 里 definite=True 视为最终
        is_final = any(u.get("definite") for u in result.get("utterances", []) or [])
        if text:
            self._on_result(text=text, speaker=speaker, is_final=is_final)

    def send(self, pcm_bytes):
        payload = gzip.compress(pcm_bytes)
        frame = _make_header(_AUDIO_ONLY_REQUEST, _NO_SEQ, _RAW, _GZIP)
        frame += len(payload).to_bytes(4, "big") + payload
        self._ws.send_binary(frame)

    def stop(self):
        self._running = False
        try:
            # 发送最后一包空音频，标记结束
            payload = gzip.compress(b"")
            frame = _make_header(_AUDIO_ONLY_REQUEST, _NEG_SEQ_LAST, _RAW, _GZIP)
            frame += len(payload).to_bytes(4, "big") + payload
            self._ws.send_binary(frame)
        except Exception:
            pass
        finally:
            if self._ws is not None:
                self._ws.close()
